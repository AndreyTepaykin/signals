"""The rules job against Postgres: candidate creation respects every CHECK, and live equals replay."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core import constants as C
from app.core.hashing import rule_hash
from app.core.rules.locked_book import is_locked_book
from app.models import enums as E
from app.models.decisions import Candidate
from app.worker import rules_job as RJ
from tests.rules_helpers import T0, add_feature_rows, bars, short_overrides

NO_BREACHES: dict = {}  # an explicit mapping: do not ask feed_health


def candidates(session, **where):
    statement = select(Candidate).order_by(Candidate.bar_close, Candidate.coin, Candidate.horizon)
    for column, value in where.items():
        statement = statement.where(getattr(Candidate, column) == value)
    return session.execute(statement).scalars().all()


def run(session, coins=('BTCUSDT',), bar=T0, **kwargs):
    kwargs.setdefault('feed_breaches', NO_BREACHES)
    kwargs.setdefault('code_version', 'testver')
    return RJ.run_rules_for_bar(session, coins, bar, **kwargs)


# ---------------------------------------------------------------------------
# What a created candidate looks like
# ---------------------------------------------------------------------------
def test_a_passing_bar_writes_one_pending_candidate_per_horizon(session):
    add_feature_rows(session, 'BTCUSDT', T0)
    report = run(session, now=T0 + timedelta(seconds=40))
    assert report.created == 3
    rows = candidates(session)
    assert [r.horizon for r in rows] == sorted(C.HORIZONS)  # one per horizon: position, scalp, swing
    for r in rows:
        assert (r.coin, r.direction, r.source, r.trigger) == ('BTCUSDT', 'long', 'live', 'swing_break')
        assert (r.setup_family, r.trend_family) == ('breakout', 'trend')
        assert (r.status, r.status_reason) == ('watch', C.STATUS_REASON_PENDING_OPTIONS_CHECK)
        assert r.sample_group_reason == 'new' and r.sample_group_leader_id is None


def test_m4_writes_the_pre_options_layer_and_leaves_the_rest_to_m5_and_m6(session):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',))
    run(session, now=T0 + timedelta(seconds=40))
    (r,) = candidates(session)
    assert r.pre_options_stop_price == Decimal('95000')
    assert r.pre_options_target_price == Decimal('98000')
    assert r.stop_price == r.pre_options_stop_price  # NOT NULL; M5 owns it from here
    for name in (
        'entry_price',
        'one_r',
        'upper_barrier_price',
        'lower_barrier_price',
        'vertical_deadline',
        'target_cap_price',
        'options_verdict',
        'options_venue',
        'options_vol_tag',
        'options_dealer_gamma_sign',
        'options_nearest_path_strike',
        'options_hours_to_expiry',
        'options_expression',
        'size_multiplier',
        'options_influence',
        'options_confidence',
        'options_metric_id',
        'options_snapshot_time',
        'p_upper_before_stop',
        'expected_r',
        'expected_mae_r',
        'median_hours_to_resolution',
        'pool_level',
        'pool_n',
        'expected_r_ci_low',
        'expected_r_ci_high',
        'pool_interval_confidence',
    ):
        assert getattr(r, name) is None, name


def test_the_three_hashes_and_the_feature_row_link(session):
    (row,) = add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',))
    run(session, now=T0 + timedelta(seconds=40), code_version='abc1234')
    (r,) = candidates(session)
    assert r.feature_hash == row.feature_hash and r.feature_row_id == row.id
    assert r.rule_hash == rule_hash() and len(r.rule_hash) == 64
    assert r.code_version == 'abc1234'
    assert r.decision_time == T0 + timedelta(seconds=40) and r.bar_close == T0


def test_a_short_candidate_is_written_with_the_stop_above(session):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',), **short_overrides())
    run(session)
    (r,) = candidates(session)
    assert (r.direction, r.pre_options_stop_price, r.pre_options_target_price) == ('short', Decimal('98000'), Decimal('95000'))


def test_the_schema_refuses_an_alert_without_a_verdict_so_m4_never_writes_one(session):
    add_feature_rows(session, 'BTCUSDT', T0)
    run(session)
    assert {r.status for r in candidates(session)} <= {'watch', 'cancelled'}
    assert session.execute(select(func.count()).select_from(Candidate).where(Candidate.status == 'alert')).scalar_one() == 0


def test_the_setup_family_check_refuses_anything_outside_the_vocabulary(session):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',))
    run(session)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(update(Candidate).values(setup_family='swing_break_trend'))
    for family in C.SETUP_FAMILIES:
        session.execute(update(Candidate).values(setup_family=family))


# ---------------------------------------------------------------------------
# Idempotence and dry runs
# ---------------------------------------------------------------------------
def test_a_rerun_changes_nothing(session):
    add_feature_rows(session, 'BTCUSDT', T0)
    first = run(session)
    before = [(r.id, r.status, r.sample_group_id, r.rule_hash) for r in candidates(session)]
    second = run(session)
    assert (first.created, second.created, second.count(RJ.RESULT_PRESENT)) == (3, 0, 3)
    assert [(r.id, r.status, r.sample_group_id, r.rule_hash) for r in candidates(session)] == before


def test_a_dry_run_decides_but_writes_nothing(session):
    add_feature_rows(session, 'BTCUSDT', T0)
    report = run(session, dry_run=True)
    assert report.count(RJ.RESULT_DRY_RUN) == 3 and report.created == 0
    assert candidates(session) == []


def test_a_bar_with_no_feature_row_is_reported_not_guessed(session):
    report = run(session)
    assert [o.result for o in report.outcomes] == [RJ.RESULT_NO_FEATURE_ROW] * 3
    assert candidates(session) == []


def test_no_trigger_no_row(session):
    add_feature_rows(session, 'BTCUSDT', T0, swing_break_event=False)
    report = run(session)
    assert candidates(session) == [] and {o.skipped for o in report.outcomes} == {'no_trigger'}


def test_decision_time_cannot_precede_the_bar_close(session):
    add_feature_rows(session, 'BTCUSDT', T0)
    with pytest.raises(ValueError):
        run(session, decision_time=T0 - timedelta(seconds=1))
    with pytest.raises(ValueError):
        run(session, source='paper')


# ---------------------------------------------------------------------------
# Live equals replay
# ---------------------------------------------------------------------------
COMPARABLE = (
    'coin horizon direction trigger setup_family trend_family pre_options_stop_price pre_options_target_price '
    'stop_price status status_reason locked_book sample_group_reason bar_close rule_hash code_version'
).split()


def snapshot(rows):
    return [{name: getattr(r, name) for name in COMPARABLE} for r in rows]


def test_the_same_features_give_the_same_candidates_live_and_replay(session):
    for source in (E.SOURCE_LIVE, E.SOURCE_REPLAY):
        add_feature_rows(session, 'BTCUSDT', T0, source=source, event_flag=True)
        add_feature_rows(session, 'ETHUSDT', T0, source=source, alt_residual_z_signal_bar=1.4)
    run(session, ('BTCUSDT', 'ETHUSDT'), source=E.SOURCE_LIVE, now=T0 + timedelta(seconds=45))
    run(session, ('BTCUSDT', 'ETHUSDT'), source=E.SOURCE_REPLAY)
    live, replay = candidates(session, source='live'), candidates(session, source='replay')
    assert len(live) == len(replay) == 6
    assert snapshot(live) == snapshot(replay)
    assert all(r.decision_time == T0 for r in replay)  # replay: decision at the bar close, no wall clock
    assert all(r.decision_time == T0 + timedelta(seconds=45) for r in live)


def test_sample_groups_never_cross_the_source_boundary(session):
    for source in (E.SOURCE_LIVE, E.SOURCE_REPLAY):
        add_feature_rows(session, 'BTCUSDT', T0, source=source, horizons=('swing',))
    run(session, source=E.SOURCE_LIVE)
    run(session, source=E.SOURCE_REPLAY)
    live, replay = candidates(session, source='live')[0], candidates(session, source='replay')[0]
    assert live.sample_group_id != replay.sample_group_id
    assert live.sample_group_reason == replay.sample_group_reason == 'new'


def test_replay_is_reproducible_row_for_row_including_group_ids(session):
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY, horizons=('swing',))
    run(session, source=E.SOURCE_REPLAY)
    first = candidates(session)[0].sample_group_id
    session.execute(Candidate.__table__.delete())
    run(session, source=E.SOURCE_REPLAY)
    assert candidates(session)[0].sample_group_id == first


def test_replay_does_not_consult_feed_health_but_live_does(session):
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_LIVE, horizons=('swing',))
    add_feature_rows(session, 'BTCUSDT', T0, source=E.SOURCE_REPLAY, horizons=('swing',))
    # no feed_health rows exist: every expected feed is a breach for a live decision
    RJ.run_rules_for_bar(session, ['BTCUSDT'], T0, source=E.SOURCE_LIVE, now=T0 + timedelta(seconds=40), code_version='t')
    RJ.run_rules_for_bar(session, ['BTCUSDT'], T0, source=E.SOURCE_REPLAY, code_version='t')
    (live,), (replay,) = candidates(session, source='live'), candidates(session, source='replay')
    assert (live.status, live.status_reason) == ('cancelled', 'stand_down:feed_stale')
    assert (replay.status, replay.status_reason) == ('watch', C.STATUS_REASON_PENDING_OPTIONS_CHECK)


def test_live_feed_breaches_come_from_stand_down_breaches(session):
    names = RJ.live_feed_breaches(session, T0, 'ETHUSDT')
    assert names and all(':' in n for n in names)
    assert any(n.endswith(':ETHUSDT') for n in names) and any(n.endswith(':BTCUSDT') for n in names)


def test_the_live_tick_picks_the_newest_closed_bar(session, monkeypatch):
    monkeypatch.setattr(RJ, 'live_feed_breaches', lambda *a, **k: ())
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',))
    summary = RJ.run_rules_job(session, ['BTCUSDT'], now=T0 + timedelta(seconds=45))
    assert summary['bar_close'] == T0.isoformat() and summary['created'] == 1
    (r,) = candidates(session)
    assert r.decision_time == T0 + timedelta(seconds=45) and r.status_reason == C.STATUS_REASON_PENDING_OPTIONS_CHECK


# ---------------------------------------------------------------------------
# Shadow rows: stand-down and CVD still produce a resolvable candidate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('columns', 'status', 'reason'),
    [
        ({'event_flag': True}, 'cancelled', 'stand_down:event_window'),
        ({'perp_spot_disagreement_atr': 1.5}, 'cancelled', 'stand_down:perp_spot_disagreement'),
        ({'cvd_divergence': True, 'cvd_divergence_against': 'long'}, 'cancelled', 'cvd_divergence_against'),
        ({'aggressor_imbalance_z': -2.0, 'oi_price_quadrant': 'price_down_oi_up'}, 'watch', 'vote_conflict'),
    ],
)
def test_filters_leave_a_shadow_row_with_the_same_stop_and_target(session, columns, status, reason):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',), **columns)
    run(session)
    (r,) = candidates(session)
    assert (r.status, r.status_reason) == (status, reason)
    assert (r.pre_options_stop_price, r.pre_options_target_price) == (Decimal('95000'), Decimal('98000'))


# ---------------------------------------------------------------------------
# One open signal per coin per horizon, and the groups that follow
# ---------------------------------------------------------------------------
def test_one_open_signal_blocks_the_next_bar_and_the_slot_frees_when_it_is_not_an_alert(session):
    nxt = T0 + timedelta(minutes=15)
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',))
    add_feature_rows(session, 'BTCUSDT', nxt, horizons=('swing',))
    run(session, horizons=('swing',))
    blocked = run(session, bar=nxt, horizons=('swing',))
    assert [o.skipped for o in blocked.outcomes] == ['open_signal'] and len(candidates(session)) == 1

    # M5 turns the pending row into a skip: the slot is free again, but the hold still groups the new row
    session.execute(update(Candidate).values(status='cancelled', status_reason='headwind_room'))
    again = run(session, bar=nxt, horizons=('swing',))
    assert again.created == 1
    first, second = candidates(session)
    assert (second.sample_group_reason, second.sample_group_leader_id, second.sample_group_id) == (
        'overlap',
        first.id,
        first.sample_group_id,
    )


def test_an_alert_holds_the_slot_for_the_whole_horizon_and_not_a_bar_longer(session):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('scalp',))
    run(session, horizons=('scalp',))
    session.execute(update(Candidate).values(status='alert', status_reason=None, options_verdict='align'))
    for minutes, expect in ((15, 'open_signal'), (225, 'open_signal'), (240, None)):  # scalp hold = 4h = 240 minutes
        bar = T0 + timedelta(minutes=minutes)
        add_feature_rows(session, 'BTCUSDT', bar, horizons=('scalp',))
        report = run(session, bar=bar, horizons=('scalp',))
        assert report.outcomes[0].skipped == expect, minutes
    last = candidates(session)[-1]
    assert last.bar_close == T0 + timedelta(minutes=240) and last.sample_group_reason == 'new'


def test_horizons_are_independent(session):
    nxt = T0 + timedelta(minutes=15)
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('scalp',))
    add_feature_rows(session, 'BTCUSDT', nxt, horizons=('swing',))
    run(session, horizons=('scalp',))
    report = run(session, bar=nxt, horizons=('swing',))
    assert report.created == 1
    assert {r.horizon: r.sample_group_reason for r in candidates(session)} == {'scalp': 'new', 'swing': 'new'}


def test_the_rules_ignore_candidates_from_the_future_of_the_bar(session):
    later = T0 + timedelta(hours=1)
    add_feature_rows(session, 'BTCUSDT', later, horizons=('swing',), source=E.SOURCE_REPLAY)
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',), source=E.SOURCE_REPLAY)
    run(session, bar=later, source=E.SOURCE_REPLAY)
    report = run(session, bar=T0, source=E.SOURCE_REPLAY)  # a re-run of older history
    assert report.created == 1
    by_bar = {r.bar_close: r for r in candidates(session)}
    assert by_bar[T0].sample_group_reason == 'new'  # did not see the candidate an hour later
    assert by_bar[later].sample_group_reason == 'new'  # created before T0 existed, and not rewritten


def test_alts_on_a_btc_impulse_share_the_btc_group_whatever_order_they_are_asked_in(session):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',))
    for coin in ('ETHUSDT', 'SOLUSDT'):
        add_feature_rows(session, coin, T0, horizons=('swing',), alt_residual_z_signal_bar=1.3)
    run(session, ('SOLUSDT', 'ETHUSDT', 'BTCUSDT'))  # deliberately alts first
    rows = {r.coin: r for r in candidates(session)}
    assert set(rows) == {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'}
    btc = rows['BTCUSDT']
    assert btc.sample_group_reason == 'new'
    for coin in ('ETHUSDT', 'SOLUSDT'):
        assert (rows[coin].sample_group_reason, rows[coin].sample_group_leader_id) == ('btc_impulse', btc.id)
        assert rows[coin].sample_group_id == btc.sample_group_id
    count = session.execute(select(func.count(func.distinct(Candidate.sample_group_id)))).scalar_one()
    assert count == 1


def test_an_alt_that_does_not_clear_the_residual_gate_gets_no_row(session):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',))
    add_feature_rows(session, 'ETHUSDT', T0, horizons=('swing',), alt_residual_z_signal_bar=0.4)
    add_feature_rows(session, 'SOLUSDT', T0, horizons=('swing',), alt_residual_z_signal_bar=None)
    report = run(session, ('BTCUSDT', 'ETHUSDT', 'SOLUSDT'))
    assert [r.coin for r in candidates(session)] == ['BTCUSDT']
    assert {o.coin: o.skipped for o in report.outcomes if o.skipped} == {'ETHUSDT': 'alt_residual_gate', 'SOLUSDT': 'alt_residual_gate'}


def test_an_alt_on_its_own_without_a_btc_candidate_is_a_new_sample(session):
    add_feature_rows(session, 'BTCUSDT', T0, horizons=('swing',), swing_break_event=False)
    add_feature_rows(session, 'ETHUSDT', T0, horizons=('swing',), alt_residual_z_signal_bar=2.0)
    run(session, ('BTCUSDT', 'ETHUSDT'))
    (eth,) = candidates(session)
    assert (eth.coin, eth.sample_group_reason, eth.sample_group_leader_id) == ('ETHUSDT', 'new', None)


# ---------------------------------------------------------------------------
# The locked book on stored rows
# ---------------------------------------------------------------------------
def test_locked_book_is_stored_from_the_hash_and_is_about_one_in_five(session):
    start = T0
    for bar in bars(150, start, timedelta(hours=24)):  # far apart: every bar gets its own candidate
        add_feature_rows(session, 'BTCUSDT', bar, horizons=('scalp',))
        run(session, bar=bar, horizons=('scalp',))
    rows = candidates(session)
    assert len(rows) == 150
    assert all(r.locked_book == is_locked_book(r.coin, r.bar_close) for r in rows)
    share = sum(r.locked_book for r in rows) / len(rows)
    assert 0.10 < share < 0.30


def test_locked_book_is_the_same_for_every_horizon_and_status_of_a_bar(session):
    for bar in bars(12, T0, timedelta(hours=100)):
        add_feature_rows(session, 'BTCUSDT', bar, event_flag=(bar.day % 2 == 0))
        run(session, bar=bar)
    by_bar: dict = {}
    for r in candidates(session):
        by_bar.setdefault(r.bar_close, set()).add(r.locked_book)
    assert all(len(flags) == 1 for flags in by_bar.values())
