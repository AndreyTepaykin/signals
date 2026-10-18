"""Elder simulator: parts, stops, trailing, cap, tide exit, costs, funding, censoring.

Scenarios are written for a long (entry 100, stop 95, R = 5, T1 = 107.5 at t1_r 1.5,
T2 = 150) and run for both directions through ``px``, which mirrors prices around the entry.
"""

import math
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.research.elder.params import COST_ROUND_TRIP, T2_R, ElderParams
from app.research.elder.resample import STEP_4H, OHLCV
from app.research.elder.signals import RIPPLE_STEP, Entry, build_context
from app.research.elder.simulate import (
    FundingPrint,
    PartExit,
    Trade,
    _walk,
    simulate_trade,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 5, tzinfo=UTC)
M30 = timedelta(minutes=30)  # one ripple bar
FAR = T0 + timedelta(days=400)
E0 = 100.0
DIST = 5.0
COST_R = COST_ROUND_TRIP * E0 / DIST  # 0.024 per unit of share


def P(**overrides):
    values = {
        'tide_mode': 'ema',
        'wave_osc': 'stoch',
        'wave_threshold': 20.0,
        'wave_block_impulse': False,
        'ripple_expiry_bars': 4,
        'stop_lookback_bars': 1,
        'stop_atr_buffer': 0.0,
        'min_stop_atr': 0.5,
        't1_r': 1.5,
        't1_share': 0.5,
        'be_after_t1': True,
        'trail_atr': 2.0,
    }
    values.update(overrides)
    return ElderParams(**values)


def px(d, v):
    return E0 + d * (v - E0)


def at(i):
    return T0 + i * M30


def make_bars(d, rows, skip=()):
    """rows: long-form (open, high, low, close) per ripple-bar index; mirrored for a short."""
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        if i in skip:
            continue
        a, b, lo, cl = px(d, o), px(d, h), px(d, l), px(d, c)
        hi, lw = (b, lo) if d > 0 else (lo, b)
        out.append(OHLCV(at(i), at(i + 1), a, hi, lw, cl, 10.0))
    return out


def make_entry(d, bars, fill_index=0, entry=E0, dist=DIST, coin='BTCUSDT'):
    stop = entry - d * dist
    return Entry(
        coin=coin,
        direction=d,
        armed_time=bars[fill_index].open_time,
        fill_time=bars[fill_index].open_time,
        fill_index=fill_index,
        fill_price=entry,
        stop=stop,
        r=dist,
        atr_wave=1.0,
        wave_close_time=T0,
        tide_close_time=T0,
    )


def make_ctx(bars, d, tide=None, wave=None):
    """tide: [(close_time, tide_dir, impulse)]; wave: [(close_time, atr)] (fake wave closes)."""
    base = build_context([], [], [], P())
    tide = tide if tide is not None else [(T0 - timedelta(days=1), d, d)]
    wave = wave or []
    return replace(
        base,
        bars_ripple=list(bars),
        close_times_tide=[t[0] for t in tide],
        tide_dir=[t[1] for t in tide],
        impulse_tide=[t[2] for t in tide],
        close_times_wave=[s[0] for s in wave],
        atr_wave=[s[1] for s in wave],
    )


def run(d, rows, *, params=None, funding=(), end=FAR, fill_index=0, tide=None, wave=None, skip=()):
    params = params or P()
    bars = make_bars(d, rows, skip)
    entry = make_entry(d, bars, fill_index)
    ctx = make_ctx(bars, d, tide, wave)
    return simulate_trade(entry, ctx, params, list(funding), end), bars


FILL = (100, 101, 99, 100)
BOTH = pytest.mark.parametrize('d', [1, -1])


def approx(x):
    return pytest.approx(x, abs=1e-9)


# ----------------------------------------------------------------- stops


@BOTH
def test_initial_stop_hit(d):
    t, bars = run(d, [FILL, (100, 101, 94, 95)])
    assert len(t.exits) == 1
    x = t.exits[0]
    assert (x.reason, x.stop_kind, x.gapped, x.ambiguous, x.share) == ('stop', 'initial', False, False, 1.0)
    assert x.price == approx(px(d, 95))
    assert x.time == bars[1].close_time
    assert t.price_r == approx(-1.0)
    assert t.cost_r == approx(COST_R)
    assert t.total_r == approx(-1.0 - COST_R)
    assert t.stopped_before_t1 and not t.hit_t1 and not t.hit_t2
    assert not t.censored and t.censor_reason is None
    assert t.closed_time == bars[1].close_time
    assert t.last_index == 1 and t.bars_held == 2
    assert t.open_share == 0.0 and t.mark_r is None
    assert t.mae_r == approx(1.0)
    assert t.mfe_r == 0.0  # stop-out bar and fill bar contribute their adverse side only
    assert (t.coin, t.direction, t.entry_price, t.initial_stop, t.r) == ('BTCUSDT', d, E0, px(d, 95), DIST)


@BOTH
def test_stop_and_t1_same_bar_is_stop_and_ambiguous(d):
    t, _ = run(d, [FILL, (100, 108, 94, 100)])
    x = t.exits[0]
    assert (x.reason, x.ambiguous, x.gapped) == ('stop', True, False)
    assert x.price == approx(px(d, 95))
    assert not t.hit_t1 and t.stopped_before_t1
    assert t.mfe_r == 0.0


