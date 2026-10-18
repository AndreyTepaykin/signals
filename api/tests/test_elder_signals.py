"""Elder signals: tide, wave, ripple lifecycle, stop, and the no-lookahead guarantees."""

import math
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pytest

from app.research.elder.params import COST_ROUND_TRIP, MAX_COST_R, ElderParams
from app.research.elder.resample import (
    OHLCV,
    STEP_1D,
    STEP_4H,
    STEP_15M,
    to_1d,
    to_30m,
)
from app.research.elder.signals import (
    RIPPLE_STEP,
    TIDE_STEP,
    WAVE_STEP,
    Dropped,
    Entry,
    SignalContext,
    SignalResult,
    build_context,
    generate_entries,
    stop_for,
    tide_at,
    tide_permits,
    wave_active,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 5, tzinfo=UTC)  # Monday 00:00 UTC; a day, wave and ripple boundary
M30 = timedelta(minutes=30)
PER_WAVE = STEP_4H // M30  # ripple bars per wave bar (8)


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
        'trail_atr': 3.0,
    }
    values.update(overrides)
    return ElderParams(**values)


def bar(open_time, step, o, h, l, c, v=10.0):
    return OHLCV(open_time, open_time + step, float(o), float(h), float(l), float(c), float(v))


# ------------------------------------------------------------------ tide


def daily(closes):
    return [bar(T0 + i * STEP_1D, STEP_1D, c, c + 1, c - 1, c) for i, c in enumerate(closes)]


UP = [100 + 0.2 * i * i for i in range(60)]
DOWN = [1000 - 0.2 * i * i for i in range(60)]
# A strong move that stalls: EMA13 keeps rising (falling) while the MACD histogram turns down (up).
DECEL_UP = [100.0 + 10 * i for i in range(45)] + [540.0 + (i + 1) for i in range(5)]
DECEL_DOWN = [1000.0 - 10 * i for i in range(45)] + [560.0 - (i + 1) for i in range(5)]


def tide_ctx(closes, mode):
    bars = daily(closes)
    return build_context([], [], bars, P(tide_mode=mode))


def last_tide(closes, mode):
    ctx = tide_ctx(closes, mode)
    return tide_at(ctx, ctx.close_times_tide[-1])


@pytest.mark.parametrize('mode', ['ema', 'macd', 'both'])
def test_tide_up_and_down(mode):
    assert last_tide(UP, mode)[0] == 1
    assert last_tide(DOWN, mode)[0] == -1


def test_tide_modes_disagree():
    # Decelerating advance: EMA13 still rising, MACD histogram falling.
    assert last_tide(DECEL_UP, 'ema')[0] == 1
    assert last_tide(DECEL_UP, 'macd')[0] == -1
    assert last_tide(DECEL_UP, 'both')[0] == 0
    assert last_tide(DECEL_DOWN, 'ema')[0] == -1
    assert last_tide(DECEL_DOWN, 'macd')[0] == 1
    assert last_tide(DECEL_DOWN, 'both')[0] == 0


def test_tide_reports_tide_impulse():
    tide, imp = last_tide(UP, 'ema')
    assert (tide, imp) == (1, 1)
    tide, imp = last_tide(DOWN, 'ema')
    assert (tide, imp) == (-1, -1)


def test_zero_slope_is_zero():
    ctx = tide_ctx([100.0] * 60, 'ema')
    assert tide_at(ctx, ctx.close_times_tide[-1])[0] == 0
    assert not tide_permits(ctx, 1, ctx.close_times_tide[-1])
    assert not tide_permits(ctx, -1, ctx.close_times_tide[-1])


def test_cold_and_empty_are_none():
    ctx = tide_ctx(UP[:8], 'ema')
    assert tide_at(ctx, ctx.close_times_tide[-1]) == (None, None)
    # EMA is warm after 14 days but MACD needs 35 plus a slope bar
    ctx = tide_ctx(UP[:20], 'macd')
    assert tide_at(ctx, ctx.close_times_tide[-1])[0] is None
    ctx = tide_ctx(UP[:20], 'both')
    assert tide_at(ctx, ctx.close_times_tide[-1])[0] is None
    assert tide_at(ctx, T0 - timedelta(days=30)) == (None, None)  # before any tide bar
    empty = build_context([], [], [], P())
    assert tide_at(empty, T0) == (None, None)


def test_tide_uses_only_closed_days():
    ctx = tide_ctx(UP, 'ema')
    last_close = ctx.close_times_tide[-1]
    just_before = last_close - timedelta(microseconds=1)
    assert tide_at(ctx, just_before) == tide_at(ctx, ctx.close_times_tide[-2])
    assert tide_at(ctx, last_close) == last_tide(UP, 'ema')


def test_hole_in_days_is_cold():
    bars = daily(UP)
    del bars[50]  # skipped bucket -> hole slot
    ctx = build_context([], [], bars, P(tide_mode='ema'))
    assert ctx.bars_tide[50] is None
    assert tide_at(ctx, ctx.close_times_tide[50])[0] is None
    assert tide_at(ctx, ctx.close_times_tide[51])[0] is None  # EMA restarts after the hole


def test_impulse_veto_and_none():
    ctx = tide_ctx(UP, 'ema')
    now = ctx.close_times_tide[-1]
    assert tide_permits(ctx, 1, now)
    assert not tide_permits(ctx, -1, now)  # tide is long
    against = replace(ctx, impulse_tide=[-1] * len(ctx.impulse_tide))
    assert not tide_permits(against, 1, now)
    neutral = replace(ctx, impulse_tide=[0] * len(ctx.impulse_tide))
    assert tide_permits(neutral, 1, now)
    unknown = replace(ctx, impulse_tide=[None] * len(ctx.impulse_tide))
    assert not tide_permits(unknown, 1, now)
    down = tide_ctx(DOWN, 'ema')
    against_short = replace(down, impulse_tide=[1] * len(down.impulse_tide))
    assert tide_permits(down, -1, now)
    assert not tide_permits(against_short, -1, now)


# ------------------------------------------------------------------ wave


def four_hour(rows):
    """rows: (close, high, low, volume) -> 4H bars from T0."""
    return [bar(T0 + i * STEP_4H, STEP_4H, c, h, l, c, v) for i, (c, h, l, v) in enumerate(rows)]


