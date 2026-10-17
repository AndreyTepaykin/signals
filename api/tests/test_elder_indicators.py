"""Hand-computed golden series for the Elder indicators.

A hole is NULL, not a bridge: every indicator below is checked cold (None, not
0), then against a short series worked by hand, then against the same series
cut by a None so it has to warm again.
"""

from datetime import datetime, timezone

import pytest

from app.core.features.series import Bar
from app.core.features.volatility import true_ranges
from app.core.features.volatility import wilder_atr as core_wilder_atr
from app.core.features.volatility import wilder_atr_from_ranges
from app.research.elder.indicators import (
    elder_ray,
    ema,
    force_index,
    impulse,
    macd_hist,
    ohlcv_fields,
    slope,
    stochastic,
    wilder_atr,
)
from app.research.elder.resample import STEP_1H, OHLCV

UTC = timezone.utc


def utc(year, month, day, hour=0):
    return datetime(year, month, day, hour, tzinfo=UTC)


def assert_series(got, expected):
    assert len(got) == len(expected)
    for index, (actual, wanted) in enumerate(zip(got, expected)):
        if wanted is None:
            assert actual is None, index
        else:
            assert actual == pytest.approx(wanted), index


# --- EMA and slope ---------------------------------------------------------
def test_ema_seed_and_recursion():
    # period 3, alpha 1/2. Seed at index 2 is SMA(10, 12, 11) = 11.
    # index 3: 0.5*13 + 0.5*11 = 12. index 4: 0.5*15 + 0.5*12 = 13.5.
    assert_series(ema([10, 12, 11, 13, 15], 3), [None, None, 11, 12, 13.5])


def test_ema_none_resets_and_rewarms():
    # Same seed 11, then a hole. The new run (13, 15, 14) seeds at its own SMA 14,
    # not at a step from the old 11.
    assert_series(ema([10, 12, 11, None, 13, 15, 14], 3), [None, None, 11, None, None, None, 14])


def test_ema_cold_is_none_not_zero():
    assert ema([1.0, 2.0], 3) == [None, None]
    assert ema([], 3) == []
    assert ema([None, None], 2) == [None, None]


def test_slope_is_the_first_difference_and_does_not_cross_a_hole():
    assert_series(slope([None, 11.0, 12.0, None, 14.0]), [None, None, 1.0, None, None])
    assert slope([5.0]) == [None]


def test_period_must_be_positive():
    with pytest.raises(ValueError):
        ema([1.0, 2.0, 3.0], 0)
    with pytest.raises(ValueError):
        macd_hist([1.0], fast=0)
    with pytest.raises(ValueError):
        force_index([1.0], [1.0], period=-1)
    with pytest.raises(ValueError):
        stochastic([1.0], [1.0], [1.0], period=5, smooth=0)
    with pytest.raises(ValueError):
        wilder_atr([1.0], [1.0], [1.0], period=0)


# --- MACD histogram --------------------------------------------------------
def test_macd_hist_golden_value_and_first_index():
    # fast 3, slow 5, signal 3. First histogram at index slow+signal-2 = 6.
    # EMA(3) alpha 1/2: 11, 12, 27/2, 55/4, 119/8 from index 2.
    # EMA(5) alpha 1/3: 61/5, 64/5, 208/15 from index 4.
    # MACD at index 6 is 121/120. Signal seed is SMA of the first three MACD
    # values (13/10, 19/20, 121/120) = 391/360.
    # Histogram = 121/120 - 391/360 = -7/90.
    closes = [10, 12, 11, 13, 15, 14, 16]
    hist = macd_hist(closes, fast=3, slow=5, signal=3)
    assert len(hist) == len(closes)
    assert all(value is None for value in hist[:6])
    assert hist[6] == pytest.approx(-7 / 90)


def test_macd_default_periods_first_histogram_is_the_34th_bar():
    # 12/26/9: index 26+9-2 = 33, the 34th bar. A constant series has EMA = 5,
    # so the histogram is a real 0, and everything before it is cold None.
    closes = [5.0] * 40
    hist = macd_hist(closes)
    assert len(hist) == 40
    assert all(value is None for value in hist[:33])
    assert hist[33] == 0.0
    assert hist[39] == 0.0
    assert all(value is None for value in macd_hist(closes[:33]))
    assert macd_hist(closes[:34])[33] == 0.0


