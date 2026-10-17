"""Resample 1h bars to 6H and 4h bars to 1W.

Times are timezone-aware UTC. ``close_time`` is the exclusive end of the bar,
``open_time + length``, not Binance's close (which is one millisecond earlier).
The loader converts; nothing here subtracts that millisecond.

Buckets
-------
6H bars open at 00:00, 06:00, 12:00 and 18:00 UTC. Weekly bars open on Monday
00:00 UTC. A bucket is emitted only when every source bar is present, with open
times exactly ``bucket + i * source_step`` (6 hourly bars, or 42 four-hour
bars). Anything else — a leading partial bucket, a trailing partial bucket, or
a hole inside — drops that bucket and leaves its neighbours alone. A dropped
bucket is a missing bar. :func:`align` turns the gap into a ``None`` slot so an
indicator sees a hole and restarts, rather than bridging across it.

Input must be strictly sorted by ``open_time`` and each source open must sit on
the source step (exact hours for 1h; 00/04/08/12/16/20 UTC for 4h). Unsorted,
duplicate, or off-grid input raises ``ValueError``.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)

STEP_15M = timedelta(minutes=15)
STEP_1H = timedelta(hours=1)
STEP_4H = timedelta(hours=4)
STEP_6H = timedelta(hours=6)
STEP_1W = timedelta(days=7)

_BARS_PER_6H = 6
_BARS_PER_WEEK = 42


@dataclass(frozen=True)
class OHLCV:
    """One bar. ``close_time`` is the exclusive end, ``open_time + length``."""

    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _as_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('timestamp must be timezone-aware')
    return instant.astimezone(_UTC)


def _on_grid(instant: datetime, step: timedelta) -> bool:
    return (instant - _EPOCH) % step == timedelta(0)


def bucket_start_6h(t: datetime) -> datetime:
    """Floor ``t`` to 00:00, 06:00, 12:00 or 18:00 UTC.

    Converted to UTC before flooring, so the clock is UTC even when ``t``
    carries another offset. An exact boundary maps to itself: 06:00 starts the
    next bucket, 05:00 still belongs to 00:00.
    """
    u = _as_utc(t)
    hour = (u.hour // 6) * 6
    return u.replace(hour=hour, minute=0, second=0, microsecond=0)


def bucket_start_1w(t: datetime) -> datetime:
    """Floor ``t`` to Monday 00:00 UTC.

    Converted to UTC before flooring. Monday 00:00 UTC maps to itself. Sunday
    20:00 UTC, the last 4h bar of a Binance week, maps to the previous Monday.
    """
    u = _as_utc(t)
    midnight = u.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


def _validate_source(bars: Sequence[OHLCV], source_step: timedelta) -> None:
    previous: datetime | None = None
    for bar in bars:
        opened = _as_utc(bar.open_time)
        if not _on_grid(opened, source_step):
            raise ValueError(
                f'source bar open_time {bar.open_time.isoformat()} is not aligned to {source_step}'
            )
        if previous is not None and opened <= previous:
            if opened == previous:
                raise ValueError(f'duplicate open_time {opened.isoformat()}')
            raise ValueError(f'bars are not strictly sorted by open_time at {opened.isoformat()}')
        previous = opened


def _aggregate(group: Sequence[OHLCV], start: datetime, target_step: timedelta) -> OHLCV:
    return OHLCV(
        open_time=start,
        close_time=start + target_step,
        open=group[0].open,
        high=max(bar.high for bar in group),
        low=min(bar.low for bar in group),
        close=group[-1].close,
        volume=math.fsum(bar.volume for bar in group),
    )


def _resample(
    bars: Sequence[OHLCV],
    source_step: timedelta,
    count: int,
    target_step: timedelta,
    bucket_fn: Callable[[datetime], datetime],
) -> list[OHLCV]:
    _validate_source(bars, source_step)
    out: list[OHLCV] = []
    index = 0
    total = len(bars)
    while index < total:
        start = bucket_fn(bars[index].open_time)
        end = index + 1
        while end < total and bucket_fn(bars[end].open_time) == start:
            end += 1
        group = bars[index:end]
        expected = [start + offset * source_step for offset in range(count)]
        if [_as_utc(bar.open_time) for bar in group] == expected:
            out.append(_aggregate(group, start, target_step))
        index = end
    return out


def to_6h(bars_1h: Sequence[OHLCV]) -> list[OHLCV]:
    """Build 6H bars from 1h bars on the 00/06/12/18 UTC grid.

    Emitted only when hours ``bucket + 0h .. + 5h`` are all present. Open is
    the first open, high the max, low the min, close the last close, volume
    the sum. ``close_time`` is ``bucket + 6h``, taken from the bucket rather
    than from the source bars' own close times.
    """
    return _resample(bars_1h, STEP_1H, _BARS_PER_6H, STEP_6H, bucket_start_6h)


def to_1w(bars_4h: Sequence[OHLCV]) -> list[OHLCV]:
    """Build weekly bars from 4h bars, opening Monday 00:00 UTC.

    Emitted only when all 42 bars ``monday + i * 4h`` (``i = 0 .. 41``) are
    present. The last of those opens Sunday 20:00 UTC and still belongs to
    that Monday. ``close_time`` is the next Monday 00:00 UTC.
    """
    return _resample(bars_4h, STEP_4H, _BARS_PER_WEEK, STEP_1W, bucket_start_1w)


def align(bars: Sequence[OHLCV], step: timedelta) -> list[OHLCV | None]:
    """One slot per ``step`` from the first bar's open to the last.

    A slot whose open time has no bar is ``None``. That is how a bucket
    :func:`to_6h` or :func:`to_1w` skipped becomes a hole for the indicators.
    ``bars`` must be strictly sorted by ``open_time`` and every open must fall
    on the grid defined by the first bar and ``step``.
    """
    if step <= timedelta(0):
        raise ValueError('step must be positive')
    if not bars:
        return []
    opens = [_as_utc(bar.open_time) for bar in bars]
    for previous, current in zip(opens, opens[1:]):
        if current <= previous:
            if current == previous:
                raise ValueError(f'duplicate open_time {current.isoformat()}')
            raise ValueError(f'bars are not strictly sorted by open_time at {current.isoformat()}')
    origin = opens[0]
    if any((opened - origin) % step != timedelta(0) for opened in opens):
        raise ValueError('bar open_time is not aligned to step')
    out: list[OHLCV | None] = []
    cursor = origin
    index = 0
    last = opens[-1]
    while cursor <= last:
        if opens[index] == cursor:
            out.append(bars[index])
            index += 1
        else:
            out.append(None)
        cursor += step
    return out


def last_closed_index(bars: Sequence[OHLCV], as_of: datetime) -> int | None:
    """Index of the last bar with ``close_time <= as_of``, or ``None``.

    ``bars`` must be strictly sorted by ``close_time``. Equality counts as
    closed: a bar whose close time is exactly ``as_of`` is visible, and one
    microsecond earlier it is not. The search is ``bisect``. Callers choose
    ``as_of``; a decision on 15m bar ``t`` passes ``t``'s open time and therefore
    sees only higher-timeframe bars that have already closed.
    """
    as_of_utc = _as_utc(as_of)
    if not bars:
        return None
    close_times = [_as_utc(bar.close_time) for bar in bars]
    for previous, current in zip(close_times, close_times[1:]):
        if current <= previous:
            raise ValueError('bars must be strictly sorted by close_time')
    found = bisect.bisect_right(close_times, as_of_utc) - 1
    if found < 0:
        return None
    return found