@BOTH
def test_stop_and_t2_same_bar_after_t1_is_ambiguous_stop(d):
    # T1 hit on bar 1; bar 2 touches both the breakeven stop and T2 -> stop, ambiguous
    t, _ = run(d, [FILL, (100, 108, 99.5, 107), (107, 160, 99, 120)])
    assert [x.reason for x in t.exits] == ['t1', 'stop']
    assert t.exits[1].ambiguous and t.exits[1].stop_kind == 'breakeven'
    assert not t.hit_t2 and not t.stopped_before_t1


@BOTH
def test_gap_through_stop_fills_at_open_worse_than_stop(d):
    t, bars = run(d, [FILL, (100, 101, 99.5, 100.2), (92, 93, 91, 92)])
    x = t.exits[0]
    assert (x.reason, x.gapped, x.ambiguous, x.stop_kind) == ('stop', True, False, 'initial')
    assert x.price == approx(px(d, 92))
    assert x.time == bars[2].open_time
    assert t.price_r == approx(-1.6)  # worse than -1R
    assert t.mae_r == approx(1.6)
    assert t.stopped_before_t1 and t.last_index == 2


@BOTH
def test_gap_open_exactly_at_stop_counts_as_gap(d):
    t, _ = run(d, [FILL, (95, 96, 94, 95.5)])
    assert t.exits[0].gapped and t.exits[0].price == approx(px(d, 95))


@BOTH
def test_fill_bar_touching_stop_is_stopped_and_ambiguous(d):
    t, bars = run(d, [(100, 101, 94, 98), (98, 99, 97, 98)])
    x = t.exits[0]
    assert (x.reason, x.ambiguous, x.gapped, x.stop_kind) == ('stop', True, False, 'initial')
    assert x.price == approx(px(d, 95))
    assert x.time == bars[0].close_time
    assert t.last_index == 0 and t.bars_held == 1 and t.stopped_before_t1
    assert t.mae_r == approx(1.0) and t.mfe_r == 0.0


@BOTH
def test_fill_bar_reaching_targets_gets_no_credit(d):
    t, _ = run(d, [(100, 160, 99, 100), (100, 101, 94, 95)])
    assert [x.reason for x in t.exits] == ['stop']
    assert not t.hit_t1 and not t.hit_t2
    assert t.price_r == approx(-1.0)
    assert t.mfe_r == 0.0  # the fill bar's favourable side is not counted either


@BOTH
def test_fill_bar_target_only_is_not_credited_when_censored(d):
    t, _ = run(d, [(100, 160, 99, 107), (107, 107.4, 106, 106.5)])
    assert t.exits == () and not t.hit_t1
    assert t.censored and t.censor_reason == 'end_of_period'
    assert t.open_share == 1.0 and t.total_r == 0.0


# ------------------------------------------------------- breakeven, T1


@BOTH
def test_t1_then_breakeven_next_bar_not_on_the_t1_bar(d):
    rows = [
        FILL,
        (100, 108, 96, 107),  # T1 bar; its low 96 would stop a breakeven stop at 100
        (107, 108, 101, 107.5),
        (107, 107, 99, 100),  # breakeven active: stopped at entry
    ]
    t, bars = run(d, rows)
    assert [x.reason for x in t.exits] == ['t1', 'stop']
    t1, st = t.exits
    assert (t1.share, t1.price, t1.time, t1.ambiguous, t1.gapped) == (0.5, px(d, 107.5), bars[1].close_time, False, False)
    assert t1.stop_kind is None
    assert (st.share, st.stop_kind, st.gapped, st.ambiguous) == (0.5, 'breakeven', False, False)
    assert st.price == approx(E0)
    assert t.price_r == approx(0.5 * 1.5)
    assert t.cost_r == approx(COST_R)
    assert t.total_r == approx(0.75 - COST_R)
    assert t.hit_t1 and not t.stopped_before_t1 and not t.hit_t2
    assert t.last_index == 3


@BOTH
def test_be_after_t1_false_keeps_the_initial_stop(d):
    rows = [FILL, (100, 108, 99.5, 107), (107, 108, 99, 100), (100, 101, 94, 95)]
    t, _ = run(d, rows, params=P(be_after_t1=False))
    assert [x.reason for x in t.exits] == ['t1', 'stop']
    assert t.exits[1].stop_kind == 'initial' and t.exits[1].price == approx(px(d, 95))
    assert t.price_r == approx(0.75 - 0.5)
    walk = _walk(make_entry(d, make_bars(d, rows)), make_ctx(make_bars(d, rows), d), P(be_after_t1=False), FAR)
    assert [k for _, _, k in walk.stop_history] == ['initial']


@BOTH
def test_gap_through_breakeven_stop(d):
    t, bars = run(d, [FILL, (100, 108, 99.5, 107), (98, 99, 97, 98)])
    st = t.exits[1]
    assert (st.reason, st.stop_kind, st.gapped) == ('stop', 'breakeven', True)
    assert st.price == approx(px(d, 98)) and st.time == bars[2].open_time
    assert t.price_r == approx(0.5 * 1.5 + 0.5 * (-0.4))


