"""True range, Wilder ATR, Yang-Zhang realized volatility and its percentile.

Written for audit, not speed: explicit loops, every term named.

ATR (Wilder)
------------
``TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|)`` for ``i >= 1`` (the
first bar has no previous close and contributes no TR).
``ATR_p = mean(TR_1..TR_p)`` (the seed), then
``ATR_i = (ATR_{i-1} * (p - 1) + TR_i) / p``.
The recursion is path dependent, so the engine always feeds it the LAST
``ATR_HISTORY_BARS`` visible bars of a contiguous run: live and replay see the
same bars and produce the same bits. A hole in the data restarts the run.

Yang-Zhang (2000), the definition used here
-------------------------------------------
For a window of ``n`` bars (``n + 1`` bars are read because bar 1 needs the
previous close), with ``O, H, L, C`` the bar's prices and ``C_prev`` the
previous bar's close:

    o_i = ln(O_i / C_prev_i)                      overnight (close-to-open)
    c_i = ln(C_i / O_i)                           open-to-close
    sigma2_o  = sum((o_i - mean(o))^2) / (n - 1)
    sigma2_c  = sum((c_i - mean(c))^2) / (n - 1)
    sigma2_rs = sum( ln(H/C) ln(H/O) + ln(L/C) ln(L/O) ) / n     Rogers-Satchell
    k         = 0.34 / (1.34 + (n + 1) / (n - 1))
    sigma2_yz = sigma2_o + k * sigma2_c + (1 - k) * sigma2_rs

``sigma2_yz`` is a variance PER BAR. **The reported volatility is annualised**:
``vol = sqrt(sigma2_yz * bars_per_year)`` with ``bars_per_year = 365 * 24 /
bar_hours`` (crypto trades every hour of every day, so 365 days and no trading
calendar). It is a fraction (0.55 means 55 % a year), the same convention as a
Deribit implied vol, so Milestone 5 can subtract the two directly. Never mix the
per-bar variance with an annualised implied vol.

In a 24/7 market the "overnight" gap is close to nothing (a bar opens at the
price the previous one closed at, give or take a tick), so ``sigma2_o`` is tiny.
It is kept: dropping it would not be Yang-Zhang.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import constants as C
from app.core.features import params as P
from app.core.features.reading import Reading, have, missing
from app.core.features.series import Bar, Sample, contiguous_suffix, is_contiguous, visible_bars
from app.core.features.stats import mean


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------
def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """``TR_i`` for ``i = 1..len-1``; the first bar has no previous close."""
    out: list[float] = []
    for previous, bar in zip(bars, bars[1:]):
        out.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous.close),
                abs(bar.low - previous.close),
            )
        )
    return out


def wilder_atr_from_ranges(ranges: Sequence[float], period: int) -> float | None:
    """Wilder smoothing of a TR list: seed = mean of the first ``period`` values."""
    if period < 1:
        raise ValueError('period must be >= 1')
    if len(ranges) < period:
        return None
    atr = math.fsum(ranges[:period]) / period
    for tr in ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def wilder_atr(
    bars: Sequence[Bar],
    step: timedelta,
    as_of: datetime,
    *,
    period: int = C.ATR_PERIOD,
    history_bars: int = P.ATR_HISTORY_BARS,
) -> Reading:
    """Wilder ATR at the last bar visible at ``as_of``.

    Uses the longest contiguous run ending at the last visible bar, capped at
    the last ``history_bars`` bars. Needs ``period + 1`` bars (``period`` TRs).
    """
    run = contiguous_suffix(visible_bars(bars, as_of), step)
    run = run[-history_bars:]
    if len(run) < period + 1:
        return missing(f'ATR({period}) needs {period + 1} consecutive bars, have {len(run)}')
    atr = wilder_atr_from_ranges(true_ranges(run), period)
    if atr is None or atr <= 0.0:
        return missing('ATR is not positive (flat prices)')
    return have(atr)


# ---------------------------------------------------------------------------
# Yang-Zhang
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class YangZhangTerms:
    n: int
    k: float
    var_overnight: float
    var_open_close: float
    var_rogers_satchell: float
    variance: float  # per bar


def yang_zhang_k(n: int) -> float:
    """``k = 0.34 / (1.34 + (n + 1) / (n - 1))``."""
    if n < 2:
        raise ValueError('Yang-Zhang needs n >= 2')
    return 0.34 / (1.34 + (n + 1) / (n - 1))


def yang_zhang_terms(window: Sequence[Bar]) -> YangZhangTerms:
    """Per-bar Yang-Zhang variance of ``window`` (``n + 1`` bars, the first only gives C_prev)."""
    n = len(window) - 1
    if n < 2:
        raise ValueError('Yang-Zhang needs at least 3 bars (n >= 2)')
    overnight: list[float] = []
    open_close: list[float] = []
    rs_terms: list[float] = []
    for previous, bar in zip(window, window[1:]):
        for price in (bar.open, bar.high, bar.low, bar.close, previous.close):
            if price <= 0.0:
                raise ValueError('Yang-Zhang needs strictly positive prices')
        overnight.append(math.log(bar.open / previous.close))
        open_close.append(math.log(bar.close / bar.open))
        rs_terms.append(
            math.log(bar.high / bar.close) * math.log(bar.high / bar.open)
            + math.log(bar.low / bar.close) * math.log(bar.low / bar.open)
        )
    o_mean, c_mean = mean(overnight), mean(open_close)
    var_o = math.fsum((o - o_mean) ** 2 for o in overnight) / (n - 1)
    var_c = math.fsum((c - c_mean) ** 2 for c in open_close) / (n - 1)
    var_rs = math.fsum(rs_terms) / n
    k = yang_zhang_k(n)
    return YangZhangTerms(n, k, var_o, var_c, var_rs, var_o + k * var_c + (1.0 - k) * var_rs)


def bars_per_year(step: timedelta) -> float:
    return P.HOURS_PER_YEAR / (step.total_seconds() / 3600.0)


def annualise_variance(variance_per_bar: float, step: timedelta) -> float:
    """``sqrt(variance * bars_per_year)``: annualised volatility as a fraction."""
    return math.sqrt(variance_per_bar * bars_per_year(step))


def yang_zhang_vol(window: Sequence[Bar], step: timedelta) -> float:
    """Annualised Yang-Zhang volatility of a window of ``n + 1`` consecutive bars."""
    return annualise_variance(yang_zhang_terms(window).variance, step)


def realized_vol_samples(
    bars: Sequence[Bar],
    step: timedelta,
    as_of: datetime,
    *,
    n: int = P.REALIZED_VOL_BARS,
) -> list[Sample]:
    """Annualised Yang-Zhang vol of every ``n``-bar window ending at a visible bar.

    Sample ``time`` is the close of the window's last bar. Windows that span a
    hole in the data are skipped, not bridged. Each window uses only its own bars,
    so a sample never depends on anything after it.
    """
    seen = visible_bars(bars, as_of)
    samples: list[Sample] = []
    for end in range(n, len(seen)):
        window = seen[end - n : end + 1]
        if not is_contiguous(window, step):
            continue
        last = window[-1]
        samples.append(Sample(last.close_time, last.available_at, yang_zhang_vol(window, step)))
    return samples
