"""The live worker: APScheduler jobs, the force-order stream, and clean shutdown.

Jobs (intervals from ``app.config.Settings``, spec section 12 "1-5 min"):

=========================  =========  =======================================
Job                        Default    What it does
=========================  =========  =======================================
``binance_klines``         60 s       perp + spot klines, 15m/1h/4h, closed bars
``binance_open_interest``  120 s      open-interest history, stamped at the poll bar close
``binance_funding_settled``  300 s    settled prints, stamped at the print
``binance_funding_predicted`` 60 s    the current estimated rate
``feed_health``            60 s       recompute staleness for every feed
``notify``                 60 s       ntfy on stand-down changes and new live alerts
``signal_pipeline``        60 s       labels, then features/rules/options for the newest 15m bar
``deribit_options_book``   300 s      public Deribit book, BTC/ETH (only with ``deribit_enabled``)
(stream)                   always on  ``!forceOrder@arr`` consumer (own thread)
=========================  =========  =======================================

A job that has nothing due returns in milliseconds and is logged as ``skipped``;
the klines job polls every minute but only makes a request when a bar has
closed since the newest stored one. ``max_instances=1`` and ``coalesce=True``
mean a slow run delays the next instead of piling up.

Shutdown: SIGINT / SIGTERM / SIGBREAK set one flag. The jobs see
``ctx.stopping`` between series, the scheduler waits for running jobs, the
websocket thread is told to stop and joined, the engine is disposed.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timedelta

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.core.db_sync import dispose_sync_engine, get_sync_session_factory
from app.core.hashing import code_version
from app.core.time import UTC, utc_now
from app.worker.feeds import health
from app.worker.feeds.binance import BinanceClient
from app.worker.feeds.deribit import DeribitClient
from app.worker.jobs import (
    JOBS,
    OPTIONS_JOBS,
    WorkerContext,
    job_intervals,
    options_job_intervals,
    reap_orphaned_runs,
    run_job,
)
from app.worker.stream import ForceOrderRunner

log = logging.getLogger(__name__)


def build_context(client: BinanceClient | None = None, deribit: DeribitClient | None = None) -> WorkerContext:
    settings = get_settings()
    _ = settings.watchlist_symbols  # raises on a bad watchlist before anything starts
    return WorkerContext(
        settings=settings,
        client=client or BinanceClient(settings=settings),
        deribit=deribit if deribit is not None else (DeribitClient(settings=settings) if settings.deribit_enabled else None),
        session_factory=get_sync_session_factory(),
        attempts=health.AttemptLog(),
        code_version=code_version(),  # shells out to git: once per run, never per row
    )


def build_scheduler(ctx: WorkerContext, *, start_at: datetime | None = None) -> BackgroundScheduler:
    """A scheduler with every job registered (not started)."""
    scheduler = BackgroundScheduler(
        executors={'default': ThreadPoolExecutor(max_workers=5)},
        job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': 30},
        timezone=UTC,
    )
    first = start_at or utc_now()
    for index, (name, seconds) in enumerate(job_intervals(ctx.settings).items()):
        scheduler.add_job(
            run_job,
            trigger='interval',
            seconds=seconds,
            args=[ctx, name, JOBS[name]],
            id=name,
            name=name,
            next_run_time=first + timedelta(seconds=2 * index),  # stagger the first runs
        )
    if ctx.deribit is not None:  # the optional public Deribit poller (Milestone 5)
        for name, seconds in options_job_intervals(ctx.settings).items():
            scheduler.add_job(
                run_job,
                trigger='interval',
                seconds=seconds,
                args=[ctx, name, OPTIONS_JOBS[name]],
                id=name,
                name=name,
                next_run_time=first + timedelta(seconds=11),
            )
    return scheduler


class Worker:
    def __init__(self, ctx: WorkerContext, *, with_stream: bool = True) -> None:
        self.ctx = ctx
        self.scheduler = build_scheduler(ctx)
        self.stream = ForceOrderRunner(ctx) if with_stream else None
        self._stop = threading.Event()
        self._started = False

    def start(self) -> None:
        reaped = reap_orphaned_runs(self.ctx.session_factory)
        if reaped:
            log.warning('closed %d job_run row(s) left running by a previous worker', reaped)
        from app.worker import notify as notify_mod

        with self.ctx.session_factory() as session:
            self.ctx.notify_state = notify_mod.init_notify_state(session)
        self.scheduler.start()
        if self.stream is not None:
            self.stream.start()
        self._started = True
        log.info(
            'worker started: %d coins, code %s, jobs %s',
            len(self.ctx.settings.watchlist_symbols),
            self.ctx.code_version,
            ', '.join(f'{n}/{s}s' for n, s in job_intervals(self.ctx.settings).items()),
        )

    def request_stop(self, *_args: object) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):  # SIGBREAK exists on Windows only
            number = getattr(signal, name, None)
            if number is not None:
                signal.signal(number, self.request_stop)
        self.start()
        try:
            while not self._stop.wait(timeout=1.0):  # short timeout: Windows only delivers signals between waits
                pass
        finally:
            self.stop()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        log.info('worker stopping')
        self.ctx.stopping.set()
        self.scheduler.shutdown(wait=True)
        if self.stream is not None:
            self.stream.stop()
        try:
            # One last snapshot so feed_health reflects the final stored state.
            run_job(self.ctx, 'feed_health', JOBS['feed_health'])
        finally:
            self.ctx.client.close()
            if self.ctx.deribit is not None:
                self.ctx.deribit.close()
            dispose_sync_engine()
        log.info('worker stopped')

    def run_once(self) -> dict[str, str]:
        """Run every job once, in order, without the stream. For smoke tests and cron-style use."""
        results: dict[str, str] = {}
        for name, fn in JOBS.items():
            results[name] = run_job(self.ctx, name, fn)
        if self.ctx.deribit is not None:
            for name, fn in OPTIONS_JOBS.items():
                results[name] = run_job(self.ctx, name, fn)
        return results
