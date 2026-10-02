"""``python -m app.worker.backfill`` - bootstrap history for the replay harness.

    python -m app.worker.backfill --coins BTCUSDT,ETHUSDT --timeframes 15m,1h \\
        --start 2026-01-01 --end 2026-10-01

    python -m app.worker.backfill --days 30                 # the watchlist, everything, last 30 days
    python -m app.worker.backfill --days 7 --feeds klines   # klines only
    python -m app.worker.backfill --days 30 --dry-run       # show what is missing, fetch nothing

Feeds
-----
``klines``   perp AND spot, at each requested timeframe, closed bars only
``funding``  settled funding prints (the predicted rate is live-only: an
             estimate has no history)
``oi``       open-interest history. Binance serves only the last ~30 days, so an
             older ``--start`` is clamped forward and the summary says so.

Force orders are NOT backfillable: there is no historical endpoint.

Properties
----------
* **Idempotent.** Every write is ``INSERT ... ON CONFLICT DO NOTHING``.
* **Resumable.** It works out what is already stored (including holes in the
  middle of a series) and fetches only the missing runs; each page is committed,
  so an interrupted run loses nothing already written. Re-running the same
  command a second time writes 0 rows and makes no data requests for klines.
* **Honest.** The summary reports, per feed, the range requested, the range the
  database actually covers afterwards, expected versus stored counts, and the
  gaps (leading / interior / trailing). It never prints "done" on faith.

Dates are UTC. A date or datetime without a zone is read as UTC. ``--end`` is
exclusive and defaults to now. Exit status: 0 success, 2 at least one series
failed, 3 Binance refused the run outright (451 geo-block or an IP ban).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings, parse_symbols
from app.core.db_sync import dispose_sync_engine, get_sync_session_factory
from app.core.hashing import code_version
from app.core.time import UTC, ensure_utc, utc_now
from app.models import enums as E
from app.models.observability import JobRun
from app.worker.feeds import sync
from app.worker.feeds.binance import OPEN_INTEREST_HISTORY_DAYS, BinanceClient
from app.worker.feeds.http import GeoBlocked, RateLimited

log = logging.getLogger(__name__)

FEEDS = ('klines', 'funding', 'oi')
JOB_NAME = 'backfill'
EXIT_OK, EXIT_SERIES_FAILED, EXIT_REFUSED = 0, 2, 3


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
@dataclass
class FeedSummary:
    feed: str
    coins: int = 0
    failed: int = 0
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    first_stored: datetime | None = None
    last_stored: datetime | None = None
    fetched: int = 0
    written: int = 0
    already_present: int = 0
    dropped_unclosed: int = 0
    rejected: int = 0
    requests: int = 0
    expected: int | None = 0
    stored: int = 0
    leading_gap: int = 0
    interior_gap: int = 0
    trailing_gap: int = 0
    missing_before: int | None = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, report: sync.SeriesReport) -> None:
        self.coins += 1
        self.failed += 0 if report.ok else 1
        self.requested_start = report.requested_start
        self.requested_end = report.requested_end
        self.fetched += report.fetched
        self.written += report.written
        self.already_present += report.already_present
        self.dropped_unclosed += report.dropped_unclosed
        self.rejected += report.rejected
        self.requests += report.requests
        self.stored += report.stored
        self.leading_gap += report.leading_gap
        self.interior_gap += report.interior_gap
        self.trailing_gap += report.trailing_gap
        if report.missing_before is None or self.missing_before is None:
            self.missing_before = None
        else:
            self.missing_before += report.missing_before
        if report.expected is None or self.expected is None:
            self.expected = None
        else:
            self.expected += report.expected
        if report.first_stored is not None:
            self.first_stored = report.first_stored if self.first_stored is None else min(self.first_stored, report.first_stored)
        if report.last_stored is not None:
            self.last_stored = report.last_stored if self.last_stored is None else max(self.last_stored, report.last_stored)
        for note in report.notes:
            if note not in self.notes:
                self.notes.append(note)
        if report.error:
            self.errors.append(f'{report.coin}: {report.error}')

    def as_detail(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            'coins': self.coins,
            'failed': self.failed,
            'requested': [iso(self.requested_start), iso(self.requested_end)],
            'covered': [iso(self.first_stored), iso(self.last_stored)],
            'fetched': self.fetched,
            'written': self.written,
            'already_present': self.already_present,
            'dropped_unclosed': self.dropped_unclosed,
            'rejected': self.rejected,
            'requests': self.requests,
            'expected': self.expected,
            'missing_before': self.missing_before,
            'stored': self.stored,
            'gaps': {'leading': self.leading_gap, 'interior': self.interior_gap, 'trailing': self.trailing_gap},
            'errors': self.errors[:10],
        }


def format_summary(summaries: Sequence[FeedSummary], *, verbose_reports: Sequence[sync.SeriesReport] = ()) -> str:
    lines: list[str] = []

    def fmt(value: datetime | None) -> str:
        return value.strftime('%Y-%m-%d %H:%M') if value else 'n/a'

    for item in summaries:
        status = 'OK' if not item.failed else f'FAILED for {item.failed} of {item.coins} coin(s)'
        lines.append(f'{item.feed}  [{status}]')
        lines.append(f'  requested  {fmt(item.requested_start)} .. {fmt(item.requested_end)} UTC (end exclusive), {item.coins} coin(s)')
        lines.append(f'  covered    {fmt(item.first_stored)} .. {fmt(item.last_stored)} UTC   (earliest first row .. latest last row across coins)')
        expected = 'n/a (no fixed grid)' if item.expected is None else str(item.expected)
        missing = '' if item.missing_before is None else f', missing before this run {item.missing_before}'
        lines.append(f'  rows       stored {item.stored}, expected {expected}{missing}')
        lines.append(
            f'  this run   fetched {item.fetched}, NEW {item.written}, already present {item.already_present}, '
            f'unclosed dropped {item.dropped_unclosed}, malformed rejected {item.rejected}, data requests {item.requests}'
        )
        lines.append(f'  gaps       leading {item.leading_gap}, interior {item.interior_gap}, trailing {item.trailing_gap}')
        for note in item.notes[:6]:
            lines.append(f'  note       {note}')
        for error in item.errors[:5]:
            lines.append(f'  ERROR      {error}')
        lines.append('')
    if verbose_reports:
        lines.append('per series:')
        for r in verbose_reports:
            if r.first_stored is None:
                lines.append(f'  {r.feed:20s} {r.coin:10s} no rows stored  {r.error or "; ".join(r.notes)}')
                continue
            of_expected = '' if r.expected is None else f'/{r.expected}'
            lines.append(
                f'  {r.feed:20s} {r.coin:10s} new {r.written:6d}  stored {r.stored}{of_expected}  '
                f'gaps {r.leading_gap}/{r.interior_gap}/{r.trailing_gap}  first {r.first_stored:%Y-%m-%d %H:%M}'
            )
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# ---------------------------------------------------------------------------
# argument handling
# ---------------------------------------------------------------------------
def parse_instant(text: str) -> datetime:
    """ISO date or datetime -> aware UTC. A value without a zone is read as UTC."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'{text!r} is not an ISO date/datetime such as 2026-01-31 or 2026-01-31T12:00') from exc
    return ensure_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m app.worker.backfill',
        description='Bootstrap Binance klines (perp and spot), settled funding and open interest into Postgres.',
    )
    parser.add_argument('--coins', default=','.join(settings.watchlist_symbols), help='comma-separated Binance USDT symbols (default: the configured watchlist)')
    parser.add_argument('--timeframes', default=','.join(E.TIMEFRAMES), help=f'comma-separated, from {", ".join(E.TIMEFRAMES)} (default: all)')
    parser.add_argument('--markets', default=','.join(E.CANDLE_MARKETS), help='perp,spot (default: both)')
    parser.add_argument('--feeds', default=','.join(FEEDS), help=f'comma-separated, from {", ".join(FEEDS)} (default: all)')
    parser.add_argument('--start', type=parse_instant, help='range start, UTC (inclusive)')
    parser.add_argument('--end', type=parse_instant, help='range end, UTC (exclusive; default: now)')
    parser.add_argument('--days', type=int, help='shorthand for --start = end - N days')
    parser.add_argument('--dry-run', action='store_true', help='report what is missing without fetching or writing')
    parser.add_argument('-v', '--verbose', action='store_true', help='print every series, not just the per-feed totals')
    return parser