@BOTH
def test_bar_opening_above_t1_still_fills_at_t1(d):
    # open above T1 but below T2: no price improvement on the limit
    t, _ = run(d, [FILL, (112, 113, 111, 112), (112, 113, 99, 100)])
    assert t.exits[0].reason == 't1' and t.exits[0].price == approx(px(d, 107.5))


# ----------------------------------------------------------------- trailing


WAVE2 = [(at(4), 1.0), (at(8), 2.0)]


def trail_rows():
    return [
        FILL,
        (100, 108, 99.5, 107),  # T1
        (107, 109, 106, 108),
        (108, 110, 107, 109),
        (109, 111, 108.5, 110),  # first bar to see the wave close at at(4)
        (110, 120, 109, 119),
        (119, 119, 110, 112),
        (112, 113, 111, 112),
        (118, 119, 115, 116),  # sees the wave close at at(8)
    ]


@BOTH
def test_trailing_updates_only_at_a_wave_close_and_exits_with_trail_kind(d):
    t, bars = run(d, trail_rows(), wave=WAVE2)
    st = t.exits[-1]
    # bar 4: candidate = max high of bars 0..3 (110) - 2 * 1.0 = 108, bars 5..7 do not change it
    # bar 8: candidate = max high of bars 0..7 (120) - 2 * 2.0 = 116, touched on bar 8, open 118 above it
    assert (st.reason, st.stop_kind, st.gapped) == ('stop', 'trail', False)
    assert st.price == approx(px(d, 116)) and st.time == bars[8].close_time
    assert t.price_r == approx(0.5 * 1.5 + 0.5 * (16 / 5))
    assert t.last_index == 8
    walk = _walk(make_entry(d, bars), make_ctx(bars, d, wave=WAVE2), P(), FAR)
    assert [(i, k) for i, _, k in walk.stop_history] == [(0, 'initial'), (2, 'breakeven'), (4, 'trail'), (8, 'trail')]
    stops = [s for _, s, _ in walk.stop_history]
    assert [d * (b - a) > 0 for a, b in zip(stops, stops[1:])] == [True, True, True]


@BOTH
def test_trailing_uses_only_highs_of_previous_bars(d):
    rows = trail_rows()
    rows[4] = (109, 111, 107.9, 108.5)  # touches 108 = (bar 3's 110) - 2; a lookahead to bar 4's 111 would give 109
    t, _ = run(d, rows, wave=WAVE2)
    assert t.exits[-1].price == approx(px(d, 108)) and t.exits[-1].stop_kind == 'trail'
    assert t.last_index == 4


@BOTH
def test_trailing_never_loosens(d):
    wave = [(at(4), 1.0), (at(8), 10.0)]  # second candidate 120 - 20 = 100 < current 108
    rows = trail_rows()[:8] + [(112, 113, 111, 112), (112, 112, 107, 108)]
    t, _ = run(d, rows, wave=wave)
    walk = _walk(make_entry(d, make_bars(d, rows)), make_ctx(make_bars(d, rows), d, wave=wave), P(), FAR)
    assert [i for i, _, k in walk.stop_history if k == 'trail'] == [4]
    assert t.exits[-1].price == approx(px(d, 108)) and t.exits[-1].stop_kind == 'trail'


@BOTH
def test_trailing_with_missing_atr_does_not_update(d):
    wave = [(at(4), None)]
    rows = trail_rows()[:5] + [(110, 112, 99, 100)]
    t, _ = run(d, rows, wave=wave)
    assert t.exits[-1].stop_kind == 'breakeven' and t.exits[-1].price == approx(E0)


@BOTH
def test_a_wave_close_before_t1_is_not_a_trail_update(d):
    wave = [(at(2), 1.0)]  # visible at bar 2, T1 is hit on bar 3
    rows = [FILL, (100, 101, 99.5, 100.5), (100.5, 102, 100, 101), (101, 108, 99.5, 107), (107, 109, 106, 108), (108, 108, 99, 100)]
    t, bars = run(d, rows, wave=wave)
    walk = _walk(make_entry(d, bars), make_ctx(bars, d, wave=wave), P(), FAR)
    assert [k for _, _, k in walk.stop_history] == ['initial', 'breakeven']
    assert t.exits[-1].stop_kind == 'breakeven'


@BOTH
def test_trail_candidate_above_open_gaps_out_at_the_open(d):
    wave = [(at(3), 0.1)]  # candidate = 110 - 0.2 = 109.8; bar 3 opens at 109
    rows = [FILL, (100, 108, 99.5, 107), (107, 110, 106, 109), (109, 109.5, 108, 109)]
    t, bars = run(d, rows, params=P(trail_atr=2.0), wave=wave)
    st = t.exits[-1]
    assert (st.stop_kind, st.gapped) == ('trail', True)
    assert st.price == approx(px(d, 109)) and st.time == bars[3].open_time