def flat_rows(n, volume=10.0):
    return [(100.0, 101.0, 99.0, volume)] * n


def wave_ctx(rows, **overrides):
    bars = four_hour(rows)
    return build_context([], bars, [], P(**overrides)), bars[-1].close_time


def staircase(down, n=6, base_rows=50):
    rows = flat_rows(base_rows)
    close = 100.0
    for _ in range(n):
        close += -2.0 if down else 2.0
        rows.append((close, close + 1, close - 1, 10.0))
    return rows


def test_stoch_long_and_short():
    ctx, now = wave_ctx(staircase(down=True), wave_osc='stoch', wave_threshold=20.0)
    assert wave_active(ctx, 1, now) and not wave_active(ctx, -1, now)
    ctx, now = wave_ctx(staircase(down=False), wave_osc='stoch', wave_threshold=20.0)
    assert wave_active(ctx, -1, now) and not wave_active(ctx, 1, now)
    tight, now = wave_ctx(staircase(down=True), wave_osc='stoch', wave_threshold=0.5)
    assert not wave_active(tight, 1, now)
    tight, now = wave_ctx(staircase(down=False), wave_osc='stoch', wave_threshold=0.5)
    assert not wave_active(tight, -1, now)


def test_stoch_mirrored_threshold_is_100_minus():
    # %K near 50 on a flat-range series: never extreme at threshold 20
    ctx, now = wave_ctx(flat_rows(30), wave_osc='stoch', wave_threshold=20.0)
    assert not wave_active(ctx, 1, now) and not wave_active(ctx, -1, now)
    # threshold 60: %K = 50 < 60 long, and 50 > 40 short
    ctx, now = wave_ctx(flat_rows(30), wave_osc='stoch', wave_threshold=60.0)
    assert wave_active(ctx, 1, now) and wave_active(ctx, -1, now)


def test_force2_long_and_short():
    rows = flat_rows(30) + [(96.0, 97.0, 95.0, 10.0)]
    ctx, now = wave_ctx(rows, wave_osc='force2', wave_threshold=1.0)
    assert wave_active(ctx, 1, now) and not wave_active(ctx, -1, now)
    ctx, now = wave_ctx(rows, wave_osc='force2', wave_threshold=1.5)
    assert not wave_active(ctx, 1, now)
    rows = flat_rows(30) + [(104.0, 105.0, 103.0, 10.0)]
    ctx, now = wave_ctx(rows, wave_osc='force2', wave_threshold=1.0)
    assert wave_active(ctx, -1, now) and not wave_active(ctx, 1, now)
    ctx, now = wave_ctx(rows, wave_osc='force2', wave_threshold=2.0)
    assert not wave_active(ctx, -1, now)


def test_force2_is_normalized_by_atr_and_volume():
    # Same price move, 10x the volume at the last bar: raw force scales with it,
    # but the SMA20 volume scales too only partly, so the score rises; with the
    # whole series 10x the volume the normalized value is unchanged.
    rows = flat_rows(30) + [(96.0, 97.0, 95.0, 10.0)]
    big = [(c, h, l, v * 10.0) for c, h, l, v in rows]
    a, now = wave_ctx(rows, wave_osc='force2', wave_threshold=1.0)
    b, _ = wave_ctx(big, wave_osc='force2', wave_threshold=1.0)
    assert wave_active(a, 1, now) == wave_active(b, 1, now) is True
    c, _ = wave_ctx(big, wave_osc='force2', wave_threshold=1.5)
    assert not wave_active(c, 1, now)


def test_elder_ray_long_and_short():
    base_rows = flat_rows(30)
    # long: low dives (bear power very negative) at A, then recovers at B (slope > 0)
    rows = base_rows + [(100.0, 101.0, 90.0, 10.0), (100.0, 101.0, 94.0, 10.0)]
    ctx, _ = wave_ctx(rows, wave_osc='elder_ray', wave_threshold=1.0)
    t_a = ctx.close_times_wave[-2]
    t_b = ctx.close_times_wave[-1]
    assert not wave_active(ctx, 1, t_a)  # bear power still falling
    assert wave_active(ctx, 1, t_b)
    assert not wave_active(ctx, -1, t_b)
    far, _ = wave_ctx(rows, wave_osc='elder_ray', wave_threshold=4.0)
    assert not wave_active(far, 1, t_b)  # |bear| = 6, ATR ~ 2.5 -> need 10
    # short mirrored
    rows = base_rows + [(100.0, 110.0, 99.0, 10.0), (100.0, 106.0, 99.0, 10.0)]
    ctx, _ = wave_ctx(rows, wave_osc='elder_ray', wave_threshold=1.0)
    assert not wave_active(ctx, -1, ctx.close_times_wave[-2])  # bull power still rising
    assert wave_active(ctx, -1, ctx.close_times_wave[-1])
    assert not wave_active(ctx, 1, ctx.close_times_wave[-1])


def test_wave_block_impulse():
    down = staircase(down=True)
    free, now = wave_ctx(down, wave_osc='stoch', wave_threshold=20.0, wave_block_impulse=False)
    blocked, _ = wave_ctx(down, wave_osc='stoch', wave_threshold=20.0, wave_block_impulse=True)
    assert free.impulse_wave[-1] == -1
    assert wave_active(free, 1, now)
    assert not wave_active(blocked, 1, now)
    up = staircase(down=False)
    free, now = wave_ctx(up, wave_osc='stoch', wave_threshold=20.0, wave_block_impulse=False)
    blocked, _ = wave_ctx(up, wave_osc='stoch', wave_threshold=20.0, wave_block_impulse=True)
    assert free.impulse_wave[-1] == 1
    assert wave_active(free, -1, now)
    assert not wave_active(blocked, -1, now)


def test_impulse_none_blocks_only_when_requested():
    rows = staircase(down=True, n=6, base_rows=4)  # 10 bars: stoch warm, impulse cold
    free, now = wave_ctx(rows, wave_osc='stoch', wave_threshold=20.0, wave_block_impulse=False)
    assert free.impulse_wave[-1] is None
    assert wave_active(free, 1, now)
    blocked, _ = wave_ctx(rows, wave_osc='stoch', wave_threshold=20.0, wave_block_impulse=True)
    assert not wave_active(blocked, 1, now)


