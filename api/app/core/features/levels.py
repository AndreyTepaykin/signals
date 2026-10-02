"""Location indicators and the 4h structure: VWAP, volume profile, swings, chandelier.

Session
-------
"Session VWAP in UTC" and "Session profile" both reset at **00:00 UTC** (the UTC
calendar day of the signal bar), not at the Asia/Europe/US bucket boundaries.
The bucket boundaries are already the regime slice's ``session`` column; a
VWAP that restarted every 6-8 hours would be a different, much noisier object.
Flagged as an ambiguity. The day of the signal bar is the day of its OPEN, so a
bar that closes at exactly 00:00 still belongs to the day that just ended.
Fewer than ``SESSION_MIN_BARS`` bars into the day -> NULL (a one-bar VWAP is
just the price, and "distance 0" would be a false claim).

VWAP
----
``VWAP = sum(typical * volume) / sum(volume)`` with ``typical = (H + L + C) / 3``
(the textbook OHLCV form; it does not depend on the optional quote-volume
column). Distances are ``(price - level) / ATR``: POSITIVE when price is ABOVE
the level. Every distance column documents this sign.

Volume profile
--------------
``bins`` equal-width price bins between the session's lowest low and highest
high. Each bar's volume is spread uniformly over the bar's [low, high] range
in proportion to the overlap with each bin (a bar with high == low puts all of
its volume in the bin containing that price). POC is the bin with the most
volume (ties: the LOWEST bin) and is reported as the bin's centre. The value
area starts at the POC bin and repeatedly adds the larger of the single
neighbouring bins above and below (ties: above) until it holds at least 70 % of
the session volume; ``value_area_low`` / ``value_area_high`` are the outer
EDGES of the included bins. This is the single-bin expansion, not the
Market Profile two-row rule.

Swings (4h)
-----------
"The last 2-bar fractal swing" is read as **two bars on each side**: bar ``i``
is a swing high when ``high[i]`` is STRICTLY greater than the highs of bars
``i-2, i-1, i+1, i+2`` (a swing low mirrors it). A swing therefore needs two
LATER bars to exist, so it is confirmed two 4h bars after it happened and the
newest bars can never be the "last swing". A 4h close "beyond" the swing is a
close strictly above the last confirmed swing high (or below the last swing low).
If both hold, the more recently formed swing decides.

Chandelier
----------
For the direction of the swing break: the extreme high (long) or low (short)
of the 4h bars from the BROKEN swing's bar through the latest completed 4h bar,
minus (long) or plus (short) ``CHANDELIER_ATR_MULT`` x ATR. The distance is
``(price - stop) / ATR`` for a long and ``(stop - price) / ATR`` for a short,
i.e. POSITIVE when the stop is on the protective side of price; a value at or
below zero means the stop is already breached.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import constants as C
from app.core.features import params as P
from app.core.features.reading import Reading, have, missing
from app.core.features.series import Bar, contiguous_suffix, visible_bars

_DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------
def typical_price(bar: Bar) -> float:
    return (bar.high + bar.low + bar.close) / 3.0


def vwap_of(bars: Sequence[Bar]) -> float | None:
    """Volume-weighted typical price, or None if nothing traded."""
    volume = math.fsum(b.volume for b in bars)
    if volume <= 0.0:
        return None
    return math.fsum(typical_price(b) * b.volume for b in bars) / volume


def utc_day_start(instant: datetime) -> datetime:
    return instant.replace(hour=0, minute=0, second=0, microsecond=0)


def session_bars(bars: Sequence[Bar], bar_close: datetime, as_of: datetime, step: timedelta) -> list[Bar] | Reading:
    """Consecutive bars from the UTC day start of the signal bar through the signal bar."""
    seen = visible_bars(bars, as_of)
    if not seen or seen[-1].close_time != bar_close:
        return missing('signal bar is not in the visible 15m series')
    signal = seen[-1]
    start = utc_day_start(signal.open_time)
    todays = [b for b in seen if b.open_time >= start]
    run = contiguous_suffix(todays, step)
    if not run or run[0].open_time != start:
        return missing('the UTC day is not covered by consecutive bars from 00:00')
    if len(run) < P.SESSION_MIN_BARS:
        return missing(f'only {len(run)} bars into the session, need {P.SESSION_MIN_BARS}')
    return run


def session_vwap(bars: Sequence[Bar], bar_close: datetime, as_of: datetime, step: timedelta) -> Reading:
    run = session_bars(bars, bar_close, as_of, step)
    if isinstance(run, Reading):
        return run
    value = vwap_of(run)
    return have(value) if value is not None else missing('no volume in the session')


def anchored_vwap(bars: Sequence[Bar], anchor: datetime, bar_close: datetime, as_of: datetime, step: timedelta) -> Reading:
    """VWAP from the bar that opens at ``anchor`` through the signal bar."""
    seen = visible_bars(bars, as_of)
    if not seen or seen[-1].close_time != bar_close:
        return missing('signal bar is not in the visible 15m series')
    after = [b for b in seen if b.open_time >= anchor]
    run = contiguous_suffix(after, step)
    if not run or run[0].open_time != anchor:
        return missing('the anchor is not covered by consecutive 15m bars through the signal bar')
    value = vwap_of(run)
    return have(value) if value is not None else missing('no volume since the anchor')


def distance_atr(price: float, level: float, atr: float) -> float:
    """``(price - level) / atr``: positive when price is above the level."""
    return (price - level) / atr


# ---------------------------------------------------------------------------
# Volume profile
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VolumeProfile:
    edges: tuple[float, ...]  # bins + 1 boundaries, ascending
    volumes: tuple[float, ...]
    poc_index: int
    poc_price: float
    value_area_low: float
    value_area_high: float
    value_area_low_index: int
    value_area_high_index: int


def volume_profile(
    bars: Sequence[Bar],
    bins: int = C.VOLUME_PROFILE_BINS,
    value_area: float = C.VOLUME_PROFILE_VALUE_AREA,
) -> VolumeProfile | None:
    """Profile of ``bars``; None if there is no volume or no price range."""
    if bins < 1 or not 0.0 < value_area <= 1.0:
        raise ValueError('bins must be >= 1 and value_area in (0, 1]')
    if not bars:
        return None
    low = min(b.low for b in bars)
    high = max(b.high for b in bars)
    if high <= low:
        return None
    edges = [low + (high - low) * i / bins for i in range(bins + 1)]
    edges[0], edges[-1] = low, high

    contributions: list[list[float]] = [[] for _ in range(bins)]
    for bar in bars:
        if bar.volume <= 0.0:
            continue
        if bar.high == bar.low:
            index = min(int((bar.low - low) / (high - low) * bins), bins - 1)
            contributions[index].append(bar.volume)
            continue
        span = bar.high - bar.low
        for i in range(bins):
            overlap = min(bar.high, edges[i + 1]) - max(bar.low, edges[i])
            if overlap > 0.0:
                contributions[i].append(bar.volume * overlap / span)
    volumes = [math.fsum(items) for items in contributions]
    total = math.fsum(volumes)
    if total <= 0.0:
        return None

    poc = 0
    for i, volume in enumerate(volumes):
        if volume > volumes[poc]:
            poc = i
    lo_idx = hi_idx = poc
    accumulated = volumes[poc]
    target = value_area * total
    while accumulated < target:
        above = volumes[hi_idx + 1] if hi_idx + 1 < bins else None
        below = volumes[lo_idx - 1] if lo_idx - 1 >= 0 else None
        if above is None and below is None:
            break
        if below is None or (above is not None and above >= below):
            hi_idx += 1
            accumulated += volumes[hi_idx]
        else:
            lo_idx -= 1
            accumulated += volumes[lo_idx]
    return VolumeProfile(
        edges=tuple(edges),
        volumes=tuple(volumes),
        poc_index=poc,
        poc_price=(edges[poc] + edges[poc + 1]) / 2.0,
        value_area_low=edges[lo_idx],
        value_area_high=edges[hi_idx + 1],
        value_area_low_index=lo_idx,
        value_area_high_index=hi_idx,
    )


def session_volume_profile(
    bars: Sequence[Bar], bar_close: datetime, as_of: datetime, step: timedelta
) -> tuple[VolumeProfile | None, str | None]:
    run = session_bars(bars, bar_close, as_of, step)
    if isinstance(run, Reading):
        return None, run.reason
    profile = volume_profile(run)
    if profile is None:
        return None, 'no volume or no price range in the session'
    return profile, None


# ---------------------------------------------------------------------------
# Swings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Swing:
    index: int  # position in the series passed in
    open_time: datetime  # open of the 4h bar that is the swing
    price: float  # its high (swing high) or low (swing low)
    kind: str  # 'high' | 'low'


def fractal_swings(bars: Sequence[Bar], span: int = C.SWING_FRACTAL_BARS) -> list[Swing]:
    """Every CONFIRMED fractal swing in ``bars`` (consecutive bars assumed), oldest first."""
    out: list[Swing] = []
    for i in range(span, len(bars) - span):
        neighbours = [bars[i + offset] for offset in range(-span, span + 1) if offset != 0]
        if all(bars[i].high > n.high for n in neighbours):
            out.append(Swing(i, bars[i].open_time, bars[i].high, 'high'))
        if all(bars[i].low < n.low for n in neighbours):
            out.append(Swing(i, bars[i].open_time, bars[i].low, 'low'))
    return out


@dataclass(frozen=True)
class SwingState:
    run: tuple[Bar, ...]  # the consecutive 4h bars used
    last_high: Swing | None
    last_low: Swing | None
    broken: Swing | None  # the swing the latest close is beyond
    break_direction: str | None  # 'long' | 'short'
    latest_swing: Swing | None  # most recent confirmed swing of either kind


def swing_state(bars: Sequence[Bar], as_of: datetime, step: timedelta) -> tuple[SwingState | None, str | None]:
    """Swings and the break state at the last completed 4h bar visible at ``as_of``."""
    run = contiguous_suffix(visible_bars(bars, as_of), step)[-P.SWING_HISTORY_BARS :]
    if len(run) < 2 * C.SWING_FRACTAL_BARS + 2:
        return None, f'needs at least {2 * C.SWING_FRACTAL_BARS + 2} consecutive 4h bars, have {len(run)}'
    swings = fractal_swings(run)
    highs = [s for s in swings if s.kind == 'high']
    lows = [s for s in swings if s.kind == 'low']
    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None
    if last_high is None and last_low is None:
        return None, 'no confirmed 4h fractal swing in the window'
    latest = run[-1].close
    long_break = last_high is not None and latest > last_high.price
    short_break = last_low is not None and latest < last_low.price
    broken: Swing | None = None
    direction: str | None = None
    if long_break and short_break:
        broken = last_high if last_high.index > last_low.index else last_low  # type: ignore[union-attr]
        direction = 'long' if broken.kind == 'high' else 'short'
    elif long_break:
        broken, direction = last_high, 'long'
    elif short_break:
        broken, direction = last_low, 'short'
    candidates = [s for s in (last_high, last_low) if s is not None]
    latest_swing = max(candidates, key=lambda s: s.index)
    return SwingState(tuple(run), last_high, last_low, broken, direction, latest_swing), None


# ---------------------------------------------------------------------------
# Chandelier
# ---------------------------------------------------------------------------
def chandelier_stop(run: Sequence[Bar], anchor_index: int, direction: str, atr: float, multiple: float = C.CHANDELIER_ATR_MULT) -> float:
    """Extreme since the anchor bar, minus (long) or plus (short) ``multiple`` x ATR."""
    since = run[anchor_index:]
    if not since:
        raise ValueError('anchor is beyond the series')
    if direction == 'long':
        return max(b.high for b in since) - multiple * atr
    if direction == 'short':
        return min(b.low for b in since) + multiple * atr
    raise ValueError(f'direction must be long or short, got {direction!r}')


def chandelier_distance_atr(price: float, stop: float, direction: str, atr: float) -> float:
    """Room between price and the stop in ATR, positive when the stop protects."""
    return (price - stop) / atr if direction == 'long' else (stop - price) / atr
