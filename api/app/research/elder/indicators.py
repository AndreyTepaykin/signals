"""Elder indicators for the triple-screen research.

Every function takes a series that may contain ``None`` and returns a list of
the same length. A ``None`` input at index ``t`` writes ``None`` at ``t`` and
resets that indicator: the next contiguous run of numbers is warmed from
scratch, never bridged across the hole. A cold value (not enough bars yet, or
still inside the restart) is ``None``, never ``0``. A computed zero, such as a
flat Force Index or a MACD histogram that has settled on zero, is ``0.0``.

Sums use :func:`math.fsum`. EMA seeds are simple means of the first ``period``
values of the current run; the recursive step uses ``alpha = 2 / (period + 1)``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.research.elder.resample import OHLCV


def _require_period(period: int, name: str = 'period') -> None:
    if period < 1:
        raise ValueError(f'{name} must be >= 1')


def _same_length(series: Sequence[float | None], *rest: Sequence[float | None]) -> int:
    n = len(series)
    for other in rest:
        if len(other) != n:
            raise ValueError('input series must have the same length')
    return n


def _runs(valid: Sequence[bool]) -> list[tuple[int, int]]:
    """Half-open ``[start, end)`` spans of consecutive ``True`` flags."""
    spans: list[tuple[int, int]] = []
    index = 0
    total = len(valid)
    while index < total:
        if not valid[index]:
            index += 1
            continue
        start = index
        while index < total and valid[index]:
            index += 1
        spans.append((start, index))
    return spans


def ema(values: Sequence[float | None], period: int) -> list[float | None]:
    """EMA seeded by the SMA of the current run's first ``period`` values.

    The seed is written at run index ``period - 1``. From the next bar,

        ema = alpha * value + (1 - alpha) * previous,  alpha = 2 / (period + 1).

    A ``None`` ends the run. The following numbers warm a new EMA; a run shorter
    than ``period`` stays entirely ``None``.
    """
    _require_period(period)
    out: list[float | None] = [None] * len(values)
    alpha = 2.0 / (period + 1)
    present = [value is not None for value in values]
    for start, end in _runs(present):
        run = [float(values[i]) for i in range(start, end)]
        if len(run) < period:
            continue
        previous = math.fsum(run[:period]) / period
        out[start + period - 1] = previous
        for offset in range(period, len(run)):
            previous = alpha * run[offset] + (1.0 - alpha) * previous
            out[start + offset] = previous
    return out


def slope(values: Sequence[float | None]) -> list[float | None]:
    """``value[t] - value[t - 1]``, or ``None`` when either side is ``None``.

    The bar after a hole has no slope: the hole resets the difference, it does
    not subtract across the gap. There is no warmup beyond that one previous bar.
    """
    out: list[float | None] = [None] * len(values)
    for index in range(1, len(values)):
        previous, current = values[index - 1], values[index]
        if previous is None or current is None:
            continue
        out[index] = current - previous
    return out


def macd_hist(
    closes: Sequence[float | None],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list[float | None]:
    """MACD histogram: ``ema(fast) - ema(slow)``, minus an EMA of that line.

    The MACD line exists only where both EMAs exist. The signal line is
    :func:`ema` of the MACD line with period ``signal``, so its seed is the SMA
    of the first ``signal`` MACD values of the current run. Histogram is
    ``macd - signal``.

    On a clean run with ``slow >= fast`` the first histogram value is at index
    ``slow + signal - 2`` (the 34th bar for 12/26/9, index 33). A ``None`` close
    restarts both EMAs and therefore the signal line.
    """
    _require_period(fast, 'fast')
    _require_period(slow, 'slow')
    _require_period(signal, 'signal')
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd: list[float | None] = [
        None if fast_value is None or slow_value is None else fast_value - slow_value
        for fast_value, slow_value in zip(fast_ema, slow_ema)
    ]
    signal_line = ema(macd, signal)
    return [
        None if macd_value is None or signal_value is None else macd_value - signal_value
        for macd_value, signal_value in zip(macd, signal_line)
    ]


def force_index(
    closes: Sequence[float | None],
    volumes: Sequence[float | None],
    period: int = 2,
) -> list[float | None]:
    """EMA of raw Force Index, Elder's ``period`` defaulting to 2.

    A bar with a ``None`` close or volume ends the run. Inside a run the first
    bar has no previous close, so its raw value is undefined. Later bars are

        raw = (close - previous_close) * volume.

    The returned series is :func:`ema` of that raw series. The first value of a
    run therefore lands at run index ``period`` (one bar to form the first raw
    value, then ``period`` raw values for the SMA seed).
    """
    _require_period(period)
    n = _same_length(closes, volumes)
    raw: list[float | None] = [None] * n
    for index in range(1, n):
        close, volume = closes[index], volumes[index]
        previous_close, previous_volume = closes[index - 1], volumes[index - 1]
        if close is None or volume is None or previous_close is None or previous_volume is None:
            continue
        raw[index] = (close - previous_close) * volume
    return ema(raw, period)


def stochastic(
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    closes: Sequence[float | None],
    period: int = 5,
    smooth: int = 3,
) -> list[float | None]:
    """Slow %K: SMA of raw %K.

    Raw %K at ``t`` uses the last ``period`` bars of the current run:

        100 * (close - lowest low) / (highest high - lowest low).

    The result is the SMA of the last ``smooth`` raw values. The first slow %K
    of a clean run is at index ``period + smooth - 2``.

    A ``None`` high, low or close is a hole: it writes ``None``, throws away
    the price window, and the next run warms from scratch.

    A flat window (highest high == lowest low) is different. Raw %K at that bar
    is ``None``, a cold value inside the SMA window, so the smoothed value stays
    ``None`` while that bar is one of the ``smooth`` values being averaged. It
    does not reset the price history: later bars still look back through the
    flat bar, and the SMA resumes as soon as the flat raw value has slid out of
    the window. Exact equality, no tolerance.
    """
    _require_period(period, 'period')
    _require_period(smooth, 'smooth')
    n = _same_length(highs, lows, closes)
    valid = [
        highs[i] is not None and lows[i] is not None and closes[i] is not None
        for i in range(n)
    ]
    raw: list[float | None] = [None] * n
    for start, end in _runs(valid):
        run_high = [float(highs[i]) for i in range(start, end)]
        run_low = [float(lows[i]) for i in range(start, end)]
        run_close = [float(closes[i]) for i in range(start, end)]
        for offset in range(period - 1, end - start):
            window_high = run_high[offset - period + 1 : offset + 1]
            window_low = run_low[offset - period + 1 : offset + 1]
            highest = max(window_high)
            lowest = min(window_low)
            if highest == lowest:
                continue
            raw[start + offset] = 100.0 * (run_close[offset] - lowest) / (highest - lowest)
    out: list[float | None] = [None] * n
    for index in range(smooth - 1, n):
        window = raw[index - smooth + 1 : index + 1]
        if any(value is None for value in window):
            continue
        out[index] = math.fsum(value for value in window if value is not None) / smooth
    return out


def elder_ray(
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    closes: Sequence[float | None],
    period: int = 13,
) -> tuple[list[float | None], list[float | None]]:
    """Bull power and bear power: ``high - ema(close)``, ``low - ema(close)``.

    A ``None`` in high, low or close at ``t`` writes ``None`` for both powers
    and restarts the EMA. The close series the EMA sees is treated as holed
    wherever any of the three is missing, so a gap in the highs alone does not
    keep a stale average alive.
    """
    _require_period(period)
    n = _same_length(highs, lows, closes)
    closes_for_ema: list[float | None] = [
        None if highs[i] is None or lows[i] is None or closes[i] is None else float(closes[i])
        for i in range(n)
    ]
    basis = ema(closes_for_ema, period)
    bull: list[float | None] = [None] * n
    bear: list[float | None] = [None] * n
    for index, value in enumerate(basis):
        if value is None:
            continue
        bull[index] = float(highs[index]) - value
        bear[index] = float(lows[index]) - value
    return bull, bear


def impulse(
    closes: Sequence[float | None],
    ema_period: int = 13,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list[int | None]:
    """Elder's Impulse: agreement of the EMA slope and the MACD-histogram slope.

    ``+1`` when both slopes are strictly positive, ``-1`` when both are strictly
    negative, ``0`` otherwise — including a zero slope, and including slopes that
    disagree. ``None`` when either slope is ``None`` (the EMA or the histogram
    is still cold, or a hole has reset one of them).
    """
    _require_period(ema_period, 'ema_period')
    ema_slope = slope(ema(closes, ema_period))
    hist_slope = slope(macd_hist(closes, fast, slow, signal))
    out: list[int | None] = [None] * len(closes)
    for index, (ema_step, hist_step) in enumerate(zip(ema_slope, hist_slope)):
        if ema_step is None or hist_step is None:
            continue
        if ema_step > 0.0 and hist_step > 0.0:
            out[index] = 1
        elif ema_step < 0.0 and hist_step < 0.0:
            out[index] = -1
        else:
            out[index] = 0
    return out


def wilder_atr(
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    closes: Sequence[float | None],
    period: int = 14,
) -> list[float | None]:
    """Wilder ATR. The first bar of a run has no true range.

    From the run's second bar,

        TR = max(high - low, abs(high - previous_close), abs(low - previous_close)).

    The seed is the mean of the first ``period`` true ranges and is written at
    run index ``period``. After that

        ATR = (previous_ATR * (period - 1) + TR) / period.

    A ``None`` high, low or close ends the run and the next bars warm a new
    ATR. A flat market, where every true range is zero, has ATR ``0.0``: that
    zero is computed, not cold. Cold (fewer than ``period`` true ranges) is
    ``None``.

    The seed and the recursion match
    ``app.core.features.volatility.wilder_atr_from_ranges``. That module's
    public ``wilder_atr`` is a single reading on the last visible bar of a
    history-capped contiguous suffix, and it reports missing when the result
    is not positive. This function returns the whole series and keeps a
    computed zero.
    """
    _require_period(period)
    n = _same_length(highs, lows, closes)
    valid = [
        highs[i] is not None and lows[i] is not None and closes[i] is not None
        for i in range(n)
    ]
    out: list[float | None] = [None] * n
    for start, end in _runs(valid):
        if end - start < period + 1:
            continue
        true_ranges: list[float] = []
        for index in range(start + 1, end):
            high = float(highs[index])
            low = float(lows[index])
            previous_close = float(closes[index - 1])
            true_ranges.append(
                max(high - low, abs(high - previous_close), abs(low - previous_close))
            )
        atr = math.fsum(true_ranges[:period]) / period
        out[start + period] = atr
        for offset in range(period, len(true_ranges)):
            atr = (atr * (period - 1) + true_ranges[offset]) / period
            out[start + 1 + offset] = atr
    return out


def ohlcv_fields(
    bars: Sequence[OHLCV | None],
) -> tuple[
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
]:
    """``(opens, highs, lows, closes, volumes)`` from aligned bars.

    A ``None`` slot, the mark :func:`app.research.elder.resample.align` leaves
    for a skipped bucket, is ``None`` in every series.
    """
    opens: list[float | None] = []
    highs: list[float | None] = []
    lows: list[float | None] = []
    closes: list[float | None] = []
    volumes: list[float | None] = []
    for bar in bars:
        if bar is None:
            opens.append(None)
            highs.append(None)
            lows.append(None)
            closes.append(None)
            volumes.append(None)
        else:
            opens.append(bar.open)
            highs.append(bar.high)
            lows.append(bar.low)
            closes.append(bar.close)
            volumes.append(bar.volume)
    return opens, highs, lows, closes, volumes
