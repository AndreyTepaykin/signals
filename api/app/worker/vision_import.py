"""Load Binance Vision kline and open-interest zips into the feed tables.

Monthly kline zips cover a finished month. Daily kline zips are read only for a
month that has no monthly zip. Open interest comes from the 5-minute metrics
files; only the print on each hour boundary is stored, as timeframe ``1h``,
which is the series the feature engine reads.

Writes go through :func:`app.worker.feeds.store.write_candles` and
:func:`app.worker.feeds.store.write_open_interest`. Both insert nothing that is
already stored, so a live row from the API is left as it is.

``python -m app.worker.vision_import [--root DIR] [--coins A,B] [--feeds klines,oi]``
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings, parse_symbols
from app.core.db_sync import get_sync_session_factory
from app.core.features.params import OI_SERIES_TIMEFRAME
from app.core.time import utc_now
from app.models import enums as E
from app.worker.feeds import records as R
from app.worker.feeds.records import CandleRecord, OpenInterestRecord
from app.worker.feeds.store import write_candles, write_open_interest

UTC = timezone.utc
KLINE_TIMEFRAMES = ('15m', '1h', '4h')
FEEDS = ('klines', 'oi')

_ARCHIVE = Path(__file__).resolve().parents[4] / 'candles_OI_history'
_MARKETS = (
    (Path('futures') / 'um', E.MARKET_PERP),
    (Path('spot'), E.MARKET_SPOT),
)


def kline_month(path: Path) -> str:
    """``BTCUSDT-15m-2026-01.zip`` or ``...-2026-10-01.zip`` -> ``2026-01`` / ``2026-10``."""
    parts = path.stem.split('-')
    if len(parts) >= 2 and len(parts[-1]) == 2 and len(parts[-2]) == 4:
        return f'{parts[-2]}-{parts[-1]}'
    if len(parts) >= 3 and len(parts[-1]) == 2 and len(parts[-2]) == 2 and len(parts[-3]) == 4:
        return f'{parts[-3]}-{parts[-2]}'
    raise ValueError(f'cannot read a month from {path.name}')


def select_kline_zips(monthly: Iterable[Path], daily: Iterable[Path]) -> list[Path]:
    """Monthly file wins for its month. Daily files fill only the months it does not cover."""
    monthly_files = sorted(path for path in monthly if path.suffix == '.zip')
    covered = {kline_month(path) for path in monthly_files}
    daily_files = [
        path for path in sorted(daily) if path.suffix == '.zip' and kline_month(path) not in covered
    ]
    return monthly_files + daily_files


def _epoch_ms(token: str) -> int:
    """Vision stamps are milliseconds on USD-M and microseconds on spot."""
    value = int(token)
    if value >= 10**14:
        value //= 1000
    return value


def parse_vision_klines(text: str, *, coin: str, market: str, timeframe: str) -> list[CandleRecord]:
    """One Vision kline CSV, header or not, into candle records.

    A row :func:`records.parse_kline_row` refuses is skipped. The rest keep the
    same close-time and available-at rules as a live kline.
    """
    records: list[CandleRecord] = []
    for line in text.splitlines():
        if not line or line.startswith('open_time'):
            continue
        parts = line.split(',')
        if len(parts) < 11 or not parts[0].isdigit():
            continue
        raw = [
            _epoch_ms(parts[0]),
            parts[1],
            parts[2],
            parts[3],
            parts[4],
            parts[5],
            _epoch_ms(parts[6]),
            parts[7],
            int(parts[8]),
            parts[9],
            parts[10],
        ]
        try:
            records.append(R.parse_kline_row(raw, coin=coin, market=market, timeframe=timeframe))
        except R.MalformedRow:
            continue
    return _dedupe(records, key=lambda record: record.key)


def parse_vision_hourly_oi(
    text: str,
    *,
    coin: str,
    publication_lag: timedelta,
) -> list[OpenInterestRecord]:
    """Hourly prints from a 5-minute metrics CSV.

    ``create_time`` is UTC. A row is kept when that clock time sits on an hour
    boundary, and stored as the ``1h`` series the engine joins to 1h closes.
    """
    records: list[OpenInterestRecord] = []
    for line in text.splitlines():
        if not line or line.startswith('create_time'):
            continue
        parts = line.split(',')
        if len(parts) < 4:
            continue
        try:
            stamp = datetime.fromisoformat(parts[0])
        except ValueError:
            continue
        if stamp.minute or stamp.second or stamp.microsecond:
            continue
        poll_time = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
        quote = parts[3] if len(parts) > 3 and parts[3] != '' else None
        raw = {
            'sumOpenInterest': parts[2],
            'sumOpenInterestValue': quote,
            'timestamp': R.to_ms(poll_time),
        }
        try:
            records.append(
                R.parse_open_interest_row(raw, coin=coin, period=OI_SERIES_TIMEFRAME, publication_lag=publication_lag)
            )
        except R.MalformedRow:
            continue
    return _dedupe(records, key=lambda record: record.key)


def _dedupe(records: list, *, key) -> list:  # noqa: ANN001 - both record types
    seen: set = set()
    unique = []
    for record in records:
        identity = key(record)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(record)
    return unique


def _zip_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith('.csv')]
        if len(names) != 1:
            raise ValueError(f'{path.name} holds {len(names)} csv files')
        return archive.read(names[0]).decode('utf-8')


def iter_kline_jobs(root: Path, coins: Sequence[str]) -> list[tuple[Path, str, str, str]]:
    """``(zip, coin, market, timeframe)`` in a stable order."""
    jobs: list[tuple[Path, str, str, str]] = []
    for market_dir, market in _MARKETS:
        for coin in coins:
            for timeframe in KLINE_TIMEFRAMES:
                base = root / market_dir
                monthly = base / 'monthly' / 'klines' / coin / timeframe
                daily = base / 'daily' / 'klines' / coin / timeframe
                zips = select_kline_zips(
                    monthly.glob('*.zip') if monthly.is_dir() else (),
                    daily.glob('*.zip') if daily.is_dir() else (),
                )
                jobs.extend((path, coin, market, timeframe) for path in zips)
    return jobs


def iter_oi_zips(root: Path, coins: Sequence[str]) -> list[tuple[Path, str]]:
    jobs: list[tuple[Path, str]] = []
    for coin in coins:
        folder = root / 'futures' / 'um' / 'daily' / 'metrics' / coin
        if not folder.is_dir():
            continue
        jobs.extend((path, coin) for path in sorted(folder.glob('*.zip')))
    return jobs


def _closed(records: Sequence, *, now: datetime) -> list:  # noqa: ANN001
    return [record for record in records if record.available_at <= now]


def import_klines(session: Session, jobs: Sequence[tuple[Path, str, str, str]], *, now: datetime) -> tuple[int, int]:
    submitted = written = 0
    for index, (path, coin, market, timeframe) in enumerate(jobs, start=1):
        records = _closed(parse_vision_klines(_zip_text(path), coin=coin, market=market, timeframe=timeframe), now=now)
        result = write_candles(session, records, now=now)
        session.commit()
        submitted += result.submitted
        written += result.written
        if index % 25 == 0 or index == len(jobs):
            print(
                f'klines [{index}/{len(jobs)}] {coin} {market} {timeframe} {path.name}: '
                f'+{result.written} (present {result.already_present})',
                flush=True,
            )
    return submitted, written


def import_oi(
    session: Session,
    jobs: Sequence[tuple[Path, str]],
    *,
    now: datetime,
    publication_lag: timedelta,
) -> tuple[int, int]:
    submitted = written = 0
    for index, (path, coin) in enumerate(jobs, start=1):
        records = _closed(
            parse_vision_hourly_oi(_zip_text(path), coin=coin, publication_lag=publication_lag),
            now=now,
        )
        result = write_open_interest(session, records, now=now)
        session.commit()
        submitted += result.submitted
        written += result.written
        if index % 100 == 0 or index == len(jobs):
            print(
                f'oi [{index}/{len(jobs)}] {coin} {path.name}: +{result.written} (present {result.already_present})',
                flush=True,
            )
    return submitted, written


def main(argv: Sequence[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog='python -m app.worker.vision_import', description=__doc__.strip().splitlines()[0])
    parser.add_argument('--root', type=Path, default=_ARCHIVE, help='candles_OI_history directory')
    parser.add_argument('--coins', default=','.join(settings.watchlist_symbols), help='comma-separated symbols')
    parser.add_argument('--feeds', default=','.join(FEEDS), help=f'comma-separated, from {", ".join(FEEDS)}')
    args = parser.parse_args(argv)
    try:
        coins = parse_symbols(args.coins)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    feeds = tuple(dict.fromkeys(token.strip() for token in args.feeds.split(',') if token.strip()))
    unknown = [feed for feed in feeds if feed not in FEEDS]
    if unknown or not feeds:
        print(f'error: unknown feeds {unknown or args.feeds!r}', file=sys.stderr)
        return 2
    root = args.root
    if not root.is_dir():
        print(f'error: {root} is not a directory', file=sys.stderr)
        return 2

    now = utc_now()
    lag = timedelta(seconds=settings.feed_open_interest_publication_lag_seconds)
    print(f'Vision import {root} | {len(coins)} coin(s) | feeds {",".join(feeds)} | as of {now:%Y-%m-%d %H:%M} UTC')
    session = get_sync_session_factory()()
    try:
        if 'klines' in feeds:
            jobs = iter_kline_jobs(root, coins)
            print(f'kline zips: {len(jobs)}', flush=True)
            submitted, written = import_klines(session, jobs, now=now)
            print(f'klines submitted {submitted} written {written}', flush=True)
        if 'oi' in feeds:
            jobs = iter_oi_zips(root, coins)
            print(f'oi zips: {len(jobs)}', flush=True)
            submitted, written = import_oi(session, jobs, now=now, publication_lag=lag)
            print(f'oi submitted {submitted} written {written}', flush=True)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