def real_wave_ctx(bars, d):
    """A context whose wave series is real 4H bars (flat, ATR 2.0): wave slot 20 closes at T0."""
    wave_bars = []
    for i in range(30):
        opened = T0 - 21 * STEP_4H + i * STEP_4H
        wave_bars.append(OHLCV(opened, opened + STEP_4H, 100.0, 101.0, 99.0, 100.0, 10.0))
    base = build_context([], wave_bars, [], P())
    assert base.atr_wave[20] == approx(2.0) and base.atr_wave[21] == approx(2.0)
    assert base.close_times_wave[20] == T0
    return replace(
        base,
        bars_ripple=list(bars),
        close_times_tide=[T0 - timedelta(days=1)],
        tide_dir=[d],
        impulse_tide=[d],
    )


@BOTH
def test_trailing_updates_at_the_4h_close_not_every_30m_bar(d):
    # 4H closes at T0 + 4h are bar 8 of the 30m series. Bars 2..7 have highs up to 112; bar 7 dips
    # to 106, which a trail already set from that high (112 - 2 x 2.0 = 108) would have stopped.
    rows = [FILL, (100, 108, 99.5, 107.5), (107.5, 112, 106, 111)]
    rows += [(111, 112, 106, 110)] * 5  # bars 3 .. 7
    rows += [(111, 111.5, 107.9, 108.5)]  # bar 8: first bar to see the 4H close; trail 108 is touched
    bars = make_bars(d, rows)
    assert (bars[8].open_time - T0) == STEP_4H
    t = simulate_trade(make_entry(d, bars), real_wave_ctx(bars, d), P(trail_atr=2.0), [], FAR)
    assert [x.reason for x in t.exits] == ['t1', 'stop']
    st = t.exits[-1]
    assert (st.stop_kind, st.gapped) == ('trail', False)
    assert st.price == approx(px(d, 108.0)) and st.time == bars[8].close_time
    assert t.last_index == 8 and t.hit_t1 and not t.censored
    assert t.price_r == approx(0.5 * 1.5 + 0.5 * 1.6)


# ----------------------------------------------------------------- T2 cap


@BOTH
def test_t2_cap_runner_closes_at_exactly_ten_r(d):
    rows = [FILL, (100, 108, 99.5, 107), (107, 109, 106, 108), (108, 1000, 107, 900)]
    t, bars = run(d, rows)
    assert [x.reason for x in t.exits] == ['t1', 't2']
    assert t.exits[1].price == approx(px(d, 150)) and t.exits[1].time == bars[3].close_time
    assert t.hit_t1 and t.hit_t2 and not t.censored
    assert t.price_r == approx(0.75 + 0.5 * T2_R)
    assert t.mfe_r > T2_R  # excursion is reported as seen; the fill is capped


@BOTH
def test_t1_and_t2_in_the_same_bar(d):
    t, bars = run(d, [FILL, (100, 1000, 99.5, 500)])
    assert [x.reason for x in t.exits] == ['t1', 't2']
    assert all(x.time == bars[1].close_time and not x.ambiguous for x in t.exits)
    assert t.price_r == approx(0.5 * 1.5 + 0.5 * 10)
    assert t.last_index == 1


@BOTH
def test_bar_opening_above_t2_fills_at_t2(d):
    t, _ = run(d, [FILL, (200, 300, 199, 250)])
    assert [x.price for x in t.exits] == [approx(px(d, 107.5)), approx(px(d, 150))]


# ----------------------------------------------------------------- tide


@BOTH
def test_tide_flip_exits_at_the_open(d):
    tide = [(T0 - timedelta(days=1), d, d), (at(3), -d, -d)]
    rows = [FILL, (100, 102, 99, 101), (101, 103, 100, 102), (102, 103, 101, 102.5)]
    t, bars = run(d, rows, tide=tide)
    x = t.exits[0]
    assert (x.reason, x.stop_kind, x.gapped, x.ambiguous, x.share) == ('tide_exit', None, False, False, 1.0)
    assert x.price == approx(px(d, 102)) and x.time == bars[3].open_time
    assert t.price_r == approx(0.4) and t.last_index == 3 and not t.stopped_before_t1


@BOTH
def test_tide_impulse_against_exits_even_with_tide_unchanged(d):
    tide = [(T0 - timedelta(days=1), d, d), (at(2), d, -d)]
    t, bars = run(d, [FILL, (100, 102, 99, 101), (101, 103, 100, 102)], tide=tide)
    assert t.exits[0].reason == 'tide_exit' and t.exits[0].time == bars[2].open_time


@BOTH
@pytest.mark.parametrize('tide_dir, imp', [(0, 0), (None, None), (0, None), (None, 0)])
def test_tide_zero_or_none_does_not_exit(d, tide_dir, imp):
    tide = [(T0 - timedelta(days=1), d, d), (at(2), tide_dir, imp)]
    t, _ = run(d, [FILL, (100, 102, 99, 101), (101, 103, 100, 102), (102, 103, 101, 102)], tide=tide)
    assert t.exits == () and t.censored


