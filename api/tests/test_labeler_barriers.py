"""The pure labeler: barrier prices, first hit, same-bar ambiguity, gaps, timeout, costs and funding.

One long throughout unless a test says otherwise: entry 100, stop 98, so 1R = 2, upper barrier 102, lower barrier 98.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import constants as C
from app.core.labeler import barriers as B
from app.models import enums as E

UTC = timezone.utc
D = Decimal
FILL = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
LONG, SHORT = E.DIRECTION_LONG, E.DIRECTION_SHORT


def plan(direction=LONG, entry='100', stop='98', cap=None, horizon='scalp') -> B.Barriers:
    return B.plan_barriers(direction=direction, entry=D(entry), stop=D(stop), cap=None if cap is None else D(cap), fill_time=FILL, horizon=horizon)


def bar(index: int, o, h, l, c) -> B.Bar:
    opened = FILL + index * B.BAR_LENGTH
    return B.Bar(opened, opened + B.BAR_LENGTH, D(str(o)), D(str(h)), D(str(l)), D(str(c)))


def flat(count: int, price=100, start=0) -> list[B.Bar]:
    return [bar(start + i, price, price, price, price) for i in range(count)]


SCALP_BARS = 16  # 4h of 15m bars


# ---------------------------------------------------------------------------
# barrier prices
# ---------------------------------------------------------------------------
def test_a_long_gets_one_r_an_upper_barrier_above_and_the_stop_below():
    b = plan()
    assert (b.entry, b.one_r, b.upper, b.lower) == (D('100'), D('2'), D('102'), D('98'))


def test_a_short_gets_its_upper_barrier_below_the_entry():
    b = plan(SHORT, stop='102')
    assert (b.entry, b.one_r, b.upper, b.lower) == (D('100'), D('2'), D('98'), D('102'))


def test_the_deadline_is_the_fill_plus_the_horizon_not_the_signal_close():
    assert plan(horizon='scalp').deadline == FILL + timedelta(hours=4)
    assert plan(horizon='swing').deadline == FILL + timedelta(hours=24)
    assert plan(horizon='position').deadline == FILL + timedelta(hours=72)


def test_a_cap_nearer_than_one_r_becomes_the_upper_barrier():
    assert plan(cap='101').upper == D('101')
    assert plan(SHORT, stop='102', cap='99').upper == D('99')


def test_a_cap_beyond_one_r_changes_nothing():
    assert plan(cap='105').upper == D('102')
    assert plan(SHORT, stop='102', cap='95').upper == D('98')


@pytest.mark.parametrize('direction,entry,stop', [(LONG, '98', '98'), (LONG, '97', '98'), (SHORT, '102', '102'), (SHORT, '103', '102')])
def test_a_fill_at_or_through_the_stop_has_no_barriers(direction, entry, stop):
    with pytest.raises(B.BarrierPlanError) as caught:
        plan(direction, entry=entry, stop=stop)
    assert caught.value.reason == B.REASON_ENTRY_THROUGH_STOP


@pytest.mark.parametrize('direction,stop,cap', [(LONG, '98', '100'), (LONG, '98', '99'), (SHORT, '102', '100'), (SHORT, '102', '101')])
def test_a_fill_at_or_through_the_cap_has_no_barriers(direction, stop, cap):
    with pytest.raises(B.BarrierPlanError) as caught:
        plan(direction, stop=stop, cap=cap)
    assert caught.value.reason == B.REASON_ENTRY_THROUGH_CAP


# ---------------------------------------------------------------------------
# first hit
# ---------------------------------------------------------------------------
def test_the_upper_barrier_trading_first_wins_a_full_r():
    bars = [bar(0, 100, 101, 99.5, 100.5), bar(1, 100.5, 102.5, 100, 102)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert (r.first_barrier, r.exit_price, r.price_r) == (E.BARRIER_UPPER, D('102'), D('1'))
    assert r.resolved_at == FILL + 2 * B.BAR_LENGTH
    assert not r.ambiguous and not r.gapped_stop


def test_the_stop_trading_first_loses_exactly_one_r():
    bars = [bar(0, 100, 101, 99.5, 100.5), bar(1, 100.5, 101, 97.5, 98)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert (r.first_barrier, r.exit_price, r.price_r) == (E.BARRIER_LOWER, D('98'), D('-1'))
    assert not r.ambiguous and not r.gapped_stop


def test_a_short_wins_on_a_fall_and_loses_on_a_rise():
    won = B.resolve(SHORT, plan(SHORT, stop='102'), [bar(0, 100, 100.5, 97.5, 98)], FILL)
    lost = B.resolve(SHORT, plan(SHORT, stop='102'), [bar(0, 100, 102.5, 99.5, 102)], FILL)
    assert (won.first_barrier, won.price_r) == (E.BARRIER_UPPER, D('1'))
    assert (lost.first_barrier, lost.price_r) == (E.BARRIER_LOWER, D('-1'))


def test_a_wall_cap_pays_less_than_one_r():
    r = B.resolve(LONG, plan(cap='101'), [bar(0, 100, 101.2, 99.8, 101)], FILL)
    assert (r.first_barrier, r.exit_price, r.price_r) == (E.BARRIER_UPPER, D('101'), D('0.5'))


def test_the_entry_bar_itself_can_hit_a_barrier():
    r = B.resolve(LONG, plan(), [bar(0, 100, 100.4, 97.9, 98.5)], FILL)
    assert (r.first_barrier, r.resolved_at) == (E.BARRIER_LOWER, FILL + B.BAR_LENGTH)


# ---------------------------------------------------------------------------
# same-bar ambiguity
# ---------------------------------------------------------------------------
def test_a_bar_touching_both_barriers_resolves_to_the_stop_and_is_flagged():
    r = B.resolve(LONG, plan(), [bar(0, 100, 102.5, 97.5, 100)], FILL)
    assert (r.first_barrier, r.price_r, r.ambiguous) == (E.BARRIER_LOWER, D('-1'), True)


def test_the_ambiguity_rule_is_the_same_for_a_short():
    r = B.resolve(SHORT, plan(SHORT, stop='102'), [bar(0, 100, 102.5, 97.5, 100)], FILL)
    assert (r.first_barrier, r.price_r, r.ambiguous) == (E.BARRIER_LOWER, D('-1'), True)


def test_a_bar_touching_only_one_barrier_is_not_ambiguous():
    assert not B.resolve(LONG, plan(), [bar(0, 100, 102.5, 99, 102)], FILL).ambiguous
    assert not B.resolve(LONG, plan(), [bar(0, 100, 101, 97.5, 98)], FILL).ambiguous


def test_the_ambiguity_default_is_the_conservative_one():
    assert C.LABEL_AMBIGUOUS_BAR_RESOLVES_TO == 'stop'
    assert B.ambiguous_resolves_to() == E.BARRIER_LOWER


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------
def test_a_stop_that_gaps_fills_at_the_open_not_the_stop():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 97, 97.5, 96, 96.5)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert (r.first_barrier, r.exit_price, r.price_r, r.gapped_stop) == (E.BARRIER_LOWER, D('97'), D('-1.5'), True)
    assert r.mae_r == D('1.5')


def test_a_short_stop_that_gaps_fills_at_the_worse_open():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 103, 104, 102.5, 103)]
    r = B.resolve(SHORT, plan(SHORT, stop='102'), bars, FILL)
    assert (r.exit_price, r.price_r, r.gapped_stop) == (D('103'), D('-1.5'), True)


def test_a_bar_that_opens_exactly_on_the_stop_fills_at_the_stop():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 98, 99, 97.5, 98.5)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert (r.exit_price, r.price_r, r.gapped_stop) == (D('98'), D('-1'), True)


def test_a_target_that_gaps_is_filled_at_the_barrier_not_the_better_open():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 103, 104, 102.5, 103.5)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert (r.first_barrier, r.exit_price, r.price_r, r.gapped_stop) == (E.BARRIER_UPPER, D('102'), D('1'), False)


# ---------------------------------------------------------------------------
# vertical barrier
# ---------------------------------------------------------------------------
def test_no_barrier_by_the_deadline_exits_at_the_last_close_and_is_vertical():
    bars = flat(SCALP_BARS - 1) + [bar(SCALP_BARS - 1, 100, 101, 99, 101)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert r.first_barrier == E.BARRIER_VERTICAL
    assert (r.exit_price, r.price_r, r.resolved_at) == (D('101'), D('0.5'), FILL + timedelta(hours=4))


def test_a_timeout_under_water_is_a_negative_r_not_a_stop():
    bars = flat(SCALP_BARS - 1) + [bar(SCALP_BARS - 1, 100, 100.5, 98.5, 99)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert (r.first_barrier, r.price_r) == (E.BARRIER_VERTICAL, D('-0.5'))


def test_a_short_timeout_is_scored_in_its_own_direction():
    bars = flat(SCALP_BARS - 1) + [bar(SCALP_BARS - 1, 100, 100.5, 99, 99)]
    r = B.resolve(SHORT, plan(SHORT, stop='102'), bars, FILL)
    assert (r.first_barrier, r.price_r) == (E.BARRIER_VERTICAL, D('0.5'))


def test_a_barrier_on_the_last_bar_beats_the_timeout():
    bars = flat(SCALP_BARS - 1) + [bar(SCALP_BARS - 1, 100, 102.5, 99.5, 102)]
    assert B.resolve(LONG, plan(), bars, FILL).first_barrier == E.BARRIER_UPPER


def test_bars_at_or_after_the_deadline_are_ignored():
    bars = flat(SCALP_BARS) + [bar(SCALP_BARS, 100, 110, 90, 100)]
    assert B.resolve(LONG, plan(), bars, FILL).first_barrier == E.BARRIER_VERTICAL


# ---------------------------------------------------------------------------
# pending
# ---------------------------------------------------------------------------
def test_no_bars_is_pending_at_the_fill():
    assert B.resolve(LONG, plan(), [], FILL) == B.Pending(FILL)


def test_bars_that_stop_short_of_the_deadline_are_pending_at_the_next_open():
    result = B.resolve(LONG, plan(), flat(5), FILL)
    assert result == B.Pending(FILL + 5 * B.BAR_LENGTH)


def test_a_hole_with_no_earlier_hit_leaves_the_label_pending_at_the_hole():
    bars = flat(3) + flat(SCALP_BARS - 4, start=4)
    assert B.resolve(LONG, plan(), bars, FILL) == B.Pending(FILL + 3 * B.BAR_LENGTH)


def test_a_hit_before_a_hole_stands():
    bars = [bar(0, 100, 100.5, 99.5, 100), bar(1, 100, 102.5, 99.5, 102)] + flat(4, start=3)
    assert B.resolve(LONG, plan(), bars, FILL).first_barrier == E.BARRIER_UPPER


def test_a_missing_entry_bar_is_pending_even_with_later_bars():
    assert B.resolve(LONG, plan(), flat(5, start=1), FILL) == B.Pending(FILL)


# ---------------------------------------------------------------------------
# excursions
# ---------------------------------------------------------------------------
def test_mae_and_mfe_run_to_the_resolving_bar_in_r():
    bars = [bar(0, 100, 101, 99, 100.5), bar(1, 100.5, 101.6, 99.6, 101), bar(2, 101, 102.5, 100.8, 102.2)]
    r = B.resolve(LONG, plan(), bars, FILL)
    assert r.first_barrier == E.BARRIER_UPPER
    assert r.mae_r == D('0.5')  # low 99 against entry 100, over a 2-point R
    assert r.mfe_r == D('1.25')  # high 102.5
    assert r.mfe_at == FILL + 3 * B.BAR_LENGTH


def test_the_stop_bar_extends_mae_but_not_mfe():
    bars = [bar(0, 100, 101, 99.5, 100.5), bar(1, 100.5, 103, 97, 98)]  # ambiguous: resolves to the stop
    r = B.resolve(LONG, plan(), bars, FILL)
    assert r.first_barrier == E.BARRIER_LOWER
    assert r.mae_r == D('1')
    assert r.mfe_r == D('0.5')
    assert r.mfe_at == FILL + B.BAR_LENGTH


def test_an_untouched_favorable_side_reports_zero_mfe_and_no_time():
    r = B.resolve(LONG, plan(), [bar(0, 100, 100, 97.5, 98)], FILL)
    assert (r.mfe_r, r.mfe_at) == (D('0'), None)


def test_a_short_measures_excursions_downward():
    bars = [bar(0, 100, 100.8, 99, 99.5), bar(1, 99.5, 99.8, 97.5, 98)]
    r = B.resolve(SHORT, plan(SHORT, stop='102'), bars, FILL)
    assert r.first_barrier == E.BARRIER_UPPER
    assert r.mae_r == D('0.4')
    assert r.mfe_r == D('1.25')


# ---------------------------------------------------------------------------
# costs and stored R
# ---------------------------------------------------------------------------
def test_fees_are_the_frozen_round_trip_in_r_on_the_entry_price():
    assert C.ROUND_TRIP_COST_BPS == D('12')
    assert B.fees_r(plan()) == D('0.060000')  # 100 * 12bp = 0.12 price, over a 2-point R


def test_stored_r_is_price_minus_fees_plus_funding():
    assert B.stored_r(D('1'), D('0.06'), D('-0.01')) == D('0.93')


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------
GAP = timedelta(hours=8, minutes=5)


def at(hours: float) -> datetime:
    return FILL + timedelta(hours=hours)


def test_a_long_pays_a_positive_rate_and_a_short_receives_it():
    prints = [B.FundingPrint(at(2), D('0.0001'), D('100'))]
    long_r, long_bps = B.realized_funding(LONG, prints, plan(), FILL, at(4))
    short_r, short_bps = B.realized_funding(SHORT, prints, plan(SHORT, stop='102'), FILL, at(4))
    assert (long_r, long_bps) == (D('-0.005000'), D('-1.000000'))  # 100 * 0.0001 = 0.01 price, over a 2-point R
    assert (short_r, short_bps) == (D('0.005000'), D('1.000000'))


def test_a_negative_rate_is_received_by_a_long():
    r, _ = B.realized_funding(LONG, [B.FundingPrint(at(1), D('-0.0002'), D('100'))], plan(), FILL, at(4))
    assert r == D('0.010000')


def test_the_entry_price_is_the_notional_when_the_print_has_no_mark():
    r, _ = B.realized_funding(LONG, [B.FundingPrint(at(1), D('0.0001'))], plan(), FILL, at(4))
    assert r == D('-0.005000')


def test_only_prints_strictly_inside_the_hold_count():
    prints = [B.FundingPrint(FILL, D('0.001')), B.FundingPrint(at(4), D('0.001')), B.FundingPrint(at(-8), D('0.001')), B.FundingPrint(at(8), D('0.001'))]
    assert B.realized_funding(LONG, prints, plan(), FILL, at(4)) == (D('0'), D('0'))


def test_several_prints_add_up():
    prints = [B.FundingPrint(at(1), D('0.0001'), D('100')), B.FundingPrint(at(2), D('0.0003'), D('100'))]
    r, bps = B.realized_funding(LONG, prints, plan(), FILL, at(4))
    assert (r, bps) == (D('-0.020000'), D('-4.000000'))


def test_the_window_is_complete_with_a_print_before_the_fill_and_one_inside_the_gap():
    assert B.funding_window_complete([at(-3), at(5)], FILL, at(7), GAP)


def test_a_window_with_no_print_at_or_before_the_fill_is_incomplete():
    assert not B.funding_window_complete([at(2)], FILL, at(4), GAP)
    assert not B.funding_window_complete([], FILL, at(4), GAP)


def test_a_missing_settlement_inside_the_hold_makes_the_window_incomplete():
    assert not B.funding_window_complete([at(-1), at(16)], FILL, at(20), GAP)


def test_a_missing_latest_settlement_makes_the_window_incomplete():
    assert not B.funding_window_complete([at(-1)], FILL, at(7.25), GAP)
    assert B.funding_window_complete([at(-1)], FILL, at(7), GAP)


def test_regular_eight_hour_prints_with_millisecond_jitter_are_complete():
    jitter = timedelta(milliseconds=3)
    assert B.funding_window_complete([at(-2) + jitter, at(6), at(14) + jitter], FILL, at(18), GAP)


def test_a_print_after_the_exit_is_not_evidence():
    assert not B.funding_window_complete([at(-20), at(10)], FILL, at(4), GAP)


# ---------------------------------------------------------------------------
# conventions come from the hashed constants
# ---------------------------------------------------------------------------
def test_the_ambiguous_bar_can_resolve_to_the_target_when_the_constant_says_so(monkeypatch):
    monkeypatch.setattr(C, 'LABEL_AMBIGUOUS_BAR_RESOLVES_TO', 'target')
    r = B.resolve(LONG, plan(), [bar(0, 100, 102.5, 97.5, 100)], FILL)
    assert (r.first_barrier, r.price_r, r.ambiguous) == (E.BARRIER_UPPER, D('1'), True)


def test_a_stop_gap_can_fill_at_the_stop_when_the_constant_says_so(monkeypatch):
    monkeypatch.setattr(C, 'LABEL_GAP_STOP_FILLS_AT', 'stop')
    r = B.resolve(LONG, plan(), [bar(0, 100, 100.5, 99.5, 100), bar(1, 97, 97.5, 96, 96.5)], FILL)
    assert (r.exit_price, r.price_r, r.gapped_stop) == (D('98'), D('-1'), True)


def test_a_target_gap_can_fill_at_the_open_when_the_constant_says_so(monkeypatch):
    monkeypatch.setattr(C, 'LABEL_GAP_TARGET_FILLS_AT', 'open')
    r = B.resolve(LONG, plan(), [bar(0, 100, 100.5, 99.5, 100), bar(1, 103, 104, 102.5, 103.5)], FILL)
    assert (r.exit_price, r.price_r) == (D('103'), D('1.5'))


def test_every_convention_constant_names_something_the_labeler_implements():
    B.check_conventions()
    for name, supported in B._SUPPORTED_CONVENTIONS.items():
        assert getattr(C, name) in supported, name


@pytest.mark.parametrize('name', sorted(B._SUPPORTED_CONVENTIONS))
def test_an_unimplemented_convention_refuses_to_import(monkeypatch, name):
    monkeypatch.setattr(C, name, 'something_else')
    with pytest.raises(ImportError, match=name):
        B.check_conventions()


def test_the_no_entry_reasons_name_the_constant_rule():
    assert C.LABEL_NO_ENTRY_RULE == 'open_at_or_through_stop_or_through_cap'
    assert {B.REASON_ENTRY_THROUGH_STOP, B.REASON_ENTRY_THROUGH_CAP} == {'entry_through_stop', 'entry_through_cap'}


# ---------------------------------------------------------------------------
# the stall cap
# ---------------------------------------------------------------------------
def test_the_stall_cap_is_24_hours_from_the_exit_becoming_visible():
    assert B.FUNDING_STALL_CAP == timedelta(hours=24)
    visible = FILL + timedelta(hours=4, seconds=30)
    assert not B.funding_stalled(visible, visible + timedelta(hours=24) - timedelta(seconds=1))
    assert B.funding_stalled(visible, visible + timedelta(hours=24))


def test_the_funding_hole_tolerance_is_the_eight_hour_interval_plus_five_minutes():
    assert B.FUNDING_MAX_GAP == GAP == timedelta(hours=8, minutes=5)


def test_a_resolution_reports_when_its_exit_bar_became_visible():
    visible = FILL + timedelta(minutes=15, seconds=30)
    b = B.Bar(FILL, FILL + B.BAR_LENGTH, D('100'), D('100.5'), D('97.5'), D('98'), available_at=visible)
    assert B.resolve(LONG, plan(), [b], FILL).visible_at == visible