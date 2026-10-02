"""Range synchronisation: the logic shared by the backfill CLI and the live jobs.

Given a coin, a feed and a time range, work out what is already stored, fetch
only what is missing, write it idempotently page by page, and report what the
range actually ended up holding. "Resumable" falls out of that: a second run
over the same range finds nothing missing and makes no data requests beyond the
cheap trailing check, and a run that died halfway continues from where its last
committed page ended - including holes in the middle of a series, which a naive
"start after the newest row" resume would never fill.

The report is honest by construction. It states how many rows were fetched,
written and already present; how many bars were dropped for being unclosed or
rejected for being malformed; and, after the writes, how much of the range the
database really covers: the first and last stored instant, the expected and
stored counts, and the gaps split into leading (before the first row, normally
"the series starts later than you asked"), interior (a real hole) and trailing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from app.core.time import ensure_utc
from app.models import enums as E
from app.models.as_of import as_of
from app.models.feeds import Candle, Funding, OpenInterest
from app.worker.feeds import records as R
from app.worker.feeds import store
from app.worker.feeds.binance import (
    OPEN_INTEREST_HISTORY_DAYS,
    Batch,
    BinanceClient,
    ceil_to_grid,
    clamp_open_interest_start,
    floor_to_grid,
)
from app.worker.feeds.http import GeoBlocked, RateLimited

log = logging.getLogger(__name__)

_MS = timedelta(milliseconds=1)

#: Funding prints settle at most this far apart, plus slack for publication jitter.
FUNDING_GAP_SLACK = timedelta(minutes=5)

ProgressFn = Callable[[str], None]


@dataclass
class SeriesReport:
    """What one (feed, coin) range holds after a sync."""

    feed: str  # 'kline:perp:15m', 'kline:spot:1h', 'funding:settled', 'open_interest:15m'
    coin: str
    requested_start: datetime
    requested_end: datetime
    effective_start: datetime | None = None
    effective_end: datetime | None = None
    fetched: int = 0
    written: int = 0
    dropped_unclosed: int = 0
    rejected: int = 0
    requests: int = 0
    first_stored: datetime | None = None
    last_stored: datetime | None = None
    stored: int = 0
    expected: int | None = None  # None where no fixed grid exists (funding)
    leading_gap: int = 0
    interior_gap: int = 0
    trailing_gap: int = 0
    fetch_runs: int = 0
    missing_before: int | None = None  # expected points absent from the database before this run
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    skipped: bool = False  # nothing in range (e.g. range entirely in the future)
    coins_seen: list[str] = field(default_factory=list)  # predicted funding: coins the exchange returned

    @property
    def already_present(self) -> int:
        return self.fetched - self.dropped_unclosed - self.rejected - self.written

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# gap arithmetic
# ---------------------------------------------------------------------------
def grid_points(first: datetime, last: datetime, step: timedelta) -> list[datetime]:
    """Every ``step`` instant from ``first`` to ``last`` inclusive."""
    points: list[datetime] = []
    cursor = first
    while cursor <= last:
        points.append(cursor)
        cursor += step
    return points


def missing_runs(expected: Sequence[datetime], present: set[datetime]) -> list[tuple[datetime, datetime]]:
    """Contiguous runs of expected grid points that are not stored: ``[(first, last), ...]``."""
    runs: list[tuple[datetime, datetime]] = []
    run_start: datetime | None = None
    previous: datetime | None = None
    for point in expected:
        if point in present:
            if run_start is not None and previous is not None:
                runs.append((run_start, previous))
                run_start = None
        elif run_start is None:
            run_start = point
        previous = point
    if run_start is not None and previous is not None:
        runs.append((run_start, previous))
    return runs


def classify_gaps(expected: Sequence[datetime], present: set[datetime]) -> tuple[int, int, int]:
    """``(leading, interior, trailing)`` counts of expected points that are missing."""
    stored_positions = [i for i, point in enumerate(expected) if point in present]
    if not stored_positions:
        return len(expected), 0, 0
    first, last = stored_positions[0], stored_positions[-1]
    interior = sum(1 for point in expected[first : last + 1] if point not in present)
    return first, interior, len(expected) - 1 - last


# ---------------------------------------------------------------------------
# stored-time readers (every one goes through the available-at gate)
# ---------------------------------------------------------------------------
def stored_candle_opens(
    session: Session, now: datetime, *, coin: str, market: str, timeframe: str, first: datetime, last: datetime
) -> set[datetime]:
    statement = (
        as_of(now)
        .select(Candle.open_time)
        .where(
            Candle.coin == coin,
            Candle.market == market,
            Candle.timeframe == timeframe,
            Candle.open_time >= first,
            Candle.open_time <= last,
        )
    )
    return {ensure_utc(value) for value in session.execute(statement).scalars().all()}


def stored_open_interest_times(
    session: Session, now: datetime, *, coin: str, period: str, first: datetime, last: datetime
) -> set[datetime]:
    statement = (
        as_of(now)
        .select(OpenInterest.poll_time)
        .where(
            OpenInterest.coin == coin,
            OpenInterest.timeframe == period,
            OpenInterest.poll_time >= first,
            OpenInterest.poll_time <= last,
        )
    )
    return {ensure_utc(value) for value in session.execute(statement).scalars().all()}


def stored_settled_funding_times(
    session: Session, now: datetime, *, coin: str, first: datetime, last: datetime
) -> list[datetime]:
    statement = (
        as_of(now)
        .select(Funding.funding_time)
        .where(
            Funding.coin == coin,
            Funding.kind == E.FUNDING_SETTLED,
            Funding.funding_time >= first,
            Funding.funding_time <= last,
        )
        .order_by(Funding.funding_time)
    )
    return [ensure_utc(value) for value in session.execute(statement).scalars().all()]


def _utc_or_none(value: datetime | None) -> datetime | None:
    """psycopg returns instants in the SESSION time zone; normalise so printing and ``.hour`` are UTC."""
    return None if value is None else ensure_utc(value)


def latest_candle_open(session: Session, now: datetime, *, coin: str, market: str, timeframe: str) -> datetime | None:
    statement = as_of(now).gate(
        sa_select(func.max(Candle.open_time)).where(
            Candle.coin == coin, Candle.market == market, Candle.timeframe == timeframe
        )
    )
    return _utc_or_none(session.execute(statement).scalar())


def latest_open_interest_time(session: Session, now: datetime, *, coin: str, period: str) -> datetime | None:
    statement = as_of(now).gate(
        sa_select(func.max(OpenInterest.poll_time)).where(OpenInterest.coin == coin, OpenInterest.timeframe == period)
    )
    return _utc_or_none(session.execute(statement).scalar())


def latest_settled_funding_time(session: Session, now: datetime, *, coin: str) -> datetime | None:
    statement = as_of(now).gate(
        sa_select(func.max(Funding.funding_time)).where(Funding.coin == coin, Funding.kind == E.FUNDING_SETTLED)
    )
    return _utc_or_none(session.execute(statement).scalar())


# ---------------------------------------------------------------------------
# writing a fetched page
# ---------------------------------------------------------------------------
def _absorb(report: SeriesReport, batch: Batch, written: int) -> None:
    report.fetched += batch.fetched
    report.written += written
    report.dropped_unclosed += batch.dropped_unclosed
    report.rejected += batch.rejected
    report.requests += batch.requests
    report.notes.extend(batch.notes[:5])  # a flood of malformed rows is counted, not printed


# ---------------------------------------------------------------------------
# klines
# ---------------------------------------------------------------------------
def sync_klines(
    session: Session,
    client: BinanceClient,
    *,
    coin: str,
    market: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> SeriesReport:
    """Make the database hold every closed bar with ``start <= open_time < end``.

    Bars still forming are never part of the target: the range is cut at the
    newest closed bar. Re-running changes nothing that is already stored.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    report = SeriesReport(f'kline:{market}:{timeframe}', coin, start, end)
    length = R.timeframe_delta(timeframe)
    now = client.now()

    first = ceil_to_grid(start, length)
    last = min(floor_to_grid(end - _MS, length), client.latest_closed_open(timeframe, now))
    if first > last:
        report.skipped = True
        report.notes.append('no closed bar in the requested range')
        return report
    report.effective_start, report.effective_end = first, last

    expected = grid_points(first, last, length)
    report.expected = len(expected)
    present = stored_candle_opens(session, now, coin=coin, market=market, timeframe=timeframe, first=first, last=last)
    runs = missing_runs(expected, present)
    report.fetch_runs = len(runs)
    report.missing_before = len(expected) - len(present)

    if not dry_run:
        try:
            for run_first, run_last in runs:
                for batch in client.iter_klines(
                    coin=coin, market=market, timeframe=timeframe, start=run_first, end=run_last
                ):
                    result = store.write_candles(session, batch.records, now=client.now() - client.settle)
                    session.commit()
                    _absorb(report, batch, result.written)
                    if progress:
                        progress(f'  {coin} {market} {timeframe}: +{result.written} rows (to {_last_open(batch)})')
        except (GeoBlocked, RateLimited):
            session.rollback()
            raise  # affects every series: the caller stops instead of failing 90 times
        except Exception as exc:
            session.rollback()
            report.error = f'{type(exc).__name__}: {exc}'
            log.warning('kline sync failed for %s %s %s: %s', coin, market, timeframe, report.error)

    present = stored_candle_opens(session, now, coin=coin, market=market, timeframe=timeframe, first=first, last=last)
    _coverage(report, expected, present)
    if report.leading_gap and not dry_run:
        report.notes.append(
            f'{report.leading_gap} bar(s) before the first stored bar: the {market} series starts later than requested'
        )
    return report


