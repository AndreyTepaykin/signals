"""The pooled estimate: edge-line walk-up, pool keys, independence, point in time (no lookahead), interval."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import constants as C
from app.core.stats import params as P
from app.core.stats import pool as PL
from tests.stats_helpers import T0, no_entry, obs

LAG = timedelta(seconds=30)
DECISION = T0 + timedelta(days=60)  # the shared id counter spaces default rows 15 minutes apart; stay after all of them


def target(**overrides) -> PL.PoolTarget:
    values = dict(
        decision_time=DECISION, horizon='swing', source='replay', options_path='no_options_book', setup_family='squeeze',
        trend_family='chop', vol_pct=0.5, session='us', event_flag=False,
    )
    values.update(overrides)
    return PL.PoolTarget(**values)


def history(count: int, **overrides):
    return [obs(**overrides) for _ in range(count)]


def estimate(rows, **overrides):
    return PL.estimate_pool(rows, target(**overrides.pop('target', {})), knowable_lag=LAG, **overrides)


def test_thirty_independent_outcomes_at_the_full_slice_give_the_estimate_at_that_level():
    result = estimate(history(30))
    assert isinstance(result, PL.PoolEstimate)
    assert result.level == C.POOL_WALK_UP_LEVELS[0][0]
    assert result.n_independent == 30


def test_below_thirty_independent_outcomes_at_every_level_nothing_is_estimated():
    result = estimate(history(29))
    assert isinstance(result, PL.NoEstimate)
    assert result.best_n == 29


def test_p_upper_is_the_share_of_upper_first_barriers_and_a_timeout_is_a_miss():
    rows = history(12) + history(10, first_barrier='lower', stored_r=-1.0) + history(8, first_barrier='vertical', stored_r=0.3)
    result = estimate(rows)
    assert result.p_upper_before_stop == pytest.approx(12 / 30)
    # a timeout with positive stored R is a miss for P but still counts in the mean R
    assert result.expected_r == pytest.approx((12 * 1.0 - 10 + 8 * 0.3) / 30)


def test_expected_mae_is_the_mean_and_hours_the_median():
    rows = [obs(mae_r=0.2 + 0.01 * i, hours_to_resolution=float(i)) for i in range(30)]
    result = estimate(rows)
    assert result.expected_mae_r == pytest.approx(sum(0.2 + 0.01 * i for i in range(30)) / 30)
    assert result.median_hours_to_resolution == pytest.approx(14.5)


def test_the_interval_is_a_seeded_90_percent_bootstrap_and_repeats():
    rows = [obs(stored_r=float((-1) ** i)) for i in range(40)]
    first, second = estimate(rows), estimate(rows)
    assert first.expected_r_ci_low == second.expected_r_ci_low
    assert first.expected_r_ci_low < first.expected_r < first.expected_r_ci_high
    assert first.interval_confidence == P.INTERVAL_CONFIDENCE == 0.9


def test_the_walk_up_goes_one_level_at_a_time_to_the_first_cell_with_enough():
    other_event = history(30, event_flag=True)
    result = estimate(other_event)  # the full slice has none; dropping the event flag has 30
    assert result.level == C.POOL_WALK_UP_LEVELS[1][0]
    other_session = history(30, session='asia', event_flag=True)
    assert estimate(other_session).level == 'drop_session'


def test_the_walk_up_ends_at_the_setup_family_and_never_beyond_it():
    other_regime = history(30, trend_family='trend_up', vol_pct=0.9, session='asia', event_flag=True)
    assert estimate(other_regime).level == 'setup_family'
    other_family = history(60, setup_family='breakout', trend_family='trend_up', vol_pct=0.9, session='asia')
    assert isinstance(estimate(other_family), PL.NoEstimate)


def test_a_repeated_edge_line_level_is_skipped_not_counted_twice():
    names = [name for name, _dims in PL.distinct_levels()]
    assert 'trend_x_vol' not in names
    assert names[0] == 'full_slice' or names[0] == C.POOL_WALK_UP_LEVELS[0][0]


# ---------------------------------------------------------------------------
# pool keys that never walk away
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('overrides', [
    {'horizon': 'scalp'},
    {'source': 'live'},
    {'coin': 'BTCUSDT', 'options_verdict': 'align', 'status_reason': None},  # options_layer, not no_options_book
])
def test_horizon_source_and_options_path_are_never_pooled_across(overrides):
    assert isinstance(estimate(history(200, **overrides)), PL.NoEstimate)


def test_thin_lookback_outcomes_are_excluded_from_the_pool_but_a_thin_target_still_gets_an_estimate():
    thin = [obs(bar_close=P.THIN_LOOKBACK_CUTOFF - timedelta(days=1)) for _ in range(40)]
    assert isinstance(estimate(thin), PL.NoEstimate)
    full = history(30)
    assert isinstance(estimate(full, target={'decision_time': DECISION}), PL.PoolEstimate)


def test_no_entry_outcomes_are_not_pooled():
    rows = history(29) + [no_entry() for _ in range(10)]
    assert isinstance(estimate(rows), PL.NoEstimate)


def test_every_candidate_status_is_pooled():
    rows = history(10) + history(10, status='watch') + history(10, status='shadow')
    assert estimate(rows).n_independent == 30


def test_only_the_first_outcome_of_a_sample_group_counts():
    rows = history(28) + [obs(sample_group_id='g') for _ in range(10)]
    assert isinstance(estimate(rows), PL.NoEstimate)  # 28 + the ten rows of g counted once = 29
    rows = history(29) + [obs(sample_group_id='g') for _ in range(10)]
    assert estimate(rows).n_independent == 30


def test_the_group_representative_is_chosen_without_looking_at_its_outcome():
    first = obs(sample_group_id='g', resolved_at=DECISION + timedelta(hours=5))  # not yet resolved at the decision
    second = obs(sample_group_id='g', resolved_at=T0 + timedelta(hours=2))
    result = estimate(history(29) + [first, second])
    assert isinstance(result, PL.NoEstimate)  # the unresolved first member is not replaced by the later, resolved one


# ---------------------------------------------------------------------------
# point in time
# ---------------------------------------------------------------------------
def test_an_outcome_not_yet_knowable_at_the_decision_instant_is_left_out():
    rows = history(29) + [obs(resolved_at=DECISION - timedelta(seconds=10))]  # inside the settle lag
    assert isinstance(estimate(rows), PL.NoEstimate)
    rows = history(29) + [obs(resolved_at=DECISION - LAG)]  # exactly knowable
    assert isinstance(estimate(rows), PL.PoolEstimate)


def test_outcomes_decided_at_or_after_the_target_never_enter():
    later = [obs(decision_time=DECISION + timedelta(minutes=i), resolved_at=DECISION + timedelta(hours=1)) for i in range(40)]
    assert isinstance(estimate(later), PL.NoEstimate)
    same_instant = [obs(decision_time=DECISION, resolved_at=T0 + timedelta(hours=1)) for _ in range(40)]
    assert isinstance(estimate(same_instant), PL.NoEstimate)


def test_lookahead_changing_every_later_outcome_does_not_change_the_estimate():
    past = history(30)
    baseline = estimate(past)
    future = [
        obs(decision_time=DECISION + timedelta(hours=i + 1), resolved_at=DECISION + timedelta(hours=i + 2),
            first_barrier='lower', stored_r=-5.0)
        for i in range(60)
    ]
    # unresolved-at-the-decision rows that WOULD have been decided before it: also invisible
    pending = [obs(decision_time=DECISION - timedelta(hours=1), resolved_at=DECISION + timedelta(hours=3),
                   first_barrier='lower', stored_r=-5.0) for _ in range(20)]
    assert estimate(past + future + pending) == baseline


def test_outcomes_older_than_ninety_days_are_outside_the_window():
    old = [obs(decision_time=T0, resolved_at=DECISION - timedelta(days=C.POOL_WINDOW_DAYS, seconds=1)) for _ in range(40)]
    assert isinstance(estimate(old), PL.NoEstimate)
    edge = [obs(decision_time=T0, resolved_at=DECISION - timedelta(days=C.POOL_WINDOW_DAYS) + timedelta(seconds=1)) for _ in range(30)]
    assert isinstance(estimate(edge), PL.PoolEstimate)