@pytest.mark.parametrize('osc', ['force2', 'stoch', 'elder_ray'])
def test_cold_series_is_inactive(osc):
    ctx, now = wave_ctx(staircase(down=True, n=3, base_rows=1), wave_osc=osc, wave_threshold=60.0)
    assert not wave_active(ctx, 1, now) and not wave_active(ctx, -1, now)


def test_hole_makes_wave_inactive():
    rows = staircase(down=True)
    bars = four_hour(rows)
    del bars[-3]
    ctx = build_context([], bars, [], P(wave_osc='stoch', wave_threshold=20.0))
    assert ctx.bars_wave[len(rows) - 3] is None
    assert not wave_active(ctx, 1, ctx.close_times_wave[len(rows) - 3])  # the hole itself
    assert not wave_active(ctx, 1, ctx.close_times_wave[-1])  # stochastic restarted, still cold


def test_wave_none_when_atr_zero_for_force2():
    flat = [(100.0, 100.0, 100.0, 10.0)] * 40
    ctx, now = wave_ctx(flat, wave_osc='force2', wave_threshold=0.0)
    assert not wave_active(ctx, 1, now) and not wave_active(ctx, -1, now)


def test_wave_uses_only_closed_4h():
    rows = flat_rows(30) + [(96.0, 97.0, 95.0, 10.0)]
    ctx, now = wave_ctx(rows, wave_osc='force2', wave_threshold=1.0)
    assert wave_active(ctx, 1, now)
    # one microsecond before the last close the last bar is still in progress:
    # the decision is the one made on the bar before it, where nothing fired
    assert not wave_active(ctx, 1, now - timedelta(microseconds=1))
    assert not wave_active(ctx, 1, ctx.close_times_wave[-2])
    assert not wave_active(ctx, 1, T0 - timedelta(hours=1))  # before the first close


# ------------------------------------------------------------------ manual contexts


def manual_ctx(
    bars_ripple,
    params,
    *,
    wave_long=None,
    wave_short=None,
    slots=6,
    wave_bars=None,
    atr=2.0,
    tide_closes=None,
    tide=None,
    tide_impulse=None,
):
    """Hand-built context. Wave slot i closes at T0 + i * 4h, i.e. slot 0 closes at T0,
    so the first ripple bar at T0 sees slot 0 and ripple bar index 8 sees slot 1."""
    close_times = [T0 + i * STEP_4H for i in range(slots)]
    if wave_bars is None:
        wave_bars = [bar(c - STEP_4H, STEP_4H, 100, 110, 90, 100) for c in close_times]
    wl = wave_long if wave_long is not None else [False] * slots
    ws = wave_short if wave_short is not None else [False] * slots
    wk = tide_closes if tide_closes is not None else [T0 - timedelta(days=1)]
    td = tide if tide is not None else [1] * len(wk)
    wi = tide_impulse if tide_impulse is not None else [td_i or 0 for td_i in td]
    return SignalContext(
        params=params,
        bars_ripple=list(bars_ripple),
        bars_wave=list(wave_bars),
        close_times_wave=close_times,
        atr_wave=[atr] * slots,
        impulse_wave=[0] * slots,
        wave_long=list(wl),
        wave_short=list(ws),
        bars_tide=[None] * len(wk),
        close_times_tide=wk,
        tide_dir=list(td),
        impulse_tide=list(wi),
    )


def b30(index, o, h, l, c=None):
    """Ripple bar number ``index`` (0 opens at T0)."""
    opened = T0 + index * M30
    return bar(opened, M30, o, h, l, o if c is None else c)


def falling(start_index, count, high=99.0):
    """Bars whose high and low only fall: no long fill, no short fill is possible
    on a rising level (short fills need low < previous low, so they do fill)."""
    return [
        b30(start_index + i, high - 0.1 * i, high - 0.1 * i + 0.05, high - 0.1 * i - 0.05)
        for i in range(count)
    ]


def flat_inside(start_index, count):
    """Inside bars: each bar's high/low sit inside the previous bar's range."""
    return [b30(start_index + i, 100, 100.5 - 0.01 * i, 99.5 + 0.01 * i) for i in range(count)]


LONG_WAVE = [True] * 6


def run_long(bars, **p):
    params = P(**p)
    ctx = manual_ctx(bars, params, wave_long=LONG_WAVE)
    return generate_entries('BTCUSDT', ctx, params), ctx


def test_arm_on_first_bar_at_or_after_wave_close_not_before():
    # rising bars before the wave close would fill if an order existed
    before = [b30(-4 + i, 100 + i, 100.6 + i, 99.9 + i) for i in range(4)]  # -60..-15
    at = [b30(0, 104, 105, 103.9)]  # T0: previous high 103.6 -> breaks
    result, _ = run_long(before + at, ripple_expiry_bars=4)
    assert result.counts['arms'] == 1
    assert [e.fill_time for e in result.entries] == [T0]
    assert result.entries[0].armed_time == T0
    assert result.entries[0].fill_index == 4


def test_arm_waits_for_first_ripple_bar_after_close_when_bar_missing():
    # no bar opens exactly at T0; the first one at T0 + 1h (index 2) arms
    bars = [b30(-1, 100, 100.5, 99.5), b30(2, 100, 100.4, 99.6), b30(3, 100.1, 101, 99.9)]
    result, _ = run_long(bars)
    assert result.counts['arms'] == 1
    assert result.entries[0].armed_time == T0 + 2 * M30
    assert result.entries[0].fill_time == T0 + 3 * M30


def test_no_fill_before_first_wave_close_even_if_wave_slot_exists_later():
    bars = [b30(-3 + i, 100 + i, 100.6 + i, 99.9 + i) for i in range(3)]
    result, _ = run_long(bars)  # only bars before T0
    assert result.counts['arms'] == 0 and not result.entries


