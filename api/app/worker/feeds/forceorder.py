"""Force-order (liquidation) stream collector: ``!forceOrder@arr``.

What this feed is, and is not
-----------------------------
The spec: "Binance force orders - Print time. The stream is partial - Liquidation
distance, flagged estimated, shadow." Binance pushes at most the LATEST
liquidation per symbol per second, so the stream under-reports by design.
Anything derived from it is an estimate, flagged as such and kept in shadow.

There is **no historical endpoint**. Rows exist only for the time the collector
was connected, and a disconnect loses whatever printed meanwhile: the exchange
does not replay. "Resumption" therefore means reconnecting promptly and recording
the gap honestly (:class:`Gap`), never pretending the window was covered. Replay
rows (Milestone 7) leave liquidation distance NULL for this reason.

Reconnection
------------
Exponential backoff with jitter between attempts; the backoff resets only after
a connection has stayed up for ``stable_after`` seconds, so a server that accepts
and immediately drops us cannot reset it to zero each time. Binance closes every
websocket after 24 hours, so the collector reconnects proactively just before
that (``max_connection_age``) rather than waiting to be cut off.

Design for testing: the connection factory, the clock, and the sleep are
injected, and the collector knows nothing about the database. It hands batches
of records to a ``sink`` callback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.core.time import utc_now
from app.worker.feeds import records as R

log = logging.getLogger(__name__)

#: ``/ws/<stream>`` is Binance's raw-stream path.
FORCE_ORDER_STREAM = '!forceOrder@arr'
#: Binance cuts every connection after 24h; reconnect a little before.
MAX_CONNECTION_AGE = timedelta(hours=23, minutes=30)


class Connection(Protocol):
    async def recv(self) -> str | bytes: ...


Connector = Callable[[str], AbstractAsyncContextManager[Connection]]
Sink = Callable[[list[R.ForceOrderRecord]], int]


@dataclass
class Gap:
    """A stretch of time with no connection. Prints inside it are lost for good."""

    started_at: datetime
    ended_at: datetime | None = None

    @property
    def seconds(self) -> float | None:
        return None if self.ended_at is None else (self.ended_at - self.started_at).total_seconds()


@dataclass
class Backoff:
    base: float = 1.0
    cap: float = 60.0
    jitter: float = 0.25
    stable_after: float = 60.0
    rand: Callable[[], float] = random.random
    failures: int = 0

    def next_delay(self) -> float:
        self.failures += 1
        raw = min(self.cap, self.base * (2 ** (self.failures - 1)))
        return max(0.0, raw * (1 + self.jitter * (2 * self.rand() - 1)))

    def connection_was_stable(self, lasted_seconds: float) -> None:
        if lasted_seconds >= self.stable_after:
            self.failures = 0


@dataclass
class CollectorStats:
    connections: int = 0
    received: int = 0
    written: int = 0
    ignored: int = 0  # other symbols, other event types
    malformed: int = 0
    flushes: int = 0
    sink_failures: int = 0
    dropped_on_overflow: int = 0
    last_traded_at: datetime | None = None
    last_available_at: datetime | None = None
    gaps: list[Gap] = field(default_factory=list)


class Hooks:
    """Observer for the runner. Every method is optional."""

    def on_connected(self, connection_id: str, at: datetime, gap: Gap | None, stats: CollectorStats) -> None: ...

    def on_heartbeat(self, at: datetime, stats: CollectorStats) -> None: ...

    def on_disconnected(self, connection_id: str, at: datetime, reason: str, stats: CollectorStats) -> None: ...


def default_connector(url: str) -> AbstractAsyncContextManager[Connection]:
    from websockets.asyncio.client import connect

    return connect(url, ping_interval=20, ping_timeout=20, max_size=2**20, open_timeout=15)  # type: ignore[return-value]


class ForceOrderCollector:
    def __init__(
        self,
        *,
        base_url: str,
        watchlist: frozenset[str],
        sink: Sink,
        connector: Connector = default_connector,
        hooks: Hooks | None = None,
        backoff: Backoff | None = None,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Any] = asyncio.sleep,
        flush_interval: float = 1.0,
        heartbeat_interval: float = 60.0,
        batch_size: int = 200,
        max_buffer: int = 50_000,
        max_connection_age: timedelta = MAX_CONNECTION_AGE,
    ) -> None:
        self.url = f'{base_url.rstrip("/")}/ws/{FORCE_ORDER_STREAM}'
        self.watchlist = watchlist
        self.sink = sink
        self.connector = connector
        self.hooks = hooks or Hooks()
        self.backoff = backoff or Backoff()
        self.now = now
        self._sleep = sleep
        self.flush_interval = flush_interval
        self.heartbeat_interval = heartbeat_interval
        self.batch_size = batch_size
        self.max_buffer = max_buffer
        self.max_connection_age = max_connection_age
        self.stats = CollectorStats()
        self._buffer: list[R.ForceOrderRecord] = []

    # -- main loop -------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        """Collect until ``stop`` is set, reconnecting through any failure."""
        gap: Gap | None = Gap(started_at=self.now())  # until the first connect, we are not collecting
        while not stop.is_set():
            started = self.now()
            connection_id = uuid.uuid4().hex[:12]
            reason = 'closed'
            try:
                async with self.connector(self.url) as connection:
                    connected_at = self.now()
                    if gap is not None:
                        gap.ended_at = connected_at
                        self.stats.gaps.append(gap)
                        gap = None
                    self.stats.connections += 1
                    log.info('force-order stream connected (%s)', connection_id)
                    self.hooks.on_connected(
                        connection_id, connected_at, self.stats.gaps[-1] if self.stats.gaps else None, self.stats
                    )
                    reason = await self._consume(connection, connection_id, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = f'{type(exc).__name__}: {exc}'
                log.warning('force-order stream dropped: %s', reason)
            finally:
                await self._flush(force=True)
                ended = self.now()
                self.backoff.connection_was_stable((ended - started).total_seconds())
                if gap is None and not stop.is_set():
                    gap = Gap(started_at=ended)
                self.hooks.on_disconnected(connection_id, ended, reason, self.stats)

            if stop.is_set():
                break
            delay = self.backoff.next_delay()
            log.info('reconnecting force-order stream in %.1fs', delay)
            await self._wait(stop, delay)

        if gap is not None and gap.ended_at is None:
            self.stats.gaps.append(gap)

    async def _wait(self, stop: asyncio.Event, delay: float) -> None:
        """Sleep ``delay`` unless stopped sooner."""
        if self._sleep is asyncio.sleep:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        else:  # injected sleep (tests): no real waiting
            await self._sleep(delay)

    async def _consume(self, connection: Connection, connection_id: str, stop: asyncio.Event) -> str:
        sequence = 0
        opened = self.now()
        last_flush = last_beat = opened
        while not stop.is_set():
            if self.now() - opened >= self.max_connection_age:
                return 'planned reconnect before the 24h connection limit'
            try:
                raw = await asyncio.wait_for(connection.recv(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                raw = None
            if raw is not None:
                for message in self._decode(raw):
                    sequence += 1
                    self._accept(message, connection_id, sequence)
            now = self.now()
            if len(self._buffer) >= self.batch_size or (self._buffer and (now - last_flush).total_seconds() >= self.flush_interval):
                await self._flush()
                last_flush = now
            if (now - last_beat).total_seconds() >= self.heartbeat_interval:
                self.hooks.on_heartbeat(now, self.stats)
                last_beat = now
        return 'stopped'

    # -- parsing ---------------------------------------------------------
    def _decode(self, raw: str | bytes) -> list[Any]:
        try:
            body = json.loads(raw)
        except ValueError:
            self.stats.malformed += 1
            return []
        return body if isinstance(body, list) else [body]

    def _accept(self, message: Any, connection_id: str, sequence: int) -> None:
        self.stats.received += 1
        try:
            record = R.parse_force_order(
                message, connection_id=connection_id, sequence=sequence, watchlist=self.watchlist
            )
        except R.MalformedRow as exc:
            self.stats.malformed += 1
            log.warning('malformed force order: %s', exc)
            return
        if record is None:
            self.stats.ignored += 1
            return
        if len(self._buffer) >= self.max_buffer:
            del self._buffer[0]
            self.stats.dropped_on_overflow += 1
        self._buffer.append(record)

    # -- writing ---------------------------------------------------------
    async def _flush(self, *, force: bool = False) -> None:
        if not self._buffer:
            return
        batch = list(self._buffer)
        try:
            written = await asyncio.to_thread(self.sink, batch)
        except Exception as exc:
            self.stats.sink_failures += 1
            log.error('force-order sink failed (%s); keeping %d rows for the next flush', exc, len(batch))
            return
        del self._buffer[: len(batch)]
        self.stats.flushes += 1
        self.stats.written += written
        newest = max(batch, key=lambda r: r.traded_at)
        if self.stats.last_traded_at is None or newest.traded_at > self.stats.last_traded_at:
            self.stats.last_traded_at = newest.traded_at
            self.stats.last_available_at = newest.available_at
