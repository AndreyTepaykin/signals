"""Runs the force-order collector in its own thread and ties it to the database.

The collector (``feeds.forceorder``) knows nothing about SQL. This module gives
it a sink that writes ``force_order`` rows idempotently, and hooks that keep two
records honest:

* one ``job_run`` row per CONNECTION (``binance_force_order_stream``), finalised
  with the rows it wrote and, when it ended badly, the reason;
* the ``feed_health`` row for ``binance_force_order``: ``ok`` while connected,
  ``down`` while not. Staleness cannot be judged from the prints themselves (a
  quiet market prints nothing), so liveness is the connection, and this feed has
  no stand-down limit (it is shadow-only).

Disconnects are visible: each gap is kept in the collector's stats and written
into the closing ``job_run.detail``, because those prints are gone for good.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime

from sqlalchemy import update

from app.core.time import utc_now
from app.models import enums as E
from app.models.observability import JobRun
from app.worker.feeds import health, store
from app.worker.feeds import records as R
from app.worker.feeds.forceorder import CollectorStats, ForceOrderCollector, Gap, Hooks
from app.worker.jobs import JOB_FORCE_ORDER_STREAM, WorkerContext

log = logging.getLogger(__name__)

HEALTH_WINDOW_MINUTES = 60


class DatabaseHooks(Hooks):
    def __init__(self, ctx: WorkerContext) -> None:
        self.ctx = ctx
        self._run_id: int | None = None
        self._written_at_connect = 0

    # -- helpers ---------------------------------------------------------
    def _write_health(self, at: datetime, stats: CollectorStats, *, connected: bool, error: str | None = None) -> None:
        try:
            with self.ctx.session_factory() as session:
                health.write_force_order_health(
                    session,
                    now=at,
                    connected=connected,
                    last_traded_at=stats.last_traded_at,
                    last_available_at=stats.last_available_at,
                    rows=stats.written,
                    window_minutes=HEALTH_WINDOW_MINUTES,
                    error=error,
                )
                session.commit()
        except Exception:
            log.exception('could not write force-order feed_health')

    # -- Hooks -----------------------------------------------------------
    def on_connected(self, connection_id: str, at: datetime, gap: Gap | None, stats: CollectorStats) -> None:
        try:
            with self.ctx.session_factory() as session:
                row = JobRun(
                    job_name=JOB_FORCE_ORDER_STREAM,
                    started_at=at,
                    status='running',
                    source=E.SOURCE_LIVE,
                    code_version=self.ctx.code_version,
                    detail={'connection_id': connection_id, 'gap_before_seconds': gap.seconds if gap else None},
                )
                session.add(row)
                session.commit()
                self._run_id = row.id
        except Exception:
            log.exception('could not open the force-order job_run row')
            self._run_id = None
        self._written_at_connect = stats.written
        self._write_health(at, stats, connected=True)

    def on_heartbeat(self, at: datetime, stats: CollectorStats) -> None:
        self._write_health(at, stats, connected=True)

    def on_disconnected(self, connection_id: str, at: datetime, reason: str, stats: CollectorStats) -> None:
        clean = reason in ('stopped',) or reason.startswith('planned reconnect')
        self._write_health(at, stats, connected=False, error=None if clean else reason)
        if self._run_id is None:
            return
        try:
            with self.ctx.session_factory() as session:
                session.execute(
                    update(JobRun)
                    .where(JobRun.id == self._run_id)
                    .values(
                        finished_at=max(at, utc_now()),
                        status='ok' if clean else 'error',
                        rows_touched=stats.written - self._written_at_connect,
                        error=None if clean else reason[:1000],
                        detail={
                            'connection_id': connection_id,
                            'reason': reason,
                            'received': stats.received,
                            'written_total': stats.written,
                            'ignored': stats.ignored,
                            'malformed': stats.malformed,
                            'sink_failures': stats.sink_failures,
                            'gaps_seconds': [g.seconds for g in stats.gaps if g.seconds is not None][-10:],
                            'partial_stream': True,
                        },
                    )
                )
                session.commit()
        except Exception:
            log.exception('could not close the force-order job_run row')
        self._run_id = None


class ForceOrderRunner:
    """Owns the collector thread: ``start()`` returns immediately, ``stop()`` joins."""

    def __init__(self, ctx: WorkerContext, *, collector: ForceOrderCollector | None = None) -> None:
        self.ctx = ctx
        self.collector = collector or ForceOrderCollector(
            base_url=ctx.settings.binance_futures_ws_url,
            watchlist=frozenset(ctx.settings.watchlist_symbols),
            sink=self._sink,
            hooks=DatabaseHooks(ctx),
        )
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()

    def _sink(self, records: list[R.ForceOrderRecord]) -> int:
        with self.ctx.session_factory() as session:
            result = store.write_force_orders(session, records)
            session.commit()
            return result.written

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name='force-order-stream', daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            self._ready.set()
            await self.collector.run(self._stop)

        try:
            asyncio.run(main())
        except Exception:
            log.exception('force-order collector crashed')
        finally:
            self._ready.set()

    def stop(self, timeout: float = 15.0) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning('force-order thread did not stop within %.0fs', timeout)
