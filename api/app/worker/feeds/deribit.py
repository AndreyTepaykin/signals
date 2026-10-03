"""Deribit public book: the client, the parser and the snapshot record.

ONE endpoint, a GET, no key, no signature, no private method:

``/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option``
    One row per listed option with open interest, mark IV, the forward and a creation
    timestamp. About 1,000 rows / 450 KB for BTC, 840 rows / 370 KB for ETH. It is a
    point-in-time view: Deribit offers no history of it, so a snapshot that was not
    fetched when it was current is gone. That is why ``available_at`` is floored at the
    moment we received it (see :func:`app.worker.feeds.availability.options_snapshot_available_at`).

Why a snapshot is one stored row per (coin, snapshot_time)
----------------------------------------------------------
``options_snapshot.payload`` keeps the rows' ORIGINAL Deribit keys (``instrument_name``,
``open_interest``, ``mark_iv``, ``underlying_price``, ``mark_price``, ``creation_timestamp``)
for every instrument with open interest above zero, plus ``currency`` and ``index_price``
(``estimated_delivery_price``). The other ~14 fields per row (bid/ask, volume, high/low...) and the
zero-open-interest instruments are not kept: no metric reads them, and the full response would be
about 150 KB a snapshot per coin (about 90 MB a day for two coins at a 5 minute poll before
compression). Everything the options check and its metrics need can be rebuilt from the payload
(:func:`app.core.options.book.book_from_payload`).

``snapshot_time`` is the MEDIAN ``creation_timestamp`` (lower median) of the accepted rows. Every row of
one response carries a timestamp within ~15 ms of the others, so the median is the snapshot instant;
a single stale or future row cannot move it.

Rows that fail validation are counted in ``rejected`` and dropped, never stored half-parsed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings, get_settings
from app.core import constants as C
from app.core.options.book import PAYLOAD_INSTRUMENT_KEYS, InstrumentNameError, parse_instrument_name
from app.core.time import ensure_utc, utc_now
from app.worker.feeds import availability
from app.worker.feeds import records as R
from app.worker.feeds.http import BinanceHttp, RetryPolicy

log = logging.getLogger(__name__)

#: Deribit's documented budget for unauthenticated public calls is generous (credit based);
#: this is only the local soft limit that keeps a runaway loop from hammering it.
REQUESTS_PER_MINUTE = 120
BOOK_SUMMARY_PATH = '/api/v2/public/get_book_summary_by_currency'


class DeribitError(ValueError):
    """The response is not a usable book (shape wrong, or no valid row)."""


@dataclass(frozen=True)
class SnapshotRecord:
    """One parsed book, ready to store."""

    coin: str  # Binance symbol (BTCUSDT): the repo-wide `coin`
    currency: str  # Deribit currency (BTC)
    snapshot_time: datetime
    fetched_at: datetime
    available_at: datetime
    index_price: float
    payload: dict[str, Any]
    instrument_count: int  # accepted rows (before the zero-open-interest ones are dropped from the payload)
    total_open_interest: float
    rejected: int = 0
    notes: list[str] = field(default_factory=list)


def coin_for_currency(currency: str) -> str:
    for coin, name in C.OPTIONS_CURRENCY_BY_COIN.items():
        if name == currency:
            return coin
    raise KeyError(currency)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float('inf'), float('-inf')) else None


def _lower_median(values: list[float | int]) -> float | int:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def parse_book_summary(
    response: Mapping[str, Any],
    currency: str,
    *,
    fetched_at: datetime,
    publication_lag: timedelta,
) -> SnapshotRecord:
    """Validate one ``get_book_summary_by_currency`` response and build the record to store.

    Raises :class:`DeribitError` when the body is not a result list or no row survives validation.
    """
    result = response.get('result') if isinstance(response, Mapping) else None
    if not isinstance(result, list) or not result:
        raise DeribitError('book summary: "result" is missing or empty')
    accepted: list[dict[str, Any]] = []
    stamps: list[int] = []
    indexes: list[float] = []
    rejected = 0
    for row in result:
        try:
            if not isinstance(row, Mapping):
                raise ValueError('not an object')
            instrument = parse_instrument_name(str(row['instrument_name']))
            if instrument.currency != currency:
                raise ValueError('other currency')
            open_interest = _number(row.get('open_interest'))
            forward = _number(row.get('underlying_price'))
            index = _number(row.get('estimated_delivery_price'))
            created = row.get('creation_timestamp')
            if open_interest is None or open_interest < 0.0 or forward is None or forward <= 0.0 or index is None or index <= 0.0:
                raise ValueError('numeric field missing or out of range')
            if isinstance(created, bool) or not isinstance(created, int) or created <= 0:
                raise ValueError('creation_timestamp')
        except (ValueError, KeyError, InstrumentNameError):
            rejected += 1
            continue
        accepted.append({key: row.get(key) for key in PAYLOAD_INSTRUMENT_KEYS})
        stamps.append(int(created))
        indexes.append(float(index))
    if not accepted:
        raise DeribitError(f'book summary: none of the {len(result)} rows is a valid {currency} option')

    snapshot_time = R.from_ms(int(_lower_median(stamps)))
    index_price = float(_lower_median(indexes))
    total = sum(float(row['open_interest']) for row in accepted)
    kept = [row for row in accepted if float(row['open_interest']) > 0.0]
    kept.sort(key=lambda row: row['instrument_name'])
    fetched_at = ensure_utc(fetched_at)
    notes = []
    if rejected:
        notes.append(f'{rejected} of {len(result)} rows rejected')
    return SnapshotRecord(
        coin=coin_for_currency(currency),
        currency=currency,
        snapshot_time=snapshot_time,
        fetched_at=fetched_at,
        available_at=availability.options_snapshot_available_at(snapshot_time, fetched_at, publication_lag),
        index_price=index_price,
        payload={'currency': currency, 'index_price': index_price, 'instruments': kept},
        instrument_count=len(accepted),
        total_open_interest=total,
        rejected=rejected,
        notes=notes,
    )


class DeribitClient:
    """Public market-data client. No key, no signature, no private method."""

    def __init__(
        self,
        *,
        http: BinanceHttp | None = None,
        settings: Settings | None = None,
        clock: Callable[[], datetime] = utc_now,
        retry: RetryPolicy | None = None,
    ) -> None:
        settings = settings or get_settings()
        # BinanceHttp is the repo's one public-GET transport (retry, backoff, 429 handling); the name is historical.
        self.http = http or BinanceHttp(settings.deribit_url, weight_limit_1m=REQUESTS_PER_MINUTE, retry=retry)
        self.clock = clock
        self.publication_lag = timedelta(seconds=settings.feed_deribit_publication_lag_seconds)

    def close(self) -> None:
        self.http.close()

    def now(self) -> datetime:
        return self.clock()

    def book_summary(self, currency: str) -> SnapshotRecord:
        """Fetch and parse the option book of ``currency`` (``BTC`` / ``ETH``)."""
        if currency not in C.OPTIONS_CURRENCY_BY_COIN.values():
            raise KeyError(f'{currency!r} has no Deribit book in scope')
        body = self.http.get(BOOK_SUMMARY_PATH, {'currency': currency, 'kind': 'option'})
        return parse_book_summary(body, currency, fetched_at=self.clock(), publication_lag=self.publication_lag)