def _choices(raw: str, allowed: Sequence[str], what: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(token.strip() for token in raw.split(',') if token.strip()))
    unknown = [v for v in values if v not in allowed]
    if unknown or not values:
        raise SystemExit(f'error: unknown {what} {unknown or raw!r}; choose from {", ".join(allowed)}')
    return values


def resolve_range(args: argparse.Namespace, now: datetime) -> tuple[datetime, datetime]:
    end = args.end or now
    if args.start is not None and args.days is not None:
        raise SystemExit('error: give --start or --days, not both')
    if args.start is None and args.days is None:
        raise SystemExit('error: give --start or --days')
    if args.days is not None:
        if args.days <= 0:
            raise SystemExit('error: --days must be positive')
        start = end - timedelta(days=args.days)
    else:
        start = args.start
    if start >= end:
        raise SystemExit(f'error: start {start.isoformat()} is not before end {end.isoformat()}')
    return start, end


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def run_backfill(
    session: Session,
    client: BinanceClient,
    *,
    coins: Sequence[str],
    timeframes: Sequence[str],
    markets: Sequence[str],
    feeds: Sequence[str],
    start: datetime,
    end: datetime,
    settings: Settings,
    dry_run: bool = False,
    progress: Any = None,
) -> tuple[list[FeedSummary], list[sync.SeriesReport]]:
    """Run the requested feeds; returns the per-feed summaries and every series report."""
    summaries: dict[str, FeedSummary] = {}
    reports: list[sync.SeriesReport] = []
    tolerance = timedelta(hours=settings.feed_funding_interval_hours) + sync.FUNDING_GAP_SLACK

    def take(report: sync.SeriesReport) -> None:
        reports.append(report)
        summaries.setdefault(report.feed, FeedSummary(report.feed)).add(report)

    for coin in coins:
        if 'klines' in feeds:
            for market in markets:
                for timeframe in timeframes:
                    take(sync.sync_klines(session, client, coin=coin, market=market, timeframe=timeframe, start=start, end=end, dry_run=dry_run, progress=progress))
        if 'funding' in feeds:
            take(sync.sync_funding_settled(session, client, coin=coin, start=start, end=end, tolerance=tolerance, dry_run=dry_run, progress=progress))
        if 'oi' in feeds:
            for period in timeframes:
                take(sync.sync_open_interest(session, client, coin=coin, period=period, start=start, end=end, dry_run=dry_run, progress=progress))
    return list(summaries.values()), reports


