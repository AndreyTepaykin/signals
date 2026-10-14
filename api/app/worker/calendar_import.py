"""Owner-loaded event calendar: import a CSV into ``event_calendar``, and check unlock coverage.

``python -m app.worker.calendar_import --file PATH.csv [--dry-run] [--allow-stand-down-kinds] [--allow-event-flag-flip] [--available-at ISO]``
``python -m app.worker.calendar_import --coverage [--days N]``

Import contract
---------------
* CSV with a header row naming exactly ``CSV_COLUMNS``: ``kind, coin, event_time, is_large, notional_usd, title,
  feed_source``. ``coin`` is empty for macro events (``cpi``, ``fomc``) and a Binance USDT symbol for ``unlock`` and
  ``expiry``; ``event_time`` is ISO 8601 with an offset and is stored in UTC; ``feed_source`` (where the date came from)
  is required. Every row is validated before anything is written, every error is reported with its line number, and
  any error exits non-zero with nothing written.
* Insert only: ``ON CONFLICT DO NOTHING`` on ``(kind, coin, event_time)``; a second import of the same file inserts 0.
  A stored row is never changed, so a corrected date is a NEW row and the wrong one stays (and stays visible).
* ``available_at`` defaults to the import instant, so a replay of bars before the import cannot see a row we did not
  have. ``--available-at`` sets an earlier knowledge time (a date published in advance); replay then sees the row from
  that instant. It may not be in the future.
* Stand-down kinds (``cpi``, ``fomc``, ``expiry``) are refused without ``--allow-stand-down-kinds``.
* A file that brings the first CPI or FOMC row is refused without ``--allow-event-flag-flip``: that row makes the
  calendar 'loaded' (``E.CALENDAR_LOADED_KINDS``), so ``feature_row.event_flag`` turns from NULL into FALSE for every
  coin. Unlock and expiry rows alone leave it NULL.
* ``--dry-run`` validates and counts inside a READ ONLY transaction and writes nothing.

``--coverage`` is read-only (``SET TRANSACTION READ ONLY``): for every watchlist coin except BTC and ETH it reports the
visible unlock rows, the next unlock within ``--days`` (default 90) and lists the coins with no unlock row at all. It
always exits 0.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import parse_symbols
from app.core import constants as C
from app.core.time import ensure_utc, utc_now
from app.models import enums as E
from app.models.as_of import as_of
from app.models.feeds import EventCalendar

CSV_COLUMNS = ('kind', 'coin', 'event_time', 'is_large', 'notional_usd', 'title', 'feed_source')
MACRO_KINDS = (C.EVENT_KIND_CPI, C.EVENT_KIND_FOMC)
COIN_REQUIRED_KINDS = (C.EVENT_KIND_EXPIRY, E.EVENT_KIND_UNLOCK)
UNIQUE_KEY = 'uq_event_calendar_kind_coin_event_time'

#: Coins the unlock coverage check skips: no token unlock schedule applies to them.
COVERAGE_EXEMPT_COINS = ('BTCUSDT', 'ETHUSDT')
DEFAULT_COVERAGE_DAYS = 90

#: The read gate needs an instant; this one sees every stored row whatever its ``available_at``, which is what a
#: key-existence check needs (a row the gate hid would still make the insert a no-op).
EVERY_STORED_ROW = datetime(9999, 12, 31, tzinfo=timezone.utc)

STAND_DOWN_EXPLANATION = (
    'cpi, fomc and expiry are stand-down kinds. Each such row makes feature_row.event_flag TRUE on every 15m bar '
    'inside its window (cpi/fomc 30 min either side, expiry 15 min; an expiry with is_large=false never windows, an '
    'empty is_large counts as large) for EVERY coin, because feature assembly ignores the row\'s coin; the rules then '
    'cancel those candidates with stand_down:event_window, and the dashboard banner and ntfy announce the window. '
    'The first cpi or fomc row also makes event_flag read FALSE ("no event") instead of NULL ("unknown") outside '
    'the windows on later feature rows, which changes their feature_hash and their event_flag pool cell (an expiry '
    'row alone only sets TRUE inside its window). Pass --allow-stand-down-kinds '
    'only for dates from a source you trust.'
)
EVENT_FLAG_FLIP_EXPLANATION = (
    'event_calendar holds no CPI or FOMC row yet. The first one makes the calendar loaded: event_flag / '
    'active_event_kinds read FALSE / [] ("no event") instead of NULL ("unknown") on every feature row whose bar '
    'closes at or after its available_at, for every coin, outside the event windows. That changes '
    'feature_hash on those rows (rule_hash does not change) and moves their candidates and labels from the '
    'event_flag=unknown pool cell to event_flag=False. Stand-down does not change: unknown does not stand down '
    'either. Pass --allow-event-flag-flip to accept this.'
)
AVAILABLE_AT_WARNING = (
    'WARNING: --available-at {at} is earlier than the import instant. Replay of any bar closing at or after {at} '
    'will see these rows although this database only learned them now; re-running the feature job over such bars '
    'reports feature_hash mismatches on the rows already stored (they are not rewritten).'
)

CalendarKey = tuple[str, str | None, datetime]


@dataclass(frozen=True)
class CalendarRow:
    line: int
    kind: str
    coin: str | None
    event_time: datetime
    is_large: bool | None
    notional_usd: Decimal | None
    title: str | None
    feed_source: str

    @property
    def key(self) -> CalendarKey:
        return (self.kind, self.coin, self.event_time)


@dataclass
class ParseResult:
    rows: list[CalendarRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportReport:
    rows: int
    inserted: int
    already_present: int
    dry_run: bool
    available_at: datetime


@dataclass(frozen=True)
class CoinCoverage:
    coin: str
    unlock_rows: int
    next_unlock: datetime | None

    @property
    def has_any(self) -> bool:
        return self.unlock_rows > 0


# ---------------------------------------------------------------------------
# Parsing and validation (pure)
# ---------------------------------------------------------------------------
def parse_instant(raw: str) -> datetime:
    """ISO 8601 with an explicit offset -> aware UTC datetime. A naive value raises: its zone would be a guess."""
    try:
        value = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(f'{raw!r} is not an ISO 8601 date-time') from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'{raw!r} has no UTC offset (write e.g. 2026-11-12T13:30:00+00:00)')
    return ensure_utc(value)


def _optional_bool(raw: str) -> bool | None:
    text = raw.strip().lower()
    if text == '':
        return None
    if text in ('true', 'false'):
        return text == 'true'
    raise ValueError(f'is_large {raw!r} must be empty, true or false')


def _optional_amount(raw: str) -> Decimal | None:
    text = raw.strip()
    if text == '':
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f'notional_usd {raw!r} is not a number') from None
    if not value.is_finite() or value < 0:
        raise ValueError(f'notional_usd {raw!r} must be a finite, non-negative number')
    return value


def parse_record(values: Mapping[str, str], line: int) -> tuple[CalendarRow | None, list[str]]:
    """One CSV record -> a row, or every problem found in it."""
    errors: list[str] = []
    kind = values['kind'].strip()
    if kind not in E.EVENT_KINDS:
        errors.append(f'line {line}: kind {kind!r} is not one of {", ".join(E.EVENT_KINDS)}')

    coin: str | None = values['coin'].strip() or None
    if coin is not None:
        try:
            symbols = parse_symbols(coin)
        except ValueError as exc:
            errors.append(f'line {line}: coin {exc}')
        else:
            if len(symbols) != 1:
                errors.append(f'line {line}: coin {coin!r} must be exactly one symbol')
            else:
                coin = symbols[0]
    if kind in MACRO_KINDS and coin is not None:
        errors.append(f'line {line}: {kind} is a macro event, so coin must be empty (got {coin!r})')
    if kind in COIN_REQUIRED_KINDS and coin is None:
        errors.append(f'line {line}: {kind} needs a coin (a Binance USDT symbol such as SOLUSDT)')

    event_time: datetime | None = None
    try:
        event_time = parse_instant(values['event_time'])
    except ValueError as exc:
        errors.append(f'line {line}: event_time {exc}')

    is_large: bool | None = None
    notional: Decimal | None = None
    try:
        is_large = _optional_bool(values['is_large'])
    except ValueError as exc:
        errors.append(f'line {line}: {exc}')
    try:
        notional = _optional_amount(values['notional_usd'])
    except ValueError as exc:
        errors.append(f'line {line}: {exc}')

    feed_source = values['feed_source'].strip()
    if not feed_source:
        errors.append(f'line {line}: feed_source is required (where the date came from)')

    if errors or event_time is None:
        return None, errors
    row = CalendarRow(
        line=line,
        kind=kind,
        coin=coin,
        event_time=event_time,
        is_large=is_large,
        notional_usd=notional,
        title=values['title'].strip() or None,
        feed_source=feed_source,
    )
    return row, []


def parse_csv(text: str) -> ParseResult:
    """The whole file -> rows plus every error, header problems first. Rows are only usable when ``errors`` is empty."""
    result = ParseResult()
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        result.errors.append('line 1: the file is empty; a header row is required: ' + ','.join(CSV_COLUMNS))
        return result
    header = [name.strip() for name in reader.fieldnames]
    missing = [name for name in CSV_COLUMNS if name not in header]
    unknown = [name for name in header if name not in CSV_COLUMNS]
    repeated = sorted({name for name in header if header.count(name) > 1})
    if missing:
        result.errors.append(f'line 1: header is missing {", ".join(missing)}')
    if unknown:
        result.errors.append(f'line 1: header has unknown columns {", ".join(unknown)} (event_calendar has no such column)')
    if repeated:
        result.errors.append(f'line 1: header repeats {", ".join(repeated)}')
    if result.errors:
        return result
    reader.fieldnames = header

    seen: dict[CalendarKey, int] = {}
    for record in reader:
        line = reader.line_num
        if None in record:
            result.errors.append(f'line {line}: more fields than header columns')
            continue
        if any(record[name] is None for name in CSV_COLUMNS):
            result.errors.append(f'line {line}: fewer fields than header columns')
            continue
        row, errors = parse_record(record, line)
        result.errors.extend(errors)
        if row is None:
            continue
        if row.key in seen:
            result.errors.append(f'line {line}: same kind, coin and event_time as line {seen[row.key]}')
            continue
        seen[row.key] = line
        result.rows.append(row)
    return result


def stand_down_refusals(rows: Iterable[CalendarRow], *, allowed: bool) -> list[str]:
    if allowed:
        return []
    return [
        f'line {row.line}: {row.kind} is a stand-down kind; refused without --allow-stand-down-kinds'
        for row in rows
        if row.kind in E.STAND_DOWN_EVENT_KINDS
    ]


def row_notes(rows: Iterable[CalendarRow]) -> list[str]:
    return [
        f'line {row.line}: expiry with empty is_large is treated as large (stands the book down)'
        for row in rows
        if row.kind == C.EVENT_KIND_EXPIRY and row.is_large is None
    ]


def resolve_available_at(raw: str | None, now: datetime) -> datetime:
    """``--available-at`` or the import instant. A knowledge time in the future is refused."""
    now = ensure_utc(now)
    if raw is None:
        return now
    value = parse_instant(raw)
    if value > now:
        raise ValueError(f'--available-at {value.isoformat()} is in the future (now {now.isoformat()})')
    return value


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def calendar_has_rows(session: Session, kinds: Sequence[str] | None = None) -> bool:
    """Whether any row (of ``kinds``, when given) is stored, whatever its ``available_at``."""
    statement = as_of(EVERY_STORED_ROW).select(EventCalendar.id)
    if kinds is not None:
        statement = statement.where(EventCalendar.kind.in_(kinds))
    return session.execute(statement.limit(1)).first() is not None


def would_flip_event_flag(session: Session, rows: Sequence[CalendarRow]) -> bool:
    """The file brings the first CPI or FOMC row, which makes the calendar 'loaded' for feature assembly."""
    if not any(row.kind in E.CALENDAR_LOADED_KINDS for row in rows):
        return False
    return not calendar_has_rows(session, E.CALENDAR_LOADED_KINDS)


def stored_keys(session: Session, rows: Sequence[CalendarRow]) -> set[CalendarKey]:
    """The keys of ``rows`` that are already stored."""
    if not rows:
        return set()
    lo = min(row.event_time for row in rows)
    hi = max(row.event_time for row in rows)
    statement = as_of(EVERY_STORED_ROW).select(EventCalendar.kind, EventCalendar.coin, EventCalendar.event_time).where(
        EventCalendar.event_time >= lo, EventCalendar.event_time <= hi
    )
    stored = {(kind, coin, ensure_utc(at)) for kind, coin, at in session.execute(statement).all()}
    return {row.key for row in rows if row.key in stored}


def import_rows(
    session: Session,
    rows: Sequence[CalendarRow],
    *,
    available_at: datetime,
    dry_run: bool = False,
) -> ImportReport:
    """Insert ``rows`` (ON CONFLICT DO NOTHING) with one ``available_at``. Does not commit. ``dry_run`` only reads."""
    available_at = ensure_utc(available_at)
    if dry_run:
        present = len(stored_keys(session, rows))
        return ImportReport(len(rows), len(rows) - present, present, True, available_at)
    if not rows:
        return ImportReport(0, 0, 0, False, available_at)
    values = [
        {
            'kind': row.kind,
            'coin': row.coin,
            'event_time': row.event_time,
            'title': row.title,
            'feed_source': row.feed_source,
            'is_large': row.is_large,
            'notional_usd': row.notional_usd,
            'available_at': available_at,
        }
        for row in rows
    ]
    stored = session.execute(
        pg_insert(EventCalendar).values(values).on_conflict_do_nothing(constraint=UNIQUE_KEY).returning(EventCalendar.id)
    ).all()
    return ImportReport(len(rows), len(stored), len(rows) - len(stored), False, available_at)


def read_unlocks(session: Session, now: datetime) -> list[tuple[str, datetime]]:
    statement = as_of(now).select(EventCalendar.coin, EventCalendar.event_time).where(
        EventCalendar.kind == E.EVENT_KIND_UNLOCK, EventCalendar.coin.is_not(None)
    )
    return [(coin, ensure_utc(at)) for coin, at in session.execute(statement).all()]


def count_visible_rows(session: Session, now: datetime) -> int:
    return len(session.execute(as_of(now).select(EventCalendar.id)).all())


# ---------------------------------------------------------------------------
# Unlock coverage (pure)
# ---------------------------------------------------------------------------
def unlock_coverage(
    watchlist: Sequence[str],
    unlocks: Iterable[tuple[str, datetime]],
    *,
    now: datetime,
    days: int = DEFAULT_COVERAGE_DAYS,
) -> list[CoinCoverage]:
    """Per watchlist alt (BTC and ETH skipped): unlock row count and the next unlock in ``[now, now + days]``."""
    now = ensure_utc(now)
    horizon = now + timedelta(days=days)
    by_coin: dict[str, list[datetime]] = {}
    for coin, at in unlocks:
        by_coin.setdefault(coin, []).append(ensure_utc(at))
    out = []
    for coin in watchlist:
        if coin in COVERAGE_EXEMPT_COINS:
            continue
        times = by_coin.get(coin, [])
        upcoming = [at for at in times if now <= at <= horizon]
        out.append(CoinCoverage(coin, len(times), min(upcoming) if upcoming else None))
    return out


def missing_unlock_calendar(coverage: Iterable[CoinCoverage]) -> list[str]:
    return [c.coin for c in coverage if not c.has_any]


def format_coverage(coverage: Sequence[CoinCoverage], *, now: datetime, days: int, visible_rows: int) -> str:
    lines = [
        f'unlock coverage at {ensure_utc(now).isoformat()} (next unlock within {days} days; '
        f'{", ".join(COVERAGE_EXEMPT_COINS)} skipped)',
        f'event_calendar rows visible now: {visible_rows}'
        + ('' if visible_rows else ' (event_flag on new feature rows reads NULL "unknown")'),
        '',
        f'{"coin":<12} {"unlock_rows":>11}  next_unlock',
    ]
    for c in coverage:
        nxt = c.next_unlock.isoformat() if c.next_unlock is not None else ('none in window' if c.has_any else '-')
        lines.append(f'{c.coin:<12} {c.unlock_rows:>11}  {nxt}')
    missing = missing_unlock_calendar(coverage)
    lines.append('')
    if missing:
        lines.append(f'missing unlock calendar ({len(missing)}): {", ".join(missing)}')
    else:
        lines.append('missing unlock calendar: none')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _database_label() -> str:
    from sqlalchemy.engine import make_url

    from app.core.db_sync import get_sync_database_url

    return make_url(get_sync_database_url()).render_as_string(hide_password=True)


def _run_import(args: argparse.Namespace) -> int:
    from app.core.db_sync import get_sync_engine, get_sync_session_factory
    from app.worker.stats_job import readonly_session

    path = Path(args.file)
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        print(f'cannot read {path}: {exc}', file=sys.stderr)
        return 1

    now = utc_now()
    parsed = parse_csv(text)
    refusals = stand_down_refusals(parsed.rows, allowed=args.allow_stand_down_kinds)
    errors = parsed.errors + refusals
    try:
        available_at = resolve_available_at(args.available_at, now)
    except ValueError as exc:
        errors.append(str(exc))
        available_at = now
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        if refusals:
            print('', file=sys.stderr)
            print(STAND_DOWN_EXPLANATION, file=sys.stderr)
        print(f'{len(errors)} error(s); nothing written.', file=sys.stderr)
        return 1

    print(f'database: {_database_label()}')
    print(f'file: {path} ({len(parsed.rows)} row(s))')
    print(f'mode: {"dry run (READ ONLY transaction, nothing written)" if args.dry_run else "write"}')
    print(f'available_at: {available_at.isoformat()}' + (' (import instant)' if args.available_at is None else ''))
    if args.available_at is not None and available_at < now:
        print(AVAILABLE_AT_WARNING.format(at=available_at.isoformat()))
    if any(row.kind in E.STAND_DOWN_EVENT_KINDS for row in parsed.rows):
        print('NOTE: ' + STAND_DOWN_EXPLANATION)
    for note in row_notes(parsed.rows):
        print('NOTE: ' + note)

    if args.dry_run:
        with readonly_session(get_sync_engine()) as session:
            flips = would_flip_event_flag(session, parsed.rows)
            report = import_rows(session, parsed.rows, available_at=available_at, dry_run=True)
        if flips and not args.allow_event_flag_flip:
            print('NOTE: a real run would be refused without --allow-event-flag-flip. ' + EVENT_FLAG_FLIP_EXPLANATION)
        print(f'would insert {report.inserted}, already present {report.already_present}')
        return 0

    with get_sync_session_factory()() as session:
        if would_flip_event_flag(session, parsed.rows) and not args.allow_event_flag_flip:
            print(EVENT_FLAG_FLIP_EXPLANATION, file=sys.stderr)
            print('nothing written.', file=sys.stderr)
            return 1
        report = import_rows(session, parsed.rows, available_at=available_at)
        session.commit()
    print(f'inserted {report.inserted}, already present {report.already_present}')
    return 0


def _run_coverage(args: argparse.Namespace) -> int:
    from app.config import get_settings
    from app.core.db_sync import get_sync_engine
    from app.worker.stats_job import readonly_session

    now = utc_now()
    with readonly_session(get_sync_engine()) as session:
        unlocks = read_unlocks(session, now)
        visible = count_visible_rows(session, now)
    coverage = unlock_coverage(get_settings().watchlist_symbols, unlocks, now=now, days=args.days)
    print(f'database: {_database_label()} (READ ONLY)')
    print(format_coverage(coverage, now=now, days=args.days, visible_rows=visible))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Import the owner-kept event calendar (CSV), or check unlock coverage.')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--file', help='CSV to import (header: ' + ','.join(CSV_COLUMNS) + ')')
    action.add_argument('--coverage', action='store_true', help='read-only unlock coverage report for the watchlist alts')
    parser.add_argument('--dry-run', action='store_true', help='with --file: validate and count, write nothing')
    parser.add_argument('--allow-stand-down-kinds', action='store_true', help='with --file: accept cpi, fomc and expiry rows')
    parser.add_argument(
        '--allow-event-flag-flip',
        action='store_true',
        help='with --file: accept loading the first CPI or FOMC row (event_flag turns from NULL to FALSE)',
    )
    parser.add_argument('--available-at', help='with --file: earlier knowledge time, ISO 8601 with offset (default: now)')
    parser.add_argument('--days', type=int, default=DEFAULT_COVERAGE_DAYS, help='with --coverage: look-ahead window')
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error('--days must be at least 1')
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    if args.coverage:
        return _run_coverage(args)
    return _run_import(args)


if __name__ == '__main__':
    sys.exit(main())