def test_fill_is_strict_above_previous_high():
    bars = [
        b30(-1, 100, 100.0, 99.0),  # previous high 100.0
        b30(0, 99.5, 100.0, 99.0),  # high == level: no fill
        b30(1, 99.5, 100.01, 99.0),  # level is 100.0 (previous bar high), above: fill
    ]
    result, _ = run_long(bars)
    assert result.counts['arms'] == 1
    assert [e.fill_time for e in result.entries] == [T0 + M30]
    assert result.entries[0].fill_price == 100.0  # max(open 99.5, level 100.0)


def test_gap_through_level_fills_at_open():
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 101.5, 102.0, 101.0)]
    result, _ = run_long(bars)
    assert result.entries[0].fill_price == 101.5


def test_short_fill_and_gap():
    params = P(ripple_expiry_bars=4)
    short_ctx = manual_ctx(
        [b30(-1, 100, 101.0, 100.0), b30(0, 99.0, 100.5, 98.5), b30(1, 100.0, 100.2, 99.9)],
        params,
        wave_short=[True] * 6,
        tide=[-1],
        tide_impulse=[-1],
    )
    result = generate_entries('X', short_ctx, params)
    assert [e.direction for e in result.entries] == [-1]
    assert result.entries[0].fill_price == 99.0  # gap down: min(open 99.0, level 100.0)
    assert result.entries[0].stop > result.entries[0].fill_price
    # strict: low == level does not fill, below does
    ctx = manual_ctx(
        [b30(-1, 100, 101.0, 100.0), b30(0, 100.5, 100.8, 100.0), b30(1, 100.2, 100.4, 99.99)],
        params,
        wave_short=[True] * 6,
        tide=[-1],
        tide_impulse=[-1],
    )
    result = generate_entries('X', ctx, params)
    assert [e.fill_time for e in result.entries] == [T0 + M30]
    assert result.entries[0].fill_price == 100.0  # min(open 100.2, level 100.0)


def test_expiry_after_exactly_n_bars_fill_on_last_bar():
    # armed at T0 (bar 0), fills allowed on bars 0, 1, 2 for N = 3
    seed = [b30(-1, 100, 100.0, 99.0)]
    inside = [b30(0, 99.5, 99.9, 99.0), b30(1, 99.5, 99.8, 99.0)]
    breakout = [b30(2, 99.5, 100.0, 99.0)]  # level = 99.8 -> breaks
    result, _ = run_long(seed + inside + breakout, ripple_expiry_bars=3)
    assert [e.fill_time for e in result.entries] == [T0 + 2 * M30]
    assert result.counts['expired'] == 0


def test_expiry_after_n_bars_no_fill_on_next():
    seed = [b30(-1, 100, 100.0, 99.0)]
    inside = [b30(0, 99.5, 99.9, 99.0), b30(1, 99.5, 99.8, 99.0), b30(2, 99.5, 99.7, 99.0)]
    late = [b30(3, 99.5, 100.0, 99.0)]  # would break 99.7, but the order is gone
    result, _ = run_long(seed + inside + late, ripple_expiry_bars=3)
    assert result.entries == []
    assert result.counts['expired'] == 1
    assert result.counts['arms'] == 1  # no re-arm on the same wave close
    assert result.counts['fills'] == 0


def test_expiry_counts_bars_not_time():
    # bar 1 missing; N = 3: bars 0, 2, 3 are the three tries
    seed = [b30(-1, 100, 100.0, 99.0)]
    bars = seed + [b30(0, 99.5, 99.9, 99.0), b30(2, 99.5, 99.8, 99.0), b30(3, 99.5, 100.0, 99.0)]
    result, _ = run_long(bars, ripple_expiry_bars=3)
    assert [e.fill_time for e in result.entries] == [T0 + 3 * M30]
    assert result.counts['expired'] == 0


def test_no_level_after_a_ripple_hole():
    seed = [b30(-1, 100, 100.0, 99.0)]
    bars = seed + [
        b30(0, 99.5, 99.9, 99.0),
        # bar 1 missing
        b30(2, 99.5, 101.0, 99.0),  # would break any level, but previous bar not adjacent
        b30(3, 99.5, 101.5, 99.0),  # level = 101.0, breaks
    ]
    result, _ = run_long(bars, ripple_expiry_bars=10)
    assert [e.fill_time for e in result.entries] == [T0 + 3 * M30]
    assert result.entries[0].fill_price == 101.0


def test_first_bar_of_data_has_no_level():
    bars = [b30(0, 99.5, 120.0, 99.0), b30(1, 99.0, 99.5, 98.5)]
    result, _ = run_long(bars)
    assert result.counts['arms'] == 1
    assert result.entries == []


def test_cancel_when_new_wave_close_deactivates_wave():
    seed = [b30(-1, 100, 100.0, 99.0)]
    bars = seed + [b30(0, 99.5, 99.9, 99.0)]
    # keep falling until the next wave close (T0 + 4h = bar 8), then break out
    bars += falling(1, PER_WAVE, high=99.5)  # bars 1 .. 8
    bars += [b30(PER_WAVE + 1, 99.0, 110.0, 98.0)]
    wave = [True, False, True, True, True, True]
    params = P(ripple_expiry_bars=1000)
    ctx = manual_ctx(bars, params, wave_long=wave)
    result = generate_entries('X', ctx, params)
    assert result.counts['cancelled'] == 1
    assert result.entries == []
    assert result.counts['arms'] == 1  # slot 2 (T0+8h) is not reached by the data


def test_cancel_happens_at_the_close_bar_not_before():
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 99.9, 99.0)]
    bars += falling(1, PER_WAVE - 1, high=99.5)  # bars 1 .. 7, still slot 0
    bars += [b30(PER_WAVE, 99.0, 99.0, 98.0)]  # first bar of slot 1: cancelled here
    params = P(ripple_expiry_bars=1000)
    ctx = manual_ctx(bars, params, wave_long=[True, False, True, True, True, True])
    result = generate_entries('X', ctx, params)
    assert result.counts['cancelled'] == 1
    assert result.counts['expired'] == 0


def test_wave_still_active_on_next_close_keeps_order_but_no_new_arm():
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 99.9, 99.0)]
    bars += falling(1, PER_WAVE, high=99.5)  # to bar 8 (slot 1 starts)
    params = P(ripple_expiry_bars=1000)
    ctx = manual_ctx(bars, params, wave_long=[True] * 6)
    result = generate_entries('X', ctx, params)
    assert result.counts['arms'] == 1
    assert result.counts['cancelled'] == 0


