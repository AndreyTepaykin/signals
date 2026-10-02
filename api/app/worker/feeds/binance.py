"""Binance REST client for klines, funding and open interest.

This is a thin layer: it knows the endpoints, paginates, parses, and decides
which rows are fit to store. It does not touch the database.

The correctness rules that live here
------------------------------------
**Never return an unclosed bar.** Binance returns the in-progress candle as the
last element of a klines response. A bar is closed only when its closing
boundary (``open_time + timeframe``) is at or before the exchange's clock minus
the settle margin (``Settings.feed_kline_settle_seconds``, measured: the closing bar's values keep changing for up to ~8 s after the boundary). Anything later is dropped and counted in
``dropped_unclosed``. The exchange's clock is estimated from ``/time`` (see
:class:`ServerClock`), not trusted from the local machine, because a local clock
running a few seconds fast would otherwise call a half-formed bar closed.

**Pagination.** Klines and funding are paged with ``startTime`` and a cursor
that moves one bar (or one millisecond) past the last row. Open-interest history
is different and the difference matters: with both ``startTime`` and ``endTime``
it returns the LAST ``limit`` rows of the window, not the first (verified live
2026-10-02), so the client slices the range into windows no longer than
``limit`` periods and a single request can never be truncated.

**Open-interest history reaches back only ~30 days.** ``startTime`` older than
that is rejected outright with ``-1130``. :func:`clamp_open_interest_start`
moves the start forward and says so; nothing here pretends to backfill further.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings, get_settings
from app.core.time import ensure_utc, utc_now
from app.models import enums as E
from app.worker.feeds import records as R
from app.worker.feeds.http import (
    FUTURES_WEIGHT_LIMIT_1M,
    SPOT_WEIGHT_LIMIT_1M,
    BinanceHttp,
    RetryPolicy,
    SlidingWindow,
)

log = logging.getLogger(__name__)

#: Rows per klines request (Binance allows 1500 on futures, 1000 on spot).
KLINE_PAGE_LIMIT = 1000
FUNDING_PAGE_LIMIT = 1000
OPEN_INTEREST_PAGE_LIMIT = 500

#: ``openInterestHist`` serves strictly less than 30 days. The margin keeps the
#: first request of a long backfill from landing on the boundary as time passes.
OPEN_INTEREST_HISTORY_DAYS = 30
OPEN_INTEREST_MARGIN = timedelta(hours=1)


#: Re-measure the exchange clock offset this often.
CLOCK_REFRESH_SECONDS = 600.0


# ---------------------------------------------------------------------------
# exchange clock
# ---------------------------------------------------------------------------
class ServerClock:
    """Local UTC clock corrected by the measured offset to Binance's clock."""

    def __init__(
        self,
        fetch_server_ms: Callable[[], int] | None = None,
        *,
        local_now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        refresh_seconds: float = CLOCK_REFRESH_SECONDS,
    ) -> None:
        self._fetch = fetch_server_ms
        self._local_now, self._monotonic = local_now, monotonic
        self._refresh_seconds = refresh_seconds
        self._offset = timedelta(0)
        self._measured_at: float | None = None
        self._lock = threading.Lock()

    @property
    def offset(self) -> timedelta:
        return self._offset

    def now(self) -> datetime:
        self._maybe_refresh()
        return self._local_now() + self._offset

    def _maybe_refresh(self) -> None:
        if self._fetch is None:
            return
        with self._lock:
            stamp = self._monotonic()
            if self._measured_at is not None and stamp - self._measured_at < self._refresh_seconds:
                return
            try:
                before = self._local_now()
                server = R.from_ms(self._fetch())
                after = self._local_now()
            except Exception as exc:  # the time endpoint failing must not take ingest down with it
                log.warning('could not measure exchange clock offset (%r); keeping %s', exc, self._offset)
                self._measured_at = stamp
                return
            midpoint = before + (after - before) / 2
            self._offset = server - midpoint
            self._measured_at = stamp


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class Batch:
    """One page of parsed rows plus what was thrown away getting there."""

    records: list[Any]
    fetched: int = 0
    dropped_unclosed: int = 0
    rejected: int = 0
    requests: int = 1
    notes: list[str] = field(default_factory=list)