@BOTH
def test_tide_exit_through_t1_gap_books_t1_at_its_price(d):
    tide = [(T0 - timedelta(days=1), d, d), (at(2), -d, -d)]
    t, bars = run(d, [FILL, (100, 102, 99, 101), (120, 121, 119, 120)], tide=tide)
    assert [(x.reason, x.share) for x in t.exits] == [('t1', 0.5), ('tide_exit', 0.5)]
    assert t.exits[0].price == approx(px(d, 107.5)) and t.exits[1].price == approx(px(d, 120))
    assert all(x.time == bars[2].open_time for x in t.exits)
    assert t.hit_t1 and not t.hit_t2


@BOTH
def test_tide_exit_through_t2_gap_is_capped_at_ten_r(d):
    tide = [(T0 - timedelta(days=1), d, d), (at(2), -d, -d)]
    t, _ = run(d, [FILL, (100, 102, 99, 101), (400, 401, 399, 400)], tide=tide)
    assert [x.reason for x in t.exits] == ['t1', 't2']
    assert t.price_r == approx(0.75 + 5.0) and t.hit_t2


@BOTH
def test_tide_is_not_checked_on_the_fill_bar(d):
    tide = [(T0 - timedelta(days=1), -d, -d)]
    t, _ = run(d, [FILL, (100, 101, 99.5, 100.5), (100.5, 101, 94, 95)], tide=tide)
    assert t.exits[0].reason == 'tide_exit' and t.exits[0].time == at(1)  # first later bar


# ----------------------------------------------------------------- shares


@BOTH
def test_t1_share_zero_has_no_t1_part_but_triggers_breakeven(d):
    rows = [FILL, (100, 108, 99.5, 107), (107, 108, 99.9, 100.5)]
    t, _ = run(d, rows, params=P(t1_share=0.0))
    assert t.hit_t1 and len(t.exits) == 1
    x = t.exits[0]
    assert (x.share, x.reason, x.stop_kind) == (1.0, 'stop', 'breakeven')
    assert x.price == approx(E0) and t.price_r == approx(0.0)
    assert t.cost_r == approx(COST_R) and not t.stopped_before_t1


@BOTH
def test_t1_share_zero_runner_closes_at_t2(d):
    t, _ = run(d, [FILL, (100, 1000, 99.5, 500)], params=P(t1_share=0.0))
    assert [(x.reason, x.share) for x in t.exits] == [('t2', 1.0)]
    assert t.price_r == approx(10.0) and t.hit_t1 and t.hit_t2


@BOTH
def test_t1_share_one_closes_everything_at_t1(d):
    t, bars = run(d, [FILL, (100, 108, 99.5, 107), (107, 200, 100, 150)], params=P(t1_share=1.0))
    assert [(x.reason, x.share) for x in t.exits] == [('t1', 1.0)]
    assert t.exits[0].time == bars[1].close_time and t.last_index == 1
    assert t.price_r == approx(1.5) and t.hit_t1 and not t.hit_t2 and not t.censored


@BOTH
def test_t1_share_one_stop_before_t1_still_loses_one_r(d):
    t, _ = run(d, [FILL, (100, 101, 94, 95)], params=P(t1_share=1.0))
    assert t.price_r == approx(-1.0) and t.stopped_before_t1


@BOTH
def test_t1_share_fraction_shares_sum_to_one(d):
    t, _ = run(d, [FILL, (100, 1000, 99.5, 500)], params=P(t1_share=0.3))
    assert math.fsum(x.share for x in t.exits) == pytest.approx(1.0, abs=1e-12)
    assert t.price_r == approx(0.3 * 1.5 + 0.7 * 10)
    assert t.cost_r == approx(COST_R)


# ----------------------------------------------------------------- funding


FUND_ROWS = [FILL, (100, 108, 99.5, 107), (107, 108, 101, 107), (107, 107, 99, 100)]


@BOTH
def test_funding_windows_signs_and_mark_fallback(d):
    # T1 part exits at at(2) (bar 1 close), runner at at(4) (bar 3 close)
    prints = [
        FundingPrint(at(0), 0.01),  # at the fill: excluded
        FundingPrint(at(1), 0.001),  # both parts, mark falls back to the entry (100)
        FundingPrint(at(3), 0.001, 110.0),  # runner only (after the T1 exit)
        FundingPrint(at(4), 0.01, 100.0),  # at the final exit: excluded
        FundingPrint(at(10), 0.01, 100.0),  # after everything
    ]
    t, _ = run(d, FUND_ROWS, funding=prints)
    assert [x.time for x in t.exits] == [at(2), at(4)]
    paid = 0.5 * 0.001 * 100 / 5 + 0.5 * 0.001 * 100 / 5 + 0.5 * 0.001 * 110 / 5
    assert t.funding_r == approx(-d * paid)
    assert t.total_r == approx(t.price_r - t.cost_r + t.funding_r)
    negative = [replace(p, rate=-p.rate) for p in prints]
    t2, _ = run(d, FUND_ROWS, funding=negative)
    assert t2.funding_r == approx(d * paid)


@BOTH
def test_funding_print_after_the_t1_exit_does_not_count_for_the_t1_part(d):
    t, _ = run(d, FUND_ROWS, funding=[FundingPrint(at(3), 0.002)])
    assert t.funding_r == approx(-d * 0.5 * 0.002 * 100 / 5)


@BOTH
def test_no_funding_means_zero(d):
    t, _ = run(d, FUND_ROWS)
    assert t.funding_r == 0.0