def test_macd_hist_none_resets_and_rewarms():
    golden = [10, 12, 11, 13, 15, 14, 16]
    hist = macd_hist(golden + [None] + golden, fast=3, slow=5, signal=3)
    assert len(hist) == 15
    assert hist[6] == pytest.approx(-7 / 90)
    assert hist[7] is None
    assert all(value is None for value in hist[8:14])
    assert hist[14] == pytest.approx(-7 / 90)


# --- Force Index -----------------------------------------------------------
def test_force_index_golden():
    # raw: undefined, (12-10)*200 = 400, (11-12)*50 = -50, (13-11)*150 = 300.
    # EMA(2) alpha 2/3. Seed at index 2 is (400-50)/2 = 175.
    # index 3: (2/3)*300 + (1/3)*175 = 775/3.
    got = force_index([10, 12, 11, 13], [100, 200, 50, 150], period=2)
    assert_series(got, [None, None, 175, 775 / 3])


def test_force_index_unchanged_price_is_a_real_zero():
    assert_series(force_index([10, 10, 10], [5, 5, 5], period=2), [None, None, 0.0])


def test_force_index_none_resets_and_rewarms():
    # First run seeds 175 at index 2. After the hole the new raw values are
    # undefined, then (15-13)*100 = 200, (14-15)*80 = -80. Seed (200-80)/2 = 60.
    closes = [10, 12, 11, None, 13, 15, 14]
    volumes = [100, 200, 50, None, 150, 100, 80]
    assert_series(force_index(closes, volumes, period=2), [None, None, 175, None, None, None, 60])


def test_force_index_rejects_a_length_mismatch():
    with pytest.raises(ValueError):
        force_index([1.0, 2.0], [1.0])


# --- Stochastic ------------------------------------------------------------
def test_stochastic_golden():
    # period 5, smooth 3. Raw %K from index 4:
    #   250/3, 250/3, 200/3, 250/3.
    # Slow %K is the SMA of three raw values, first at index 6: 700/9.
    highs = [10, 12, 11, 13, 14, 15, 14, 16]
    lows = [8, 9, 9, 10, 11, 12, 12, 13]
    closes = [9, 11, 10, 12, 13, 14, 13, 15]
    assert_series(stochastic(highs, lows, closes, period=5, smooth=3), [None] * 6 + [700 / 9, 700 / 9])


def test_stochastic_flat_range_does_not_reset_price_history():
    # period 3, smooth 2. Indices 2, 3, 4 are flat, so raw %K is None there.
    # Index 5 looks back through the flat bars and is 50; index 6 is 75.
    # If the flat bar had reset the window, index 5 would still be cold.
    # Slow %K stays None while the flat raw value is inside the SMA window
    # (index 5) and resumes at 62.5 once it has slid out (index 6).
    highs = [5, 5, 5, 5, 5, 6, 7]
    lows = [5, 5, 5, 5, 5, 4, 3]
    closes = [5, 5, 5, 5, 5, 5, 6]
    assert_series(stochastic(highs, lows, closes, period=3, smooth=2), [None] * 6 + [62.5])


def test_stochastic_none_resets_and_rewarms():
    # smooth 1, so slow %K equals raw %K. The hole throws the window away:
    # index 4 and 5 are cold, and index 6 repeats the pre-hole value 75.
    highs = [10, 11, 12, None, 10, 11, 12]
    lows = [8, 9, 10, None, 8, 9, 10]
    closes = [9, 10, 11, None, 9, 10, 11]
    got = stochastic(highs, lows, closes, period=3, smooth=1)
    assert_series(got, [None, None, 75, None, None, None, 75])


# --- Elder-ray -------------------------------------------------------------
def test_elder_ray_golden():
    # EMA(3) of closes: seed 4 at index 2, then 0.5*6 + 0.5*4 = 5.
    # Bull 6-4 = 2, 8-5 = 3. Bear 3-4 = -1, 4-5 = -1.
    bull, bear = elder_ray([5, 7, 6, 8], [1, 2, 3, 4], [3, 4, 5, 6], period=3)
    assert_series(bull, [None, None, 2, 3])
    assert_series(bear, [None, None, -1, -1])


