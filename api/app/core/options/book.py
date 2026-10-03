"""The options book as plain data: instruments parsed from their names, one snapshot as a tuple of quotes.

Source
------
Deribit's public ``get_book_summary_by_currency`` (``kind=option``) returns one row per listed
option. The rows this code reads (original Deribit key names, nothing renamed):

``instrument_name``
    ``BTC-27NOV26-73000-P``: currency, expiry day, strike (quote currency, USD) and side.
    The expiry INSTANT is not in the row; every Deribit option settles at **08:00 UTC** on that
    day (confirmed against ``get_instruments.expiration_timestamp``, 2026-10-03: ``1791100800000``
    is ``2026-10-04T08:00:00Z`` for ``BTC-4OCT26-74000-C``), so it is derived from the name and no
    second request is needed.
``open_interest``
    In units of the underlying coin (1 contract = 1 BTC / 1 ETH), the same unit for calls and puts.
``mark_iv``
    Annualised implied vol in PERCENT (39.4 = 39.4 %); stored here as a fraction (0.394), the
    convention of ``app.core.features.volatility.yang_zhang_vol``.
``underlying_price``
    The forward (the future of that expiry) the option is priced off; differs by expiry.
``estimated_delivery_price``
    The spot index (``btc_usd`` / ``eth_usd``) at the snapshot: ``OptionsBook.index_price``.
``creation_timestamp``
    Milliseconds since the epoch when the summary was made: the snapshot instant.

Nothing here talks to a network or a database.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.time import ensure_utc

UTC = timezone.utc

#: Deribit options settle at 08:00 UTC (see the module docstring).
EXPIRY_HOUR_UTC = 8

_MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}  # fmt: skip
_NAME = re.compile(r'^(?P<currency>[A-Z]+)-(?P<day>\d{1,2})(?P<month>[A-Z]{3})(?P<year>\d{2})-(?P<strike>\d+(?:\.\d+)?)-(?P<side>[CP])$')

SIDE_CALL = 'call'
SIDE_PUT = 'put'


class InstrumentNameError(ValueError):
    """The name is not a Deribit linear-style option name."""


@dataclass(frozen=True)
class Instrument:
    currency: str
    expiry_time: datetime
    strike: float
    side: str


def parse_instrument_name(name: str) -> Instrument:
    """``'BTC-27NOV26-73000-P'`` -> BTC, 2026-11-27 08:00 UTC, 73000.0, put."""
    match = _NAME.match(name)
    if match is None:
        raise InstrumentNameError(f'{name!r} is not an option instrument name')
    month = _MONTHS.get(match['month'])
    if month is None:
        raise InstrumentNameError(f'{name!r}: unknown month {match["month"]!r}')
    try:
        expiry = datetime(2000 + int(match['year']), month, int(match['day']), EXPIRY_HOUR_UTC, tzinfo=UTC)
    except ValueError as exc:
        raise InstrumentNameError(f'{name!r}: {exc}') from exc
    strike = float(match['strike'])
    if strike <= 0.0:
        raise InstrumentNameError(f'{name!r}: strike must be positive')
    return Instrument(match['currency'], expiry, strike, SIDE_CALL if match['side'] == 'C' else SIDE_PUT)


@dataclass(frozen=True)
class OptionQuote:
    """One listed option at the snapshot."""

    name: str
    expiry_time: datetime
    strike: float
    side: str
    open_interest: float  # underlying-coin units
    iv: float | None  # annualised, fraction (0.394)
    forward: float  # underlying_price: the future of this expiry

    @property
    def has_iv(self) -> bool:
        return self.iv is not None and self.iv > 0.0


@dataclass(frozen=True)
class OptionsBook:
    """One snapshot. ``quotes`` are sorted (expiry, strike, side) so no input order reaches the arithmetic."""

    currency: str
    snapshot_time: datetime
    index_price: float
    quotes: tuple[OptionQuote, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'snapshot_time', ensure_utc(self.snapshot_time))
        ordered = tuple(sorted(self.quotes, key=lambda q: (q.expiry_time, q.strike, q.side)))
        object.__setattr__(self, 'quotes', ordered)
        if self.index_price <= 0.0:
            raise ValueError('index_price must be positive')

    @property
    def total_open_interest(self) -> float:
        return _sum(q.open_interest for q in self.quotes)

    def expiries(self) -> list[datetime]:
        return sorted({q.expiry_time for q in self.quotes})

    def for_expiry(self, expiry_time: datetime) -> tuple[OptionQuote, ...]:
        return tuple(q for q in self.quotes if q.expiry_time == expiry_time)


def _sum(values: Iterable[float]) -> float:
    return math.fsum(values)


def years_between(start: datetime, end: datetime) -> float:
    """Time to expiry in years on the 365-day year Deribit quotes vol in."""
    return (ensure_utc(end) - ensure_utc(start)) / timedelta(days=365)


# ---------------------------------------------------------------------------
# Payload <-> book. The stored payload keeps the original Deribit key names.
# ---------------------------------------------------------------------------
PAYLOAD_INSTRUMENT_KEYS = (
    'instrument_name',
    'open_interest',
    'mark_iv',
    'underlying_price',
    'mark_price',
    'creation_timestamp',
)


def book_from_payload(payload: Mapping[str, Any], snapshot_time: datetime) -> OptionsBook:
    """Rebuild an :class:`OptionsBook` from a stored ``options_snapshot.payload``.

    ``payload = {"currency": "BTC", "index_price": 84855.17, "instruments": [{<row>}, ...]}`` with
    rows carrying the Deribit keys in :data:`PAYLOAD_INSTRUMENT_KEYS`. A row that does not parse is
    skipped (the ingest rejects them before storing, so this only guards hand-edited data).
    """
    currency = str(payload['currency'])
    quotes: list[OptionQuote] = []
    for row in payload['instruments']:
        try:
            instrument = parse_instrument_name(row['instrument_name'])
            open_interest = float(row['open_interest'])
            forward = float(row['underlying_price'])
        except (KeyError, TypeError, ValueError):
            continue
        if open_interest < 0.0 or forward <= 0.0 or instrument.currency != currency:
            continue
        raw_iv = row.get('mark_iv')
        iv = float(raw_iv) / 100.0 if isinstance(raw_iv, (int, float)) and not isinstance(raw_iv, bool) and raw_iv > 0 else None
        quotes.append(OptionQuote(row['instrument_name'], instrument.expiry_time, instrument.strike, instrument.side, open_interest, iv, forward))
    return OptionsBook(currency, snapshot_time, float(payload['index_price']), tuple(quotes))


def quotes_expiring_within(quotes: Sequence[OptionQuote], snapshot_time: datetime, hours: float) -> tuple[OptionQuote, ...]:
    """Quotes that settle after the snapshot and within ``hours`` of it."""
    start, end = ensure_utc(snapshot_time), ensure_utc(snapshot_time) + timedelta(hours=hours)
    return tuple(q for q in quotes if start < q.expiry_time <= end)


__all__ = [
    'EXPIRY_HOUR_UTC',
    'PAYLOAD_INSTRUMENT_KEYS',
    'SIDE_CALL',
    'SIDE_PUT',
    'Instrument',
    'InstrumentNameError',
    'OptionQuote',
    'OptionsBook',
    'book_from_payload',
    'parse_instrument_name',
    'quotes_expiring_within',
    'years_between',
]