def _last_open(batch: Batch) -> str:
    return batch.records[-1].open_time.strftime('%Y-%m-%d %H:%M') if batch.records else 'n/a'


def _coverage(report: SeriesReport, expected: Sequence[datetime], present: set[datetime]) -> None:
    report.stored = len(present)
    report.first_stored = min(present) if present else None
    report.last_stored = max(present) if present else None
    report.leading_gap, report.interior_gap, report.trailing_gap = classify_gaps(expected, present)


# ---------------------------------------------------------------------------
# open interest
# ---------------------------------------------------------------------------
def sync_open_interest(
    session: Session,
    client: BinanceClient,
    *,
    coin: str,
    period: str,
    start: datetime,
    end: datetime,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> SeriesReport:
    """Make the database hold open-interest samples with ``start <= poll_time < end``.

    Binance serves only the last ~30 days. A ``start`` older than that is moved
    forward to the oldest instant it will answer for, and the report says so.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    report = SeriesReport(f'open_interest:{period}', coin, start, end)
    length = R.period_delta(period)
    now = client.now()

    effective_start, clamped = clamp_open_interest_start(start, now)
    if clamped:
        report.notes.append(
            f'Binance serves open-interest history for ~{OPEN_INTEREST_HISTORY_DAYS} days only: '
            f'requested from {start:%Y-%m-%d %H:%M}, fetching from {effective_start:%Y-%m-%d %H:%M}. '
            'Earlier open interest cannot be backfilled.'
        )
    first = ceil_to_grid(effective_start, length)
    # A sample is expected only once it is knowable: boundary + publication lag <= now.
    last = min(floor_to_grid(end - _MS, length), client.latest_open_interest_poll(period, now))
    if first > last:
        report.skipped = True
        report.notes.append('no sample instant in the requested range')
        return report
    report.effective_start, report.effective_end = first, last

    expected = grid_points(first, last, length)
    report.expected = len(expected)
    present = stored_open_interest_times(session, now, coin=coin, period=period, first=first, last=last)
    runs = missing_runs(expected, present)
    report.fetch_runs = len(runs)
    report.missing_before = len(expected) - len(present)

    if not dry_run:
        try:
            for run_first, run_last in runs:
                for batch in client.iter_open_interest_history(coin=coin, period=period, start=run_first, end=run_last):
                    result = store.write_open_interest(session, batch.records, now=now)
                    session.commit()
                    _absorb(report, batch, result.written)
        except (GeoBlocked, RateLimited):
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            report.error = f'{type(exc).__name__}: {exc}'
            log.warning('open-interest sync failed for %s %s: %s', coin, period, report.error)

    present = stored_open_interest_times(session, now, coin=coin, period=period, first=first, last=last)
    _coverage(report, expected, present)
    if report.trailing_gap == 1:
        report.notes.append('newest sample not served yet (Binance publishes it some time after the boundary)')
    return report


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------
def funding_runs(
    stored: Sequence[datetime], start: datetime, end: datetime, tolerance: timedelta
) -> list[tuple[datetime, datetime]]:
    """Ranges that may hold settled prints we do not have.

    Funding has no fixed grid (the interval differs by symbol and can change),
    so a hole is any stretch longer than the longest settlement interval plus
    slack. The tail after the newest stored print is always asked about: one
    cheap request, and it is where new prints appear.
    """
    if not stored:
        return [(start, end)]
    runs: list[tuple[datetime, datetime]] = []
    if stored[0] - start > tolerance:
        runs.append((start, stored[0] - _MS))
    for before, after in zip(stored, stored[1:]):
        if after - before > tolerance:
            runs.append((before + _MS, after - _MS))
    if end > stored[-1]:
        runs.append((stored[-1] + _MS, end))
    return runs


def sync_funding_settled(
    session: Session,
    client: BinanceClient,
    *,
    coin: str,
    start: datetime,
    end: datetime,
    tolerance: timedelta,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> SeriesReport:
    """Make the database hold every SETTLED funding print with ``start <= funding_time < end``."""
    start, end = ensure_utc(start), ensure_utc(end)
    report = SeriesReport('funding:settled', coin, start, end)
    now = client.now()
    last = min(end - _MS, now)
    if start > last:
        report.skipped = True
        report.notes.append('no settled print can exist in the requested range')
        return report
    report.effective_start, report.effective_end = start, last

    stored = stored_settled_funding_times(session, now, coin=coin, first=start, last=last)
    runs = funding_runs(stored, start, last, tolerance)
    report.fetch_runs = len(runs)

    if not dry_run:
        try:
            for run_first, run_last in runs:
                for batch in client.iter_funding_history(coin=coin, start=run_first, end=run_last):
                    result = store.write_funding(session, batch.records, now=now)
                    session.commit()
                    _absorb(report, batch, result.written)
        except (GeoBlocked, RateLimited):
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            report.error = f'{type(exc).__name__}: {exc}'
            log.warning('funding sync failed for %s: %s', coin, report.error)

    stored = stored_settled_funding_times(session, now, coin=coin, first=start, last=last)
    report.stored = len(stored)
    report.first_stored = stored[0] if stored else None
    report.last_stored = stored[-1] if stored else None
    report.interior_gap = sum(1 for a, b in zip(stored, stored[1:]) if b - a > tolerance)
    if stored and stored[0] - start > tolerance:
        report.notes.append(
            f'first settled print is {stored[0]:%Y-%m-%d %H:%M}, {(stored[0] - start).days} day(s) after the requested start: '
            'the contract has no earlier history'
        )
    return report


# ---------------------------------------------------------------------------
# predicted funding (live only: an estimate has no history to backfill)
# ---------------------------------------------------------------------------
def poll_predicted_funding(session: Session, client: BinanceClient, coins: Sequence[str]) -> SeriesReport:
    now = client.now()
    report = SeriesReport('funding:predicted', ','.join(coins), now, now)
    batch = client.predicted_funding(frozenset(coins))
    result = store.write_funding(session, batch.records, now=now)
    session.commit()
    _absorb(report, batch, result.written)
    report.stored = len(batch.records)
    report.coins_seen = sorted({r.coin for r in batch.records})
    missing = sorted(set(coins) - set(report.coins_seen))
    if missing:
        report.notes.append(f'no premium index row for: {", ".join(missing)}')
    return report