def test_elder_ray_none_resets_and_rewarms():
    # A missing high alone is a hole, even though the close at that bar exists.
    # The new run (closes 6, 7, 8) seeds at 7. Bull 10-7 = 3, bear 6-7 = -1.
    highs = [5, 7, 6, None, 8, 9, 10]
    lows = [1, 2, 3, 4, 4, 5, 6]
    closes = [3, 4, 5, 6, 6, 7, 8]
    bull, bear = elder_ray(highs, lows, closes, period=3)
    assert_series(bull, [None, None, 2, None, None, None, 3])
    assert_series(bear, [None, None, -1, None, None, None, -1])


# --- Impulse ---------------------------------------------------------------
def test_impulse_positive_negative_zero_and_none():
    # ema 3, fast 3, slow 5, signal 3. Both slopes exist from index 7.
    # Accelerating up: both slopes positive -> +1.
    # The same path reflected (30 - close) flips both slopes -> -1.
    # A straight line keeps the EMA rising but the histogram flat -> 0.
    # A rise whose histogram turns down at the end -> 0, slopes disagree.
    up = [1, 2, 3, 4, 5, 6, 8, 11, 15, 20]
    down = [30 - value for value in up]
    flat_hist = list(range(1, 11))
    disagree = [1, 2, 3, 4, 5, 6, 10, 8]

    rising = impulse(up, ema_period=3, fast=3, slow=5, signal=3)
    falling = impulse(down, ema_period=3, fast=3, slow=5, signal=3)
    zero_slope = impulse(flat_hist, ema_period=3, fast=3, slow=5, signal=3)
    split = impulse(disagree, ema_period=3, fast=3, slow=5, signal=3)

    assert all(value is None for value in rising[:7])
    assert rising[7:] == [1, 1, 1]
    assert all(type(value) is int for value in rising[7:])
    assert all(value is None for value in falling[:7])
    assert falling[7:] == [-1, -1, -1]
    assert all(value is None for value in zero_slope[:7])
    assert zero_slope[7:] == [0, 0, 0]
    assert all(value is None for value in split[:7])
    assert split[7] == 0


def test_impulse_none_resets_and_rewarms():
    up = [1, 2, 3, 4, 5, 6, 8, 11]
    got = impulse(up + [None] + up, ema_period=3, fast=3, slow=5, signal=3)
    assert len(got) == 17
    assert got[7] == 1
    assert all(value is None for value in got[8:16])
    assert got[16] == 1
    assert 0 not in got[:16]


# --- ATR -------------------------------------------------------------------
def test_wilder_atr_seed_and_recursion():
    # TR from the second bar: 3, 4, 3, 3. period 3.
    # Seed at index 3 is (3+4+3)/3 = 10/3.
    # index 4: (10/3 * 2 + 3) / 3 = 29/9.
    highs = [10, 12, 11, 13, 14]
    lows = [8, 9, 7, 10, 11]
    closes = [9, 11, 10, 12, 13]
    assert_series(wilder_atr(highs, lows, closes, period=3), [None, None, None, 10 / 3, 29 / 9])


def test_wilder_atr_none_resets_and_rewarms():
    highs = [10, 12, 11, 13, 14]
    lows = [8, 9, 7, 10, 11]
    closes = [9, 11, 10, 12, 13]
    got = wilder_atr(highs + [None] + highs, lows + [None] + lows, closes + [None] + closes, period=3)
    assert_series(got, [None, None, None, 10 / 3, 29 / 9, None, None, None, None, 10 / 3, 29 / 9])


def test_wilder_atr_flat_is_zero_not_cold():
    # Computed ATR of a market that never moved is 0. Core's public wrapper
    # reports that as missing; the shared seed/recursion helper returns 0.
    highs = [5.0] * 6
    series = wilder_atr(highs, highs, highs, period=3)
    assert series[:3] == [None, None, None]
    assert series[3:] == [0.0, 0.0, 0.0]
    start = utc(2026, 1, 5)
    bars = [Bar.make(start + i * STEP_1H, STEP_1H, 5, 5, 5, 5) for i in range(6)]
    reading = core_wilder_atr(bars, STEP_1H, bars[-1].close_time, period=3, history_bars=10)
    assert not reading.ok
    assert wilder_atr_from_ranges(true_ranges(bars), 3) == 0.0