def _record_job_run(session: Session, started: datetime, summaries: Sequence[FeedSummary], error: str | None, args_text: str) -> None:
    try:
        session.add(
            JobRun(
                job_name=JOB_NAME,
                started_at=started,
                finished_at=max(utc_now(), started),
                status='error' if error or any(s.failed for s in summaries) else 'ok',
                rows_touched=sum(s.written for s in summaries),
                source=None,
                code_version=code_version(),
                detail={'args': args_text, 'feeds': {s.feed: s.as_detail() for s in summaries}},
                error=error,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        log.exception('could not write the backfill job_run row')


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = build_parser(settings)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.Formatter.converter = time.gmtime

    try:
        coins = parse_symbols(args.coins)
    except ValueError as exc:
        raise SystemExit(f'error: {exc}') from exc
    if not coins:
        raise SystemExit('error: no coins given')
    timeframes = _choices(args.timeframes, E.TIMEFRAMES, 'timeframe')
    markets = _choices(args.markets, E.CANDLE_MARKETS, 'market')
    feeds = _choices(args.feeds, FEEDS, 'feed')

    client = BinanceClient(settings=settings)
    started = utc_now()
    start, end = resolve_range(args, client.now())
    print(
        f'Backfill {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M} UTC | {len(coins)} coin(s) | '
        f'markets {",".join(markets)} | timeframes {",".join(timeframes)} | feeds {",".join(feeds)}'
        f'{" | DRY RUN" if args.dry_run else ""}'
    )
    if 'oi' in feeds and start < client.now() - timedelta(days=OPEN_INTEREST_HISTORY_DAYS):
        print(f'note: open-interest history is only served for the last ~{OPEN_INTEREST_HISTORY_DAYS} days; older open interest cannot be backfilled.')

    session = get_sync_session_factory()()
    summaries: list[FeedSummary] = []
    reports: list[sync.SeriesReport] = []
    refused: str | None = None
    try:
        summaries, reports = run_backfill(
            session,
            client,
            coins=coins,
            timeframes=timeframes,
            markets=markets,
            feeds=feeds,
            start=start,
            end=end,
            settings=settings,
            dry_run=args.dry_run,
            progress=print if args.verbose else None,
        )
    except (GeoBlocked, RateLimited) as exc:
        refused = f'{type(exc).__name__}: {exc}'
        print(f'\nSTOPPED: {refused}', file=sys.stderr)
    finally:
        if not args.dry_run:
            _record_job_run(session, started, summaries, refused, ' '.join(argv if argv is not None else sys.argv[1:]))
        session.close()
        client.close()
        dispose_sync_engine()

    print()
    print(format_summary(summaries, verbose_reports=reports if args.verbose else ()))
    if refused:
        print('The run was cut short; nothing above is a complete picture. Re-run it to resume.')
        return EXIT_REFUSED
    return EXIT_SERIES_FAILED if any(s.failed for s in summaries) else EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
