"""The pooled estimate on candidates: written by the options job (with the abstain rule) and by one-off enrichment (throwaway schema)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select, text, update

from app.core import constants as C
from app.core.hashing import rule_hash
from app.models.decisions import Candidate
from app.worker import estimates as EST
from app.worker import options_job as OJ
from app.worker import rules_job as RJ
from app.worker import stats_job
from tests.conftest import make_candidate
from tests.rules_helpers import feature_row
from tests.test_stats_job import add_label

UTC = timezone.utc
CURRENT = rule_hash()
HIST0 = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)  # after the thin-lookback cutoff
TARGET = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # more than a swing hold after the history
NO_BOOK = 'options_ignored:no_options_book'
SLICE = dict(session='us', realized_vol_pct=Decimal('0.5'), event_flag=False)


def history(session, count, *, coin='SOLUSDT', source='replay', horizon='swing', verdict='ignore', reason=NO_BOOK,
            r='1.0', barrier='upper', start=0, **label):
    out = []
    for i in range(start, start + count):
        c = make_candidate(
            session, coin=coin, horizon=horizon, source=source, bar_close=HIST0 + timedelta(minutes=15 * i), rule_hash=CURRENT,
            setup_family='breakout', trend_family='trend', options_verdict=verdict, status_reason=reason, feature_overrides=SLICE,
        )
        add_label(session, c, first_barrier=barrier, stored_r=r, regime_trend_family='trend', regime_vol_pct=Decimal('0.5'),
                  regime_session='us', regime_event_flag=False, **label)
        out.append(c)
    return out


def target(session, *, coin='SOLUSDT', source='replay', horizon='swing', verdict='ignore', reason=NO_BOOK, bar=TARGET, **kw):
    kw.setdefault('rule_hash', CURRENT)
    return make_candidate(
        session, coin=coin, horizon=horizon, source=source, bar_close=bar, setup_family='breakout',
        trend_family='trend', options_verdict=verdict, status_reason=reason, feature_overrides=SLICE, **kw,
    )


def fresh(session, candidate) -> Candidate:
    session.expire_all()
    return session.get(Candidate, candidate.id)


def stored(candidate: Candidate) -> dict:
    return {name: getattr(candidate, name) for name in EST.ESTIMATE_COLUMNS}


def decision_fields(c: Candidate):
    return (c.status, c.status_reason, c.direction, c.stop_price, c.target_cap_price, c.options_verdict, c.sample_group_id)


# ---------------------------------------------------------------------------
# enrichment
# ---------------------------------------------------------------------------
def test_enrichment_fills_every_estimate_column_from_the_point_in_time_pool(session):
    history(session, 30, r='1.0')
    t = target(session)
    before = decision_fields(t)
    report = EST.enrich(session)
    c = fresh(session, t)
    assert c.pool_n == 30 and c.pool_level == 'full_slice'
    assert c.p_upper_before_stop == Decimal('1.000000') and c.expected_r == Decimal('1.000000')
    assert c.expected_mae_r == Decimal('0.200000') and c.median_hours_to_resolution == Decimal('2.0000')
    assert c.expected_r_ci_low is not None and c.expected_r_ci_low <= c.expected_r_ci_high
    assert c.pool_interval_confidence == Decimal('0.900000')
    assert decision_fields(c) == before  # the estimate changes no status, reason, stop, cap or group
    assert report.results[EST.FILLED] == 1
    assert report.filled_by_column['expected_r'] == 1 and report.filled_by_level['full_slice'] == 1


def test_history_rows_themselves_stay_null_until_their_own_pool_reaches_the_floor(session):
    rows = history(session, 30)
    report = EST.enrich(session)
    assert all(stored(fresh(session, r))['expected_r'] is None for r in rows)
    assert report.results[EST.BELOW_FLOOR] == 30


def test_enrichment_is_idempotent_and_never_overwrites_a_stored_estimate(session):
    history(session, 30)
    t = target(session)
    EST.enrich(session)
    first = stored(fresh(session, t))
    second_report = EST.enrich(session)
    assert stored(fresh(session, t)) == first
    assert second_report.results[EST.ALREADY_FILLED] == 1 and EST.FILLED not in second_report.results
    # even with a pool that has since changed, a stored value is never rewritten
    history(session, 40, start=100, r='-1.0', barrier='lower')
    EST.enrich(session)
    assert stored(fresh(session, t)) == first


def test_a_manually_stored_estimate_is_left_alone(session):
    history(session, 30)
    t = target(session)
    session.execute(update(Candidate).where(Candidate.id == t.id).values(
        p_upper_before_stop=Decimal('0.25'), expected_r=Decimal('-0.5'), pool_level='setup_family', pool_n=99))
    EST.enrich(session)
    c = fresh(session, t)
    assert (c.p_upper_before_stop, c.expected_r, c.pool_n, c.pool_level) == (Decimal('0.25'), Decimal('-0.5'), 99, 'setup_family')
    assert c.expected_r_ci_low is None  # a partial state is not completed over the top of it


def test_the_guarded_write_only_touches_rows_with_no_estimate(session):
    history(session, 30)
    t = target(session)
    values = EST.estimate_values(EST.PL.PoolEstimate('full_slice', 30, 0.5, 0.1, 0.2, 2.0, -0.1, 0.3, 0.9))
    assert EST.write_estimate(session, t.id, values) is True
    assert EST.write_estimate(session, t.id, {**values, 'pool_n': 31}) is False
    assert fresh(session, t).pool_n == 30


def test_a_dry_run_counts_and_writes_nothing(session):
    history(session, 30)
    t = target(session)
    report = EST.enrich(session, dry_run=True)
    assert report.results[EST.FILLED] == 1 and report.dry_run
    assert stored(fresh(session, t))['expected_r'] is None


def test_outcomes_not_yet_resolved_at_the_decision_instant_are_not_in_the_pool(session):
    rows = history(session, 29)
    late = history(session, 1, start=29, resolved_at=TARGET + timedelta(hours=1))  # decided before, resolved after
    t = target(session)
    EST.enrich(session)
    assert fresh(session, t).pool_n is None and late and rows
    assert stored(fresh(session, t))['expected_r'] is None


def test_outcomes_decided_after_the_candidate_do_not_change_its_estimate(session):
    history(session, 30)
    t = target(session)
    EST.enrich(session)
    first = stored(fresh(session, t))
    # a later decision cannot be in the past of t: wipe t's estimate, add a pile of later losses, enrich again
    session.execute(update(Candidate).where(Candidate.id == t.id).values({name: None for name in EST.ESTIMATE_COLUMNS}))
    for i in range(40):
        later = target(session, bar=TARGET + timedelta(hours=1 + i))
        add_label(session, later, first_barrier='lower', stored_r='-1.0', regime_trend_family='trend',
                  regime_vol_pct=Decimal('0.5'), regime_session='us', regime_event_flag=False)
    EST.enrich(session)
    assert stored(fresh(session, t)) == first


def test_pools_never_cross_source_or_horizon_in_the_database(session):
    history(session, 40, source='live')
    history(session, 40, horizon='scalp', start=100)
    t = target(session)
    EST.enrich(session)
    assert fresh(session, t).pool_n is None


def test_thin_lookback_outcomes_do_not_feed_a_pool_but_a_thin_candidate_still_gets_one(session):
    full = history(session, 30)
    thin = [make_candidate(session, coin='SOLUSDT', horizon='swing', source='replay',
                           bar_close=datetime(2026, 9, 10, 0, 0, tzinfo=UTC) + timedelta(minutes=15 * i), rule_hash=CURRENT,
                           setup_family='breakout', trend_family='trend', options_verdict='ignore', status_reason=NO_BOOK,
                           feature_overrides=SLICE) for i in range(40)]
    for c in thin:
        add_label(session, c, regime_trend_family='trend', regime_vol_pct=Decimal('0.5'), regime_session='us', regime_event_flag=False)
    t = target(session)
    EST.enrich(session)
    assert fresh(session, t).pool_n == 30  # only the 30 full-lookback rows
    # a candidate on thin lookback still receives an estimate (from the full rows decided before it): none here, so NULL
    assert full and stored(fresh(session, thin[-1]))['expected_r'] is None


def test_a_btc_candidate_whose_options_check_has_not_run_has_no_pool_to_look_up(session):
    history(session, 30, coin='BTCUSDT', verdict='ignore', reason='options_ignored:no_options_history')
    t = target(session, coin='BTCUSDT', verdict=None, reason=C.STATUS_REASON_PENDING_OPTIONS_CHECK, status='watch')
    report = EST.enrich(session)
    assert report.results[EST.NO_OPTIONS_PATH] == 1
    assert stored(fresh(session, t))['expected_r'] is None


def test_btc_pools_are_kept_per_options_path(session):
    history(session, 30, coin='BTCUSDT', verdict='ignore', reason='options_ignored:no_options_history')
    layer = target(session, coin='BTCUSDT', source='replay', verdict='align', reason=None)
    ignored_history = target(session, coin='BTCUSDT', verdict='ignore', reason='options_ignored:no_options_history',
                             bar=TARGET + timedelta(hours=1))
    EST.enrich(session)
    assert fresh(session, layer).pool_n is None  # options_layer never borrows no_options_history outcomes
    assert fresh(session, ignored_history).pool_n == 30


def test_only_current_rule_hash_outcomes_and_candidates_take_part(session):
    for c in history(session, 30):
        session.execute(text('UPDATE candidate SET rule_hash = :h WHERE id = :i'), {'h': '0' * 64, 'i': c.id})
    session.execute(text("UPDATE label SET rule_hash = :h"), {'h': '0' * 64})
    t = target(session)
    stale = target(session, bar=TARGET + timedelta(hours=1), rule_hash='0' * 64)
    report = EST.enrich(session)
    assert fresh(session, t).pool_n is None
    assert report.candidates_considered == 1 and stale


# ---------------------------------------------------------------------------
# write paths
# ---------------------------------------------------------------------------
def add_feature_rows(db, coin, bar_close, *, source, horizons, **columns):
    # not rules_helpers.add_feature_rows: the regime column named `session` would clash with its first argument
    rows = [feature_row(coin, horizon, bar_close, source, **columns) for horizon in horizons]
    db.add_all(rows)
    db.flush()
    return rows


def run_rules(session, coins, bar=TARGET):
    return RJ.run_rules_for_bar(session, coins, bar, source='replay', feed_breaches={}, code_version='testver')


def decide_alt(session, **columns):
    """Rules then options for one SOL swing candidate at TARGET. Returns the stored candidate."""
    add_feature_rows(session, 'SOLUSDT', TARGET, source='replay', horizons=('swing',), alt_residual_z_signal_bar=1.3,
                     **SLICE, **columns)
    run_rules(session, ('SOLUSDT',))
    (c,) = session.execute(select(Candidate).where(Candidate.bar_close == TARGET)).scalars()
    assert stored(c)['expected_r'] is None  # the rules job writes no estimate
    OJ.run_options_job(session, source='replay')
    return fresh(session, c)


def test_the_options_job_writes_the_estimate_and_a_clearing_pool_keeps_the_alert(session):
    history(session, 30)
    c = decide_alt(session)
    assert (c.status, c.status_reason) == ('alert', NO_BOOK)
    assert c.pool_n == 30 and c.expected_r == Decimal('1.000000') and c.p_upper_before_stop == Decimal('1.000000')


def test_without_a_pool_the_estimate_stays_null_and_the_alert_stands(session):
    history(session, 5, r='-1.0', barrier='lower')
    c = decide_alt(session)
    assert c.status == 'alert' and stored(c)['expected_r'] is None and stored(c)['p_upper_before_stop'] is None


def test_a_pool_whose_interval_sits_below_zero_abstains(session):
    history(session, 30, r='-1.0', barrier='lower')
    c = decide_alt(session)
    assert (c.status, c.status_reason) == ('abstained', C.STATUS_REASON_ABSTAIN_EDGE_BELOW_COST)
    assert c.pool_n == 30 and c.expected_r_ci_low < 0
    assert c.options_verdict == 'ignore' and c.stop_price == c.pre_options_stop_price  # the verdict and prices are kept


@pytest.mark.parametrize(
    ('carry', 'status'),
    [(None, 'alert'), (Decimal('50'), 'alert'), (Decimal('100'), 'abstained'), (Decimal('-100'), 'abstained')],
)
def test_the_interval_is_tested_against_the_adverse_edge_of_the_funding_band(session, carry, status):
    # every pooled outcome is +0.5 R, so the interval is [0.5, 0.5]; 1R = 1,500 on a 96,500 close, so the edge in R is
    # |carry| / 1e4 * 64.33: 50 bps -> 0.32 R clears, 100 bps -> 0.64 R does not, whichever sign the carry has
    history(session, 30, r='0.5')
    c = decide_alt(session, funding_carry_bps_horizon=carry)
    assert c.status == status and c.expected_r_ci_low == Decimal('0.500000')


def test_shadow_rows_get_the_estimate_but_never_abstain(session):
    history(session, 30, r='-1.0', barrier='lower')
    c = decide_alt(session, cvd_divergence=True, cvd_divergence_against='long')
    assert (c.status, c.status_reason) == ('cancelled', C.STATUS_REASON_CVD_DIVERGENCE)
    assert c.pool_n == 30


def test_a_stored_estimate_is_used_rather_than_recomputed(session):
    history(session, 30)  # this pool would clear
    add_feature_rows(session, 'SOLUSDT', TARGET, source='replay', horizons=('swing',), alt_residual_z_signal_bar=1.3, **SLICE)
    run_rules(session, ('SOLUSDT',))
    (c,) = session.execute(select(Candidate).where(Candidate.bar_close == TARGET)).scalars()
    session.execute(update(Candidate).where(Candidate.id == c.id).values(pool_n=40, expected_r_ci_low=Decimal('-0.2')))
    OJ.run_options_job(session, source='replay')
    c = fresh(session, c)
    assert c.status == 'abstained' and c.pool_n == 40


def test_a_btc_candidate_gets_its_estimate_when_the_options_check_writes_the_verdict(session):
    history(session, 30, coin='BTCUSDT', verdict='ignore', reason='options_ignored:no_options_history')
    add_feature_rows(session, 'BTCUSDT', TARGET, source='replay', horizons=('swing',), **SLICE)
    run_rules(session, ('BTCUSDT',))
    (c,) = session.execute(select(Candidate).where(Candidate.bar_close == TARGET)).scalars()
    assert stored(c)['expected_r'] is None  # the path is not known at creation
    OJ.run_options_job(session, source='replay')
    c = fresh(session, c)
    assert c.pool_n == 30 and c.status == 'watch'  # an ignored covered coin never alerts, so it cannot abstain


def test_an_estimate_failure_defers_the_options_check_instead_of_alerting(session, monkeypatch):
    history(session, 30, r='-1.0', barrier='lower')
    add_feature_rows(session, 'SOLUSDT', TARGET, source='replay', horizons=('swing',), alt_residual_z_signal_bar=1.3, **SLICE)

    def boom(*args, **kwargs):
        raise RuntimeError('estimator down')

    run_rules(session, ('SOLUSDT',))
    monkeypatch.setattr(EST.PL, 'estimate_pool', boom)
    report = OJ.run_options_job(session, source='replay')
    assert [o.result for o in report.outcomes] == [OJ.RESULT_ESTIMATE_ERROR]
    (c,) = session.execute(select(Candidate).where(Candidate.bar_close == TARGET)).scalars()
    assert (c.status, c.status_reason, c.options_verdict) == ('watch', C.STATUS_REASON_PENDING_OPTIONS_CHECK, None)
    monkeypatch.undo()
    OJ.run_options_job(session, source='replay')  # the next run retries it
    assert fresh(session, c).status == 'abstained'


def test_the_m6_fill_trigger_does_not_block_the_estimate_columns(session):
    history(session, 30)
    t = target(session, stop_price=Decimal('98'), pre_options_stop_price=Decimal('98'), entry_price=Decimal('100'), one_r=Decimal('2'), upper_barrier_price=Decimal('102'),
               lower_barrier_price=Decimal('98'), vertical_deadline=TARGET + timedelta(hours=24))
    EST.enrich(session)
    assert fresh(session, t).pool_n == 30  # filled although the fill values are frozen


def test_the_stats_cli_flag_is_wired(session):
    import argparse

    from app.worker import stats_job as SJ

    source = open(SJ.__file__, encoding='utf-8').read()
    assert '--enrich-estimates' in source and 'estimates.enrich' in source
    assert argparse.ArgumentParser  # the flag is the only write path of the job
    assert stats_job.fetch_observations is not None
