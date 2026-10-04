"""The label job against Postgres: entry at the next open, write-once barriers, no lookahead, shadow rows, idempotency, hashes.

One BTC long on the ``scalp`` horizon (4h = 16 bars) unless a test says otherwise. Signal bar closes 2026-03-02 14:00, so the
entry is bar 0's open (100), the stop is 98 (1R = 2), the upper barrier 102 and the deadline 18:00. A bar is visible 30 s after
its close; settled funding prints are on the 00/08/16 UTC grid and visible 30 s after settlement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core import constants as C
from app.core.hashing import rule_hash as current_rule_hash
from app.core.labeler import barriers as B
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.feeds import Candle, Funding
from app.models.labels import Label
from app.worker import label_job as LJ
from tests.conftest import make_candidate

UTC = timezone.utc
D = Decimal
T0 = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
LATER = T0 + timedelta(days=3)
BAR = timedelta(minutes=15)
SETTLE = timedelta(seconds=30)
GAP = timedelta(hours=8, minutes=5)
VERSION = 'test123'
SCALP_BARS = 16
RULE_HASH = current_rule_hash()


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def add_bar(session, index, o, h, l, c, *, coin='BTCUSDT', available_at=None) -> Candle:
    opened = T0 + index * BAR
    row = Candle(
        coin=coin, market='perp', timeframe='15m', open_time=opened, close_time=opened + BAR,
        open_price=D(str(o)), high_price=D(str(h)), low_price=D(str(l)), close_price=D(str(c)),
        base_volume=D('10'), taker_buy_base_volume=D('5'),
        available_at=available_at if available_at is not None else opened + BAR + SETTLE,
    )
    session.add(row)
    session.flush()
    return row


def add_flat(session, first, last, price=100, *, coin='BTCUSDT') -> None:
    for index in range(first, last + 1):
        add_bar(session, index, price, price, price, price, coin=coin)


def add_funding_grid(session, *, coin='BTCUSDT', rates=None, skip=()) -> None:
    rates = rates or {}
    when = datetime(2026, 3, 1, 16, 0, tzinfo=UTC)
    while when <= datetime(2026, 3, 6, 0, 0, tzinfo=UTC):
        if when not in skip:
            session.add(Funding(
                coin=coin, kind=E.FUNDING_SETTLED, funding_time=when, funding_rate=D(str(rates.get(when, 0))),
                mark_price=D('100'), available_at=when + SETTLE,
            ))
        when += timedelta(hours=8)
    session.flush()


def cand(session, *, direction='long', stop=None, cap=None, horizon='scalp', status=E.STATUS_ALERT, reason=None,
         verdict='align', source=E.SOURCE_LIVE, coin='BTCUSDT', **overrides) -> Candidate:
    stop = stop if stop is not None else ('98' if direction == 'long' else '102')
    overrides.setdefault('rule_hash', RULE_HASH)
    return make_candidate(
        session, coin=coin, horizon=horizon, bar_close=T0, source=source, direction=direction, status=status, status_reason=reason,
        options_verdict=verdict, stop_price=D(stop), pre_options_stop_price=D(stop),
        target_cap_price=None if cap is None else D(str(cap)), vertical_deadline=None, **overrides,
    )


def run(session, as_of=LATER, **kwargs) -> LJ.LabelReport:
    report = LJ.run_label_job(session, as_of, code_version=VERSION, funding_max_gap=GAP, **kwargs)
    session.expire_all()
    return report


def labels(session) -> list[Label]:
    return list(session.execute(select(Label).order_by(Label.candidate_id)).scalars())


def only_label(session) -> Label:
    rows = labels(session)
    assert len(rows) == 1
    return rows[0]


def stop_scenario(session, **kwargs) -> Candidate:
    """Entry 100; bar 1 trades down through 98."""
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    add_bar(session, 1, 100, 100.5, 97.5, 98)
    add_funding_grid(session)
    return cand(session, **kwargs)


def timeout_scenario(session, *, last_close=100, **kwargs) -> Candidate:
    add_flat(session, 0, SCALP_BARS - 2)
    add_bar(session, SCALP_BARS - 1, 100, 100.5, 99.5, last_close)
    return cand(session, **kwargs)


# ---------------------------------------------------------------------------
# entry, barriers, deadline
# ---------------------------------------------------------------------------
def test_the_entry_is_the_next_bars_open_and_the_barriers_follow_from_it(session):
    c = stop_scenario(session)
    add_bar(session, 2, 98, 98, 98, 98)
    run(session)
    session.refresh(c)
    assert (c.entry_price, c.one_r, c.upper_barrier_price, c.lower_barrier_price) == (D('100'), D('2'), D('102'), D('98'))
    assert c.vertical_deadline == T0 + timedelta(hours=4)
    label = only_label(session)
    assert (label.fill_time, label.fill_price) == (T0, D('100'))


def test_a_short_is_framed_with_its_upper_barrier_below_the_entry(session):
    add_bar(session, 0, 100, 100.4, 97.5, 98)
    add_funding_grid(session)
    c = cand(session, direction='short')
    run(session)
    session.refresh(c)
    assert (c.upper_barrier_price, c.lower_barrier_price, c.one_r) == (D('98'), D('102'), D('2'))
    label = only_label(session)
    assert (label.first_barrier, label.price_r) == (E.BARRIER_UPPER, D('1'))


def test_the_deadline_is_the_fill_plus_the_horizon(session):
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    c = cand(session, horizon='swing')
    run(session)
    session.refresh(c)
    assert c.vertical_deadline == T0 + timedelta(hours=24)


def test_the_options_cap_is_the_upper_barrier_when_it_is_nearer_than_one_r(session):
    add_bar(session, 0, 100, 101.2, 99.8, 101)
    add_funding_grid(session)
    c = cand(session, cap='101', status=E.STATUS_CANCELLED, reason=C.STATUS_REASON_OPTIONS_HEADWIND_ROOM, verdict='headwind')
    run(session)
    session.refresh(c)
    assert c.upper_barrier_price == D('101')
    label = only_label(session)
    assert (label.first_barrier, label.price_r) == (E.BARRIER_UPPER, D('0.5'))


def test_the_stop_after_the_options_check_is_the_one_that_frames_the_trade(session):
    add_bar(session, 0, 100, 100.5, 98.6, 99)
    add_bar(session, 1, 99, 99.5, 98.4, 98.5)
    add_funding_grid(session)
    c = cand(session, stop='98.4')
    c.pre_options_stop_price = D('99')  # the rules' stop was tighter; the widened one wins
    session.flush()
    run(session)
    session.refresh(c)
    assert (c.lower_barrier_price, c.one_r, c.upper_barrier_price) == (D('98.4'), D('1.6'), D('101.6'))
    assert only_label(session).first_barrier == E.BARRIER_LOWER


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------
def test_first_hit_stop(session):
    stop_scenario(session)
    run(session)
    label = only_label(session)
    assert (label.first_barrier, label.price_r, label.barrier_ambiguous, label.gapped_stop) == (E.BARRIER_LOWER, D('-1'), False, False)
    assert label.resolved_at == T0 + 2 * BAR
    assert label.hours_to_resolution == D('0.5')
    assert (label.fees_r, label.funding_r) == (D('0.06'), D('0'))
    assert label.stored_r == D('-1.06')
    assert label.mae_r == D('1')


def test_first_hit_target(session):
    add_bar(session, 0, 100, 101, 99.5, 100.5)
    add_bar(session, 1, 100.5, 102.4, 100, 102)
    add_funding_grid(session)
    cand(session)
    run(session)
    label = only_label(session)
    assert (label.first_barrier, label.price_r, label.stored_r) == (E.BARRIER_UPPER, D('1'), D('0.94'))
    assert label.mfe_r == D('1.2')


def test_a_bar_touching_both_barriers_is_a_flagged_stop(session):
    add_bar(session, 0, 100, 102.5, 97.5, 100)
    add_funding_grid(session)
    cand(session)
    run(session)
    label = only_label(session)
    assert (label.first_barrier, label.price_r, label.barrier_ambiguous) == (E.BARRIER_LOWER, D('-1'), True)


def test_a_gapped_stop_fills_at_the_open(session):
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    add_bar(session, 1, 97, 97.5, 96, 96.5)
    add_funding_grid(session)
    cand(session)
    run(session)
    label = only_label(session)
    assert (label.first_barrier, label.price_r, label.gapped_stop, label.mae_r) == (E.BARRIER_LOWER, D('-1.5'), True, D('1.5'))


def test_the_vertical_barrier_scores_the_close_at_the_deadline(session):
    add_funding_grid(session)
    timeout_scenario(session, last_close=101)
    run(session)
    label = only_label(session)
    assert label.first_barrier == E.BARRIER_VERTICAL
    assert (label.price_r, label.resolved_at, label.hours_to_resolution) == (D('0.5'), T0 + timedelta(hours=4), D('4'))
    assert label.stored_r == D('0.5') - D('0.06') + label.funding_r


def test_realized_funding_inside_the_hold_is_charged_in_r(session):
    add_funding_grid(session, rates={datetime(2026, 3, 2, 16, 0, tzinfo=UTC): D('0.0001')})
    timeout_scenario(session)
    run(session)
    label = only_label(session)
    assert (label.funding_r, label.realized_funding_bps) == (D('-0.005'), D('-1'))
    assert label.stored_r == label.price_r - label.fees_r + label.funding_r == D('-0.065')


def test_a_short_receives_positive_funding(session):
    add_funding_grid(session, rates={datetime(2026, 3, 2, 16, 0, tzinfo=UTC): D('0.0001')})
    timeout_scenario(session, direction='short')
    run(session)
    assert only_label(session).funding_r == D('0.005')


def test_every_cost_assumption_is_recorded_on_the_label(session):
    stop_scenario(session)
    run(session)
    label = only_label(session)
    assert (label.cost_assumption_bps, label.taker_fee_bps_per_side, label.slippage_bps_per_side) == (D('12'), D('4'), D('2'))


def test_the_regime_slice_is_copied_from_the_decision_time_feature_row(session):
    stop_scenario(session, feature_overrides={'realized_vol_pct': D('0.4'), 'session': 'europe', 'event_flag': False, 'is_weekend': False})
    run(session)
    label = only_label(session)
    assert (label.regime_trend_family, label.regime_vol_pct, label.regime_session) == ('trend', D('0.4'), 'europe')
    assert (label.regime_event_flag, label.regime_is_weekend) == (False, False)


# ---------------------------------------------------------------------------
# which candidates are labeled
# ---------------------------------------------------------------------------
SHADOW_ROWS = [
    (E.STATUS_ALERT, None, 'align'),
    (E.STATUS_WATCH, C.STATUS_REASON_VOTE_CONFLICT, 'align'),
    (E.STATUS_WATCH, f'{C.STATUS_REASON_OPTIONS_IGNORED_PREFIX}{C.OPTIONS_IGNORE_OI_BELOW_FLOOR}', 'ignore'),
    (E.STATUS_CANCELLED, C.STATUS_REASON_CVD_DIVERGENCE, 'align'),
    (E.STATUS_CANCELLED, C.STATUS_REASON_STAND_DOWN_PREFIX + C.STAND_DOWN_EVENT_WINDOW, 'dampen'),
    (E.STATUS_DELAYED, C.STATUS_REASON_OPTIONS_EXPIRY_PIN, 'expiry_pin'),
    (E.STATUS_ABSTAINED, 'pool_cannot_clear_cost', 'align'),
]


@pytest.mark.parametrize('status,reason,verdict', SHADOW_ROWS)
@pytest.mark.parametrize('source', E.SOURCES)
def test_every_status_and_both_sources_are_labeled(session, status, reason, verdict, source):
    stop_scenario(session, status=status, reason=reason, verdict=verdict, source=source)
    report = run(session)
    assert report.labeled == 1
    assert only_label(session).first_barrier == E.BARRIER_LOWER


def test_a_candidate_the_options_check_has_not_seen_is_left_alone(session):
    c = stop_scenario(session, status=E.STATUS_WATCH, reason=C.STATUS_REASON_PENDING_OPTIONS_CHECK, verdict=None)
    report = run(session)
    session.refresh(c)
    assert report.outcomes == [] and labels(session) == []
    assert c.entry_price is None and c.vertical_deadline is None


def test_the_job_can_be_limited_to_one_source_or_some_coins(session):
    stop_scenario(session, source=E.SOURCE_LIVE)
    assert run(session, source=E.SOURCE_REPLAY).outcomes == []
    assert run(session, coins=['ETHUSDT']).outcomes == []
    assert run(session, source=E.SOURCE_LIVE, coins=['BTCUSDT']).labeled == 1


def test_each_candidate_is_resolved_on_its_own_coins_bars(session):
    stop_scenario(session)
    add_bar(session, 0, 50, 50.2, 49.9, 50, coin='ETHUSDT')
    add_bar(session, 1, 50, 51.2, 49.9, 51, coin='ETHUSDT')
    add_funding_grid(session, coin='ETHUSDT')
    eth = cand(session, coin='ETHUSDT', stop='49')
    run(session)
    by_id = {label.candidate_id: label for label in labels(session)}
    assert by_id[eth.id].first_barrier == E.BARRIER_UPPER
    assert len(by_id) == 2


# ---------------------------------------------------------------------------
# no lookahead
# ---------------------------------------------------------------------------
def test_a_bar_not_yet_available_is_not_used_even_though_it_exists(session):
    c = stop_scenario(session)
    stop_bar_available = T0 + 2 * BAR + SETTLE
    as_of_before = stop_bar_available - timedelta(seconds=1)
    report = run(session, as_of_before)
    assert labels(session) == []
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_PENDING, LJ.PENDING_AWAITING_BARS)]
    session.refresh(c)
    assert c.entry_price == D('100')  # the fill is already known, so it is frozen
    assert run(session, stop_bar_available).labeled == 1


def test_a_late_ingested_bar_is_ignored_until_its_available_at(session):
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    add_bar(session, 1, 100, 100.5, 97.5, 98, available_at=T0 + timedelta(hours=3))
    add_funding_grid(session)
    cand(session)
    assert run(session, T0 + timedelta(hours=2)).labeled == 0
    assert run(session, T0 + timedelta(hours=3)).labeled == 1


def test_bars_after_the_as_of_instant_do_not_change_an_earlier_resolution(session):
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    add_bar(session, 1, 100, 102.5, 99.8, 102)
    add_bar(session, 2, 102, 103, 90, 91)  # a crash after the target
    add_funding_grid(session)
    cand(session)
    run(session, T0 + 2 * BAR + SETTLE)
    label = only_label(session)
    assert (label.first_barrier, label.price_r, label.resolved_at) == (E.BARRIER_UPPER, D('1'), T0 + 2 * BAR)


def test_without_the_entry_bar_nothing_is_written(session):
    c = cand(session)
    add_bar(session, 0, 100, 100.5, 99.5, 100, available_at=T0 + timedelta(hours=1))
    add_funding_grid(session)
    report = run(session, T0 + BAR)
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_PENDING, LJ.PENDING_ENTRY_BAR)]
    session.refresh(c)
    assert (c.entry_price, c.one_r, c.upper_barrier_price, c.lower_barrier_price, c.vertical_deadline) == (None,) * 5


def test_a_candidate_whose_entry_bar_has_not_closed_is_not_even_considered(session):
    stop_scenario(session)
    assert run(session, T0 + timedelta(minutes=10)).outcomes == []


def test_a_hole_in_the_bars_holds_the_label_until_it_is_filled(session):
    add_funding_grid(session)
    add_flat(session, 0, 2)
    add_flat(session, 4, SCALP_BARS - 1)
    cand(session)
    report = run(session)
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_PENDING, LJ.PENDING_MISSING_BAR)]
    add_flat(session, 3, 3)
    assert run(session).labeled == 1
    assert only_label(session).first_barrier == E.BARRIER_VERTICAL


def test_a_hit_before_a_hole_is_labeled_without_waiting_for_the_hole(session):
    add_funding_grid(session)
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    add_bar(session, 1, 100, 100.5, 97.5, 98)
    add_flat(session, 5, 9)
    cand(session)
    assert run(session).labeled == 1


def test_the_label_waits_for_settled_funding_it_can_prove_complete(session):
    stop_scenario(session)
    session.execute(Funding.__table__.delete())
    session.flush()
    soon = T0 + timedelta(hours=1)
    report = run(session, soon)
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_PENDING, LJ.PENDING_FUNDING)]
    add_funding_grid(session)
    assert run(session, soon).labeled == 1


def test_a_missing_settlement_inside_the_hold_keeps_the_label_pending(session):
    add_funding_grid(session, skip={datetime(2026, 3, 2, 16, 0, tzinfo=UTC)})
    timeout_scenario(session)
    report = run(session, T0 + timedelta(hours=4, minutes=1))
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_PENDING, LJ.PENDING_FUNDING)]


def test_funding_that_settles_after_the_as_of_instant_is_not_counted(session):
    add_funding_grid(session, rates={datetime(2026, 3, 2, 16, 0, tzinfo=UTC): D('0.0001')})
    timeout_scenario(session)
    assert run(session, T0 + timedelta(hours=4, minutes=1)).labeled == 1
    assert only_label(session).funding_r == D('-0.005')


# ---------------------------------------------------------------------------
# a fill that cannot carry barriers: no entry
# ---------------------------------------------------------------------------
NO_R_COLUMNS = ('price_r', 'fees_r', 'funding_r', 'stored_r', 'mae_r', 'mfe_r', 'hours_to_mfe', 'hours_to_resolution', 'realized_funding_bps')


def assert_no_entry(label: Label, c: Candidate, observed_open) -> None:
    assert label.first_barrier == E.BARRIER_NO_ENTRY
    assert (label.fill_time, label.fill_price, label.resolved_at) == (T0, D(str(observed_open)), T0)
    for name in NO_R_COLUMNS:
        assert getattr(label, name) is None, name  # NULL, not 0 and not -1
    assert (label.barrier_ambiguous, label.gapped_stop, label.funding_partial) == (False, False, False)
    assert label.feature_hash == c.feature_hash and label.rule_hash == c.rule_hash == RULE_HASH and label.code_version == VERSION
    assert label.cost_assumption_bps == D('12')
    assert (c.entry_price, c.one_r, c.upper_barrier_price, c.lower_barrier_price) == (D(str(observed_open)), None, None, None)
    assert c.vertical_deadline == T0 + timedelta(hours=4)


def test_an_entry_that_opens_through_the_stop_is_a_no_entry_label_with_no_r(session):
    add_bar(session, 0, 97, 97.5, 96.5, 97)
    c = cand(session)
    report = run(session)
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_NO_ENTRY, B.REASON_ENTRY_THROUGH_STOP)]
    assert (report.labeled, report.no_entry) == (0, 1)
    session.refresh(c)
    assert_no_entry(only_label(session), c, 97)


def test_an_entry_exactly_on_the_stop_is_a_no_entry(session):
    add_bar(session, 0, 98, 98.5, 97.5, 98)
    c = cand(session)
    run(session)
    session.refresh(c)
    assert_no_entry(only_label(session), c, 98)


def test_an_entry_that_opens_through_the_cap_is_a_no_entry_label(session):
    add_bar(session, 0, 101.5, 102, 101, 101.8)
    c = cand(session, stop='99', cap='101', status=E.STATUS_CANCELLED, reason=C.STATUS_REASON_OPTIONS_HEADWIND_ROOM, verdict='headwind')
    report = run(session)
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_NO_ENTRY, B.REASON_ENTRY_THROUGH_CAP)]
    session.refresh(c)
    assert_no_entry(only_label(session), c, 101.5)


def test_a_short_that_opens_through_its_stop_is_a_no_entry(session):
    add_bar(session, 0, 103, 103.5, 102.5, 103)
    c = cand(session, direction='short')
    run(session)
    session.refresh(c)
    assert_no_entry(only_label(session), c, 103)


def test_a_no_entry_needs_no_funding_and_no_later_bars(session):
    add_bar(session, 0, 97, 97.5, 96.5, 97)
    cand(session)
    assert run(session, T0 + BAR + SETTLE).no_entry == 1


def test_a_no_entry_is_written_once_and_not_revisited(session):
    add_bar(session, 0, 97, 97.5, 96.5, 97)
    c = cand(session)
    run(session)
    before = {k: getattr(only_label(session), k) for k in Label.__table__.columns.keys()}
    assert run(session).outcomes == []
    assert {k: getattr(only_label(session), k) for k in Label.__table__.columns.keys()} == before
    for column, value in (('one_r', D('2')), ('upper_barrier_price', D('96')), ('lower_barrier_price', D('98')), ('entry_price', D('96'))):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(update(Candidate).where(Candidate.id == c.id).values({column: value}))


def test_a_stored_no_entry_fill_without_a_label_is_finished_from_the_stored_values(session):
    add_bar(session, 0, 97, 97.5, 96.5, 97)
    c = cand(session)
    run(session, dry_run=False)
    session.execute(Label.__table__.delete())
    session.execute(update(Candle).where(Candle.open_time == T0).values(open_price=D('100')))  # the bar is "corrected"
    session.flush()
    assert run(session).no_entry == 1
    session.refresh(c)
    assert_no_entry(only_label(session), c, 97)


def test_a_no_entry_dry_run_writes_nothing(session):
    add_bar(session, 0, 97, 97.5, 96.5, 97)
    c = cand(session)
    report = run(session, dry_run=True)
    assert [(o.result, o.first_barrier) for o in report.outcomes] == [(LJ.RESULT_DRY, E.BARRIER_NO_ENTRY)]
    session.refresh(c)
    assert c.entry_price is None and labels(session) == []


def test_the_database_only_allows_no_r_on_a_no_entry_label(session):
    c = stop_scenario(session)
    run(session)
    only = only_label(session)
    for stmt in (
        update(Label).values(first_barrier=E.BARRIER_NO_ENTRY),  # keeps its R
        update(Label).values(price_r=None, stored_r=None),  # a filled label with a hole
    ):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(stmt.where(Label.candidate_id == only.candidate_id))
    assert c.id == only.candidate_id


# ---------------------------------------------------------------------------
# write-once and idempotency
# ---------------------------------------------------------------------------
def test_a_second_run_changes_nothing_and_finds_nothing_to_do(session):
    c = stop_scenario(session)
    run(session)
    session.refresh(c)
    before_candidate = (c.entry_price, c.one_r, c.upper_barrier_price, c.lower_barrier_price, c.vertical_deadline)
    before_label = {k: getattr(only_label(session), k) for k in Label.__table__.columns.keys()}
    report = run(session)
    assert report.outcomes == []
    session.refresh(c)
    assert (c.entry_price, c.one_r, c.upper_barrier_price, c.lower_barrier_price, c.vertical_deadline) == before_candidate
    assert {k: getattr(only_label(session), k) for k in Label.__table__.columns.keys()} == before_label


def test_the_frozen_fill_survives_a_corrected_bar_and_a_rerun(session):
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    add_funding_grid(session)
    c = cand(session)
    assert run(session, T0 + BAR + SETTLE).labeled == 0  # frozen, still open
    session.execute(update(Candle).where(Candle.open_time == T0).values(open_price=D('105')))
    add_bar(session, 1, 100, 100.5, 97.5, 98)
    run(session)
    session.refresh(c)
    assert (c.entry_price, c.one_r) == (D('100'), D('2'))
    label = only_label(session)
    assert (label.fill_price, label.price_r) == (D('100'), D('-1'))


def test_a_label_is_only_inserted_once_even_if_the_job_races(session):
    c = stop_scenario(session)
    run(session)
    outcome = LJ.label_candidate(session, c, LATER, code_version=VERSION, funding_max_gap=GAP)
    assert outcome.result == LJ.RESULT_RACE
    assert len(labels(session)) == 1


def test_dry_run_writes_neither_the_fill_nor_the_label(session):
    c = stop_scenario(session)
    report = run(session, dry_run=True)
    assert [o.result for o in report.outcomes] == [LJ.RESULT_DRY]
    assert report.outcomes[0].first_barrier == E.BARRIER_LOWER
    session.refresh(c)
    assert c.entry_price is None and labels(session) == []


def test_the_database_refuses_to_change_a_frozen_fill_value(session):
    c = stop_scenario(session)
    run(session)
    for column, value in (
        ('entry_price', D('101')),
        ('one_r', D('3')),
        ('upper_barrier_price', D('103')),
        ('vertical_deadline', T0 + timedelta(hours=5)),
        ('entry_price', None),
        ('vertical_deadline', None),
    ):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(update(Candidate).where(Candidate.id == c.id).values({column: value}))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(update(Candidate).where(Candidate.id == c.id).values(stop_price=D('97'), lower_barrier_price=D('97')))
    session.refresh(c)
    assert (c.entry_price, c.one_r, c.upper_barrier_price, c.lower_barrier_price) == (D('100'), D('2'), D('102'), D('98'))


def test_rewriting_a_frozen_value_with_the_same_value_is_harmless(session):
    c = stop_scenario(session)
    run(session)
    session.execute(update(Candidate).where(Candidate.id == c.id).values(entry_price=D('100'), status_reason='noted'))
    session.refresh(c)
    assert c.entry_price == D('100')


def test_the_fill_values_can_only_be_written_together(session):
    c = cand(session)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(update(Candidate).where(Candidate.id == c.id).values(entry_price=D('100')))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(Candidate).where(Candidate.id == c.id).values(entry_price=D('100'), one_r=D('2'), upper_barrier_price=D('102'), lower_barrier_price=D('98'))
        )
    with pytest.raises(IntegrityError), session.begin_nested():  # an entry without its deadline
        session.execute(update(Candidate).where(Candidate.id == c.id).values(entry_price=D('97'), vertical_deadline=None))
    with pytest.raises(IntegrityError), session.begin_nested():  # barriers without 1R
        session.execute(
            update(Candidate).where(Candidate.id == c.id).values(
                entry_price=D('97'), upper_barrier_price=D('99'), lower_barrier_price=D('98'), vertical_deadline=T0 + timedelta(hours=4)
            )
        )
    session.execute(  # a no-entry fill: the observed open and the deadline, nothing else
        update(Candidate).where(Candidate.id == c.id).values(entry_price=D('97'), vertical_deadline=T0 + timedelta(hours=4))
    )
    session.refresh(c)
    assert c.entry_price == D('97') and c.one_r is None


def test_the_five_fill_values_are_accepted_together(session):
    c = cand(session)
    session.execute(
        update(Candidate).where(Candidate.id == c.id).values(
            entry_price=D('100'), one_r=D('2'), upper_barrier_price=D('102'), lower_barrier_price=D('98'), vertical_deadline=T0 + timedelta(hours=4)
        )
    )


# ---------------------------------------------------------------------------
# the three hashes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('status,reason,verdict', SHADOW_ROWS)
def test_every_label_carries_the_candidates_hashes_and_the_resolver_version(session, status, reason, verdict):
    c = stop_scenario(session, status=status, reason=reason, verdict=verdict)
    run(session)
    label = only_label(session)
    assert label.feature_hash == c.feature_hash and len(label.feature_hash) == 64
    assert label.rule_hash == c.rule_hash == RULE_HASH
    assert label.code_version == VERSION


def test_a_label_without_a_hash_or_code_version_cannot_be_stored(session):
    c = stop_scenario(session)
    run(session)
    for missing in ('feature_hash', 'rule_hash', 'code_version'):
        for label in labels(session):
            session.delete(label)
        session.flush()
        row = {
            'candidate_id': c.id, 'fill_time': T0, 'fill_price': D('100'), 'resolved_at': T0 + BAR, 'first_barrier': 'lower',
            'hours_to_resolution': D('0.25'), 'price_r': D('-1'), 'fees_r': D('0.06'), 'funding_r': D('0'), 'stored_r': D('-1.06'),
            'mae_r': D('1'), 'cost_assumption_bps': D('12'), 'feature_hash': 'f' * 64, 'rule_hash': RULE_HASH, 'code_version': 'x',
        }
        row[missing] = None
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(Label(**row))
            session.flush()


def test_the_default_code_version_comes_from_the_repo(session):
    stop_scenario(session)
    LJ.run_label_job(session, LATER, funding_max_gap=GAP)
    session.expire_all()
    assert only_label(session).code_version


def test_the_live_tick_labels_live_candidates_and_reports_counts(session):
    stop_scenario(session)
    summary = LJ.run_live_label_job(session, now=LATER)
    assert (summary['labeled'], summary['pending'], summary['errors']) == (1, 0, 0)


# ---------------------------------------------------------------------------
# the hashed labeler semantics: stale rule hashes
# ---------------------------------------------------------------------------
def test_a_candidate_from_another_rule_hash_is_skipped_and_counted(session):
    stale = stop_scenario(session, rule_hash='s' * 64)
    report = run(session)
    assert (report.outcomes, report.stale_rule_hash, report.rule_hash) == ([], 1, RULE_HASH)
    session.refresh(stale)
    assert labels(session) == [] and stale.entry_price is None


def test_only_the_current_hash_is_labeled_beside_a_stale_one(session):
    add_bar(session, 0, 100, 100.5, 99.5, 100)
    add_bar(session, 1, 100, 100.5, 97.5, 98)
    add_funding_grid(session)
    cand(session, coin='BTCUSDT')
    cand(session, coin='ETHUSDT', rule_hash='s' * 64)
    for index in (0, 1):
        add_bar(session, index, 100, 100.5 if index == 0 else 100.5, 99.5 if index == 0 else 97.5, 100 if index == 0 else 98, coin='ETHUSDT')
    add_funding_grid(session, coin='ETHUSDT')
    report = run(session)
    assert (report.labeled, report.stale_rule_hash) == (1, 1)
    assert [row.rule_hash for row in labels(session)] == [RULE_HASH]


def test_the_rule_hash_to_match_can_be_given(session):
    stop_scenario(session, rule_hash='s' * 64)
    report = run(session, rule_hash='s' * 64)
    assert (report.labeled, report.stale_rule_hash) == (1, 0)
    assert only_label(session).rule_hash == 's' * 64


def test_a_stale_candidate_that_is_already_labeled_is_not_counted(session):
    stop_scenario(session, rule_hash='s' * 64)
    run(session, rule_hash='s' * 64)
    assert run(session).stale_rule_hash == 0


def test_the_summary_carries_the_rule_hash_and_the_stale_count(session):
    stop_scenario(session, rule_hash='s' * 64)
    summary = LJ.run_live_label_job(session, now=LATER)
    assert summary['stale_rule_hash'] == 1 and summary['rule_hash'] == RULE_HASH
    assert (summary['labeled'], summary['no_entry'], summary['funding_partial'], summary['pending'], summary['errors']) == (0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# the funding stall cap
# ---------------------------------------------------------------------------
EXIT_VISIBLE = T0 + timedelta(hours=4) + SETTLE
STALL = timedelta(hours=C.LABEL_FUNDING_STALL_CAP_HOURS)


def stalled_funding_scenario(session, **kwargs) -> Candidate:
    """A scalp timeout whose 16:00 print is visible but whose 08:00 print never arrived: the chain can't be proven complete."""
    add_funding_grid(session, rates={datetime(2026, 3, 2, 16, 0, tzinfo=UTC): D('0.0001')}, skip={datetime(2026, 3, 2, 8, 0, tzinfo=UTC)})
    return timeout_scenario(session, **kwargs)