def test_cancel_on_tide_loss():
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 99.9, 99.0), b30(1, 99.5, 99.8, 99.0)]
    bars += [b30(2, 99.5, 99.7, 99.0)]  # the tide flips to 0 at bar 2 (visible at its open)
    bars += [b30(3, 99.5, 120.0, 99.0)]  # would fill
    params = P(ripple_expiry_bars=100)
    ctx = manual_ctx(
        bars,
        params,
        wave_long=LONG_WAVE,
        tide_closes=[T0 - timedelta(days=1), T0 + 2 * M30],
        tide=[1, 0],
        tide_impulse=[1, 0],
    )
    result = generate_entries('X', ctx, params)
    assert result.counts['cancelled'] == 1
    assert result.entries == []


def test_tide_flip_to_opposite_cancels_and_may_arm_short_at_same_close():
    # at slot 1 (T0 + 4h) the tide turns short and the short wave is on
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 99.9, 99.0)]
    bars += falling(1, PER_WAVE - 1, high=99.5)  # to bar 7
    bars += [b30(PER_WAVE, 99.0, 99.0, 98.0), b30(PER_WAVE + 1, 98.9, 98.95, 97.0)]
    params = P(ripple_expiry_bars=100)
    ctx = manual_ctx(
        bars,
        params,
        wave_long=[True, False, False, False, False, False],
        wave_short=[False, True, False, False, False, False],
        tide_closes=[T0 - timedelta(days=1), T0 + STEP_4H],
        tide=[1, -1],
        tide_impulse=[1, -1],
    )
    result = generate_entries('X', ctx, params)
    assert result.counts['cancelled'] == 1
    assert result.counts['arms'] == 2
    assert [e.direction for e in result.entries] == [-1]


def test_no_rearm_on_same_wave_close_after_fill():
    bars = [b30(-1, 100, 100.0, 99.0)] + [b30(i, 100 + i, 100.6 + i, 99.9 + i) for i in range(6)]
    result, _ = run_long(bars, ripple_expiry_bars=10)
    assert result.counts['arms'] == 1
    assert [e.fill_time for e in result.entries] == [T0]


def test_rearm_on_a_later_wave_close():
    bars = [b30(-1, 100, 100.0, 99.0)]
    bars += [b30(i, 100 + i * 0.1, 100.06 + i * 0.1, 99.9 + i * 0.1) for i in range(0, PER_WAVE + 2)]
    result, _ = run_long(bars, ripple_expiry_bars=10)
    assert result.counts['arms'] == 2
    assert [e.fill_time for e in result.entries] == [T0, T0 + STEP_4H]


def test_pending_order_blocks_a_new_arm_and_close_is_spent():
    # order pending at slot 1's close (wave still on); it expires later, no arm in the same slot
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 99.9, 99.0)]
    bars += falling(1, PER_WAVE, high=99.5)  # to bar 8 (slot 1)
    bars += falling(PER_WAVE + 1, 5, high=95.0)
    params = P(ripple_expiry_bars=PER_WAVE + 2)
    ctx = manual_ctx(bars, params, wave_long=LONG_WAVE)
    result = generate_entries('X', ctx, params)
    assert result.counts['arms'] == 1
    assert result.counts['expired'] == 1


def test_expiry_at_a_new_close_can_rearm_on_that_close():
    # N = 8 bars: tries bars 0 .. 7; expiry seen at bar 8, which is also slot 1's first bar
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 99.9, 99.0)]
    bars += falling(1, PER_WAVE, high=99.5)
    params = P(ripple_expiry_bars=PER_WAVE)
    ctx = manual_ctx(bars, params, wave_long=LONG_WAVE)
    result = generate_entries('X', ctx, params)
    assert result.counts['expired'] == 1
    assert result.counts['arms'] == 2


def test_both_directions_never_pending_together():
    params = P(ripple_expiry_bars=100)
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 99.9, 99.0)] + falling(1, 3)
    ctx = manual_ctx(bars, params, wave_long=LONG_WAVE, wave_short=LONG_WAVE)
    result = generate_entries('X', ctx, params)
    assert result.counts['arms'] == 1  # tide is long: only the long side arms


def test_generate_entries_checks_params_match_context():
    params = P(wave_threshold=20.0)
    ctx = manual_ctx([], params)
    with pytest.raises(ValueError):
        generate_entries('X', ctx, P(wave_threshold=25.0))
    generate_entries('X', ctx, P(wave_threshold=20.0, trail_atr=9.0))  # non-context field may differ


def test_build_context_rejects_bad_ripple_bars():
    a = b30(0, 100, 101, 99)
    b = b30(1, 100, 101, 99)
    with pytest.raises(ValueError):
        build_context([b, a], [], [], P())
    with pytest.raises(ValueError):
        build_context([a, a], [], [], P())
    off_grid = bar(T0 + timedelta(minutes=7), M30, 100, 101, 99, 100)
    with pytest.raises(ValueError):
        build_context([off_grid], [], [], P())
    quarter = bar(T0 + STEP_15M, M30, 100, 101, 99, 100)  # on the 15m grid, not on the 30m grid
    with pytest.raises(ValueError):
        build_context([quarter], [], [], P())
    naive = bar(datetime(2026, 1, 5), M30, 100, 101, 99, 100)
    with pytest.raises(ValueError):
        build_context([naive], [], [], P())


def test_context_close_times_and_alignment():
    bars = four_hour(flat_rows(10))
    del bars[4]
    ctx = build_context([], bars, [], P())
    assert len(ctx.bars_wave) == 10 and ctx.bars_wave[4] is None
    assert ctx.close_times_wave == [T0 + (i + 1) * WAVE_STEP for i in range(10)]
    assert len(ctx.atr_wave) == 10 and len(ctx.wave_long) == 10 and len(ctx.impulse_wave) == 10


# ------------------------------------------------------------------ stop


def make_wave_bars(rows):
    """rows: (low, high) per 4H bar; slot i closes at T0 + i * 4h."""
    return [bar(T0 + (i - 1) * STEP_4H, STEP_4H, 100, h, l, 100) for i, (l, h) in enumerate(rows)]


