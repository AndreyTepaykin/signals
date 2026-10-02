"""Golden fixtures for the feature math.

Every expected value in this file was worked out BY HAND from the definition on a
series small enough to follow on paper, and the working is in the comment above
the assertion. None was produced by running the code under test. Where a
transcendental function is unavoidable (Yang-Zhang), the logarithms are exact
combinations of ln 2, ln 3 and ln 5 and the arithmetic was done with a calculator
that has nothing to do with the engine.

Constants (periods, bands, bins) are passed explicitly where the production value
would need 20+ bars, so the fixtures stay hand-checkable; separate tests pin the
production values to the spec.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from app.core import constants as C
from app.core.features import cross_asset, indicators, levels, liquidation, positioning, stats, trend, volatility
from app.core.features import params as P
from app.core.features.params import HistoryPolicy
from app.core.features.series import Bar, ForcePrint, FundingPrint, OIPoint, Sample
from feature_helpers import H1, H4, M15, bars_from_rows, dt, flat_bars

# A policy that lets tiny fixtures speak; the production policy is tested separately.
TINY = HistoryPolicy(window_days=90, min_span_days=0, min_samples=3)
NOW = dt(2030, 1, 1)


def approx(value, expected, rel=1e-9, abs_=1e-12):
    return value == pytest.approx(expected, rel=rel, abs=abs_)


# ---------------------------------------------------------------------------
# ATR (Wilder), period 3
#
#   bar : H   L   C        TR_i = max(H-L, |H-Cprev|, |L-Cprev|)
#    0  : 12   8  10       (no previous close, no TR)
#    1  : 13   9  12       max(4, 3, 1) = 4
#    2  : 16  11  15       max(5, 4, 1) = 5
#    3  : 15  12  13       max(3, 0, 3) = 3
#    4  : 17  10  16       max(7, 4, 3) = 7
#    5  : 14   9  10       max(5, 2, 7) = 7
#
#   seed ATR (after bar 3) = (4 + 5 + 3) / 3 = 4
#   bar 4: (4 * 2 + 7) / 3 = 5
#   bar 5: (5 * 2 + 7) / 3 = 17/3
# ---------------------------------------------------------------------------
ATR_ROWS = [
    (10, 12, 8, 10),
    (10, 13, 9, 12),
    (12, 16, 11, 15),
    (15, 15, 12, 13),
    (13, 17, 10, 16),
    (12, 14, 9, 10),
]


def atr_bars(count: int):
    return bars_from_rows(dt(2026, 1, 1), H4, [(o, max(h, o, c), min(l, o, c), c) for o, h, l, c in ATR_ROWS[:count]])


def test_true_ranges_by_hand():
    assert volatility.true_ranges(atr_bars(6)) == [4, 5, 3, 7, 7]


@pytest.mark.parametrize('count, expected', [(4, 4.0), (5, 5.0), (6, 17 / 3)])
def test_wilder_atr_by_hand(count, expected):
    bars = atr_bars(count)
    got = volatility.wilder_atr(bars, H4, bars[-1].close_time, period=3)
    assert got.ok
    assert approx(got.value, expected)


def test_atr_needs_period_plus_one_bars():
    bars = atr_bars(3)  # two TRs, period 3 needs three
    got = volatility.wilder_atr(bars, H4, bars[-1].close_time, period=3)
    assert got.value is None and '4 consecutive bars' in got.reason


def test_atr_restarts_after_a_hole_instead_of_bridging_it():
    bars = atr_bars(6)
    holed = bars[:2] + bars[3:]  # bar 2 missing: only bars 3,4,5 are consecutive -> 2 TRs
    got = volatility.wilder_atr(holed, H4, holed[-1].close_time, period=3)
    assert got.value is None


def test_atr_period_in_production_is_the_owner_confirmed_wilder_14():
    assert C.ATR_PERIOD == 14


# ---------------------------------------------------------------------------
# Yang-Zhang, n = 2 (three bars; bar 0 only supplies the previous close)
#
#   bar 0: C = 10
#   bar 1: O=12 H=15 L=10 C=15      o1 = ln(12/10) = ln 1.2         c1 = ln(15/12) = ln 1.25
#   bar 2: O=15 H=20 L=12 C=20      o2 = ln(15/15) = 0              c2 = ln(20/15) = ln(4/3)
#
#   overnight   o = [ln 1.2, 0]            mean = ln1.2 / 2
#               var_o = sum((o-mean)^2)/(n-1) = 2 (ln1.2/2)^2 = (ln 1.2)^2 / 2          = 0.016620575
#   open-close  c = [ln 1.25, ln 4/3]      var_c = (ln1.25 - ln(4/3))^2 / 2            = 0.0020826104
#   R-S         bar1: ln(15/15)ln(15/12) + ln(10/15)ln(10/12) = 0 + ln1.5 * ln1.2     = 0.07392..
#               bar2: ln(20/20)ln(20/15) + ln(12/20)ln(12/15) = 0 + ln(5/3) * ln1.25  = 0.11398..
#               var_rs = (bar1 + bar2) / n                                            = 0.0939562368
#   k = 0.34 / (1.34 + (n+1)/(n-1)) = 0.34 / (1.34 + 3) = 0.34 / 4.34               = 0.0783410138
#   var_yz = var_o + k var_c + (1-k) var_rs                                          = 0.1033793388
#   annualised on 1h bars: sqrt(var_yz * 8760)                                       = 30.0932385687
# ---------------------------------------------------------------------------
YZ_BARS = bars_from_rows(
    dt(2026, 1, 1),
    H1,
    [(10, 10, 10, 10), (12, 15, 10, 15), (15, 20, 12, 20)],
)


def test_yang_zhang_terms_by_hand():
    terms = volatility.yang_zhang_terms(YZ_BARS)
    assert terms.n == 2
    assert approx(terms.var_overnight, 0.016620575035885606)
    assert approx(terms.var_open_close, 0.0020826103553123546)
    assert approx(terms.var_rogers_satchell, 0.09395623676267459)
    assert approx(terms.k, 0.34 / 4.34)
    assert approx(terms.variance, 0.10337933876203872)


def test_yang_zhang_overnight_term_is_in_the_result_and_not_dropped():
    # If the overnight term were dropped the variance would be k*var_c + (1-k)*var_rs.
    dropped = 0.0783410138248848 * 0.0020826103553123546 + (1 - 0.0783410138248848) * 0.09395623676267459
    assert volatility.yang_zhang_terms(YZ_BARS).variance - dropped == pytest.approx(0.016620575035885606)


def test_yang_zhang_is_annualised_with_a_365_day_year():
    assert approx(volatility.yang_zhang_vol(YZ_BARS, H1), 30.093238568745956)
    # 15m bars: four times as many per year -> exactly twice the volatility for the same per-bar variance
    assert approx(volatility.annualise_variance(0.0001, M15), math.sqrt(0.0001 * 365 * 24 * 4))
    assert volatility.bars_per_year(H1) == 8760.0
    assert volatility.bars_per_year(H4) == 2190.0


@pytest.mark.parametrize('n, expected', [(2, 0.34 / 4.34), (24, 0.34 / (1.34 + 25 / 23)), (10, 0.34 / (1.34 + 11 / 9))])
def test_yang_zhang_k_weighting(n, expected):
    # k = 0.34 / (1.34 + (n+1)/(n-1)); a common mistake is n/(n-1) or (n-1)/(n+1)
    assert approx(volatility.yang_zhang_k(n), expected)
    assert volatility.yang_zhang_k(n) != pytest.approx(0.34 / (1.34 + n / (n - 1)))


def test_rogers_satchell_terms_are_never_negative():
    # H >= O, C and L <= O, C make each product of two same-signed logs >= 0.
    terms = volatility.yang_zhang_terms(YZ_BARS)
    assert terms.var_rogers_satchell >= 0.0


# ---------------------------------------------------------------------------
# Trend efficiency
#   closes 100 102 101 105 104 (n = 4):  net = |104-100| = 4, path = 2+1+4+1 = 8, ER = 0.5
# ---------------------------------------------------------------------------
def test_efficiency_ratio_by_hand():
    assert trend.efficiency_of_closes([100, 102, 101, 105, 104]) == 0.5


def test_efficiency_extremes():
    assert trend.efficiency_of_closes(list(range(100, 125))) == 1.0  # straight line
    assert trend.efficiency_of_closes([100, 101] * 12 + [100]) == 0.0  # out and back: net 0, a REAL zero
    assert trend.efficiency_of_closes([100.0] * 25) is None  # never moved: no ratio, not 0 and not 1


@pytest.mark.parametrize(
    'ratio, family',
    [(0.40, 'trend'), (0.4000001, 'trend'), (0.25, 'chop'), (0.2499, 'chop'), (0.30, None), (0.3999, None), (0.2501, None), (1.0, 'trend'), (0.0, 'chop')],
)
def test_family_thresholds_are_inclusive_at_both_ends(ratio, family):
    assert trend.classify_efficiency(ratio) == family


def test_family_state_machine_by_hand():
    #            0.30  0.45   0.30   0.20   0.30   0.40   0.25
    expected = [None, 'trend', 'trend', 'chop', 'chop', 'trend', 'chop']
    assert trend.incremental_family([0.30, 0.45, 0.30, 0.20, 0.30, 0.40, 0.25]) == expected


def _bars_with_efficiencies(closes):
    return flat_bars(dt(2026, 1, 1), H1, closes)


def test_trend_state_cold_start_is_none_not_a_default():
    # 24 steps: six +1, then seven (+1, -1) pairs, then four flat steps.
    # net = 6 + 0 = 6 ; path = 6 + 14 = 20 ; ER = 6 / 20 = 0.30, between the bands, and it is the
    # ONLY reading (25 closes), so there is nothing earlier to carry.
    deltas = [1.0] * 6 + [1.0, -1.0] * 7 + [0.0] * 4
    assert len(deltas) == 24
    closes = [100.0]
    for d in deltas:
        closes.append(closes[-1] + d)
    bars = _bars_with_efficiencies(closes)
    state = trend.trend_state(bars, H1, bars[-1].close_time)
    assert state.ratio == pytest.approx(0.30)
    assert state.net_change == pytest.approx(6.0)
    assert state.family is None and state.family_source is None
    assert 'cold start' in state.reason


def test_trend_state_decisive_reading_sets_the_family_directly():
    closes = [100.0 + i for i in range(25)]  # ER = 24/24 = 1.0
    bars = _bars_with_efficiencies(closes)
    state = trend.trend_state(bars, H1, bars[-1].close_time)
    assert (state.ratio, state.family, state.family_source) == (1.0, 'trend', 'decisive')
    assert state.net_change == 24.0


def test_derive_family_carries_through_the_middle_band_by_hand():
    from app.core.features.trend import EfficiencyPoint

    base = dt(2026, 1, 1)

    def family(ratios):
        points = [EfficiencyPoint(base + H1 * i, r, 1.0) for i, r in enumerate(ratios)]
        return trend.derive_family(points, points[-1].time, H1)

    assert family([0.9, 0.3, 0.3]) == ('trend', 'carried')  # between: keep trend
    assert family([0.1, 0.3, 0.3]) == ('chop', 'carried')  # between: keep chop
    assert family([0.1, 0.9, 0.3]) == ('trend', 'carried')  # the MOST RECENT decisive reading wins
    assert family([0.1, 0.3, 0.9]) == ('trend', 'decisive')
    assert family([0.3, 0.3, 0.3]) == (None, None)  # cold start
    assert trend.derive_family([], base, H1) == (None, None)
    # latest reading is not at the latest bar (a hole): refuse to carry
    points = [EfficiencyPoint(base, 0.9, 1.0)]
    assert trend.derive_family(points, base + H1, H1) == (None, None)


def test_derived_family_equals_the_textbook_state_machine():
    """The replay-safe derivation reproduces the process-memory machine whenever its memory covers the run."""
    import random

    rng = random.Random(11)
    closes = [100.0]
    for _ in range(200):
        closes.append(closes[-1] + rng.gauss(0.0, 1.0) + 0.15)
    bars = _bars_with_efficiencies(closes)
    points = trend.efficiency_points(bars, H1, bars[-1].close_time)
    machine = trend.incremental_family([p.ratio for p in points])
    for i in range(len(points)):
        prefix = points[: i + 1]
        derived, _ = trend.derive_family(prefix, prefix[-1].time, H1)
        assert derived == machine[i]


def test_family_memory_window_forgets_after_the_documented_length(monkeypatch):
    monkeypatch.setattr(P, 'FAMILY_MEMORY_BARS', 3)
    from app.core.features.trend import EfficiencyPoint

    base = dt(2026, 1, 1)
    points = [EfficiencyPoint(base + H1 * i, r, 1.0) for i, r in enumerate([0.9, 0.3, 0.3, 0.3, 0.3])]
    # decisive reading is 4 bars back, memory is 3 -> cold start
    family, source = trend.derive_family(points, points[-1].time, H1)
    assert (family, source) == (None, None)
    family, source = trend.derive_family(points[:4], points[3].time, H1)  # decisive reading exactly 3 bars back
    assert family is None  # memory_start = latest - 3h excludes the reading closing at that instant
    family, source = trend.derive_family(points[:3], points[2].time, H1)
    assert (family, source) == ('trend', 'carried')


# ---------------------------------------------------------------------------
# RSI (Wilder), period 3.  closes 10 12 11 13 12 15 -> changes +2 -1 +2 -1 +3
#   seed over the first 3 changes: gains 2,0,2 -> 4/3 ; losses 0,1,0 -> 1/3 ; RS = 4 ; RSI = 100 - 100/5 = 80
#   change -1: avg_gain = (4/3*2 + 0)/3 = 8/9 ; avg_loss = (1/3*2 + 1)/3 = 5/9 ; RS = 1.6 ; RSI = 100 - 100/2.6
#   change +3: avg_gain = (8/9*2 + 3)/3 = 43/27 ; avg_loss = (5/9*2)/3 = 10/27 ; RS = 4.3 ; RSI = 100 - 100/5.3
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'closes, expected',
    [
        ([10, 12, 11, 13], 80.0),
        ([10, 12, 11, 13, 12], 100 - 100 / 2.6),
        ([10, 12, 11, 13, 12, 15], 100 - 100 / 5.3),
    ],
)
def test_wilder_rsi_by_hand(closes, expected):
    assert approx(indicators.wilder_rsi(closes, 3), expected)


def test_rsi_edges():
    assert indicators.wilder_rsi([1, 2, 3, 4, 5], 3) == 100.0  # no losses at all
    assert indicators.wilder_rsi([5, 5, 5, 5, 5], 3) is None  # never moved: undefined, not 50 and not 0
    assert indicators.wilder_rsi([1, 2], 3) is None  # too short
    assert indicators.wilder_rsi([5, 4, 3, 2, 1], 3) == 0.0


# ---------------------------------------------------------------------------
# Bollinger / Keltner (period 4)
#   closes 10 12 14 16: mean 13, deviations -3 -1 1 3, population variance (9+1+1+9)/4 = 5
#   Bollinger(2 sigma): 13 +/- 2*sqrt(5) = 13 +/- 4.47213595499958 -> [8.52786404500042, 17.47213595499958]
#   (the SAMPLE std would be sqrt(20/3) = 2.5819889 -> half-width 5.1639778: a different, wrong, number)
#   Keltner(1.5 ATR) around the same SMA: ATR 2 -> 13 +/- 3 ; ATR 4 -> 13 +/- 6
# ---------------------------------------------------------------------------
def test_bollinger_uses_population_std():
    bands = indicators.bollinger([10, 12, 14, 16], 2.0)
    assert (bands.middle, bands.upper, bands.lower) == pytest.approx((13.0, 13 + 2 * math.sqrt(5), 13 - 2 * math.sqrt(5)))
    assert bands.upper != pytest.approx(13 + 2 * math.sqrt(20 / 3))


def test_keltner_by_hand():
    narrow = indicators.keltner([10, 12, 14, 16], 2.0, 1.5)
    wide = indicators.keltner([10, 12, 14, 16], 4.0, 1.5)
    assert (narrow.lower, narrow.upper) == (10.0, 16.0)
    assert (wide.lower, wide.upper) == (7.0, 19.0)
    # Bollinger [8.53, 17.47]: outside the narrow channel, inside the wide one.
    bb = indicators.bollinger([10, 12, 14, 16], 2.0)
    assert not (bb.upper < narrow.upper and bb.lower > narrow.lower)
    assert bb.upper < wide.upper and bb.lower > wide.lower
    # ratio of widths: 4*sigma / (2*1.5*ATR) = 8.94427191 / 12
    assert approx((bb.upper - bb.lower) / (wide.upper - wide.lower), 8.94427190999916 / 12)


def _squeeze_series(closes, half_range):
    """20 bars: open = previous close, high/low pad the open/close by half_range."""
    rows = []
    previous = closes[0]
    for c in closes:
        rows.append((previous, max(previous, c) + half_range, min(previous, c) - half_range, c, 1.0, 0.5))
        previous = c
    return bars_from_rows(dt(2026, 1, 1), M15, rows)


def test_squeeze_on_with_the_production_periods():
    # closes alternate 101/99 (mean 100, population sigma exactly 1): Bollinger half-width 2.
    # open = previous close (first open 99), high 101.5 and low 98.5 on every bar.
    closes = [101.0 if i % 2 == 0 else 99.0 for i in range(20)]
    rows = [(99.0 if i == 0 else closes[i - 1], 101.5, 98.5, closes[i], 1.0, 0.5) for i in range(20)]
    bars = bars_from_rows(dt(2026, 1, 1), M15, rows)
    # TR = max(3, |101.5 - Cprev|, |98.5 - Cprev|) = 3 for Cprev in {99, 101} -> ATR = 3 exactly
    # Keltner half-width 1.5 * 3 = 4.5 > 2 -> Bollinger inside -> squeeze ON, ratio 4 / 9
    result = indicators.squeeze(bars, bars[-1].close_time, bars[-1].close_time, M15)
    assert result.on.value is True
    assert approx(result.bandwidth_ratio.value, 4 / 9)


def test_squeeze_off_when_the_trend_widens_the_bollinger_band():
    # closes 100..119 (+1 each): population sigma = sqrt((20^2 - 1)/12) = sqrt(33.25) = 5.766281297
    # each bar O = Cprev, C = O + 1, H = C + 0.1, L = O - 0.1: TR = max(1.2, 1.1, 0.1) = 1.2 -> ATR 1.2
    # Bollinger width 4 sigma = 23.0651252 ; Keltner width 2*1.5*1.2 = 3.6 ; ratio = 6.406979219
    closes = [100.0 + i for i in range(20)]
    bars = _squeeze_series(closes, 0.1)
    # first bar's open is its own close in the helper; TR for later bars is what matters
    result = indicators.squeeze(bars, bars[-1].close_time, bars[-1].close_time, M15)
    assert result.on.value is False
    assert approx(result.bandwidth_ratio.value, 4 * math.sqrt(33.25) / 3.6, rel=1e-9)


def test_squeeze_production_parameters_match_the_spec():
    assert (C.BOLLINGER_PERIOD, C.BOLLINGER_SIGMA, C.KELTNER_PERIOD, C.KELTNER_ATR_MULT) == (20, 2.0, 20, 1.5)


# ---------------------------------------------------------------------------
# VWAP.  typical = (H+L+C)/3.
#   b1 H12 L8 C10 V100 -> 10 ; b2 H14 L10 C12 V200 -> 12 ; b3 H15 L9 C12 V100 -> 12
#   VWAP = (10*100 + 12*200 + 12*100) / 400 = 4600/400 = 11.5
# ---------------------------------------------------------------------------
def test_vwap_by_hand():
    bars = bars_from_rows(dt(2026, 1, 1), M15, [(10, 12, 8, 10, 100), (10, 14, 10, 12, 200), (12, 15, 9, 12, 100)])
    assert levels.vwap_of(bars) == pytest.approx(11.5)
    assert levels.distance_atr(12.0, 11.5, 2.0) == 0.25  # (12 - 11.5) / 2: positive above
    assert levels.distance_atr(11.0, 11.5, 0.5) == -1.0


def _session_rows(count=8, first_tp=10.0, second_tp=20.0, split=4):
    rows = []
    for i in range(count):
        tp = first_tp if i < split else second_tp
        volume = 1.0 if i < split else 3.0
        rows.append((tp, tp + 1, tp - 1, tp, volume, 0.5))
    return rows


def test_session_vwap_by_hand_resets_at_utc_midnight():
    # eight bars from 00:00: four at 10 with volume 1, four at 20 with volume 3
    # VWAP = (4*10 + 12*20)/16 = 280/16 = 17.5
    start = dt(2026, 3, 3)
    bars = bars_from_rows(start, M15, _session_rows())
    # yesterday's bars must not leak in
    yesterday = bars_from_rows(start - M15 * 96, M15, [(500, 501, 499, 500, 1000, 500)] * 96)
    result = levels.session_vwap(yesterday + bars, bars[-1].close_time, bars[-1].close_time, M15)
    assert result.value == pytest.approx(17.5)


def test_session_vwap_is_missing_early_in_the_session_not_a_one_bar_price():
    start = dt(2026, 3, 3)
    bars = bars_from_rows(start, M15, _session_rows())[: P.SESSION_MIN_BARS - 1]
    got = levels.session_vwap(bars, bars[-1].close_time, bars[-1].close_time, M15)
    assert got.value is None and 'into the session' in got.reason


def test_session_vwap_missing_when_midnight_bar_absent():
    start = dt(2026, 3, 3, 0, 15)  # the 00:00 bar is not there
    bars = bars_from_rows(start, M15, _session_rows(9))
    got = levels.session_vwap(bars, bars[-1].close_time, bars[-1].close_time, M15)
    assert got.value is None


def test_the_bar_closing_at_midnight_belongs_to_the_day_that_just_ended():
    day = dt(2026, 3, 3)
    bars = bars_from_rows(day, M15, [(10 + i * 0.0, 11, 9, 10, 1.0, 0.5) for i in range(96)])
    close_at_midnight = bars[-1].close_time
    assert close_at_midnight == dt(2026, 3, 4)
    got = levels.session_vwap(bars, close_at_midnight, close_at_midnight, M15)
    assert got.value == pytest.approx(10.0)  # full day of tp = (11+9+10)/3 = 10


def test_anchored_vwap_starts_at_the_anchor_bar():
    start = dt(2026, 3, 3)
    bars = bars_from_rows(start, M15, _session_rows())
    anchor = start + M15 * 4  # the first bar at 20
    got = levels.anchored_vwap(bars, anchor, bars[-1].close_time, bars[-1].close_time, M15)
    assert got.value == pytest.approx(20.0)
    missing = levels.anchored_vwap(bars, start - M15, bars[-1].close_time, bars[-1].close_time, M15)
    assert missing.value is None  # anchor before the data


# ---------------------------------------------------------------------------
# Volume profile, 4 bins, 70% value area.
#   A: L10 H14 V40 -> bins [10,12) [12,14): 20 / 20
#   B: L12 H16 V80 -> [12,14) [14,16): 40 / 40
#   C: L14 H18 V60 -> [14,16) [16,18]: 30 / 30
#   bin volumes [20, 60, 70, 30], total 180, POC = bin 2 -> centre 15
#   value area target 0.7 * 180 = 126: start 70; above = 30, below = 60 -> add below -> 130 >= 126
#   value area = bins 1..2 -> [12, 16]
# ---------------------------------------------------------------------------
def test_volume_profile_by_hand():
    bars = bars_from_rows(
        dt(2026, 1, 1), M15, [(12, 14, 10, 13, 40), (14, 16, 12, 15, 80), (16, 18, 14, 17, 60)]
    )
    profile = levels.volume_profile(bars, bins=4, value_area=0.70)
    assert profile.edges == (10.0, 12.0, 14.0, 16.0, 18.0)
    assert profile.volumes == pytest.approx((20.0, 60.0, 70.0, 30.0))
    assert (profile.poc_index, profile.poc_price) == (2, 15.0)
    assert (profile.value_area_low, profile.value_area_high) == (12.0, 16.0)
    assert (profile.value_area_low_index, profile.value_area_high_index) == (1, 2)


def test_volume_profile_flat_distribution_ties_resolve_to_lowest_poc_and_expand_upward():
    # one bar spanning the whole range spreads 100 evenly: [25 25 25 25]
    bars = bars_from_rows(dt(2026, 1, 1), M15, [(10, 18, 10, 18, 100)])
    profile = levels.volume_profile(bars, bins=4, value_area=0.70)
    assert profile.poc_index == 0 and profile.poc_price == 11.0  # tie -> lowest bin
    # 25 -> add above -> 50 -> add above -> 75 >= 70
    assert (profile.value_area_low, profile.value_area_high) == (10.0, 16.0)


def test_volume_profile_equal_neighbours_expand_upward():
    # bins [30, 40, 30]: POC bin 1; target 70 -> acc 40; above 30 == below 30 -> take ABOVE -> 70 >= 70 stop
    bars = bars_from_rows(dt(2026, 1, 1), M15, [(0, 1, 0, 1, 30), (1, 2, 1, 2, 40), (2, 3, 2, 3, 30)])
    profile = levels.volume_profile(bars, bins=3, value_area=0.70)
    assert profile.volumes == pytest.approx((30.0, 40.0, 30.0))
    assert (profile.value_area_low, profile.value_area_high) == (1.0, 3.0)


def test_volume_profile_degenerate_inputs_are_none():
    assert levels.volume_profile([], bins=4) is None
    flat = bars_from_rows(dt(2026, 1, 1), M15, [(5, 5, 5, 5, 10)])
    assert levels.volume_profile(flat, bins=4) is None  # no price range
    nothing = bars_from_rows(dt(2026, 1, 1), M15, [(5, 6, 4, 5, 0)])
    assert levels.volume_profile(nothing, bins=4) is None  # no volume


def test_volume_profile_production_parameters_match_the_spec():
    assert (C.VOLUME_PROFILE_BINS, C.VOLUME_PROFILE_VALUE_AREA) == (50, 0.70)


# ---------------------------------------------------------------------------
# Swings (4h), two bars each side, strict.
#   i : H     L
#   0 : 10    8
#   1 : 11    9
#   2 : 15   12     <- swing high 15 (15 > 10, 11, 12, 11)
#   3 : 12   10
#   4 : 11    9     <- swing low 9 (9 < 12, 10, 10, 11)
#   5 : 13   10
#   6 : 14   11     <- swing high 14 (14 > 11, 13, 12, 13)
#   7 : 12    9     <- swing low 9 IF bar 9's low stays above 9 (9 < 10, 11, 10, 10)
#   8 : 13   10
#   9 : 13   10     the "last" bar: its close is the parameter, and its high/low stretch to hold it
# A bar that closes beyond a swing cannot be one of the two bars that confirm it,
# which is why the fixture has a bar 9 after the confirming bars 7 and 8.
# ---------------------------------------------------------------------------
SWING_HL = [(10, 8), (11, 9), (15, 12), (12, 10), (11, 9), (13, 10), (14, 11), (12, 9), (13, 10), (13, 10)]


def swing_bars(last_close):
    rows = []
    for i, (h, l) in enumerate(SWING_HL):
        close = last_close if i == len(SWING_HL) - 1 else (h + l) / 2
        rows.append((l + 1, max(h, close), min(l, close), close, 1.0, 0.5))
    return bars_from_rows(dt(2026, 1, 1), H4, rows)


def test_fractal_swings_by_hand():
    swings = levels.fractal_swings(swing_bars(11.0))
    assert [(s.index, s.kind, s.price) for s in swings] == [(2, 'high', 15), (4, 'low', 9), (6, 'high', 14), (7, 'low', 9)]


def test_a_swing_needs_two_later_bars_so_the_newest_bars_never_qualify():
    bars = swing_bars(11.0)
    assert [s.index for s in levels.fractal_swings(bars)] == [2, 4, 6, 7]
    assert [s.index for s in levels.fractal_swings(bars[:9])] == [2, 4, 6]  # bar 7 has only one later bar
    assert [s.index for s in levels.fractal_swings(bars[:8])] == [2, 4]  # bar 6 has only one later bar
    assert levels.fractal_swings(bars[:7]) == [levels.Swing(2, bars[2].open_time, 15, 'high'), levels.Swing(4, bars[4].open_time, 9, 'low')]


def test_equal_highs_are_not_a_fractal():
    bars = bars_from_rows(dt(2026, 1, 1), H4, [(1, 5, 1, 2), (1, 6, 1, 2), (1, 8, 1, 2), (1, 8, 1, 2), (1, 5, 1, 2)])
    assert [s for s in levels.fractal_swings(bars) if s.kind == 'high'] == []


def test_swing_break_long_by_hand():
    # last close 15.5 > last swing high 14 (index 6) -> long break of that swing; 15.5 < last low 9? no.
    # The last swing of EITHER kind is the low at index 7 (that is what the swing-anchored VWAP uses).
    bars = swing_bars(15.5)
    state, why = levels.swing_state(bars, bars[-1].close_time, H4)
    assert why is None
    assert state.break_direction == 'long' and state.broken.index == 6 and state.broken.price == 14
    assert (state.last_high.price, state.last_low.price) == (14, 9)
    assert state.latest_swing.index == 7


def test_swing_break_short_by_hand():
    # last close 8 < last swing low 9 -> short break. Bar 9's low (8) is below bar 7's low (9),
    # so bar 7 is NOT a swing low any more and the last low is the one at index 4.
    bars = swing_bars(8.0)
    state, _ = levels.swing_state(bars, bars[-1].close_time, H4)
    assert state.break_direction == 'short' and state.broken.index == 4 and state.broken.price == 9
    assert state.latest_swing.index == 6


def test_no_break_inside_the_swing_range_and_the_close_must_be_strictly_beyond():
    for close in (11.0, 14.0, 9.0):  # inside, exactly at the high, exactly at the low
        bars = swing_bars(close)
        state, _ = levels.swing_state(bars, bars[-1].close_time, H4)
        assert state.break_direction is None and state.broken is None


def test_when_a_close_is_beyond_both_swings_the_more_recent_swing_decides():
    # SH 10 @2, SL 10.5 @7 (higher than the high: price gapped up and back); last close 10.3 is above SH and below SL.
    rows = [
        (7, 8, 6, 7), (8, 9, 7, 8), (9, 10, 8, 9), (9, 9.5, 8.5, 9), (9.2, 9.8, 9.0, 9.4),
        (11.5, 12, 11, 11.5), (12, 13, 11.5, 12.5), (12, 13.5, 10.5, 13), (12, 14, 11.2, 13), (13, 14.5, 11.8, 13.5),
        (12, 15, 10.2, 10.3),
    ]
    bars = bars_from_rows(dt(2026, 1, 1), H4, rows)
    state, _ = levels.swing_state(bars, bars[-1].close_time, H4)
    assert (state.last_high.index, state.last_high.price) == (2, 10)
    assert (state.last_low.index, state.last_low.price) == (7, 10.5)
    assert 10 < bars[-1].close < 10.5  # beyond BOTH
    assert state.broken.kind == 'low' and state.break_direction == 'short'


def test_no_swing_at_all_is_reported_not_guessed():
    bars = bars_from_rows(dt(2026, 1, 1), H4, [(i, i + 1, i - 1, i) for i in range(1, 10)])  # monotone: no fractal
    state, why = levels.swing_state(bars, bars[-1].close_time, H4)
    assert state is None and 'no confirmed' in why


# ---------------------------------------------------------------------------
# Chandelier (same swing bars, ATR 1.0)
#   long, broken swing index 6: highest high since bar 6 = max(14, 12, 15.5-wick) ...
# ---------------------------------------------------------------------------
def test_chandelier_long_by_hand():
    bars = swing_bars(13.0)  # last bar: H13 L10, close 13 -> inside the range; use the run directly
    # highs from index 6: 14, 12, 13 -> 14 ; stop = 14 - 3*1 = 11 ; price 13 -> distance (13-11)/1 = 2
    assert levels.chandelier_stop(bars, 6, 'long', 1.0) == 11.0
    assert levels.chandelier_distance_atr(13.0, 11.0, 'long', 1.0) == 2.0


def test_chandelier_short_by_hand():
    bars = swing_bars(8.0)
    # lows from index 4: 9, 10, 11, 9, min(10, 8 close wick) = 8 -> 8 ; stop = 8 + 3*1 = 11 ; price 8 -> (11-8)/1 = 3
    assert levels.chandelier_stop(bars, 4, 'short', 1.0) == 11.0
    assert levels.chandelier_distance_atr(8.0, 11.0, 'short', 1.0) == 3.0


def test_chandelier_distance_is_in_atr_not_price():
    # the same 3-unit gap is 1.5 ATR on a quiet day and 0.5 ATR on a wild one
    assert levels.chandelier_distance_atr(103.0, 100.0, 'long', 2.0) == 1.5
    assert levels.chandelier_distance_atr(103.0, 100.0, 'long', 6.0) == 0.5


def test_chandelier_distance_goes_non_positive_when_the_stop_is_breached():
    assert levels.chandelier_distance_atr(99.0, 100.0, 'long', 2.0) == -0.5
    assert levels.chandelier_distance_atr(101.0, 100.0, 'short', 2.0) == -0.5


def test_chandelier_multiple_is_the_spec_three():
    assert C.CHANDELIER_ATR_MULT == 3.0 and C.SWING_FRACTAL_BARS == 2 and C.SWING_TIMEFRAME == '4h'


# ---------------------------------------------------------------------------
# OLS and the lagged beta
#   x = -2 -1 0 1 2 ; y = -3 -2 1 2 3
#   Sxx = 10 ; Sxy = 16 ; beta = 1.6 ; alpha = mean(y) - beta*0 = 0.2
#   residuals 0, -0.6, 0.8, 0.2, -0.4 (sum 0) ; SSR = 1.2 ; sigma = sqrt(1.2/3) = sqrt(0.4)
#   signal bar x=1, y=3: residual = 3 - 0.2 - 1.6 = 1.2 ; z = 1.2 / sqrt(0.4) = 1.89736659610
#   fit INCLUDING the signal pair (6 points): beta = (55/3)/(65/6) = 22/13 = 1.6923 (not 1.6)
# ---------------------------------------------------------------------------
def test_ols_by_hand():
    fit = stats.ols([-2, -1, 0, 1, 2], [-3, -2, 1, 2, 3])
    assert approx(fit.beta, 1.6) and approx(fit.alpha, 0.2) and approx(fit.residual_std, math.sqrt(0.4))
    assert fit.n == 5
    inclusive = stats.ols([-2, -1, 0, 1, 2, 1], [-3, -2, 1, 2, 3, 3])
    assert approx(inclusive.beta, 22 / 13)


def test_ols_refuses_degenerate_input():
    assert stats.ols([1, 1, 1, 1], [1, 2, 3, 4]) is None  # no variance in x
    assert stats.ols([1, 2], [1, 2]) is None  # fewer than three pairs


X = [-2.0, -1.0, 0.0, 1.0, 2.0]
Y = [-3.0, -2.0, 1.0, 2.0, 3.0]


def _returns(pairs_by_time):
    return {t: v for t, v in pairs_by_time}


def test_lagged_beta_by_hand_and_the_signal_bar_is_excluded():
    close = dt(2026, 1, 1, 12)
    times = [close - M15 * (5 - i) for i in range(5)]  # last one is bar_close - 15m = the signal bar's open
    signal = close
    alt = dict(zip(times + [signal], Y + [3.0]))
    btc = dict(zip(times + [signal], X + [1.0]))
    beta, z, fit_end = cross_asset.alt_residual_z(alt, btc, close, M15, TINY)
    assert approx(beta.value, 1.6)
    assert approx(z.value, 1.2 / math.sqrt(0.4))
    assert fit_end == close - M15  # last return used closed when the signal bar OPENED


def test_lagged_beta_test_fails_if_the_lag_is_removed():
    """Including the signal bar changes the beta (1.6 -> 22/13). A fit that peeked would return 22/13."""
    close = dt(2026, 1, 1, 12)
    times = [close - M15 * (5 - i) for i in range(5)]
    alt = dict(zip(times + [close], Y + [3.0]))
    btc = dict(zip(times + [close], X + [1.0]))
    beta, _, _ = cross_asset.alt_residual_z(alt, btc, close, M15, TINY)
    peeking = stats.ols([btc[t] for t in sorted(btc)], [alt[t] for t in sorted(alt)])
    assert approx(peeking.beta, 22 / 13)
    assert beta.value != pytest.approx(peeking.beta, abs=1e-6)
    assert beta.value == pytest.approx(1.6)


def test_lagged_beta_ignores_returns_that_close_after_the_signal_bar_opens():
    close = dt(2026, 1, 1, 12)
    times = [close - M15 * (5 - i) for i in range(5)]
    alt = dict(zip(times, Y))
    btc = dict(zip(times, X))
    future = {close + M15 * i: 50.0 * i for i in range(1, 6)}
    alt.update({close: 3.0, **future})
    btc.update({close: 1.0, **{t: -50.0 * i for i, t in enumerate(future, 1)}})
    beta, z, _ = cross_asset.alt_residual_z(alt, btc, close, M15, TINY)
    assert approx(beta.value, 1.6) and approx(z.value, 1.2 / math.sqrt(0.4))


def test_lagged_beta_window_is_90_days_trailing():
    close = dt(2026, 6, 1)
    old = close - timedelta(days=91)
    fresh = [close - M15 * (5 - i) for i in range(5)]
    alt = dict(zip(fresh, Y))
    btc = dict(zip(fresh, X))
    alt[old], btc[old] = 1000.0, -1000.0  # would wreck the fit if it were included
    alt[close], btc[close] = 3.0, 1.0
    beta, _, _ = cross_asset.alt_residual_z(alt, btc, close, M15, TINY)
    assert approx(beta.value, 1.6)


def test_log_returns_need_the_previous_bar():
    bars = bars_from_rows(dt(2026, 1, 1), M15, [(100, 101, 99, 100), (100, 111, 99, 110), (110, 111, 109, 121)])
    returns = cross_asset.log_returns(bars, M15, bars[-1].close_time)
    assert set(returns) == {bars[1].close_time, bars[2].close_time}
    assert returns[bars[1].close_time] == pytest.approx(math.log(1.1))
    gap = [bars[0], bars[2]]  # bar 1 missing: no return across the hole
    assert cross_asset.log_returns(gap, M15, bars[-1].close_time) == {}


# ---------------------------------------------------------------------------
# z-score and percentile.  history 1 2 3 4 5 : mean 3, unbiased var 10/4 = 2.5, std 1.5811388
#   x = 6 -> z = 3 / 1.5811388300841898 = 1.8973665961
# ---------------------------------------------------------------------------
def hourly_samples(values, start=None):
    start = start or dt(2026, 1, 1)
    return [Sample(start + H1 * i, start + H1 * i, v) for i, v in enumerate(values)]


def test_zscore_by_hand():
    history = hourly_samples([1, 2, 3, 4, 5])
    now = history[-1].time + H1
    got = stats.trailing_zscore(history, now, 6.0, now, TINY)
    assert approx(got.value, 3 / math.sqrt(2.5))
    assert approx(got.value, 1.8973665961010275)


def test_percentile_by_hand_with_midrank_ties():
    history = hourly_samples(list(range(1, 11)))
    now = history[-1].time + H1

    def pct(x):
        return stats.trailing_percentile(history, now, x, now, TINY).value

    assert pct(5.5) == 0.5  # 5 below, 0 equal
    assert pct(3.0) == 0.25  # 2 below + half of 1 equal = 2.5 / 10
    assert pct(11.0) == 1.0
    assert pct(0.0) == 0.0


def test_zscore_of_a_flat_history_is_missing_not_zero_and_not_infinite():
    history = hourly_samples([2.0] * 6)
    now = history[-1].time + H1
    got = stats.trailing_zscore(history, now, 2.0, now, TINY)
    assert got.value is None and 'zero variance' in got.reason


def test_zscore_uses_the_unbiased_standard_deviation():
    history = hourly_samples([1, 2, 3, 4, 5])
    now = history[-1].time + H1
    got = stats.trailing_zscore(history, now, 6.0, now, TINY).value
    population = 3 / math.sqrt(2.0)
    assert got != pytest.approx(population)


# ---------------------------------------------------------------------------
# CVD divergence, 4-bar window.  delta = 2*taker_buy - volume
#   volume 10 each, taker_buy 7, 6, 4, 4.5 -> deltas +4 +2 -2 -1 ; cumulative 4 6 4 3 ; sum 3
#   closes 10 11 12 13: the signal close 13 > max(10, 11, 12) -> new high
#   cumulative at the signal bar is 3, the prior maximum is 6 -> CVD does NOT confirm -> against LONG
# ---------------------------------------------------------------------------
def cvd_bars(closes, taker_buys):
    rows = [(c - 0.5, c + 0.5, c - 1.0, c, 10.0, tb) for c, tb in zip(closes, taker_buys)]
    return bars_from_rows(dt(2026, 1, 1), M15, rows)


def test_cvd_divergence_against_long_by_hand():
    bars = cvd_bars([10, 11, 12, 13], [7, 6, 4, 4.5])
    result = indicators.cvd_divergence(bars, bars[-1].close_time, bars[-1].close_time, M15, n=4, extreme_bars=4)
    assert result.delta.value == 3.0
    assert result.divergence.value is True and result.against == 'long'


def test_cvd_confirmation_is_not_a_divergence():
    # deltas -1 -1 +2 +6 -> cumulative -1 -2 0 6 ; new high with cum 6 > max(-1,-2,0) = 0 -> confirmed
    bars = cvd_bars([10, 11, 12, 13], [4.5, 4.5, 6, 8])
    result = indicators.cvd_divergence(bars, bars[-1].close_time, bars[-1].close_time, M15, n=4, extreme_bars=4)
    assert result.delta.value == 6.0
    assert result.divergence.value is False and result.against is None


def test_cvd_divergence_against_short_by_hand():
    # closes 13 12 11 10 -> new low ; deltas -2 -2 +3 +1 (taker_buy 4 4 6.5 5.5) -> cumulative -2 -4 -1 0
    # new low needs cumulative < min(-2,-4,-1) = -4 to confirm ; 0 is not -> against SHORT
    bars = cvd_bars([13, 12, 11, 10], [4, 4, 6.5, 5.5])
    result = indicators.cvd_divergence(bars, bars[-1].close_time, bars[-1].close_time, M15, n=4, extreme_bars=4)
    assert result.delta.value == 0.0  # a REAL zero: the deltas sum to zero
    assert result.divergence.value is True and result.against == 'short'


def test_cvd_no_price_extreme_no_divergence():
    bars = cvd_bars([10, 12, 11, 11.5], [5, 5, 5, 5])
    result = indicators.cvd_divergence(bars, bars[-1].close_time, bars[-1].close_time, M15, n=4, extreme_bars=4)
    assert result.divergence.value is False and result.against is None


def test_cvd_close_equal_to_the_prior_high_is_not_a_new_high():
    bars = cvd_bars([10, 12, 11, 12], [4, 4, 4, 4])
    result = indicators.cvd_divergence(bars, bars[-1].close_time, bars[-1].close_time, M15, n=4, extreme_bars=4)
    assert result.divergence.value is False


def test_cvd_production_window_is_twenty_15m_bars():
    assert (C.CVD_BARS, C.CVD_TIMEFRAME, C.CVD_PRICE_EXTREME_BARS) == (20, '15m', 20)


# ---------------------------------------------------------------------------
# Positioning
# ---------------------------------------------------------------------------
def test_aggressor_imbalance_by_hand():
    bar = Bar.make(dt(2026, 1, 1), M15, 1, 2, 1, 2, 10.0, 6.0)
    assert positioning.imbalance_of(bar) == 0.6
    assert positioning.imbalance_of(Bar.make(dt(2026, 1, 1), M15, 1, 2, 1, 2, 0.0, 0.0)) is None  # no volume: no ratio


def test_funding_carry_scales_linearly_to_the_horizon():
    # 0.0001 per 8h interval: swing (24h) = 3 intervals = 3 bps ; scalp (4h) = 0.5 interval = 0.5 bps ; position (72h) = 9 bps
    assert positioning.funding_carry_bps(0.0001, 8, 24) == pytest.approx(3.0)
    assert positioning.funding_carry_bps(0.0001, 8, 4) == pytest.approx(0.5)
    assert positioning.funding_carry_bps(0.0001, 8, 72) == pytest.approx(9.0)
    assert positioning.funding_carry_bps(-0.0002, 8, 24) == pytest.approx(-6.0)
    # a 1h-interval symbol pays 8x as often
    assert positioning.funding_carry_bps(0.0001, 1, 24) == pytest.approx(24.0)


def test_basis_bps_by_hand():
    assert positioning.basis_bps(100.5, 100.0) == pytest.approx(50.0)
    assert positioning.basis_bps(99.9, 100.0) == pytest.approx(-10.0)
    assert positioning.basis_bps(100.0, 0.0) is None


def test_perp_spot_disagreement_is_a_magnitude_in_atr():
    perp = bars_from_rows(dt(2026, 1, 1), M15, [(100, 101, 99, 100)])
    spot_hi = bars_from_rows(dt(2026, 1, 1), M15, [(100, 105, 99, 103)])
    spot_lo = bars_from_rows(dt(2026, 1, 1), M15, [(100, 101, 95, 97)])
    close = perp[0].close_time
    assert positioning.perp_spot_disagreement(perp, spot_hi, close, close, 2.0).value == 1.5
    assert positioning.perp_spot_disagreement(perp, spot_lo, close, close, 2.0).value == 1.5  # sign-free


def _oi_fixture(current_change, price_up=True, available_lag=timedelta(minutes=5)):
    """Six hourly OI points whose five changes are exactly -0.2%, -0.1%, 0, +0.1%, +0.2% (mean 0,
    unbiased sigma = sqrt(2.5e-6) = 0.001581138830), then the current change."""
    start = dt(2026, 1, 1)
    changes = [-0.002, -0.001, 0.0, 0.001, 0.002]
    values = [1000.0]
    for c in changes:
        values.append(values[-1] * (1.0 + c))
    values.append(values[-1] * (1.0 + current_change))
    points = [OIPoint(start + H1 * i, start + H1 * i + available_lag, v) for i, v in enumerate(values)]
    last = start + H1 * 6
    prev = last - H1
    closes = {prev: 100.0, last: 101.0 if price_up else 99.0}
    price = []
    for t, c in closes.items():
        price.append(Bar.make(t - H1, H1, 100.0, 102.0, 98.0, c))
    return points, price, last + timedelta(minutes=10)


SIGMA_OI = math.sqrt(2.5e-6)


@pytest.mark.parametrize(
    'change, price_up, expected',
    [
        (0.9 * 0.25 * SIGMA_OI, True, 'price_up_oi_flat'),  # inside the dead zone, price side kept
        (0.9 * 0.25 * SIGMA_OI, False, 'price_down_oi_flat'),
        (-0.9 * 0.25 * SIGMA_OI, True, 'price_up_oi_flat'),
        (1.1 * 0.25 * SIGMA_OI, True, 'price_up_oi_up'),  # just outside
        (-1.1 * 0.25 * SIGMA_OI, True, 'price_up_oi_down'),
        (1.1 * 0.25 * SIGMA_OI, False, 'price_down_oi_up'),
        (-1.1 * 0.25 * SIGMA_OI, False, 'price_down_oi_down'),
        (0.004, True, 'price_up_oi_up'),
    ],
)
def test_oi_price_quadrant_with_the_dead_zone_by_hand(change, price_up, expected):
    points, price, as_of_ = _oi_fixture(change, price_up)
    result = positioning.oi_price_quadrant(points, price, as_of_, TINY, oi_step=H1, price_step=H1)
    assert result.quadrant.value == expected
    assert approx(result.oi_change_z.value, change / SIGMA_OI, rel=1e-6)  # mean 0, so z = change / sigma


def test_oi_quadrant_uses_the_newest_VISIBLE_reading_not_the_one_that_is_not_published_yet():
    points, price, _ = _oi_fixture(0.004)
    boundary = points[-1].poll_time
    before_publication = boundary + timedelta(minutes=4)  # lag is 5 minutes
    result = positioning.oi_price_quadrant(points, price, before_publication, TINY, oi_step=H1, price_step=H1)
    # the newest visible reading is the PREVIOUS boundary, so the paired price move ends there; no price bars exist for it
    assert result.poll_time == boundary - H1
    assert result.quadrant.value is None


def test_oi_quadrant_pairs_price_over_the_same_interval_as_the_oi_change():
    points, price, as_of_ = _oi_fixture(0.004, price_up=True)
    result = positioning.oi_price_quadrant(points, price, as_of_, TINY, oi_step=H1, price_step=H1)
    assert result.price_return == pytest.approx(math.log(101 / 100))
    assert result.poll_time == points[-1].poll_time


def test_oi_dead_zone_and_spec_constant():
    assert C.OI_CHANGE_DEAD_ZONE_SIGMA == 0.25


def test_the_six_quadrant_strings_match_the_schema_vocabulary():
    from app.models import enums as E

    names = {
        positioning.QUADRANT_PRICE_UP_OI_UP,
        positioning.QUADRANT_PRICE_UP_OI_DOWN,
        positioning.QUADRANT_PRICE_DOWN_OI_UP,
        positioning.QUADRANT_PRICE_DOWN_OI_DOWN,
        positioning.QUADRANT_PRICE_UP_OI_FLAT,
        positioning.QUADRANT_PRICE_DOWN_OI_FLAT,
    }
    assert names == set(E.OI_PRICE_QUADRANTS)


def test_oi_changes_do_not_bridge_a_gap():
    start = dt(2026, 1, 1)
    points = [OIPoint(start + H1 * i, start + H1 * i + timedelta(minutes=5), 1000.0 + i) for i in (0, 1, 3, 4)]
    changes = positioning.oi_changes(points, H1, 1)
    assert [p.poll_time for p, _ in changes] == [start + H1, start + H1 * 4]  # 3 has no predecessor at 2


def test_funding_z_reads_only_settled_prints_and_excludes_the_newest_from_its_own_history():
    start = dt(2026, 1, 1)
    prints = [FundingPrint(start + timedelta(hours=8 * i), start + timedelta(hours=8 * i, seconds=30), r) for i, r in enumerate([1, 2, 3, 4, 5, 6.0])]
    as_of_ = prints[-1].funding_time + timedelta(minutes=1)
    got = positioning.funding_z(prints, as_of_, TINY)
    # history = first five (mean 3, std sqrt(2.5)); current = 6 -> z = 3 / 1.5811388
    assert got.last_print.rate == 6.0
    assert approx(got.z.value, 1.8973665961010275)


# ---------------------------------------------------------------------------
# Liquidation estimate.  ATR 2 -> bin width 1.0 (0.5 ATR... see below).
# ---------------------------------------------------------------------------
def test_liquidation_estimate_by_hand():
    now = dt(2026, 1, 1, 12)
    atr = 2.0  # bin width = 0.5 * 2 = 1.0, so bin = floor(price)
    prints = [
        ForcePrint(now - timedelta(minutes=5), now - timedelta(minutes=5), 'sell', 100.2, 3.0),
        ForcePrint(now - timedelta(minutes=50), now - timedelta(minutes=50), 'sell', 100.8, 2.0),
        ForcePrint(now - timedelta(minutes=70), now - timedelta(minutes=70), 'sell', 100.5, 5.0),
        ForcePrint(now - timedelta(hours=3), now - timedelta(hours=3), 'buy', 95.5, 1.0),
        ForcePrint(now - timedelta(hours=4), now - timedelta(hours=4), 'buy', 95.9, 1.0),
    ]
    # bin 100: notional 100.2*3 + 100.8*2 + 100.5*5 = 300.6 + 201.6 + 502.5 = 1004.7 (bin 95: 191.4)
    # pool = sum(p * p*q) / sum(p*q) = (100.2*300.6 + 100.8*201.6 + 100.5*502.5) / 1004.7
    pool = (100.2 * 300.6 + 100.8 * 201.6 + 100.5 * 502.5) / 1004.7
    out, why = liquidation.liquidation_shadow_metrics(prints, 98.0, atr, now)
    assert why is None
    assert out[liquidation.KEY_DISTANCE] == pytest.approx((pool - 98.0) / 2.0)
    assert out[liquidation.KEY_ESTIMATED] is True
    # only the 100.2 print is inside the last 15 minutes: 3.0 / 15
    assert out[liquidation.KEY_RATE] == pytest.approx(3.0 / 15)


def test_liquidation_keys_stay_inside_the_documented_shadow_vocabulary():
    from app.models.features import SHADOW_METRIC_KEYS

    assert {liquidation.KEY_DISTANCE, liquidation.KEY_ESTIMATED, liquidation.KEY_RATE} <= set(SHADOW_METRIC_KEYS)


def test_liquidation_with_too_few_prints_reports_nothing_rather_than_zero():
    now = dt(2026, 1, 1, 12)
    prints = [ForcePrint(now - timedelta(minutes=5), now - timedelta(minutes=5), 'sell', 100.0, 1.0)] * 2
    out, why = liquidation.liquidation_shadow_metrics(prints, 100.0, 2.0, now)
    assert out == {} and 'need 5' in why
    assert liquidation.liquidation_shadow_metrics([], 100.0, 2.0, now)[0] == {}


def test_liquidation_ignores_prints_not_yet_available():
    now = dt(2026, 1, 1, 12)
    later = [ForcePrint(now - timedelta(minutes=5), now + timedelta(minutes=1), 'sell', 100.0, 1.0)] * 6
    assert liquidation.liquidation_shadow_metrics(later, 100.0, 2.0, now)[0] == {}


# ---------------------------------------------------------------------------
# Pullback reset (three-valued)
# ---------------------------------------------------------------------------
def _signal_bar(low=99.0, high=101.0):
    return Bar.make(dt(2026, 1, 1), M15, 100, high, low, 100)


@pytest.mark.parametrize(
    'family, net, rsi_value, vwap, expected',
    [
        ('trend', +1.0, 45.0, 120.0, True),  # up-trend, RSI in the long band 40-50
        ('trend', +1.0, 55.0, 120.0, False),  # up-trend, RSI in the SHORT band: not a long reset; VWAP not tagged
        ('trend', -1.0, 55.0, 120.0, True),  # down-trend, RSI in the short band 50-60
        ('trend', -1.0, 45.0, 120.0, False),
        ('trend', +1.0, 40.0, 120.0, True),  # inclusive ends
        ('trend', +1.0, 50.0, 120.0, True),
        ('trend', -1.0, 60.0, 120.0, True),
        ('trend', +1.0, 80.0, 100.0, True),  # VWAP tagged by the bar range
        ('chop', +1.0, 45.0, 100.0, False),  # not applicable outside the trend family
        ('trend', +1.0, None, 120.0, None),  # RSI unknown and nothing else fired -> unknown
        ('trend', +1.0, None, 100.0, True),  # RSI unknown but the VWAP tag fired
        ('trend', +1.0, 80.0, None, None),
        (None, +1.0, 45.0, 100.0, None),  # cold start: unknown family
        ('trend', 0.0, 45.0, 120.0, None),  # no trend direction: the RSI band is undefined
    ],
)
def test_pullback_reset_truth_table(family, net, rsi_value, vwap, expected):
    got = indicators.pullback_reset(family, net, rsi_value, vwap, _signal_bar())
    assert got.value == expected


def test_pullback_bands_are_the_spec_bands():
    assert C.PULLBACK_RSI_LONG_BAND == (40.0, 50.0) and C.PULLBACK_RSI_SHORT_BAND == (50.0, 60.0)
    assert C.PULLBACK_RSI_PERIOD == 14
