"""The shadow promotion gate (pure): floors, embargo, independence, locked book, cohorts, bootstrap verdicts, replay
previews, and the pair arithmetic of both registered filter audits."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core import constants as C
from app.core.promotion import gate as G
from app.core.promotion import layers as L
from app.core.promotion import params as P
from app.core.stats import observations as O
from app.core.stats import params as SP
from app.core.stats.observations import CohortKey, MixedCohortError
from app.models import promotion as MP
from tests.stats_helpers import no_entry, obs

UTC = timezone.utc
START = datetime(2026, 10, 1, tzinfo=UTC)
LIVE_ALTS = CohortKey('live', O.PATH_NO_BOOK, O.LOOKBACK_FULL)
CVD = L.CVD_DIVERGENCE_CANCEL
VOTE = L.VOTE_CONFLICT_WATCH
ALERT_CF = L.Counterfactual(vote_confirmed=True, status_if_pending='alert')


def row(i: int = 0, **overrides):
    """A live, locked-book, full-lookback alt alert on its own coin (so no embargo between rows), swing horizon."""
    when = overrides.pop('decision_time', START + timedelta(hours=i))
    values = {
        'coin': f'C{i}USDT',
        'source': 'live',
        'horizon': 'swing',
        'decision_time': when,
        'bar_close': when,
        'resolved_at': when + timedelta(hours=1),
        'status': 'alert',
        'status_reason': None,
        'locked_book': True,
    }
    values.update(overrides)
    return obs(**values)


def cvd_row(i: int = 0, **overrides):
    return row(i, status='cancelled', status_reason=C.STATUS_REASON_CVD_DIVERGENCE, **overrides)


def vote_row(i: int = 0, **overrides):
    return row(i, status='watch', status_reason=C.STATUS_REASON_VOTE_CONFLICT, **overrides)


def cell(observations, *, layer=CVD, as_of=None, facts=None, cohort=LIVE_ALTS, horizon='swing'):
    as_of = as_of or START + timedelta(days=30)
    return G.evaluate_cell(layer, observations, cohort=cohort, horizon=horizon, as_of=as_of, counterfactuals=facts)


def blocked_set(n: int, r: float, *, maker=cvd_row, source='live'):
    rows = [maker(i, stored_r=r, source=source) for i in range(n)]
    return rows, {o.candidate_id: ALERT_CF for o in rows}


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(('days', 'insufficient'), [(27, True), (28, False)])
def test_the_shadow_window_must_run_four_weeks(days, insufficient):
    rows = [row(i) for i in range(60)]
    result = cell(rows, as_of=rows[0].decision_time + timedelta(days=days))
    assert result.window_days == pytest.approx(days)
    assert (result.verdict == G.VERDICT_INSUFFICIENT) is insufficient
    assert any(r.startswith(G.REASON_WINDOW) for r in result.reasons) is insufficient


@pytest.mark.parametrize(('n', 'insufficient'), [(49, True), (50, False)])
def test_the_locked_book_needs_fifty_independent_pairs(n, insufficient):
    result = cell([row(i) for i in range(n)])
    assert result.n_pairs == n
    assert (result.verdict == G.VERDICT_INSUFFICIENT) is insufficient
    assert any(r.startswith(G.REASON_PAIRS) for r in result.reasons) is insufficient


def test_both_floors_failing_list_both_reasons_and_an_empty_cell_is_insufficient():
    result = cell([row(i) for i in range(3)], as_of=START + timedelta(days=2))
    assert result.verdict == G.VERDICT_INSUFFICIENT
    assert [r.split(':')[0] for r in result.reasons] == [G.REASON_WINDOW, G.REASON_PAIRS]
    empty = cell([row(0, locked_book=False)])
    assert (empty.n_pairs, empty.shadow_start, empty.window_days, empty.mean_diff_r) == (0, None, 0.0, None)
    assert empty.verdict == G.VERDICT_INSUFFICIENT


# ---------------------------------------------------------------------------
# Bootstrap verdict
# ---------------------------------------------------------------------------
def test_a_filter_that_blocked_losers_is_promoted():
    blocked, facts = blocked_set(30, -1.0)
    passed = [row(100 + i, stored_r=1.0) for i in range(20)]
    result = cell(blocked + passed, facts=facts)
    assert result.n_pairs == 50 and result.n_blocked_pairs == 30
    assert result.mean_diff_r == pytest.approx(0.6)
    assert result.ci_low > 0
    assert result.verdict == G.VERDICT_PROMOTE and result.binding
    assert result.reasons == (G.REASON_ALL_MET,)


def test_a_filter_that_blocked_winners_is_rejected_and_says_so():
    blocked, facts = blocked_set(30, 1.0)
    passed = [row(100 + i, stored_r=-1.0) for i in range(20)]
    result = cell(blocked + passed, facts=facts)
    assert result.mean_diff_r == pytest.approx(-0.6)
    assert result.verdict == G.VERDICT_REJECT
    codes = [r.split(':')[0] for r in result.reasons]
    assert codes == [G.REASON_MEAN, G.REASON_INTERVAL, G.REASON_BLOCKS_WINNERS]


def test_only_pass_through_alerts_give_a_zero_difference_and_a_reject():
    result = cell([row(i, stored_r=(-1.0) ** i) for i in range(60)])
    assert result.mean_diff_r == 0.0
    assert result.verdict == G.VERDICT_REJECT
    assert any(r.startswith(G.REASON_MEAN) for r in result.reasons)


def test_the_interval_uses_the_stats_engine_bootstrap():
    blocked, facts = blocked_set(30, -1.0)
    noisy = [row(100 + i, stored_r=0.5) for i in range(5)] + [cvd_row(200 + i, stored_r=0.3) for i in range(20)]
    facts.update({o.candidate_id: ALERT_CF for o in noisy})
    result = cell(blocked + noisy, facts=facts)
    from app.core.stats.summary import bootstrap_interval

    diffs = [p.diff for p in result.pairs]
    expected = bootstrap_interval(diffs, confidence=C.PROMOTION_INTERVAL_CONFIDENCE, resamples=SP.BOOTSTRAP_RESAMPLES, seed=SP.BOOTSTRAP_SEED)
    assert (result.ci_low, result.ci_high) == expected


# ---------------------------------------------------------------------------
# Replay never binds and never promotes
# ---------------------------------------------------------------------------
def test_replay_gets_an_advisory_preview_that_is_never_promote():
    blocked, facts = blocked_set(30, -1.0, source='replay')
    passed = [row(100 + i, stored_r=1.0, source='replay') for i in range(20)]
    cohort = CohortKey('replay', O.PATH_NO_BOOK, O.LOOKBACK_FULL)
    result = cell(blocked + passed, facts=facts, cohort=cohort)
    assert result.verdict == 'preview_promote' != G.VERDICT_PROMOTE
    assert not result.binding
    assert result.reasons[-1].startswith(G.REASON_ADVISORY)
    assert G.VERDICT_PROMOTE not in G.PREVIEW_VERDICTS
    weak = cell([row(i, source='replay') for i in range(3)], cohort=cohort)
    assert weak.verdict == 'preview_insufficient'


# ---------------------------------------------------------------------------
# Embargo, independence, locked book, point in time, no_entry
# ---------------------------------------------------------------------------
def test_the_embargo_is_one_horizon_from_the_last_kept_pair_on_the_same_coin():
    t = START
    same = [row(0, coin='SOLUSDT', decision_time=t + timedelta(hours=h)) for h in (0, 23, 24, 30, 47, 48)]
    other = [row(1, coin='ADAUSDT', decision_time=t + timedelta(hours=1))]
    result = cell(same + other)
    kept = sorted((p.coin, p.decision_time - t) for p in result.pairs)
    assert kept == [('ADAUSDT', timedelta(hours=1)), ('SOLUSDT', timedelta(0)), ('SOLUSDT', timedelta(hours=24)), ('SOLUSDT', timedelta(hours=48))]
    assert result.counts['embargoed'] == 3


@pytest.mark.parametrize(('horizon', 'hours'), sorted(C.VERTICAL_BARRIER_HOURS.items()))
def test_the_embargo_equals_each_horizon(horizon, hours):
    t = START
    rows = [row(0, coin='SOLUSDT', horizon=horizon, decision_time=t + timedelta(hours=h)) for h in (0, hours - 0.25, hours)]
    result = cell(rows, horizon=horizon)
    assert [p.decision_time - t for p in result.pairs] == [timedelta(0), timedelta(hours=hours)]
    assert result.params['embargo_hours_this_cell'] == hours


def test_a_sample_group_counts_once_and_its_first_row_is_the_pair():
    group = str(uuid.uuid4())
    first = cvd_row(0, sample_group_id=group, stored_r=-1.0)
    later = row(1, sample_group_id=group, stored_r=2.0, decision_time=START + timedelta(minutes=30))
    result = cell([later, first], facts={first.candidate_id: ALERT_CF})
    assert [p.candidate_id for p in result.pairs] == [first.candidate_id]
    assert result.counts['independent'] == 1


def test_a_group_whose_first_row_is_no_entry_contributes_nothing():
    group = str(uuid.uuid4())
    first = no_entry(coin='SOLUSDT', source='live', status='alert', status_reason=None, locked_book=True,
                     decision_time=START, bar_close=START, resolved_at=START, sample_group_id=group)
    later = row(1, sample_group_id=group, decision_time=START + timedelta(hours=1))
    result = cell([first, later])
    assert result.n_pairs == 0 and result.counts['no_entry'] == 1


def test_only_the_locked_book_is_read():
    rows = [row(0), row(1, locked_book=False), row(2, locked_book=None)]
    result = cell(rows)
    assert result.n_pairs == 1
    assert (result.counts['not_locked_book'], result.counts['locked_book_unknown'], result.counts['locked_book']) == (1, 1, 1)


def test_an_outcome_resolved_after_as_of_did_not_exist_yet():
    as_of = START + timedelta(days=30)
    late = row(0, resolved_at=as_of + timedelta(minutes=1))
    result = cell([late, row(1)], as_of=as_of)
    assert result.n_pairs == 1 and result.counts['resolved_after_as_of'] == 1


# ---------------------------------------------------------------------------
# Cohorts and horizons are never pooled
# ---------------------------------------------------------------------------
def test_a_cell_refuses_a_foreign_cohort_or_horizon():
    with pytest.raises(MixedCohortError):
        cell([row(0), row(1, source='replay')])
    with pytest.raises(MixedCohortError):
        cell([row(0), row(1, coin='BTCUSDT', options_verdict='align', options_snapshot_time=START)])
    with pytest.raises(ValueError, match='horizon'):
        cell([row(0), row(1, horizon='scalp')])


def test_run_gate_splits_cohorts_and_horizons():
    rows = [row(0), row(1, horizon='scalp'), row(2, source='replay'), row(3, bar_close=datetime(2026, 9, 10, tzinfo=UTC))]
    cells = G.run_gate(CVD, rows, as_of=START + timedelta(days=30))
    keys = [(c.cohort.label, c.horizon, c.n_pairs) for c in cells]
    assert keys == [
        ('live/no_options_book/full', 'scalp', 1),
        ('live/no_options_book/full', 'swing', 1),
        ('live/no_options_book/thin', 'swing', 1),
        ('replay/no_options_book/full', 'swing', 1),
    ]


# ---------------------------------------------------------------------------
# The pair arithmetic of both registered layers
# ---------------------------------------------------------------------------
def test_cvd_pairs():
    alert = row(0, stored_r=0.7)
    blocked = cvd_row(1, stored_r=-0.4)
    assert CVD.pair(alert, None) == (0.7, 0.7)
    assert CVD.pair(blocked, ALERT_CF) == (P.SKIPPED_TRADE_R, -0.4) == (0.0, -0.4)
    assert CVD.pair(blocked, None) is None
    assert CVD.pair(blocked, L.Counterfactual(vote_confirmed=False, status_if_pending='alert')) is None  # hidden vote conflict
    for status in ('watch', 'delayed', 'cancelled', 'abstained'):
        assert CVD.pair(blocked, L.Counterfactual(vote_confirmed=True, status_if_pending=status)) is None
    assert CVD.pair(vote_row(2), ALERT_CF) is None
    assert CVD.pair(row(3, status='cancelled', status_reason='stand_down:event_window'), ALERT_CF) is None
    assert CVD.pair(row(4, status='abstained', status_reason=C.STATUS_REASON_ABSTAIN_EDGE_BELOW_COST), ALERT_CF) is None
    assert CVD.pair(no_entry(status='alert', status_reason=None), None) is None
    assert CVD.blocks(blocked) and not CVD.blocks(alert) and not CVD.blocks(vote_row(5))


def test_vote_conflict_pairs():
    blocked = vote_row(1, stored_r=1.3)
    assert VOTE.pair(row(0, stored_r=-1.0), None) == (-1.0, -1.0)
    assert VOTE.pair(blocked, L.Counterfactual(vote_confirmed=False, status_if_pending='alert')) == (0.0, 1.3)
    assert VOTE.pair(blocked, L.Counterfactual(vote_confirmed=False, status_if_pending='watch')) is None
    assert VOTE.pair(blocked, None) is None
    assert VOTE.pair(cvd_row(2), ALERT_CF) is None
    assert VOTE.blocks(blocked) and not VOTE.blocks(cvd_row(3))


def test_a_blocked_row_without_a_counterfactual_is_counted_but_not_paired():
    blocked = cvd_row(0, stored_r=-1.0)
    result = cell([blocked, row(1)])
    assert (result.counts['blocked_by_layer'], result.counts['blocked_paired'], result.counts['not_in_pair']) == (1, 0, 1)


def test_the_registry():
    assert set(L.LAYERS) == {'cvd_divergence_cancel', 'vote_conflict_watch'}
    assert L.get_layer('vote_conflict_watch') is VOTE
    with pytest.raises(KeyError):
        L.get_layer('nope')
    assert CVD.definition['blocks_reason'] == C.STATUS_REASON_CVD_DIVERGENCE
    assert VOTE.definition['blocks_reason'] == C.STATUS_REASON_VOTE_CONFLICT


# ---------------------------------------------------------------------------
# The verdict is self-describing; parameters are read, not restated
# ---------------------------------------------------------------------------
def test_every_parameter_used_is_in_the_verdict():
    params = cell([row(0)]).params
    assert params['min_shadow_weeks'] == C.PROMOTION_MIN_SHADOW_WEEKS == 4
    assert params['min_shadow_days'] == 28
    assert params['min_paired_outcomes'] == C.PROMOTION_MIN_PAIRED_OUTCOMES == 50
    assert params['interval_confidence'] == C.PROMOTION_INTERVAL_CONFIDENCE == 0.9
    assert params['mean_diff_min_exclusive'] == 0.0
    assert params['bootstrap'] == {'method': 'percentile', 'resamples': SP.BOOTSTRAP_RESAMPLES, 'seed': SP.BOOTSTRAP_SEED}
    assert params['embargo_hours'] == dict(C.VERTICAL_BARRIER_HOURS)
    assert params['bin_edges'] == {'lookback_thin_before': SP.THIN_LOOKBACK_CUTOFF.isoformat()}
    assert params['layer']['name'] == CVD.name and params['layer']['blocks_status'] == 'cancelled'
    assert params['binding_source'] == 'live'


def test_the_model_vocabularies_match_their_sources():
    assert MP.VERDICTS == G.VERDICTS
    assert MP.BINDING_VERDICTS == G.BINDING_VERDICTS
    assert MP.OPTIONS_PATHS == O.OPTIONS_PATHS
    assert MP.LOOKBACKS == (O.LOOKBACK_THIN, O.LOOKBACK_FULL)


def test_the_gate_params_are_outside_the_rule_hash():
    import inspect

    from app.core import hashing

    source = inspect.getsource(hashing)
    assert 'promotion' not in source.replace('PROMOTION_', '')