def stop_ctx(rows, atr=2.0):
    params = P()
    wave_bars = make_wave_bars(rows)
    return manual_ctx([], params, slots=len(rows), wave_bars=wave_bars, atr=atr)


def test_stop_long_value():
    ctx = stop_ctx([(90, 110), (85, 108), (92, 112), (95, 109)])
    params = P(stop_lookback_bars=3, stop_atr_buffer=0.5, min_stop_atr=0.5)
    # last three closed by slot 3 close: lows 85, 92, 95 -> 85 - 0.5 * 2 = 84
    stop, reason = stop_for(ctx, 1, 100.0, T0 + 3 * STEP_4H, params)
    assert reason is None and stop == 84.0
    # one bar earlier: slots 0..2 -> lows 90, 85, 92
    stop, reason = stop_for(ctx, 1, 100.0, T0 + 2 * STEP_4H, params)
    assert stop == 84.0
    # lookback 1: only the last closed bar
    stop, reason = stop_for(ctx, 1, 100.0, T0 + 3 * STEP_4H, P(stop_lookback_bars=1, stop_atr_buffer=0.5))
    assert stop == 94.0


def test_stop_short_value():
    ctx = stop_ctx([(90, 110), (85, 108), (92, 112), (95, 109)])
    params = P(stop_lookback_bars=3, stop_atr_buffer=0.5, min_stop_atr=0.5)
    stop, reason = stop_for(ctx, -1, 100.0, T0 + 3 * STEP_4H, params)
    assert reason is None and stop == 113.0  # max(108, 112, 109) + 1.0


def test_stop_uses_only_closed_bars():
    ctx = stop_ctx([(90, 110), (50, 150), (92, 112)])
    params = P(stop_lookback_bars=1, stop_atr_buffer=0.0)
    stop, _ = stop_for(ctx, 1, 100.0, T0 + 2 * STEP_4H - timedelta(microseconds=1), params)
    assert stop == 50.0  # slot 1 is the last closed
    stop, _ = stop_for(ctx, 1, 100.0, T0 + STEP_4H - timedelta(microseconds=1), params)
    assert stop == 90.0  # slot 0


def test_stop_reasons_in_order():
    params = P(stop_lookback_bars=1, stop_atr_buffer=0.0, min_stop_atr=1.0)
    ctx = stop_ctx([(90, 110)])
    t = T0
    # wrong side: stop (90) above the fill for a long
    assert stop_for(ctx, 1, 85.0, t, params) == (None, 'stop_wrong_side')
    # R == 0
    assert stop_for(ctx, 1, 90.0, t, params) == (None, 'stop_wrong_side')
    # short: stop 110 below the fill
    assert stop_for(ctx, -1, 115.0, t, params) == (None, 'stop_wrong_side')
    # too close in ATR: R = 1.5 < 1.0 * 2.0
    assert stop_for(ctx, 1, 91.5, t, params) == (None, 'stop_too_close_atr')
    assert stop_for(ctx, -1, 108.5, t, params) == (None, 'stop_too_close_atr')
    # costs: stop 200, ATR 2. R = fill - 200 passes the ATR floor from fill 202,
    # but 0.0012 * fill > 0.1 * R holds for fill < 202.43
    ctx_big = stop_ctx([(200, 300)], atr=2.0)
    assert stop_for(ctx_big, 1, 202.4, t, params) == (None, 'costs_over_cap')
    assert stop_for(ctx_big, 1, 202.5, t, params) == (200.0, None)
    # passes
    assert stop_for(ctx_big, 1, 220.0, t, params)[1] is None


def test_costs_over_cap_exact_boundary():
    params = P(stop_lookback_bars=1, stop_atr_buffer=0.0, min_stop_atr=0.0)
    ctx = stop_ctx([(100, 130)], atr=1.0)
    # R = fill - 100; costs <= cap  <=>  R >= COST * 100 / (MAX_COST_R - COST)
    r_min = COST_ROUND_TRIP * 100.0 / (MAX_COST_R - COST_ROUND_TRIP)
    stop, _ = stop_for(ctx, 1, 100.0 + r_min + 0.01, T0, params)
    assert stop == 100.0
    assert stop_for(ctx, 1, 100.0 + r_min - 0.01, T0, params) == (None, 'costs_over_cap')


def test_stop_no_wave_context():
    params = P(stop_lookback_bars=3, stop_atr_buffer=0.0, min_stop_atr=0.0)
    ctx = stop_ctx([(90, 110), (85, 108), (92, 112), (95, 109)])
    holey = replace(ctx, bars_wave=[ctx.bars_wave[0], None, ctx.bars_wave[2], ctx.bars_wave[3]])
    assert stop_for(holey, 1, 100.0, T0 + 3 * STEP_4H, params) == (None, 'no_wave_context')
    # the None is outside a window of 2 bars
    stop, reason = stop_for(holey, 1, 100.0, T0 + 3 * STEP_4H, P(stop_lookback_bars=2, stop_atr_buffer=0.0, min_stop_atr=0.0))
    assert reason is None and stop == 92.0
    # fewer bars than the window
    assert stop_for(ctx, 1, 100.0, T0 + STEP_4H, params) == (None, 'no_wave_context')
    # before any closed bar
    assert stop_for(ctx, 1, 100.0, T0 - timedelta(hours=1), params) == (None, 'no_wave_context')
    # cold ATR
    cold = replace(ctx, atr_wave=[None] * 4)
    assert stop_for(cold, 1, 100.0, T0 + 3 * STEP_4H, params) == (None, 'no_wave_context')


def test_stop_direction_validated():
    ctx = stop_ctx([(90, 110)])
    with pytest.raises(ValueError):
        stop_for(ctx, 0, 100.0, T0, P())