def test_wilder_atr_matches_core_seed_and_recursion():
    period = 14
    start = utc(2026, 1, 1)
    highs, lows, closes, bars = [], [], [], []
    for i in range(30):
        high = 100 + (i % 7) * 1.5 + i * 0.25
        low = high - (2 + (i % 4) * 0.5)
        close = low + (high - low) * ((i % 5) + 1) / 6
        highs.append(high)
        lows.append(low)
        closes.append(close)
        bars.append(Bar.make(start + i * STEP_1H, STEP_1H, close, high, low, close, volume=1.0))
    series = wilder_atr(highs, lows, closes, period)
    ranges = true_ranges(bars)
    for index in range(len(highs)):
        expected = wilder_atr_from_ranges(ranges[:index], period)
        if expected is None:
            assert series[index] is None
        else:
            assert series[index] == pytest.approx(expected)
    reading = core_wilder_atr(bars, STEP_1H, bars[-1].close_time, period=period, history_bars=len(bars))
    assert reading.ok
    assert series[-1] == pytest.approx(reading.value)


def test_wilder_atr_hole_restarts_like_core_contiguous_suffix():
    period = 5
    start = utc(2026, 1, 1)

    def path(origin, count, scale):
        highs, lows, closes, bars = [], [], [], []
        for i in range(count):
            high = scale + i * 0.7 + (i % 3)
            low = high - 1.2 - (i % 2) * 0.4
            close = (high + low) / 2
            highs.append(high)
            lows.append(low)
            closes.append(close)
            opened = origin + i * STEP_1H
            bars.append(Bar.make(opened, STEP_1H, close, high, low, close, volume=1.0))
        return highs, lows, closes, bars

    pre_h, pre_l, pre_c, pre_bars = path(start, 8, 50)
    # One missing hour, then a fresh run. Core sees the gap and drops the prefix.
    suf_h, suf_l, suf_c, suf_bars = path(start + 9 * STEP_1H, 8, 80)
    series = wilder_atr(pre_h + [None] + suf_h, pre_l + [None] + suf_l, pre_c + [None] + suf_c, period)
    assert series[7] == pytest.approx(wilder_atr_from_ranges(true_ranges(pre_bars), period))
    assert series[8] is None
    assert series[-1] == pytest.approx(wilder_atr_from_ranges(true_ranges(suf_bars), period))
    gapped = pre_bars + suf_bars
    reading = core_wilder_atr(gapped, STEP_1H, gapped[-1].close_time, period=period, history_bars=100)
    assert reading.ok
    assert reading.value == pytest.approx(series[-1])


# --- length, cold values, field split --------------------------------------
def test_every_indicator_keeps_the_input_length_and_stays_cold_across_a_hole():
    closes = [1.0, 2.0, 3.0, None, 4.0]
    highs = [2.0, 3.0, 4.0, None, 5.0]
    lows = [0.0, 1.0, 2.0, None, 3.0]
    volumes = [1.0, 1.0, 1.0, None, 1.0]
    bull, bear = elder_ray(highs, lows, closes, period=2)
    series = [
        ema(closes, 2),
        slope(closes),
        macd_hist(closes, fast=2, slow=3, signal=2),
        force_index(closes, volumes, period=2),
        stochastic(highs, lows, closes, period=2, smooth=2),
        bull,
        bear,
        impulse(closes, ema_period=2, fast=2, slow=3, signal=2),
        wilder_atr(highs, lows, closes, period=2),
    ]
    for got in series:
        assert len(got) == len(closes)
        assert got[3] is None


def test_ohlcv_fields_preserves_holes():
    opened = utc(2026, 1, 5)
    bar = OHLCV(opened, opened + STEP_1H, 1.0, 3.0, 0.5, 2.0, 8.0)
    opens, highs, lows, closes, volumes = ohlcv_fields([bar, None])
    assert opens == [1.0, None]
    assert highs == [3.0, None]
    assert lows == [0.5, None]
    assert closes == [2.0, None]
    assert volumes == [8.0, None]
    assert ema(closes, 2) == [None, None]