def test_a_label_missing_funding_stays_pending_until_the_stall_cap(session):
    stalled_funding_scenario(session)
    report = run(session, EXIT_VISIBLE + STALL - timedelta(seconds=1))
    assert [(o.result, o.detail) for o in report.outcomes] == [(LJ.RESULT_PENDING, LJ.PENDING_FUNDING)]
    assert labels(session) == []


def test_a_label_still_missing_funding_after_the_cap_is_written_partial(session):
    c = stalled_funding_scenario(session)
    report = run(session, EXIT_VISIBLE + STALL)
    assert (report.labeled, LJ.summarize(report)['funding_partial']) == (1, 1)
    label = only_label(session)
    assert label.funding_partial is True
    assert (label.first_barrier, label.funding_r) == (E.BARRIER_VERTICAL, D('-0.005'))  # the funding seen so far
    assert label.stored_r == label.price_r - label.fees_r + label.funding_r
    assert label.rule_hash == c.rule_hash and label.resolved_at == T0 + timedelta(hours=4)


def test_a_complete_label_is_not_marked_partial(session):
    timeout_scenario(session)
    add_funding_grid(session)
    run(session)
    assert only_label(session).funding_partial is False


def test_a_partial_label_is_written_once(session):
    stalled_funding_scenario(session)
    run(session, EXIT_VISIBLE + STALL)
    before = {k: getattr(only_label(session), k) for k in Label.__table__.columns.keys()}
    late = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # the missing print finally arrives
    session.add(Funding(coin='BTCUSDT', kind=E.FUNDING_SETTLED, funding_time=late, funding_rate=D('0.01'), mark_price=D('100'), available_at=late + SETTLE))
    session.flush()
    assert run(session, EXIT_VISIBLE + STALL + timedelta(days=1)).outcomes == []
    assert {k: getattr(only_label(session), k) for k in Label.__table__.columns.keys()} == before


def test_the_stall_clock_starts_when_the_exit_became_visible_not_at_the_exit(session):
    add_flat(session, 0, SCALP_BARS - 2)
    add_bar(session, SCALP_BARS - 1, 100, 100.5, 99.5, 100, available_at=T0 + timedelta(hours=4, minutes=10))
    add_funding_grid(session, skip={datetime(2026, 3, 2, 8, 0, tzinfo=UTC)})
    cand(session)
    assert run(session, T0 + timedelta(hours=4) + SETTLE + STALL).labeled == 0
    assert run(session, T0 + timedelta(hours=4, minutes=10) + STALL).labeled == 1