def test_generate_entries_records_drops_by_reason():
    # ATR 2.0 bar 90..110: long fill ~100 gives R = 10 -> passes; use a tight 4H bar to drop
    tight_wave = [bar(T0 - STEP_4H + i * STEP_4H, STEP_4H, 100, 100.2, 99.9, 100) for i in range(6)]
    params = P(stop_lookback_bars=1, stop_atr_buffer=0.0, min_stop_atr=0.0)
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 100.5, 99.0)]
    ctx = manual_ctx(bars, params, wave_long=LONG_WAVE, wave_bars=tight_wave)
    result = generate_entries('X', ctx, params)
    assert result.entries == []
    assert result.counts['fills'] == 1 and result.counts['entries'] == 0
    assert result.counts['dropped_costs_over_cap'] == 1
    assert [d.reason for d in result.dropped] == ['costs_over_cap']
    assert isinstance(result.dropped[0], Dropped)
    assert result.dropped[0].fill_price == 100.0
    # a dropped fill empties the order: no second try in the same wave close
    assert result.counts['arms'] == 1


def test_entry_fields_and_counts_keys():
    bars = [b30(-1, 100, 100.0, 99.0), b30(0, 99.5, 100.5, 99.0)]
    params = P(stop_lookback_bars=1, stop_atr_buffer=0.5, min_stop_atr=0.5)
    ctx = manual_ctx(bars, params, wave_long=LONG_WAVE, atr=2.0)
    result = generate_entries('ETHUSDT', ctx, params)
    entry = result.entries[0]
    assert isinstance(entry, Entry) and isinstance(result, SignalResult)
    assert entry.coin == 'ETHUSDT' and entry.direction == 1
    assert entry.fill_price == 100.0 and entry.stop == 89.0 and entry.r == 11.0
    assert entry.atr_wave == 2.0
    assert entry.fill_index == 1
    assert entry.wave_close_time == T0
    assert entry.tide_close_time == T0 - timedelta(days=1)
    for key in (
        'arms',
        'fills',
        'expired',
        'cancelled',
        'dropped_stop_wrong_side',
        'dropped_stop_too_close_atr',
        'dropped_costs_over_cap',
        'dropped_no_wave_context',
        'entries',
    ):
        assert key in result.counts


# ------------------------------------------------------------------ synthetic data, no lookahead

WEEKS = 62
SYNTH_PARAMS = P(
    tide_mode='ema',
    wave_osc='stoch',
    wave_threshold=30.0,
    wave_block_impulse=False,
    ripple_expiry_bars=3,
    stop_lookback_bars=3,
    stop_atr_buffer=0.5,
    min_stop_atr=0.5,
)
SYNTH_START = datetime(2025, 1, 6, tzinfo=UTC)  # Monday


@lru_cache(maxsize=1)
def synthetic():
    rng = random.Random(20260117)
    hole_rng = random.Random(99)
    price = 100.0
    drift = 0.0
    full = []
    for i in range(WEEKS * 672):
        drift = 0.998 * drift + rng.gauss(0.0, 0.00001)
        change = drift + rng.gauss(0.0, 0.0035)
        opened = price
        closed = opened * math.exp(change)
        high = max(opened, closed) * (1.0 + abs(rng.gauss(0.0, 0.0015)))
        low = min(opened, closed) * (1.0 - abs(rng.gauss(0.0, 0.0015)))
        volume = 100.0 * rng.uniform(0.5, 1.5)
        start = SYNTH_START + i * STEP_15M
        full.append(OHLCV(start, start + STEP_15M, opened, high, low, closed, volume))
        price = closed
    signal_15m = [b for i, b in enumerate(full) if i == 0 or hole_rng.random() > 0.004]
    assert len(signal_15m) < len(full)  # the 15m input really has holes
    return full, signal_15m


def regroup(full, per, step, upto=None):
    """Own resampling helper: 15m -> 4h (per 16), whole groups only."""
    out = []
    usable = [b for b in full if upto is None or b.close_time <= upto]
    for i in range(0, len(usable) - per + 1, per):
        group = usable[i : i + per]
        out.append(
            OHLCV(
                group[0].open_time,
                group[0].open_time + step,
                group[0].open,
                max(b.high for b in group),
                min(b.low for b in group),
                group[-1].close,
                math.fsum(b.volume for b in group),
            )
        )
    return out


def build(upto=None, mutate_wave=None, mutate_tide=None, params=SYNTH_PARAMS):
    full, signal_15m = synthetic()
    bars_ripple = to_30m([b for b in signal_15m if upto is None or b.close_time <= upto])
    bars_4h = regroup(full, 16, STEP_4H, upto)
    bars_wave = bars_4h
    bars_tide = to_1d(bars_4h)
    if mutate_wave is not None:
        bars_wave = mutate_wave(bars_wave)
    if mutate_tide is not None:
        bars_tide = mutate_tide(bars_tide)
    return build_context(bars_ripple, bars_wave, bars_tide, params)


@lru_cache(maxsize=1)
def full_result():
    ctx = build()
    return ctx, generate_entries('SYN', ctx, SYNTH_PARAMS)


def test_synthetic_dataset_is_not_vacuous():
    ctx, result = full_result()
    counts = result.counts
    print('synthetic counts', counts, 'bars_ripple', len(ctx.bars_ripple))
    assert len(result.entries) >= 8
    assert counts['arms'] >= counts['fills'] >= counts['entries'] >= 8
    assert counts['expired'] >= 1
    assert {e.direction for e in result.entries} == {1, -1}
    assert counts['entries'] == len(result.entries)
    assert counts['fills'] == counts['entries'] + sum(
        counts[k] for k in counts if k.startswith('dropped_')
    )
    for e in result.entries:
        assert e.r > 0
        assert e.stop < e.fill_price if e.direction > 0 else e.stop > e.fill_price
        assert ctx.bars_ripple[e.fill_index].open_time == e.fill_time


def test_every_entry_uses_only_closed_bars():
    ctx, result = full_result()
    assert result.entries
    for e in result.entries:
        assert e.wave_close_time <= e.fill_time
        assert e.tide_close_time <= e.fill_time
        assert e.armed_time <= e.fill_time
        # the very next wave/tide close after the one used is still in the future
        later_wave = [c for c in ctx.close_times_wave if c > e.wave_close_time]
        later_tide = [c for c in ctx.close_times_tide if c > e.tide_close_time]
        assert not later_wave or later_wave[0] > e.fill_time
        assert not later_tide or later_tide[0] > e.fill_time


