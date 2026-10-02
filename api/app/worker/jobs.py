"""The live jobs, and the wrapper that writes a ``job_run`` row for each execution.

Every job is a plain function ``(session, ctx) -> JobOutcome``. The wrapper
:func:`run_job` owns the bookkeeping: it inserts a ``running`` row, runs the job
in its own session, then finalises the row as ``ok``, ``skipped`` (nothing was
due) or ``error`` (with the traceback tail) and the rows touched. The job_run
rows use their own short sessions so a job that fails and rolls back cannot lose
the record that it failed.

The jobs do not know how to talk to Binance or how to write a row; they decide
WHAT is due (using the newest stored row per series) and delegate to
``app.worker.feeds.sync``. The same code path therefore serves the live worker
and the backfill CLI, which is what makes a gap left by downtime heal itself on
the next poll.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.time import utc_now
from app.models import enums as E
from app.models.observability import JobRun
from app.worker.feeds import health, sync
from app.worker.feeds import records as R
from app.worker.feeds.binance import OPEN_INTEREST_HISTORY_DAYS, BinanceClient
from app.worker.feeds.http import GeoBlocked, RateLimited

log = logging.getLogger(__name__)

# Job names, as written to job_run.job_name.
JOB_KLINES = 'binance_klines'
JOB_OPEN_INTEREST = 'binance_open_interest'
JOB_FUNDING_SETTLED = 'binance_funding_settled'
JOB_FUNDING_PREDICTED = 'binance_funding_predicted'
JOB_FEED_HEALTH = 'feed_health'
JOB_FORCE_ORDER_STREAM = 'binance_force_order_stream'
LIVE_JOB_NAMES = (
    JOB_KLINES,
    JOB_OPEN_INTEREST,
    JOB_FUNDING_SETTLED,
    JOB_FUNDING_PREDICTED,
    JOB_FEED_HEALTH,
    JOB_FORCE_ORDER_STREAM,
)

#: How much history a brand-new database gets from the live worker. Deeper
#: history is the backfill CLI's job.
LIVE_BOOTSTRAP_BARS = 200
LIVE_FUNDING_BOOTSTRAP = timedelta(days=30)

JobFn = Callable[[Session, 'WorkerContext'], 'JobOutcome']


@dataclass
class WorkerContext:
    settings: Settings
    client: BinanceClient
    session_factory: sessionmaker[Session]
    attempts: health.AttemptLog
    code_version: str
    #: Set on shutdown; jobs poll it between series so stopping is prompt.
    stopping: threading.Event = field(default_factory=threading.Event)


@dataclass
class JobOutcome:
    rows_touched: int = 0
    skipped: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# job_run bookkeeping
# ---------------------------------------------------------------------------
def run_job(ctx: WorkerContext, name: str, fn: JobFn) -> str:
    """Execute ``fn`` once with a ``job_run`` row around it. Never raises. Returns the final status."""
    started = utc_now()
    try:
        with ctx.session_factory() as bookkeeping:
            row = JobRun(job_name=name, started_at=started, status='running', source=E.SOURCE_LIVE, code_version=ctx.code_version)
            bookkeeping.add(row)
            bookkeeping.commit()
            run_id = row.id
    except Exception:
        log.exception('could not write job_run row for %s; running the job anyway', name)
        run_id = None

    status, outcome, error = 'ok', JobOutcome(), None
    try:
        with ctx.session_factory() as work:
            outcome = fn(work, ctx)
            work.commit()
        if outcome.errors:
            status, error = 'error', '; '.join(outcome.errors)[:4000]
        elif outcome.skipped:
            status = 'skipped'
    except Exception as exc:
        status = 'error'
        error = f'{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}'
        log.exception('job %s failed', name)

    if run_id is not None:
        try:
            with ctx.session_factory() as bookkeeping:
                bookkeeping.execute(
                    update(JobRun)
                    .where(JobRun.id == run_id)
                    .values(
                        finished_at=utc_now(),
                        status=status,
                        rows_touched=outcome.rows_touched,
                        detail=outcome.detail or None,
                        error=error,
                    )
                )
                bookkeeping.commit()
        except Exception:
            log.exception('could not finalise job_run %s for %s', run_id, name)
    return status


def reap_orphaned_runs(session_factory: sessionmaker[Session]) -> int:
    """Close job_run rows a previous worker left ``running`` when it died.

    Assumes ONE worker process per database (the spec's single ``worker``
    service). Only the live job names are touched, never the backfill's.
    """
    with session_factory() as session:
        result = session.execute(
            update(JobRun)
            .where(JobRun.status == 'running', JobRun.job_name.in_(LIVE_JOB_NAMES))
            .values(
                status='error',
                finished_at=utc_now(),
                error='orphaned: the worker stopped without finishing this run',
            )
        )
        session.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
def _abort_message(exc: Exception) -> str:
    return f'{type(exc).__name__}: {exc}'


def job_klines(session: Session, ctx: WorkerContext) -> JobOutcome:
    """Perp and spot klines at every stored timeframe, closed bars only, at bar close."""
    client, outcome = ctx.client, JobOutcome()
    now = client.now()
    checked = fetched = 0
    try:
        for coin in ctx.settings.watchlist_symbols:
            for market in E.CANDLE_MARKETS:
                for timeframe in E.TIMEFRAMES:
                    if ctx.stopping.is_set():
                        outcome.detail['interrupted'] = True
                        return _finish_klines(outcome, checked, fetched)
                    feed = health.kline_feed(market, timeframe)
                    checked += 1
                    length = R.timeframe_delta(timeframe)
                    newest = client.latest_closed_open(timeframe, now)
                    last = sync.latest_candle_open(session, now, coin=coin, market=market, timeframe=timeframe)
                    if last is not None and last >= newest:
                        ctx.attempts.success(feed, coin, utc_now())
                        continue
                    start = newest - length * (LIVE_BOOTSTRAP_BARS - 1) if last is None else last + length
                    report = sync.sync_klines(
                        session, client, coin=coin, market=market, timeframe=timeframe, start=start, end=newest + length
                    )
                    fetched += 1
                    outcome.rows_touched += report.written
                    _record(ctx, feed, coin, report, outcome)
    except (GeoBlocked, RateLimited) as exc:
        outcome.errors.append(_abort_message(exc))
        for spec in health.expected_feeds(ctx.settings):
            if spec.feed.startswith('binance_kline_'):
                ctx.attempts.error(spec.feed, spec.coin, utc_now(), _abort_message(exc))
    return _finish_klines(outcome, checked, fetched)


def _finish_klines(outcome: JobOutcome, checked: int, fetched: int) -> JobOutcome:
    outcome.detail.update(series_checked=checked, series_fetched=fetched)
    outcome.skipped = fetched == 0 and not outcome.errors
    return outcome


def _record(ctx: WorkerContext, feed: str, coin: str, report: sync.SeriesReport, outcome: JobOutcome) -> None:
    if report.error:
        outcome.errors.append(f'{feed} {coin}: {report.error}')
        ctx.attempts.error(feed, coin, utc_now(), report.error)
    else:
        ctx.attempts.success(feed, coin, utc_now())
    if report.dropped_unclosed:
        outcome.detail['dropped_unclosed'] = outcome.detail.get('dropped_unclosed', 0) + report.dropped_unclosed
    if report.rejected:
        outcome.detail['rejected'] = outcome.detail.get('rejected', 0) + report.rejected


def job_open_interest(session: Session, ctx: WorkerContext) -> JobOutcome:
    """Open-interest history at each configured period, stamped at the close of the poll bar."""
    client, outcome = ctx.client, JobOutcome()
    now = client.now()
    checked = fetched = 0
    try:
        for coin in ctx.settings.watchlist_symbols:
            for period in ctx.settings.open_interest_periods:
                if ctx.stopping.is_set():
                    outcome.detail['interrupted'] = True
                    break
                feed = health.open_interest_feed(period)
                checked += 1
                length = R.period_delta(period)
                newest = client.latest_open_interest_poll(period, now)  # boundary + publication lag <= now
                last = sync.latest_open_interest_time(session, now, coin=coin, period=period)
                if last is not None and last >= newest:
                    ctx.attempts.success(feed, coin, utc_now())
                    continue
                start = newest - timedelta(days=OPEN_INTEREST_HISTORY_DAYS - 1) if last is None else last + length
                report = sync.sync_open_interest(
                    session, client, coin=coin, period=period, start=start, end=newest + length
                )
                fetched += 1
                outcome.rows_touched += report.written
                _record(ctx, feed, coin, report, outcome)
    except (GeoBlocked, RateLimited) as exc:
        outcome.errors.append(_abort_message(exc))
        for spec in health.expected_feeds(ctx.settings):
            if spec.feed.startswith('binance_open_interest_'):
                ctx.attempts.error(spec.feed, spec.coin, utc_now(), _abort_message(exc))
    outcome.detail.update(series_checked=checked, series_fetched=fetched)
    outcome.skipped = fetched == 0 and not outcome.errors
    return outcome


def job_funding_settled(session: Session, ctx: WorkerContext) -> JobOutcome:
    """Settled funding prints, available at the print. New prints appear a few times a day."""
    client, outcome = ctx.client, JobOutcome()
    now = client.now()
    tolerance = timedelta(hours=ctx.settings.feed_funding_interval_hours) + sync.FUNDING_GAP_SLACK
    try:
        for coin in ctx.settings.watchlist_symbols:
            if ctx.stopping.is_set():
                outcome.detail['interrupted'] = True
                break
            last = sync.latest_settled_funding_time(session, now, coin=coin)
            start = now - LIVE_FUNDING_BOOTSTRAP if last is None else last + timedelta(milliseconds=1)
            report = sync.sync_funding_settled(
                session, client, coin=coin, start=start, end=now + timedelta(milliseconds=1), tolerance=tolerance
            )
            outcome.rows_touched += report.written
            _record(ctx, health.FEED_FUNDING_SETTLED, coin, report, outcome)
    except (GeoBlocked, RateLimited) as exc:
        outcome.errors.append(_abort_message(exc))
        for spec in health.expected_feeds(ctx.settings):
            if spec.feed == health.FEED_FUNDING_SETTLED:
                ctx.attempts.error(spec.feed, spec.coin, utc_now(), _abort_message(exc))
    outcome.skipped = outcome.rows_touched == 0 and not outcome.errors
    return outcome


def job_funding_predicted(session: Session, ctx: WorkerContext) -> JobOutcome:
    """The current estimated funding rate, for the live carry band (one request for every coin)."""
    outcome = JobOutcome()
    coins = ctx.settings.watchlist_symbols
    try:
        report = sync.poll_predicted_funding(session, ctx.client, coins)
    except Exception as exc:  # GeoBlocked and RateLimited included: all of them fail the same single request
        session.rollback()
        outcome.errors.append(_abort_message(exc))
        for coin in coins:
            ctx.attempts.error(health.FEED_FUNDING_PREDICTED, coin, utc_now(), _abort_message(exc))
        return outcome
    outcome.rows_touched = report.written
    outcome.detail.update(coins=len(coins), rejected=report.rejected, notes=report.notes[:5])
    for coin in coins:
        if coin in report.coins_seen:
            ctx.attempts.success(health.FEED_FUNDING_PREDICTED, coin, utc_now())
        else:
            ctx.attempts.error(health.FEED_FUNDING_PREDICTED, coin, utc_now(), 'no premium index row returned')
    return outcome


def job_feed_health(session: Session, ctx: WorkerContext) -> JobOutcome:
    """Recompute ``feed_health`` for every gating feed from what is actually stored."""
    outcome = JobOutcome()
    statuses = health.refresh_feed_health(session, now=ctx.client.now(), attempts=ctx.attempts, settings=ctx.settings)
    counts: dict[str, int] = {}
    for item in statuses:
        counts[item.status] = counts.get(item.status, 0) + 1
    outcome.rows_touched = len(statuses)
    outcome.detail.update(status_counts=counts)
    return outcome


JOBS: dict[str, JobFn] = {
    JOB_KLINES: job_klines,
    JOB_OPEN_INTEREST: job_open_interest,
    JOB_FUNDING_SETTLED: job_funding_settled,
    JOB_FUNDING_PREDICTED: job_funding_predicted,
    JOB_FEED_HEALTH: job_feed_health,
}


def job_intervals(settings: Settings) -> dict[str, int]:
    """Seconds between runs for each scheduled job (spec section 12: 1-5 min)."""
    return {
        JOB_KLINES: settings.feed_kline_poll_seconds,
        JOB_OPEN_INTEREST: settings.feed_open_interest_poll_seconds,
        JOB_FUNDING_SETTLED: settings.feed_funding_settled_poll_seconds,
        JOB_FUNDING_PREDICTED: settings.feed_funding_predicted_poll_seconds,
        JOB_FEED_HEALTH: settings.feed_health_poll_seconds,
    }