# ----------------------------------------------------------------- costs


@BOTH
def test_cost_is_charged_per_closed_part(d):
    # only the T1 part closed, the runner is still open when the data is cut
    rows = [FILL, (100, 108, 99.5, 107), (107, 109, 106, 108)]
    t, _ = run(d, rows, end=at(3))
    assert t.censored and len(t.exits) == 1
    assert t.cost_r == approx(0.5 * COST_R)
    assert t.price_r == approx(0.75)


# ----------------------------------------------------------------- censoring


CUT_ROWS = [
    FILL,
    (100, 108, 99.5, 107),  # T1
    (107, 109, 106, 108),
    (108, 110, 107, 109),
    (109, 111, 108.5, 109.5),  # last bar of the period
    (90, 91, 80, 85),  # beyond end_time: would gap the trade out
    (85, 400, 80, 300),
]


@BOTH
def test_end_time_cuts_the_trade_and_hidden_bars_are_never_read(d):
    end = at(5)  # bar 4 closes exactly here, so it is usable
    t, bars = run(d, CUT_ROWS, end=end)
    assert t.censored and t.censor_reason == 'end_of_period'
    assert t.last_index == 4 and t.bars_held == 5 and t.closed_time is None
    assert [x.reason for x in t.exits] == ['t1']
    assert t.open_share == approx(0.5)
    assert t.mark_r == approx((109.5 - 100) / 5)
    assert t.price_r == approx(0.75) and t.cost_r == approx(0.5 * COST_R)
    assert t.total_r == approx(0.75 - 0.5 * COST_R)  # closed shares only, mark_r is not in it
    # mutate every bar with close_time > end: nothing may change
    mutated = []
    for b in bars:
        if b.close_time > end:
            mutated.append(OHLCV(b.open_time, b.close_time, 1e-3, 1e9, 1e-6, 5e8, 1.0))
        else:
            mutated.append(b)
    entry = make_entry(d, bars)
    ctx = make_ctx(mutated, d)
    assert simulate_trade(entry, ctx, P(), [], end) == t


@BOTH
def test_bar_closing_one_microsecond_after_end_is_not_used(d):
    t, _ = run(d, CUT_ROWS, end=at(5) - timedelta(microseconds=1))
    assert t.last_index == 3 and t.mark_r == approx((109 - 100) / 5)


@BOTH
def test_censored_trade_with_nothing_closed(d):
    t, _ = run(d, [FILL, (100, 102, 99, 101)], end=at(2))
    assert t.censored and t.exits == () and t.price_r == t.cost_r == t.funding_r == t.total_r == 0.0
    assert t.open_share == 1.0 and t.mark_r == approx(0.2)
    assert t.mae_r == approx(0.2) and t.mfe_r == approx(0.4)  # fill bar adverse only: 99 -> 0.2; bar 1 high 102 -> 0.4


@BOTH
def test_fill_bar_beyond_end_time_is_censored_without_bars(d):
    t, _ = run(d, [FILL, (100, 102, 99, 101)], end=at(1) - timedelta(seconds=1))
    assert t.censored and t.censor_reason == 'end_of_period' and t.exits == ()
    assert t.last_index == 0 and t.open_share == 1.0 and t.mark_r == 0.0 and t.total_r == 0.0


@BOTH
def test_data_that_ends_is_end_of_period(d):
    t, _ = run(d, [FILL, (100, 102, 99, 101)])
    assert t.censored and t.censor_reason == 'end_of_period' and t.last_index == 1


@BOTH
def test_ripple_hole_censors_at_the_last_bar_before_it(d):
    rows = CUT_ROWS[:4] + [(109, 111, 108.5, 109.5), (90, 91, 80, 85)]
    t, _ = run(d, rows, skip=(3,))  # bar 3 missing: bars 0,1,2 then 4
    assert t.censored and t.censor_reason == 'hole'
    assert t.last_index == 2 and t.bars_held == 3 and t.closed_time is None
    assert [x.reason for x in t.exits] == ['t1']
    assert t.mark_r == approx((108 - 100) / 5)
    assert t.total_r == approx(0.75 - 0.5 * COST_R)


@BOTH
def test_exit_before_a_hole_stands(d):
    rows = [FILL, (100, 101, 94, 95), (100, 101, 99, 100), (100, 101, 99, 100)]
    t, _ = run(d, rows, skip=(2,))
    assert not t.censored and t.exits[0].reason == 'stop'


@BOTH
def test_end_of_period_wins_over_a_hole_after_the_end(d):
    rows = CUT_ROWS[:4] + [(109, 111, 108.5, 109.5)]
    t, _ = run(d, rows, skip=(3,), end=at(3))  # bar 2 is the last usable bar and bar 3 would lie past end
    assert t.censor_reason == 'end_of_period' and t.last_index == 2


