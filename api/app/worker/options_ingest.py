"""Store one Deribit book and the figures derived from it.

``ingest_snapshot`` takes a parsed :class:`~app.worker.feeds.deribit.SnapshotRecord` and, in the caller's
transaction (it never commits):

1. inserts the ``options_snapshot`` row (idempotent on ``(venue, coin, snapshot_time)``);
2. derives one ``options_metric`` row per horizon from the stored payload (pure maths in
   ``app.core.options``), reading the perp candles it needs through ``as_of(available_at)``;
3. optionally writes an ``event_calendar`` ``expiry`` row for each expiry that has entered the 72h window.

Every derived row has the SAME ``available_at`` as its snapshot: a figure cannot be known before the book it
was computed from, and it is computed in the same instant.

What is read, and from where
----------------------------
* RV: 1h perp candles ending at or before the snapshot's ``available_at`` (``app.models.as_of``).
* ATR: Wilder ATR of the 4h perp candles visible at the same instant (the ATR every M3/M4 distance uses).
* Percentiles (``iv_minus_rv_pct``, ``skew_25d_pct``): the coin's own earlier ``options_metric`` rows, read through
  ``as_of(available_at)``, thinned to one per ``OPTIONS_PERCENTILE_SAMPLE_MINUTES``, against
  ``PRODUCTION_POLICY`` (90 days, at least 7 days of span and 30 samples). A new database therefore has NULL
  percentiles for the first week; with a NULL percentile there is no ``vol_rich`` / ``vol_cheap`` tag and nothing else changes.

``gamma_flip_distance_atr`` is ``(index - flip) / ATR``: positive when spot is above the flip.
``nearest_path_strike*`` stay NULL here (the path depends on a candidate's stop and direction; the options check
records it on the candidate).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.features import params as P
from app.core.features.params import PRODUCTION_POLICY
from app.core.features.series import Bar, Sample
from app.core.features.stats import trailing_percentile
from app.core.features.volatility import wilder_atr
from app.core.options import metrics as M
from app.core.options.book import OptionsBook, book_from_payload
from app.core.time import ensure_utc
from app.models import enums as E
from app.models.as_of import as_of
from app.models.feeds import EventCalendar
from app.models.options import OptionsMetric, OptionsSnapshot
from app.worker.feature_job import read_bars
from app.worker.feeds.deribit import DeribitClient, SnapshotRecord

log = logging.getLogger(__name__)

VENUE = E.OPTIONS_VENUE_DERIBIT
FOUR_HOURS = timedelta(hours=4)


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(repr(float(value)))


@dataclass
class IngestResult:
    coin: str
    snapshot_id: int | None
    inserted: bool
    snapshot_time: datetime
    available_at: datetime
    metrics_written: int = 0
    calendar_written: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# reading what the metrics need (all through as_of)
# ---------------------------------------------------------------------------
def read_perp_inputs(session: Session, coin: str, at: datetime) -> tuple[list[Bar], float | None]:
    """1h perp bars (for RV) and the 4h Wilder ATR, both as visible at ``at``."""
    hourly = read_bars(session, coin, E.MARKET_PERP, '1h', at, timedelta(hours=C.OPTIONS_RV_MAX_BARS + 2))
    four_hour = read_bars(session, coin, E.MARKET_PERP, '4h', at, FOUR_HOURS * (P.ATR_HISTORY_BARS + 2))
    atr = wilder_atr(four_hour, FOUR_HOURS, at)
    return hourly, (float(atr.value) if atr.ok else None)


def history_samples(session: Session, coin: str, horizon: str, column: str, *, at: datetime, snapshot_time: datetime) -> list[Sample]:
    """Earlier values of ``column`` visible at ``at``, within the policy window, thinned to one per sample bucket."""
    attribute = getattr(OptionsMetric, column)
    statement = (
        as_of(at)
        .select(OptionsMetric.snapshot_time, OptionsMetric.available_at, attribute)
        .where(
            OptionsMetric.venue == VENUE,
            OptionsMetric.coin == coin,
            OptionsMetric.horizon == horizon,
            attribute.is_not(None),
            OptionsMetric.snapshot_time >= snapshot_time - PRODUCTION_POLICY.window,
            OptionsMetric.snapshot_time < snapshot_time,
        )
        .order_by(OptionsMetric.snapshot_time)
    )
    bucket_seconds = C.OPTIONS_PERCENTILE_SAMPLE_MINUTES * 60
    samples: list[Sample] = []
    seen: set[int] = set()
    for taken, available, value in session.execute(statement).all():
        bucket = int(ensure_utc(taken).timestamp()) // bucket_seconds
        if bucket in seen:
            continue
        seen.add(bucket)
        samples.append(Sample(ensure_utc(taken), ensure_utc(available), float(value)))
    return samples


def _percentile(samples: Sequence[Sample], value: float | None, *, snapshot_time: datetime, at: datetime) -> float | None:
    if value is None:
        return None
    reading = trailing_percentile(samples, snapshot_time, value, at, PRODUCTION_POLICY)
    return float(reading.value) if reading.ok else None


# ---------------------------------------------------------------------------
# deriving and writing
# ---------------------------------------------------------------------------
def metric_row(
    session: Session,
    record: SnapshotRecord,
    book: OptionsBook,
    snapshot_id: int,
    horizon: str,
    hourly: Sequence[Bar],
    atr: float | None,
) -> dict[str, Any]:
    at = record.available_at
    hm = M.derive_horizon_metrics(book, horizon, hourly, bars_as_of=at)
    iv_samples = history_samples(session, record.coin, horizon, 'iv_minus_rv', at=at, snapshot_time=record.snapshot_time)
    skew_samples = history_samples(session, record.coin, horizon, 'skew_25d', at=at, snapshot_time=record.snapshot_time)
    flip_distance = None
    if hm.gamma_flip_price is not None and atr is not None and atr > 0.0:
        flip_distance = (book.index_price - hm.gamma_flip_price) / atr
    return {
        'snapshot_id': snapshot_id,
        'venue': VENUE,
        'coin': record.coin,
        'snapshot_time': record.snapshot_time,
        'horizon': horizon,
        'available_at': at,
        'dealer_gamma_sign': hm.dealer_gamma_sign,
        'dealer_gamma_estimated': True,
        'dealer_gamma_proxy': C.OPTIONS_GAMMA_PROXY,
        'gamma_flip_price': _dec(hm.gamma_flip_price),
        'gamma_flip_distance_atr': _dec(flip_distance),
        'skew_25d': _dec(hm.skew_25d),
        'skew_25d_pct': _dec(_percentile(skew_samples, hm.skew_25d, snapshot_time=record.snapshot_time, at=at)),
        'atm_iv': _dec(hm.atm_iv),
        'same_tenor_rv': _dec(hm.same_tenor_rv),
        'iv_minus_rv': _dec(hm.iv_minus_rv),
        'iv_minus_rv_pct': _dec(_percentile(iv_samples, hm.iv_minus_rv, snapshot_time=record.snapshot_time, at=at)),
        'max_pain_strike': _dec(hm.max_pain_strike),
        'top_strike_oi_share': _dec(hm.top_strike_oi_share),
        'expiry_time': hm.expiry_time,
        'hours_to_expiry': _dec(hm.hours_to_expiry),
        'expiry_open_interest': _dec(hm.expiry_open_interest),
        'near_dated_open_interest': _dec(hm.near_dated_open_interest),
        'total_open_interest': _dec(hm.total_open_interest),
        'atr_price_used': _dec(atr),
    }


def calendar_rows(record: SnapshotRecord, book: OptionsBook) -> list[dict[str, Any]]:
    """One ``expiry`` calendar row per expiry inside the near-dated window, classified at this snapshot."""
    rows = []
    horizon_end = book.snapshot_time + timedelta(hours=C.OPTIONS_NEAR_DATED_HOURS)
    total = book.total_open_interest
    for expiry in book.expiries():
        if not (book.snapshot_time < expiry <= horizon_end):
            continue
        size = M.expiry_open_interest(book, expiry)
        rows.append(
            {
                'kind': C.EVENT_KIND_EXPIRY,
                'coin': record.coin,
                'event_time': expiry,
                'title': f'Deribit {record.currency} options expiry',
                'feed_source': VENUE,
                'is_large': M.is_large_expiry(size, total),
                'notional_usd': _dec(size * book.index_price),
                'available_at': record.available_at,
            }
        )
    return rows


def ingest_snapshot(session: Session, record: SnapshotRecord, *, write_calendar: bool = False) -> IngestResult:
    """Store ``record`` and its derived rows. Idempotent; does not commit."""
    result = IngestResult(record.coin, None, False, record.snapshot_time, record.available_at)
    inserted_id = session.execute(
        pg_insert(OptionsSnapshot)
        .values(
            venue=VENUE,
            coin=record.coin,
            snapshot_time=record.snapshot_time,
            payload=record.payload,
            instrument_count=record.instrument_count,
            total_open_interest=_dec(record.total_open_interest),
            available_at=record.available_at,
        )
        .on_conflict_do_nothing(index_elements=['venue', 'coin', 'snapshot_time'])
        .returning(OptionsSnapshot.id)
    ).scalar_one_or_none()
    if inserted_id is None:
        result.notes.append('snapshot already stored')
        return result
    result.snapshot_id, result.inserted = int(inserted_id), True

    book = book_from_payload(record.payload, record.snapshot_time)
    hourly, atr = read_perp_inputs(session, record.coin, record.available_at)
    if atr is None:
        result.notes.append('no ATR: *_atr columns NULL')
    rows = [metric_row(session, record, book, result.snapshot_id, horizon, hourly, atr) for horizon in C.HORIZONS]
    written = session.execute(
        pg_insert(OptionsMetric)
        .values(rows)
        .on_conflict_do_nothing(index_elements=['venue', 'coin', 'snapshot_time', 'horizon'])
        .returning(OptionsMetric.id)
    ).all()
    result.metrics_written = len(written)

    if write_calendar:
        events = calendar_rows(record, book)
        if events:
            stored = session.execute(
                pg_insert(EventCalendar)
                .values(events)
                .on_conflict_do_nothing(index_elements=['kind', 'coin', 'event_time'])
                .returning(EventCalendar.id)
            ).all()
            result.calendar_written = len(stored)
    return result


def ingest_coin(session: Session, client: DeribitClient, coin: str, *, write_calendar: bool = False) -> IngestResult:
    """Fetch, parse and store one coin's book inside a SAVEPOINT, so one coin's failure cannot undo another's rows."""
    currency = C.OPTIONS_CURRENCY_BY_COIN[coin]
    record = client.book_summary(currency)
    with session.begin_nested():
        return ingest_snapshot(session, record, write_calendar=write_calendar)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    from app.config import get_settings
    from app.core.db_sync import get_sync_session_factory

    parser = argparse.ArgumentParser(prog='python -m app.worker.options_ingest', description='Fetch the Deribit option book once and store it.')
    parser.add_argument('--coins', nargs='*', help='Binance symbols (default: the watchlist coins Deribit lists)')
    parser.add_argument('--calendar', action='store_true', help='also write expiry rows to event_calendar (see config)')
    args = parser.parse_args(argv)
    settings = get_settings()
    coins = [c for c in (args.coins or settings.watchlist_symbols) if c in C.OPTIONS_CURRENCY_BY_COIN]
    client = DeribitClient(settings=settings)
    failures = 0
    try:
        with get_sync_session_factory()() as session:
            for coin in coins:
                try:
                    result = ingest_coin(session, client, coin, write_calendar=args.calendar or settings.deribit_write_expiry_calendar)
                    session.commit()
                    print(
                        f'{coin}: snapshot {result.snapshot_time.isoformat()} available {result.available_at.isoformat()} '
                        f'inserted={result.inserted} metrics={result.metrics_written} calendar={result.calendar_written} {"; ".join(result.notes)}'
                    )
                except Exception as exc:
                    session.rollback()
                    failures += 1
                    print(f'{coin}: FAILED {type(exc).__name__}: {exc}', file=sys.stderr)
    finally:
        client.close()
    return 1 if failures else 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
