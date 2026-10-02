"""Plain data carriers and the visibility filter every pure feature uses.

The feature math never touches a database. It receives lists of these records
(read through ``as_of`` by the assembly job, or loaded from memory by a replay)
and returns numbers. Two guarantees live here because everything else leans on
them:

1. **Visibility.** Every record carries ``available_at``. :func:`visible`
   drops anything not yet available at the as-of instant, so a pure function is
   causal even if a caller hands it the whole future by mistake. The database
   gate (``as_of``) and this filter are two independent layers of the same rule.
2. **Order independence.** :func:`visible` returns records sorted by their
   economic time, and raises on a duplicate. Input order, dict ordering and
   iteration order therefore never reach the arithmetic.

Prices and volumes are ``float`` here. They come from ``numeric`` columns as
``Decimal`` and are converted once, at the boundary, in the worker.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from app.core.time import ensure_utc

T = TypeVar('T')


def _utc(instant: datetime) -> datetime:
    return ensure_utc(instant)


@dataclass(frozen=True)
class Bar:
    """A closed OHLCV bar."""

    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ('open_time', 'close_time', 'available_at'):
            object.__setattr__(self, name, _utc(getattr(self, name)))
        if self.close_time <= self.open_time:
            raise ValueError('close_time must be after open_time')
        if self.high < self.low:
            raise ValueError('high must not be below low')

    @classmethod
    def make(
        cls,
        open_time: datetime,
        step: timedelta,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
        taker_buy_volume: float = 0.0,
    ) -> Bar:
        """A bar whose ``available_at`` is its close, as for every kline."""
        close_time = _utc(open_time) + step
        return cls(open_time, close_time, open, high, low, close, volume, taker_buy_volume, close_time)


@dataclass(frozen=True)
class OIPoint:
    """An open-interest reading. ``poll_time`` is the boundary it describes."""

    poll_time: datetime
    available_at: datetime
    open_interest: float

    def __post_init__(self) -> None:
        object.__setattr__(self, 'poll_time', _utc(self.poll_time))
        object.__setattr__(self, 'available_at', _utc(self.available_at))


@dataclass(frozen=True)
class FundingPrint:
    """A funding print (settled or predicted); ``rate`` is per interval."""

    funding_time: datetime
    available_at: datetime
    rate: float
    interval_hours: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'funding_time', _utc(self.funding_time))
        object.__setattr__(self, 'available_at', _utc(self.available_at))


@dataclass(frozen=True)
class ForcePrint:
    """A liquidation print. ``quantity`` is base units; ``side`` is the liquidating order's side."""

    traded_at: datetime
    available_at: datetime
    side: str
    price: float
    quantity: float

    def __post_init__(self) -> None:
        object.__setattr__(self, 'traded_at', _utc(self.traded_at))
        object.__setattr__(self, 'available_at', _utc(self.available_at))


@dataclass(frozen=True)
class CalendarEvent:
    """A calendar occurrence. ``is_large`` None means undecided, not "not large"."""

    kind: str
    event_time: datetime
    available_at: datetime
    is_large: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'event_time', _utc(self.event_time))
        object.__setattr__(self, 'available_at', _utc(self.available_at))


@dataclass(frozen=True)
class Sample:
    """One observation of a scalar statistic.

    ``time`` is the economic instant the value describes and is what the
    90-day window is measured on; ``available_at`` is when it could be known and
    is what visibility is judged on.
    """

    time: datetime
    available_at: datetime
    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, 'time', _utc(self.time))
        object.__setattr__(self, 'available_at', _utc(self.available_at))


class SeriesError(ValueError):
    """The input series is malformed (duplicate timestamps, wrong order)."""


def visible(
    items: Sequence[T],
    as_of: datetime,
    *,
    time_of: Callable[[T], datetime],
    available_of: Callable[[T], datetime],
) -> list[T]:
    """Items whose ``available_at`` is at or before ``as_of``, sorted by economic time.

    Raises :class:`SeriesError` on a duplicate economic time.
    """
    cutoff = _utc(as_of)
    kept = [item for item in items if available_of(item) <= cutoff]
    kept.sort(key=time_of)
    for earlier, later in zip(kept, kept[1:]):
        if time_of(earlier) == time_of(later):
            raise SeriesError(f'duplicate timestamp {time_of(earlier).isoformat()}')
    return kept


def visible_bars(bars: Sequence[Bar], as_of: datetime) -> list[Bar]:
    return visible(bars, as_of, time_of=lambda b: b.open_time, available_of=lambda b: b.available_at)


def visible_oi(points: Sequence[OIPoint], as_of: datetime) -> list[OIPoint]:
    return visible(points, as_of, time_of=lambda p: p.poll_time, available_of=lambda p: p.available_at)


def visible_funding(prints: Sequence[FundingPrint], as_of: datetime) -> list[FundingPrint]:
    return visible(prints, as_of, time_of=lambda p: p.funding_time, available_of=lambda p: p.available_at)


def visible_samples(samples: Sequence[Sample], as_of: datetime) -> list[Sample]:
    return visible(samples, as_of, time_of=lambda s: s.time, available_of=lambda s: s.available_at)


def is_contiguous(bars: Sequence[Bar], step: timedelta) -> bool:
    """True when each bar opens exactly ``step`` after the previous one and lasts ``step``."""
    for bar in bars:
        if bar.close_time - bar.open_time != step:
            return False
    return all(later.open_time - earlier.open_time == step for earlier, later in zip(bars, bars[1:]))


def contiguous_suffix(bars: Sequence[Bar], step: timedelta) -> list[Bar]:
    """The longest run of consecutive bars that ends at the last bar.

    A hole in the data restarts the run. Anything before the hole is not used,
    which is deterministic: the result depends only on the bars, not on how they
    were fetched.
    """
    if not bars:
        return []
    start = len(bars) - 1
    if bars[start].close_time - bars[start].open_time != step:
        return []
    while start > 0:
        previous, current = bars[start - 1], bars[start]
        if current.open_time - previous.open_time != step or previous.close_time - previous.open_time != step:
            break
        start -= 1
    return list(bars[start:])


def tail_if_contiguous(bars: Sequence[Bar], count: int, step: timedelta) -> list[Bar] | None:
    """The last ``count`` bars if they are consecutive, else None."""
    if count <= 0 or len(bars) < count:
        return None
    tail = list(bars[-count:])
    return tail if is_contiguous(tail, step) else None


def floor_to(instant: datetime, step: timedelta) -> datetime:
    """Round ``instant`` down to a multiple of ``step`` counted from the Unix epoch (UTC)."""
    utc = _utc(instant)
    epoch = datetime(1970, 1, 1, tzinfo=utc.tzinfo)
    return epoch + ((utc - epoch) // step) * step