def cut_points(result):
    """30m-aligned cut times: mid-4H, mid-day, exact boundaries, around fills."""
    entries = result.entries
    anchors = [entries[len(entries) * k // 8].fill_time for k in range(1, 8)]
    points = []
    for anchor in anchors:
        day = anchor.replace(hour=0, minute=0)
        points.append(anchor + timedelta(hours=2, minutes=30))  # usually mid 4H bar
        points.append(day + timedelta(hours=13, minutes=30))  # mid day, mid 4H
        points.append(day + timedelta(hours=23, minutes=30))  # last 30m of a day
        points.append(day + STEP_1D)  # exact day close
        points.append(anchor.replace(hour=(anchor.hour // 4) * 4, minute=0) + STEP_4H)  # exact 4H close
        points.append(anchor)  # the fill bar itself is cut away
        points.append(anchor + RIPPLE_STEP)  # the fill bar is the last one kept
    return sorted(set(points))


def key(e: Entry):
    return (e.direction, e.armed_time, e.fill_time, e.fill_index, e.fill_price, e.stop, e.r, e.atr_wave,
            e.wave_close_time, e.tide_close_time)


def test_truncation_invariance():
    _, full = full_result()
    points = cut_points(full)
    assert len(points) >= 10
    checked_nonempty = 0
    for T in points:
        ctx = build(upto=T)
        cut = generate_entries('SYN', ctx, SYNTH_PARAMS)
        expected = [key(e) for e in full.entries if e.fill_time < T]
        got = [key(e) for e in cut.entries if e.fill_time < T]
        assert got == expected, f'entries differ for T={T}'
        assert not [e for e in cut.entries if e.fill_time >= T]
        checked_nonempty += bool(expected)
    assert checked_nonempty >= 8


def test_truncation_invariance_also_for_drops_and_counts_prefix():
    _, full = full_result()
    assert full.dropped
    T = full.dropped[len(full.dropped) // 2].fill_time + RIPPLE_STEP
    cut = generate_entries('SYN', build(upto=T), SYNTH_PARAMS)
    assert [(d.fill_time, d.reason) for d in cut.dropped if d.fill_time < T] == [
        (d.fill_time, d.reason) for d in full.dropped if d.fill_time < T
    ]


def in_progress(bars, instant):
    for index, b in enumerate(bars):
        if b.open_time <= instant < b.close_time:
            return index
    raise AssertionError('no bar in progress')


def distorted(b: OHLCV) -> OHLCV:
    return OHLCV(b.open_time, b.close_time, b.open, b.high * 1.6, b.low * 0.4, b.close * 1.3, b.volume * 7.0)


def probe_times(full, shares=(1, 2, 3, 4, 5, 6, 7)):
    entries = full.entries
    return [entries[len(entries) * k // 8].fill_time + timedelta(minutes=90) for k in shares]


def test_mutating_the_4h_bar_in_progress_changes_nothing_before_its_close():
    _, full = full_result()
    full_ctx = build()
    changed_after = 0
    times = probe_times(full)
    for T in times:
        index = in_progress(full_ctx.bars_wave, T)
        target = full_ctx.bars_wave[index]
        assert target is not None

        def mutate(bars, target=target):
            return [distorted(b) if b.open_time == target.open_time else b for b in bars]

        mutated = generate_entries('SYN', build(mutate_wave=mutate), SYNTH_PARAMS)
        before = target.close_time
        expected = [key(e) for e in full.entries if e.fill_time < before]
        assert expected, 'non-vacuous prefix'
        assert [key(e) for e in mutated.entries if e.fill_time < before] == expected
        changed_after += [key(e) for e in mutated.entries] != [key(e) for e in full.entries]
    assert len(times) == 7
    assert changed_after >= 1  # the distortion is visible once the bar is closed


def test_mutating_the_day_in_progress_changes_nothing_before_its_close():
    _, full = full_result()
    full_ctx = build()
    changed_after = 0
    for T in probe_times(full):
        index = in_progress(full_ctx.bars_tide, T)
        target = full_ctx.bars_tide[index]
        assert target is not None

        def mutate(bars, target=target):
            return [distorted(b) if b.open_time == target.open_time else b for b in bars]

        mutated = generate_entries('SYN', build(mutate_tide=mutate), SYNTH_PARAMS)
        before = target.close_time
        expected = [key(e) for e in full.entries if e.fill_time < before]
        assert expected, 'non-vacuous prefix'
        assert [key(e) for e in mutated.entries if e.fill_time < before] == expected
        changed_after += [key(e) for e in mutated.entries] != [key(e) for e in full.entries]
    assert changed_after >= 1


def test_mutating_both_in_progress_bars_at_once():
    _, full = full_result()
    full_ctx = build()
    T = probe_times(full, shares=(4,))[0]
    t6 = full_ctx.bars_wave[in_progress(full_ctx.bars_wave, T)]
    tw = full_ctx.bars_tide[in_progress(full_ctx.bars_tide, T)]

    def m6(bars):
        return [distorted(b) if b.open_time == t6.open_time else b for b in bars]

    def mw(bars):
        return [distorted(b) if b.open_time == tw.open_time else b for b in bars]

    mutated = generate_entries('SYN', build(mutate_wave=m6, mutate_tide=mw), SYNTH_PARAMS)
    before = t6.close_time
    expected = [key(e) for e in full.entries if e.fill_time < before]
    assert expected
    assert [key(e) for e in mutated.entries if e.fill_time < before] == expected


@pytest.mark.parametrize('mode,osc', [('macd', 'force2'), ('both', 'elder_ray')])
def test_truncation_invariance_other_modes(mode, osc):
    params = replace(SYNTH_PARAMS, tide_mode=mode, wave_osc=osc, wave_threshold=0.2, wave_block_impulse=True)
    full_ctx = build(params=params)
    full = generate_entries('SYN', full_ctx, params)
    print('counts', mode, osc, full.counts)
    assert len(full.entries) >= 4
    for T in (full.entries[len(full.entries) // 3].fill_time + timedelta(hours=2, minutes=30),
              full.entries[2 * len(full.entries) // 3].fill_time + RIPPLE_STEP):
        cut = generate_entries('SYN', build(upto=T, params=params), params)
        assert [key(e) for e in cut.entries if e.fill_time < T] == [
            key(e) for e in full.entries if e.fill_time < T
        ]
        assert not [e for e in cut.entries if e.fill_time >= T]
