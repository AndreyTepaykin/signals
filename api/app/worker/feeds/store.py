"""Idempotent writers for the raw feed tables.

Every writer uses ``INSERT ... ON CONFLICT DO NOTHING`` against the uniqueness
constraint M1 created, so a re-run, an overlapping backfill window or a retried
batch can neither double-insert nor change a row that is already stored. The
value returned is the number of rows actually written (``RETURNING id`` only
reports rows that were inserted), so a report can say how many were new.

There is exactly one deliberate exception, ``kind = 'predicted'`` funding: it is
a rolling estimate for the NEXT settlement, so the natural key
``(coin, funding_time, kind)`` is hit on every poll. A predicted row is updated
in place, but only by a strictly newer observation. A settled print is never
updated.

Writers do not commit. Callers commit per page so an interrupted backfill keeps
everything before the interruption.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.time import ensure_utc
from app.models import enums as E
from app.models.feeds import Candle, ForceOrder, Funding, OpenInterest
from app.worker.feeds.records import CandleRecord, ForceOrderRecord, FundingRecord, OpenInterestRecord

#: Rows per INSERT. 1000 rows x 16 columns stays far below psycopg's 65,535 parameter limit.
CHUNK = 1000


class UnclosedBarError(ValueError):
    """A candle whose bar had not closed was handed to the writer."""


@dataclass(frozen=True)
class WriteResult:
    submitted: int
    written: int

    @property
    def already_present(self) -> int:
        return self.submitted - self.written


def _chunks(rows: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for start in range(0, len(rows), CHUNK):
        yield rows[start : start + CHUNK]


def write_candles(session: Session, records: Sequence[CandleRecord], *, now: datetime) -> WriteResult:
    """Insert closed bars; skip any that already exist.

    ``now`` is the exchange-clock instant. A record whose ``available_at`` (the
    bar close) lies after it is a bar still being formed, and writing it would
    put a partial candle into the feature path. The client already drops those;
    this is the second lock on the same door, and it raises rather than skips,
    because reaching it means a caller bypassed the client.
    """
    now = ensure_utc(now)
    rows = []
    for record in records:
        if record.available_at > now or record.close_time > now:
            raise UnclosedBarError(
                f'refusing to write {record.coin} {record.market} {record.timeframe} bar opening '
                f'{record.open_time.isoformat()}: it closes {record.close_time.isoformat()}, after now={now.isoformat()}'
            )
        rows.append(
            {
                'coin': record.coin,
                'market': record.market,
                'timeframe': record.timeframe,
                'open_time': record.open_time,
                'close_time': record.close_time,
                'open_price': record.open_price,
                'high_price': record.high_price,
                'low_price': record.low_price,
                'close_price': record.close_price,
                'base_volume': record.base_volume,
                'quote_volume': record.quote_volume,
                'taker_buy_base_volume': record.taker_buy_base_volume,
                'taker_buy_quote_volume': record.taker_buy_quote_volume,
                'trade_count': record.trade_count,
                'available_at': record.available_at,
            }
        )
    written = 0
    for chunk in _chunks(rows):
        statement = (
            pg_insert(Candle)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=['coin', 'market', 'timeframe', 'open_time'])
            .returning(Candle.id)
        )
        written += len(session.execute(statement).all())
    return WriteResult(submitted=len(rows), written=written)


def write_open_interest(session: Session, records: Sequence[OpenInterestRecord], *, now: datetime) -> WriteResult:
    """Insert OI samples; skip any that exist.

    A sample whose ``available_at`` (poll boundary + publication lag) is still in the future is refused,
    the same lock as :func:`write_candles`: a row is stored only once it is knowable, so live and
    replay see the same history. The client already drops those; reaching this means it was bypassed.
    """
    now = ensure_utc(now)
    for r in records:
        if r.available_at > now:
            raise UnclosedBarError(
                f'refusing to write {r.coin} {r.timeframe} open interest for {r.poll_time.isoformat()}: '
                f'it is available at {r.available_at.isoformat()}, after now={now.isoformat()}'
            )
    rows = [
        {
            'coin': r.coin,
            'timeframe': r.timeframe,
            'poll_time': r.poll_time,
            'open_interest_base': r.open_interest_base,
            'open_interest_quote': r.open_interest_quote,
            'available_at': r.available_at,
        }
        for r in records
    ]
    written = 0
    for chunk in _chunks(rows):
        statement = (
            pg_insert(OpenInterest)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=['coin', 'timeframe', 'poll_time'])
            .returning(OpenInterest.id)
        )
        written += len(session.execute(statement).all())
    return WriteResult(submitted=len(rows), written=written)


def write_funding(session: Session, records: Sequence[FundingRecord], *, now: datetime) -> WriteResult:
    """Settled prints: insert-or-ignore. Predicted rates: insert, or refresh if newer.

    A SETTLED print whose ``available_at`` (settlement + publication lag) is still in the future is refused,
    like an unclosed bar. Predicted rows legitimately describe a future settlement and are not checked.
    """
    now = ensure_utc(now)
    for r in records:
        if r.kind == E.FUNDING_SETTLED and r.available_at > now:
            raise UnclosedBarError(
                f'refusing to write settled funding {r.coin} {r.funding_time.isoformat()}: '
                f'it is available at {r.available_at.isoformat()}, after now={now.isoformat()}'
            )
    settled = [r for r in records if r.kind == E.FUNDING_SETTLED]
    predicted = [r for r in records if r.kind == E.FUNDING_PREDICTED]
    unknown = [r for r in records if r.kind not in (E.FUNDING_SETTLED, E.FUNDING_PREDICTED)]
    if unknown:
        raise ValueError(f'unknown funding kind {unknown[0].kind!r}')

    written = 0
    for chunk in _chunks([_funding_row(r) for r in settled]):
        statement = (
            pg_insert(Funding)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=['coin', 'funding_time', 'kind'])
            .returning(Funding.id)
        )
        written += len(session.execute(statement).all())

    # Keep only the newest observation per key: DO UPDATE cannot touch one row twice in a statement.
    newest: dict[tuple[str, datetime], FundingRecord] = {}
    for r in predicted:
        current = newest.get((r.coin, r.funding_time))
        if current is None or r.available_at > current.available_at:
            newest[(r.coin, r.funding_time)] = r
    for chunk in _chunks([_funding_row(r) for r in newest.values()]):
        insert = pg_insert(Funding).values(list(chunk))
        statement = insert.on_conflict_do_update(
            index_elements=['coin', 'funding_time', 'kind'],
            set_={
                'funding_rate': insert.excluded.funding_rate,
                'interval_hours': insert.excluded.interval_hours,
                'mark_price': insert.excluded.mark_price,
                'index_price': insert.excluded.index_price,
                'available_at': insert.excluded.available_at,
            },
            where=Funding.available_at < insert.excluded.available_at,
        ).returning(Funding.id)
        written += len(session.execute(statement).all())
    return WriteResult(submitted=len(settled) + len(newest), written=written)


def _funding_row(r: FundingRecord) -> dict[str, Any]:
    return {
        'coin': r.coin,
        'kind': r.kind,
        'funding_time': r.funding_time,
        'funding_rate': r.funding_rate,
        'interval_hours': r.interval_hours,
        'mark_price': r.mark_price,
        'index_price': r.index_price,
        'available_at': r.available_at,
    }


def write_force_orders(session: Session, records: Sequence[ForceOrderRecord]) -> WriteResult:
    rows = [
        {
            'coin': r.coin,
            'side': r.side,
            'traded_at': r.traded_at,
            'event_time': r.event_time,
            'price': r.price,
            'average_price': r.average_price,
            'original_quantity': r.original_quantity,
            'filled_quantity': r.filled_quantity,
            'order_status': r.order_status,
            'dedup_key': r.dedup_key,
            'available_at': r.available_at,
        }
        for r in records
    ]
    written = 0
    for chunk in _chunks(rows):
        statement = (
            pg_insert(ForceOrder)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=['dedup_key'])
            .returning(ForceOrder.id)
        )
        written += len(session.execute(statement).all())
    return WriteResult(submitted=len(rows), written=written)