def test_validation_errors():
    bars = make_bars(1, [FILL, (100, 101, 99, 100)])
    ctx = make_ctx(bars, 1)
    good = make_entry(1, bars)
    with pytest.raises(ValueError):
        simulate_trade(replace(good, fill_index=1), ctx, P(), [], FAR)
    with pytest.raises(ValueError):
        simulate_trade(replace(good, stop=101.0, r=1.0), ctx, P(), [], FAR)
    with pytest.raises(ValueError):
        simulate_trade(replace(good, r=4.0), ctx, P(), [], FAR)
    with pytest.raises(ValueError):
        simulate_trade(replace(good, direction=2), ctx, P(), [], FAR)


def test_partexit_and_trade_are_frozen():
    x = PartExit(1.0, 1.0, T0, 'stop', 'initial', False, False)
    with pytest.raises(Exception):
        x.share = 0.5  # type: ignore[misc]


# ----------------------------------------------------------------- randomized property test


def random_case(rng):
    d = rng.choice([1, -1])
    n = rng.randint(3, 260)
    price = rng.uniform(50.0, 500.0)
    sigma = rng.choice([0.002, 0.005, 0.01, 0.02])
    drift = rng.choice([0.0, 0.0, 0.002, -0.002]) * (1 if rng.random() < 0.5 else 3)
    rows = []
    for _ in range(n):
        o = price * (1.0 + (rng.gauss(0, sigma * 3) if rng.random() < 0.05 else 0.0))  # occasional gap
        c = o * (1.0 + rng.gauss(drift, sigma))
        hi = max(o, c) * (1.0 + abs(rng.gauss(0, sigma)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0, sigma)))
        rows.append((o, hi, lo, c))
        price = c
    skip = set()
    if n > 8 and rng.random() < 0.2:
        skip.add(rng.randint(3, n - 1))
    bars = []
    for i, (o, h, l, c) in enumerate(rows):
        if i not in skip:
            bars.append(OHLCV(at(i), at(i + 1), o, h, l, c, 10.0))
    entry_price = bars[0].open
    dist = entry_price * rng.choice([0.004, 0.008, 0.015, 0.03])
    entry = Entry(
        coin='ETHUSDT', direction=d, armed_time=at(0), fill_time=at(0), fill_index=0,
        fill_price=entry_price, stop=entry_price - d * dist, r=dist, atr_wave=1.0,
        wave_close_time=T0, tide_close_time=T0,
    )
    wave = []
    j = rng.randint(1, 20)
    while j < n + 24:
        wave.append((at(j), None if rng.random() < 0.1 else entry_price * rng.uniform(0.002, 0.03)))
        j += 24
    tide = [(T0 - timedelta(days=1), d if rng.random() < 0.85 else rng.choice([0, -d]), d if rng.random() < 0.85 else rng.choice([0, -d, None]))]
    for j in range(rng.randint(0, 3)):
        tide.append((at(rng.randint(2, n + 5)), rng.choice([d, d, 0, -d, None]), rng.choice([d, d, 0, -d, None])))
    tide.sort(key=lambda t: t[0])
    tide = [t for k, t in enumerate(tide) if k == 0 or t[0] != tide[k - 1][0]]
    funding = [
        FundingPrint(at(k * 32 + rng.randint(-3, 3)), rng.uniform(-0.0005, 0.0015), rng.choice([None, entry_price * rng.uniform(0.9, 1.1)]))
        for k in range(n // 32 + 1)
    ]
    params = P(
        t1_r=rng.uniform(1.0, 2.0),
        t1_share=rng.choice([0.0, 0.25, 0.5, 0.7, 1.0, rng.random()]),
        be_after_t1=rng.random() < 0.7,
        trail_atr=rng.uniform(0.3, 4.0),
    )
    end = FAR if rng.random() < 0.6 else at(rng.randint(1, n + 1))
    return d, bars, entry, make_ctx(bars, d, tide, wave), params, funding, end


def check_trade(t, walk, d, entry, bars, params, end):
    e, r = entry.fill_price, entry.r
    eps = 1e-9
    # ``r`` is priced in simulate_trade (funding is not known to the walk).
    assert tuple(replace(x, r=0.0) for x in t.exits) == tuple(walk.exits)
    assert math.isclose(sum(part.r for part in t.exits), t.total_r, rel_tol=1e-9, abs_tol=1e-9)
    for x in t.exits:
        pr = d * (x.price - e) / r
        assert pr <= T2_R + eps
        assert x.share > 0.0 and x.time <= end
        if x.reason == 't1':
            assert pr == pytest.approx(params.t1_r, abs=eps) and not x.gapped
        if x.reason == 't2':
            assert pr == pytest.approx(T2_R, abs=eps)
        if x.reason == 'stop':
            assert x.stop_kind in ('initial', 'breakeven', 'trail')
            if not x.gapped:
                assert pr >= -1.0 - eps  # a stop never loses more than 1R unless it gaps
            if x.stop_kind == 'initial' and not x.gapped:
                assert pr == pytest.approx(-1.0, abs=eps)
            if x.stop_kind == 'breakeven' and not x.gapped:
                assert pr == pytest.approx(0.0, abs=eps)
            assert x.stop_kind != 'breakeven' or t.hit_t1
        else:
            assert x.stop_kind is None and not x.gapped and not x.ambiguous
    times = [x.time for x in t.exits]
    assert times == sorted(times)
    assert sum(1 for x in t.exits if x.reason == 't1') <= 1 and sum(1 for x in t.exits if x.reason == 't2') <= 1
    assert t.hit_t2 == any(x.reason == 't2' for x in t.exits)
    if any(x.reason == 't1' for x in t.exits):
        assert t.hit_t1
    shares = math.fsum(x.share for x in t.exits)
    if t.censored:
        assert t.censor_reason in ('end_of_period', 'hole') and t.closed_time is None
        assert t.open_share > 0.0 and t.mark_r is not None
        assert shares + t.open_share == pytest.approx(1.0, abs=1e-12)
    else:
        assert t.censor_reason is None and t.open_share == 0.0 and t.mark_r is None
        assert shares == pytest.approx(1.0, abs=1e-12)
        assert t.closed_time == t.exits[-1].time
    assert t.stopped_before_t1 == (
        not t.hit_t1 and not t.censored and t.exits[-1].reason == 'stop'
    )
    if t.stopped_before_t1:
        assert len(t.exits) == 1 and t.exits[0].share == 1.0
    assert t.total_r == pytest.approx(t.price_r - t.cost_r + t.funding_r, abs=1e-12)
    assert t.cost_r == pytest.approx(shares * COST_ROUND_TRIP * e / r, abs=1e-12)
    assert t.mae_r >= 0.0 and t.mfe_r >= 0.0
    assert t.bars_held == t.last_index - t.fill_index + 1 >= 1
    # stops are monotone: every update tightens
    history = walk.stop_history
    assert history[0][1] == entry.stop and history[0][2] == 'initial'
    for (_, a, _), (i, b, kind) in zip(history, history[1:]):
        assert d * (b - a) > 0 and kind in ('breakeven', 'trail') and i > t.fill_index
    # no bar past end_time was used
    if not (t.censored and t.last_index == t.fill_index and bars[t.fill_index].close_time > end):
        assert all(b.close_time <= end for b in bars[t.fill_index : t.last_index + 1])
    if t.censor_reason == 'hole':
        nxt = t.last_index + 1
        assert nxt < len(bars) and bars[nxt].open_time != bars[t.last_index].open_time + RIPPLE_STEP
        assert bars[t.last_index].close_time + RIPPLE_STEP <= end


def test_randomized_invariants_over_many_trades():
    rng = random.Random(20261017)
    seen = {k: 0 for k in ('t1', 't2', 'stop_initial', 'stop_breakeven', 'stop_trail', 'gapped', 'tide_exit', 'hole', 'eop', 'ambiguous', 'closed')}
    for _ in range(1500):
        d, bars, entry, ctx, params, funding, end = random_case(rng)
        walk = _walk(entry, ctx, params, end)
        t = simulate_trade(entry, ctx, params, funding, end)
        check_trade(t, walk, d, entry, bars, params, end)
        # hidden bars never matter
        hidden = [
            OHLCV(b.open_time, b.close_time, 1e-3, 1e9, 1e-6, 5e8, 1.0) if b.close_time > end else b
            for b in bars
        ]
        assert simulate_trade(entry, replace(ctx, bars_ripple=hidden), params, funding, end) == t
        for x in t.exits:
            seen['t1'] += x.reason == 't1'
            seen['t2'] += x.reason == 't2'
            seen['tide_exit'] += x.reason == 'tide_exit'
            seen['gapped'] += x.gapped
            seen['ambiguous'] += x.ambiguous
            if x.reason == 'stop':
                seen['stop_' + x.stop_kind] += 1
        seen['hole'] += t.censor_reason == 'hole'
        seen['eop'] += t.censor_reason == 'end_of_period'
        seen['closed'] += not t.censored
    assert all(v > 0 for v in seen.values()), seen


def test_long_and_short_are_mirror_images():
    rng = random.Random(7)
    checked = 0
    for _ in range(400):
        d, bars, entry, ctx, params, funding, end = random_case(rng)
        pivot = 2 * entry.fill_price
        mirrored = [OHLCV(b.open_time, b.close_time, pivot - b.open, pivot - b.low, pivot - b.high, pivot - b.close, b.volume) for b in bars]
        m_entry = replace(entry, direction=-d, stop=pivot - entry.stop)
        m_ctx = replace(
            ctx,
            bars_ripple=mirrored,
            tide_dir=[None if v is None else -v for v in ctx.tide_dir],
            impulse_tide=[None if v is None else -v for v in ctx.impulse_tide],
        )
        a = simulate_trade(entry, ctx, params, funding, end)
        b = simulate_trade(m_entry, m_ctx, params, funding, end)
        assert [(x.reason, x.stop_kind, x.gapped, x.ambiguous, x.time) for x in a.exits] == [
            (x.reason, x.stop_kind, x.gapped, x.ambiguous, x.time) for x in b.exits
        ]
        assert (a.censor_reason, a.last_index, a.hit_t1, a.hit_t2) == (b.censor_reason, b.last_index, b.hit_t1, b.hit_t2)
        assert a.price_r == pytest.approx(b.price_r, abs=1e-7)
        assert a.mae_r == pytest.approx(b.mae_r, abs=1e-7)
        assert a.mfe_r == pytest.approx(b.mfe_r, abs=1e-7)
        checked += 1
    assert checked == 400