def futures_kline_weight(limit: int) -> int:
    """Documented cost of ``/fapi/v1/klines`` by ``limit``."""
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def clamp_open_interest_start(start: datetime, now: datetime) -> tuple[datetime, bool]:
    """Earliest start Binance's OI history will accept, and whether we moved it."""
    earliest = now - timedelta(days=OPEN_INTEREST_HISTORY_DAYS) + OPEN_INTEREST_MARGIN
    start = ensure_utc(start)
    return (earliest, True) if start < earliest else (start, False)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------
class BinanceClient:
    """Public market-data client. No key, no signature, no trading endpoint."""

    def __init__(
        self,
        *,
        futures: BinanceHttp | None = None,
        spot: BinanceHttp | None = None,
        clock: ServerClock | None = None,
        settings: Settings | None = None,
        kline_page_limit: int = KLINE_PAGE_LIMIT,
        funding_page_limit: int = FUNDING_PAGE_LIMIT,
        open_interest_page_limit: int = OPEN_INTEREST_PAGE_LIMIT,
        settle: timedelta | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.futures = futures or BinanceHttp(
            settings.binance_futures_url,
            weight_limit_1m=FUTURES_WEIGHT_LIMIT_1M,
            retry=retry,
            extra_windows={
                # Documented request-count budgets, per IP, per 5 minutes.
                'funding': SlidingWindow(500, 300),
                'open_interest_hist': SlidingWindow(1000, 300),
            },
        )
        self.spot = spot or BinanceHttp(settings.binance_spot_url, weight_limit_1m=SPOT_WEIGHT_LIMIT_1M, retry=retry)
        self.clock = clock or ServerClock(lambda: int(self.futures.get('/fapi/v1/time')['serverTime']))
        self.kline_page_limit = kline_page_limit
        self.funding_page_limit = funding_page_limit
        self.open_interest_page_limit = open_interest_page_limit
        #: A bar is called closed only this long after its boundary: Binance serves the closing bar with
        #: PROVISIONAL values for a few seconds (measured; see ``Settings.feed_kline_settle_seconds``).
        self.settle = settle if settle is not None else timedelta(seconds=settings.feed_kline_settle_seconds)
        #: Same idea for a settled funding print.
        self.funding_lag = timedelta(seconds=settings.feed_funding_publication_lag_seconds)
        #: How long after a poll boundary Binance serves the open-interest sample (measured; see config).
        self.open_interest_lag = timedelta(seconds=settings.feed_open_interest_publication_lag_seconds)
        self._funding_intervals: dict[str, int] | None = None
        self._funding_intervals_at: float = 0.0

    def close(self) -> None:
        self.futures.close()
        self.spot.close()

    def now(self) -> datetime:
        """The exchange's current time (UTC), as best we can tell."""
        return self.clock.now()

    @property
    def request_count(self) -> int:
        return self.futures.request_count + self.spot.request_count

    # -- klines ------------------------------------------------------------
    def latest_closed_open(self, timeframe: str, now: datetime | None = None) -> datetime:
        """Open time of the newest bar that is closed (and settled) at ``now``."""
        length = R.timeframe_delta(timeframe)
        cutoff = (now or self.now()) - self.settle
        boundary = _floor(cutoff, length)  # the most recent closing boundary <= cutoff
        return boundary - length

    def iter_klines(
        self,
        *,
        coin: str,
        market: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[Batch]:
        """Yield closed bars with ``open_time`` in ``[start, end]``, page by page.

        Pages are yielded as they arrive so the caller can commit each one; a
        crash halfway through a long backfill keeps everything before it.
        """
        if market not in E.CANDLE_MARKETS:
            raise ValueError(f'unknown market {market!r}')
        length = R.timeframe_delta(timeframe)
        length_ms = length // timedelta(milliseconds=1)
        now = self.now()
        cutoff = now - self.settle  # a bar is closed iff open_time + length <= cutoff
        cursor_ms = R.to_ms(start)
        end_ms = R.to_ms(end)
        seen: set[int] = set()

        while cursor_ms <= end_ms:
            wanted = (end_ms - cursor_ms) // length_ms + 1
            limit = max(1, min(self.kline_page_limit, wanted))
            page = self._kline_page(market, coin, timeframe, cursor_ms, end_ms, limit)
            if not page:
                yield Batch(records=[], fetched=0)  # an empty answer is still a request the summary must count
                return
            batch = Batch(records=[], fetched=len(page))
            for raw in page:
                try:
                    record = R.parse_kline_row(raw, coin=coin, market=market, timeframe=timeframe)
                except R.MalformedRow as exc:
                    batch.rejected += 1
                    batch.notes.append(f'{coin} {market} {timeframe}: rejected a malformed bar: {exc}')
                    continue
                if record.close_time > cutoff:
                    batch.dropped_unclosed += 1  # the in-progress candle: never stored
                    continue
                open_ms = R.to_ms(record.open_time)
                if open_ms in seen or open_ms > end_ms:
                    continue
                seen.add(open_ms)
                batch.records.append(record)
            yield batch

            next_cursor = int(page[-1][0]) + length_ms
            if next_cursor <= cursor_ms or len(page) < limit:
                return
            cursor_ms = next_cursor

    def klines(self, **kwargs: Any) -> Batch:
        """All closed bars in the range as one :class:`Batch` (see :meth:`iter_klines`)."""
        return _collect(self.iter_klines(**kwargs))

    def _kline_page(self, market: str, coin: str, timeframe: str, start_ms: int, end_ms: int, limit: int) -> list[Any]:
        params = {'symbol': coin, 'interval': timeframe, 'startTime': start_ms, 'endTime': end_ms, 'limit': limit}
        if market == E.MARKET_PERP:
            body = self.futures.get('/fapi/v1/klines', params, weight=futures_kline_weight(limit))
        else:
            body = self.spot.get('/api/v3/klines', params, weight=2)
        if not isinstance(body, list):
            raise R.MalformedRow(f'klines response is not a list: {str(body)[:120]}')
        return body

    # -- funding -----------------------------------------------------------
    def iter_funding_history(self, *, coin: str, start: datetime, end: datetime) -> Iterator[Batch]:
        """Yield SETTLED funding prints with ``funding_time`` in ``[start, end]``."""
        cursor_ms = R.to_ms(start)
        end_ms = R.to_ms(end)
        now = self.now()
        seen: set[int] = set()
        while cursor_ms <= end_ms:
            page = self.futures.get(
                '/fapi/v1/fundingRate',
                {'symbol': coin, 'startTime': cursor_ms, 'endTime': end_ms, 'limit': self.funding_page_limit},
                weight=1,
                windows=('funding',),
            )
            if not isinstance(page, list):
                raise R.MalformedRow(f'fundingRate response is not a list: {str(page)[:120]}')
            if not page:
                yield Batch(records=[], fetched=0)  # an empty answer is still a request the summary must count
                return
            batch = Batch(records=[], fetched=len(page))
            for raw in page:
                try:
                    record = R.parse_funding_settled_row(raw, publication_lag=self.funding_lag)
                except R.MalformedRow as exc:
                    batch.rejected += 1
                    batch.notes.append(f'{coin}: rejected a malformed funding print: {exc}')
                    continue
                if record.available_at > now:  # not settled yet, or settled but not yet served (print + publication lag)
                    batch.dropped_unclosed += 1
                    continue
                ms = R.to_ms(record.funding_time)
                if ms in seen:
                    continue
                seen.add(ms)
                batch.records.append(record)
            yield batch
            next_cursor = int(page[-1]['fundingTime']) + 1
            if next_cursor <= cursor_ms or len(page) < self.funding_page_limit:
                return
            cursor_ms = next_cursor

    def funding_history(self, **kwargs: Any) -> Batch:
        return _collect(self.iter_funding_history(**kwargs))

    def funding_intervals(self, *, max_age_seconds: float = 3600.0) -> dict[str, int]:
        """``symbol -> settlement interval in hours`` for symbols that deviate from 8h.

        Binance lists only symbols whose interval or caps were adjusted; any
        symbol absent from the list settles every 8 hours (their documented
        default), which callers apply with ``.get(symbol, 8)``.
        """
        stamp = time.monotonic()
        if self._funding_intervals is not None and stamp - self._funding_intervals_at < max_age_seconds:
            return self._funding_intervals
        body = self.futures.get('/fapi/v1/fundingInfo', weight=1, windows=('funding',))
        intervals: dict[str, int] = {}
        for item in body if isinstance(body, list) else []:
            hours = item.get('fundingIntervalHours')
            if isinstance(item.get('symbol'), str) and isinstance(hours, int) and hours > 0:
                intervals[item['symbol']] = hours
        self._funding_intervals, self._funding_intervals_at = intervals, stamp
        return intervals

    def predicted_funding(self, coins: set[str] | frozenset[str]) -> Batch:
        """The CURRENT estimated funding rate for each coin (one request, all symbols)."""
        body = self.futures.get('/fapi/v1/premiumIndex', weight=10)
        if not isinstance(body, list):
            raise R.MalformedRow(f'premiumIndex response is not a list: {str(body)[:120]}')
        try:
            intervals = self.funding_intervals()
        except Exception as exc:  # interval is advisory; the rate itself is what matters
            log.warning('fundingInfo unavailable (%r); predicted rows will carry interval_hours=NULL', exc)
            intervals = None  # type: ignore[assignment]
        batch = Batch(records=[], fetched=0, requests=1)
        for raw in body:
            if not isinstance(raw, dict) or raw.get('symbol') not in coins:
                continue
            batch.fetched += 1
            try:
                hours = None if intervals is None else intervals.get(raw['symbol'], 8)
                batch.records.append(R.parse_funding_predicted_row(raw, interval_hours=hours))
            except R.MalformedRow as exc:
                batch.rejected += 1
                batch.notes.append(f'{raw.get("symbol")}: rejected a malformed premium index row: {exc}')
        return batch

    # -- open interest -----------------------------------------------------
    def latest_open_interest_poll(self, period: str, now: datetime | None = None) -> datetime:
        """The newest poll boundary whose sample is knowable at ``now`` (boundary + publication lag <= now)."""
        return _floor((now or self.now()) - self.open_interest_lag, R.period_delta(period))

    def iter_open_interest_history(
        self,
        *,
        coin: str,
        period: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[Batch]:
        """Yield open-interest samples with ``poll_time`` in ``[start, end]``.

        ``start`` must already be within Binance's 30-day window
        (:func:`clamp_open_interest_start`); an older one is rejected by the
        exchange. The range is sliced into windows of at most one page of
        periods so the "last ``limit`` rows" behaviour can never truncate it.
        """
        length = R.period_delta(period)
        length_ms = length // timedelta(milliseconds=1)
        span_ms = length_ms * self.open_interest_page_limit
        cursor_ms = R.to_ms(start)
        end_ms = R.to_ms(end)
        now = self.now()
        seen: set[int] = set()
        while cursor_ms <= end_ms:
            window_end = min(end_ms, cursor_ms + span_ms - 1)
            page = self.futures.get(
                '/futures/data/openInterestHist',
                {
                    'symbol': coin,
                    'period': period,
                    'startTime': cursor_ms,
                    'endTime': window_end,
                    'limit': self.open_interest_page_limit,
                },
                weight=1,
                windows=('open_interest_hist',),
            )
            if not isinstance(page, list):
                raise R.MalformedRow(f'openInterestHist response is not a list: {str(page)[:120]}')
            batch = Batch(records=[], fetched=len(page))
            for raw in page:
                try:
                    record = R.parse_open_interest_row(raw, coin=coin, period=period, publication_lag=self.open_interest_lag)
                except R.MalformedRow as exc:
                    batch.rejected += 1
                    batch.notes.append(f'{coin} {period}: rejected a malformed OI sample: {exc}')
                    continue
                if record.available_at > now:  # boundary + publication lag not reached: not yet knowable
                    batch.dropped_unclosed += 1
                    continue
                ms = R.to_ms(record.poll_time)
                if ms in seen or not (cursor_ms <= ms <= end_ms):
                    continue
                seen.add(ms)
                batch.records.append(record)
            yield batch
            cursor_ms = window_end + 1

    def open_interest_history(self, **kwargs: Any) -> Batch:
        return _collect(self.iter_open_interest_history(**kwargs))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _floor(instant: datetime, length: timedelta) -> datetime:
    """Largest multiple of ``length`` (counted from the Unix epoch) at or before ``instant``."""
    ms = R.to_ms(instant)
    length_ms = length // timedelta(milliseconds=1)
    return R.from_ms(ms - ms % length_ms) if ms >= length_ms else R.EPOCH


def floor_to_grid(instant: datetime, length: timedelta) -> datetime:
    return _floor(instant, length)


def ceil_to_grid(instant: datetime, length: timedelta) -> datetime:
    floored = _floor(instant, length)
    return floored if floored == ensure_utc(instant) else floored + length


def _collect(batches: Iterator[Batch]) -> Batch:
    total = Batch(records=[], fetched=0, requests=0)
    for batch in batches:
        total.records.extend(batch.records)
        total.fetched += batch.fetched
        total.dropped_unclosed += batch.dropped_unclosed
        total.rejected += batch.rejected
        total.requests += batch.requests
        total.notes.extend(batch.notes)
    return total
