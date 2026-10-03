"""Builders for the M5 options tests. No network: a "Deribit response" here is a dict.

``deribit_row`` mimics one row of ``get_book_summary_by_currency`` (including keys the parser must ignore).
``response`` wraps rows the way the endpoint does. ``record`` parses them with the production parser.
``option_quote`` builds a pure-layer quote for the metric maths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.options.book import OptionQuote, OptionsBook, parse_instrument_name
from app.worker.feeds.deribit import SnapshotRecord, parse_book_summary
from app.worker.feeds.records import to_ms

UTC = timezone.utc
MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')

#: The signal bar of the DB tests (see tests/rules_helpers.py): a Monday, on the 15m grid.
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
LAG = timedelta(seconds=10)


def expiry(day: int, month: int, year: int = 2026) -> datetime:
    """A Deribit expiry instant: 08:00 UTC on the date."""
    return datetime(year, month, day, 8, tzinfo=UTC)


def instrument_name(currency: str, when: datetime, strike: float, side: str) -> str:
    return f'{currency}-{when.day}{MONTHS[when.month - 1]}{when.year % 100:02d}-{strike:g}-{"C" if side == "call" else "P"}'


def deribit_row(
    currency: str,
    when: datetime,
    strike: float,
    side: str,
    oi: float,
    *,
    iv_percent: float | None = 50.0,
    forward: float = 100.0,
    index: float = 100.0,
    created: datetime = T0,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        'instrument_name': instrument_name(currency, when, strike, side),
        'open_interest': oi,
        'mark_iv': iv_percent if iv_percent is not None else 0.0,
        'underlying_price': forward,
        'estimated_delivery_price': index,
        'creation_timestamp': to_ms(created),
        'mark_price': 0.01,
        'volume': 1.5,  # a key the parser must not keep
        'bid_price': 0.009,
        'ask_price': 0.011,
        'base_currency': currency,
        'quote_currency': currency,
        'interest_rate': 0.0,
    }
    row.update(extra)
    return row


def response(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'result': rows, 'usIn': 1, 'usOut': 2, 'usDiff': 1, 'testnet': False}


def record(
    rows: list[dict[str, Any]],
    *,
    currency: str = 'BTC',
    snapshot: datetime | None = None,
    fetched_at: datetime | None = None,
    lag: timedelta = LAG,
) -> SnapshotRecord:
    """Parse ``rows`` (their ``creation_timestamp`` is overwritten with ``snapshot`` when given)."""
    if snapshot is not None:
        rows = [{**row, 'creation_timestamp': to_ms(snapshot)} for row in rows]
    taken = snapshot or T0
    return parse_book_summary(response(rows), currency, fetched_at=fetched_at or taken + timedelta(seconds=1), publication_lag=lag)


def option_quote(
    when: datetime,
    strike: float,
    side: str,
    oi: float,
    *,
    iv: float | None = 0.5,
    forward: float = 100.0,
    currency: str = 'BTC',
) -> OptionQuote:
    name = instrument_name(currency, when, strike, side)
    parsed = parse_instrument_name(name)
    return OptionQuote(name, parsed.expiry_time, parsed.strike, parsed.side, oi, iv, forward)


def book(quotes: list[OptionQuote], snapshot_time: datetime, index_price: float = 100.0) -> OptionsBook:
    return OptionsBook('BTC', snapshot_time, index_price, tuple(quotes))
