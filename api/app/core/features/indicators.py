"""Filter and trigger indicators: CVD divergence, squeeze, RSI, pullback reset.

Every function takes the visible bars and the instant it is evaluated at, uses
only bars whose ``available_at`` is at or before it, and requires the bars it
reads to be CONSECUTIVE (a hole is a NULL, not a bridge).

CVD divergence
--------------
Spec: "20 bars of 15m taker-volume delta. Price makes a 20-bar high or low and
CVD does not."

* Per-bar delta = taker-buy volume minus taker-sell volume
  = ``2 * taker_buy - volume``.
* ``cvd_delta`` is the sum of the deltas of the last 20 bars.
* The cumulative CVD line starts at 0 at the window's first bar.
* Price "makes a 20-bar high" when the signal bar's CLOSE is strictly above the
  closes of the 19 bars before it (low: strictly below). Closes, not wicks: a
  bar cannot then be a new high and a new low at once.
* CVD "does not" confirm when the cumulative line at the signal bar is NOT
  strictly above the highest cumulative value of the prior 19 bars (for a low:
  not strictly below the lowest).
* A new price high without CVD confirmation argues AGAINST longs (continuation
  is filtered; the fade is allowed); a new low without confirmation argues
  against shorts.

Squeeze
-------
Bollinger(20, 2 sigma) with the POPULATION standard deviation (Bollinger's own
convention) around the 20-bar SMA, versus Keltner(20, 1.5 ATR) around the same
SMA, with the 15m Wilder ATR(14). Using the same centre for both makes
"Bollinger inside Keltner" identical to ``bollinger_width / keltner_width < 1``,
which is what ``feature_row.squeeze_bandwidth_ratio`` documents. Strict
inequalities.

RSI
---
Wilder RSI on 15m closes: seed = mean gain / mean loss of the first ``p``
changes, then ``avg = (avg * (p - 1) + x) / p``; ``RSI = 100 - 100 / (1 + RS)``;
all-gain history is 100; a history that never moves has no RSI.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import constants as C
from app.core.features import params as P
from app.core.features.reading import Reading, have, missing
from app.core.features.series import Bar, contiguous_suffix, tail_if_contiguous, visible_bars
from app.core.features.stats import mean, population_std
from app.core.features.volatility import wilder_atr

DIRECTION_LONG = 'long'
DIRECTION_SHORT = 'short'


# ---------------------------------------------------------------------------
# CVD divergence
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CvdResult:
    delta: Reading
    divergence: Reading  # True / False when computable
    against: str | None  # 'long' | 'short' | None
    reason: str | None = None


def bar_delta(bar: Bar) -> float:
    """Taker-buy minus taker-sell volume of one bar."""
    return 2.0 * bar.taker_buy_volume - bar.volume


def cvd_divergence(
    bars: Sequence[Bar],
    bar_close: datetime,
    as_of: datetime,
    step: timedelta,
    *,
    n: int = C.CVD_BARS,
    extreme_bars: int = C.CVD_PRICE_EXTREME_BARS,
) -> CvdResult:
    if extreme_bars > n:
        raise ValueError('the price-extreme window cannot be longer than the CVD window')
    seen = visible_bars(bars, as_of)
    if not seen or seen[-1].close_time != bar_close:
        gone = missing('signal bar is not in the visible 15m series')
        return CvdResult(gone, gone, None, gone.reason)
    window = tail_if_contiguous(seen, n, step)
    if window is None:
        gone = missing(f'needs {n} consecutive 15m bars ending at the signal bar')
        return CvdResult(gone, gone, None, gone.reason)

    deltas = [bar_delta(b) for b in window]
    cumulative: list[float] = []
    running = 0.0
    for delta in deltas:
        running += delta
        cumulative.append(running)
    total = math.fsum(deltas)

    closes = [b.close for b in window]
    price_now = closes[-1]
    prior_closes = closes[-extreme_bars:-1]
    prior_cvd = cumulative[:-1]
    if price_now > max(prior_closes):
        confirmed = cumulative[-1] > max(prior_cvd)
        return CvdResult(have(total), have(not confirmed), None if confirmed else DIRECTION_LONG)
    if price_now < min(prior_closes):
        confirmed = cumulative[-1] < min(prior_cvd)
        return CvdResult(have(total), have(not confirmed), None if confirmed else DIRECTION_SHORT)
    return CvdResult(have(total), have(False), None)


# ---------------------------------------------------------------------------
# Squeeze
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Bands:
    middle: float
    upper: float
    lower: float


def bollinger(closes: Sequence[float], sigma: float = C.BOLLINGER_SIGMA) -> Bands:
    """SMA +/- ``sigma`` population standard deviations of ``closes``."""
    centre = mean(closes)
    width = sigma * population_std(closes)
    return Bands(centre, centre + width, centre - width)


def keltner(closes: Sequence[float], atr: float, multiple: float = C.KELTNER_ATR_MULT) -> Bands:
    """SMA of ``closes`` +/- ``multiple`` x ATR."""
    centre = mean(closes)
    return Bands(centre, centre + multiple * atr, centre - multiple * atr)


@dataclass(frozen=True)
class SqueezeResult:
    on: Reading
    bandwidth_ratio: Reading


def squeeze(bars: Sequence[Bar], bar_close: datetime, as_of: datetime, step: timedelta) -> SqueezeResult:
    seen = visible_bars(bars, as_of)
    if not seen or seen[-1].close_time != bar_close:
        gone = missing('signal bar is not in the visible 15m series')
        return SqueezeResult(gone, gone)
    need = max(C.BOLLINGER_PERIOD, C.KELTNER_PERIOD)
    window = tail_if_contiguous(seen, need, step)
    if window is None:
        gone = missing(f'needs {need} consecutive 15m bars ending at the signal bar')
        return SqueezeResult(gone, gone)
    atr = wilder_atr(seen, step, as_of)
    if not atr.ok:
        gone = missing('15m ATR unavailable: ' + (atr.reason or ''))
        return SqueezeResult(gone, gone)
    closes = [b.close for b in window]
    bb = bollinger(closes[-C.BOLLINGER_PERIOD :])
    kc = keltner(closes[-C.KELTNER_PERIOD :], atr.value)
    kc_width = kc.upper - kc.lower
    ratio = (bb.upper - bb.lower) / kc_width
    inside = bb.upper < kc.upper and bb.lower > kc.lower
    return SqueezeResult(have(inside), have(ratio))


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------
def wilder_rsi(closes: Sequence[float], period: int = C.PULLBACK_RSI_PERIOD) -> float | None:
    """Wilder RSI at the last close; None if there are too few closes or none ever moved."""
    if period < 1:
        raise ValueError('period must be >= 1')
    if len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = math.fsum(gains[:period]) / period
    avg_loss = math.fsum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0.0:
        return None if avg_gain == 0.0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(bars: Sequence[Bar], bar_close: datetime, as_of: datetime, step: timedelta) -> Reading:
    seen = visible_bars(bars, as_of)
    if not seen or seen[-1].close_time != bar_close:
        return missing('signal bar is not in the visible 15m series')
    run = contiguous_suffix(seen, step)[-P.RSI_HISTORY_BARS :]
    value = wilder_rsi([b.close for b in run], C.PULLBACK_RSI_PERIOD)
    if value is None:
        return missing(f'RSI({C.PULLBACK_RSI_PERIOD}) needs {C.PULLBACK_RSI_PERIOD + 1} consecutive moving closes')
    return have(value)


# ---------------------------------------------------------------------------
# Pullback reset
# ---------------------------------------------------------------------------
def _in_band(value: float, band: tuple[float, float]) -> bool:
    return band[0] <= value <= band[1]


def pullback_reset(
    family: str | None,
    trend_net_change: float | None,
    rsi_value: float | None,
    session_vwap: float | None,
    signal_bar: Bar | None,
) -> Reading:
    """Pullback reset trigger, three-valued.

    Only while the efficiency family is ``trend``. The trend's direction is the
    sign of the efficiency window's net move: an up-trend resets when RSI is in
    the long band (40-50, inclusive), a down-trend when it is in the short band
    (50-60, inclusive). Either also resets when the signal bar's range contains
    the session VWAP ("price tagging the VWAP").

    * family unknown (cold start)          -> missing
    * family ``chop``                      -> False (the trigger does not apply)
    * any evidence True                    -> True
    * otherwise any evidence unavailable   -> missing (unknown, not False)
    * otherwise                            -> False
    """
    if family is None:
        return missing('trend family is unknown (cold start)')
    if family != C.FAMILY_TREND:
        return have(False)

    rsi_hit: bool | None = None
    if rsi_value is not None and trend_net_change is not None and trend_net_change != 0.0:
        band = C.PULLBACK_RSI_LONG_BAND if trend_net_change > 0.0 else C.PULLBACK_RSI_SHORT_BAND
        rsi_hit = _in_band(rsi_value, band)
    vwap_hit: bool | None = None
    if session_vwap is not None and signal_bar is not None:
        vwap_hit = signal_bar.low <= session_vwap <= signal_bar.high

    evidence = (rsi_hit, vwap_hit)
    if any(item is True for item in evidence):
        return have(True)
    if any(item is None for item in evidence):
        return missing('RSI or VWAP evidence unavailable and no other evidence fired')
    return have(False)
