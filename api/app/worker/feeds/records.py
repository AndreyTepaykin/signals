"""Typed feed records and the parsing that produces them.

Everything numeric is :class:`~decimal.Decimal` (never ``float``: the columns are
exact ``numeric``) and every instant is an aware UTC ``datetime``. Binance sends
epoch milliseconds; :func:`from_ms` converts with integer arithmetic so there is
no float rounding, and :func:`to_ms` converts back.

A row that fails validation raises :class:`MalformedRow`; callers count and skip
it rather than letting one bad print abort a whole batch (a CHECK violation in
the database would roll back every row in the statement).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.time import ensure_utc
from app.models import enums as E
from app.worker.feeds import availability

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)

#: Binance's open-interest-history ``period`` vocabulary. Wider than the kline
#: timeframes we store (``candle.timeframe`` is CHECKed against
#: ``enums.TIMEFRAMES``), which is why ``open_interest.timeframe`` has no CHECK.
OPEN_INTEREST_PERIODS = ('5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d')

_UNIT_SECONDS = {'m': 60, 'h': 3600, 'd': 86400}
_INTERVAL = re.compile(r'^(\d+)([mhd])$')


class MalformedRow(ValueError):
    """A feed row the exchange sent that we refuse to store."""


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------
def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime. Exact (integer arithmetic)."""
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise MalformedRow(f'epoch milliseconds must be an int, got {type(ms).__name__}: {ms!r}')
    if ms <= 0:
        raise MalformedRow(f'epoch milliseconds must be positive, got {ms}')
    return EPOCH + timedelta(milliseconds=ms)


def to_ms(instant: datetime) -> int:
    """Aware datetime -> epoch milliseconds. Naive input raises ``ValueError``."""
    return (ensure_utc(instant) - EPOCH) // _MILLISECOND


def interval_delta(interval: str, *, allowed: Sequence[str]) -> timedelta:
    """``'15m'`` -> ``timedelta(minutes=15)``, restricted to ``allowed``."""
    if interval not in allowed:
        raise ValueError(f'{interval!r} is not one of {tuple(allowed)}')
    match = _INTERVAL.match(interval)
    assert match is not None, interval
    return timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2)])


def timeframe_delta(timeframe: str) -> timedelta:
    """Length of a stored candle timeframe (``enums.TIMEFRAMES`` only)."""
    return interval_delta(timeframe, allowed=E.TIMEFRAMES)


def period_delta(period: str) -> timedelta:
    """Length of an open-interest history period."""
    return interval_delta(period, allowed=OPEN_INTEREST_PERIODS)


# ---------------------------------------------------------------------------
# field helpers
# ---------------------------------------------------------------------------
def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, float):
        raise MalformedRow(f'{name}: refusing a binary float ({value!r}); exchange values must arrive as strings')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MalformedRow(f'{name}: not a number: {value!r}') from exc
    if not number.is_finite():
        raise MalformedRow(f'{name}: not finite: {value!r}')
    return number


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedRow(f'{name}: expected a non-empty string, got {value!r}')
    return value


def _aware(value: datetime, name: str) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise MalformedRow(f'{name}: {exc}') from exc


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CandleRecord:
    coin: str
    market: str
    timeframe: str
    open_time: datetime
    close_time: datetime  # the closing boundary (exclusive end), == available_at
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    trade_count: int
    available_at: datetime

    def __post_init__(self) -> None:  # naive datetimes never survive construction
        for name in ('open_time', 'close_time', 'available_at'):
            object.__setattr__(self, name, _aware(getattr(self, name), name))

    @property
    def key(self) -> tuple[str, str, str, datetime]:
        return (self.coin, self.market, self.timeframe, self.open_time)


@dataclass(frozen=True)
class OpenInterestRecord:
    coin: str
    timeframe: str
    poll_time: datetime
    open_interest_base: Decimal
    open_interest_quote: Decimal | None
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ('poll_time', 'available_at'):
            object.__setattr__(self, name, _aware(getattr(self, name), name))

    @property
    def key(self) -> tuple[str, str, datetime]:
        return (self.coin, self.timeframe, self.poll_time)


@dataclass(frozen=True)
class FundingRecord:
    coin: str
    kind: str  # 'settled' | 'predicted'
    funding_time: datetime
    funding_rate: Decimal
    interval_hours: int | None
    mark_price: Decimal | None
    index_price: Decimal | None
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ('funding_time', 'available_at'):
            object.__setattr__(self, name, _aware(getattr(self, name), name))

    @property
    def key(self) -> tuple[str, datetime, str]:
        return (self.coin, self.funding_time, self.kind)


@dataclass(frozen=True)
class ForceOrderRecord:
    coin: str
    side: str  # 'buy' | 'sell'
    traded_at: datetime
    event_time: datetime | None
    price: Decimal
    average_price: Decimal | None
    original_quantity: Decimal
    filled_quantity: Decimal | None
    order_status: str | None
    dedup_key: str
    available_at: datetime

    def __post_init__(self) -> None:
        for name in ('traded_at', 'available_at'):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if self.event_time is not None:
            object.__setattr__(self, 'event_time', _aware(self.event_time, 'event_time'))


# ---------------------------------------------------------------------------
# klines
# ---------------------------------------------------------------------------
def parse_kline_row(raw: Sequence[Any], *, coin: str, market: str, timeframe: str) -> CandleRecord:
    """One Binance kline array -> :class:`CandleRecord`.

    Binance layout: ``[openTime, open, high, low, close, volume, closeTime,
    quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]``.

    This only parses and validates; whether the bar has CLOSED is the caller's
    decision (``BinanceClient.klines``), because it depends on the clock.
    """
    if market not in E.CANDLE_MARKETS:
        raise ValueError(f'unknown market {market!r}')
    length = timeframe_delta(timeframe)
    if not isinstance(raw, (list, tuple)) or len(raw) < 11:
        raise MalformedRow(f'kline row has {len(raw) if hasattr(raw, "__len__") else "?"} fields, expected 12')

    open_ms, close_ms = raw[0], raw[6]
    open_time = from_ms(open_ms)
    declared_close = from_ms(close_ms)
    length_ms = length // _MILLISECOND
    if open_ms % length_ms != 0:
        raise MalformedRow(f'kline open {open_time.isoformat()} is not aligned to a {timeframe} boundary')
    if close_ms != open_ms + length_ms - 1:
        raise MalformedRow(
            f'kline {open_time.isoformat()} declares closeTime {declared_close.isoformat()}, '
            f'expected open + {timeframe} - 1ms'
        )

    open_price = _decimal(raw[1], 'open')
    high_price = _decimal(raw[2], 'high')
    low_price = _decimal(raw[3], 'low')
    close_price = _decimal(raw[4], 'close')
    base_volume = _decimal(raw[5], 'volume')
    quote_volume = _decimal(raw[7], 'quote_volume')
    taker_buy_base = _decimal(raw[9], 'taker_buy_base')
    taker_buy_quote = _decimal(raw[10], 'taker_buy_quote')
    trades = raw[8]
    if isinstance(trades, bool) or not isinstance(trades, int) or trades < 0:
        raise MalformedRow(f'trade count must be a non-negative int, got {trades!r}')

    if min(open_price, high_price, low_price, close_price) <= 0:
        raise MalformedRow('non-positive price')
    if high_price < low_price:
        raise MalformedRow('high below low')
    if min(base_volume, quote_volume, taker_buy_base, taker_buy_quote) < 0:
        raise MalformedRow('negative volume')
    if taker_buy_base > base_volume:
        raise MalformedRow('taker buy volume exceeds total volume')

    return CandleRecord(
        coin=coin,
        market=market,
        timeframe=timeframe,
        open_time=open_time,
        close_time=open_time + length,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        close_price=close_price,
        base_volume=base_volume,
        quote_volume=quote_volume,
        taker_buy_base_volume=taker_buy_base,
        taker_buy_quote_volume=taker_buy_quote,
        trade_count=trades,
        available_at=availability.kline_available_at(open_time, length),
    )


# ---------------------------------------------------------------------------
# open interest
# ---------------------------------------------------------------------------
def parse_open_interest_row(
    raw: Mapping[str, Any], *, coin: str, period: str, publication_lag: timedelta
) -> OpenInterestRecord:
    """One ``openInterestHist`` object -> :class:`OpenInterestRecord`.

    ``available_at`` is the poll boundary plus ``publication_lag`` (see ``availability``).
    """
    length = period_delta(period)
    if not isinstance(raw, Mapping):
        raise MalformedRow(f'open interest row is not an object: {raw!r}')
    poll_ms = raw.get('timestamp')
    poll_time = from_ms(poll_ms)  # type: ignore[arg-type]
    if poll_ms % (length // _MILLISECOND) != 0:  # type: ignore[operator]
        raise MalformedRow(f'open interest timestamp {poll_time.isoformat()} is not aligned to a {period} boundary')
    base = _decimal(raw.get('sumOpenInterest'), 'sumOpenInterest')
    if base < 0:
        raise MalformedRow('negative open interest')
    quote_raw = raw.get('sumOpenInterestValue')
    quote = _decimal(quote_raw, 'sumOpenInterestValue') if quote_raw not in (None, '') else None
    return OpenInterestRecord(
        coin=coin,
        timeframe=period,
        poll_time=poll_time,
        open_interest_base=base,
        open_interest_quote=quote,
        available_at=availability.open_interest_available_at(poll_time, publication_lag),
    )


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------
def parse_funding_settled_row(raw: Mapping[str, Any], *, publication_lag: timedelta) -> FundingRecord:
    """One ``/fapi/v1/fundingRate`` object: a print that has settled.

    ``interval_hours`` is left NULL: the history endpoint does not say what
    interval a given print used, and a symbol's interval can change over time.
    Consumers derive it from adjacent ``funding_time`` values.
    """
    if not isinstance(raw, Mapping):
        raise MalformedRow(f'funding row is not an object: {raw!r}')
    funding_time = from_ms(raw.get('fundingTime'))  # type: ignore[arg-type]
    mark_raw = raw.get('markPrice')
    mark = _decimal(mark_raw, 'markPrice') if mark_raw not in (None, '') else None
    if mark is not None and mark <= 0:
        mark = None  # old prints carry a zero/blank mark; that is "unknown", not a price
    return FundingRecord(
        coin=_text(raw.get('symbol'), 'symbol'),
        kind=E.FUNDING_SETTLED,
        funding_time=funding_time,
        funding_rate=_decimal(raw.get('fundingRate'), 'fundingRate'),
        interval_hours=None,
        mark_price=mark,
        index_price=None,
        available_at=availability.funding_settled_available_at(funding_time, publication_lag),
    )


def parse_funding_predicted_row(raw: Mapping[str, Any], *, interval_hours: int | None) -> FundingRecord:
    """One ``/fapi/v1/premiumIndex`` object: the CURRENT estimated rate.

    ``lastFundingRate`` in that payload is the live estimate for the next
    settlement (it moves between settlements and differs from the last settled
    print; verified against the API on 2026-10-02). ``funding_time`` is the
    settlement it applies to (``nextFundingTime``, in the future) and
    ``available_at`` is the exchange's own observation stamp (``time``).
    """
    if not isinstance(raw, Mapping):
        raise MalformedRow(f'premium index row is not an object: {raw!r}')
    next_settlement = from_ms(raw.get('nextFundingTime'))  # type: ignore[arg-type]
    observed_at = from_ms(raw.get('time'))  # type: ignore[arg-type]
    mark_raw, index_raw = raw.get('markPrice'), raw.get('indexPrice')
    return FundingRecord(
        coin=_text(raw.get('symbol'), 'symbol'),
        kind=E.FUNDING_PREDICTED,
        funding_time=next_settlement,
        funding_rate=_decimal(raw.get('lastFundingRate'), 'lastFundingRate'),
        interval_hours=interval_hours,
        mark_price=_decimal(mark_raw, 'markPrice') if mark_raw not in (None, '') else None,
        index_price=_decimal(index_raw, 'indexPrice') if index_raw not in (None, '') else None,
        available_at=availability.funding_predicted_available_at(observed_at),
    )


# ---------------------------------------------------------------------------
# force orders
# ---------------------------------------------------------------------------
_FORCE_SIDES = {'BUY': 'buy', 'SELL': 'sell'}


def force_order_dedup_key(connection_id: str, sequence: int, order: Mapping[str, Any]) -> str:
    """Idempotency key for ONE received message.

    M1 left ``force_order`` without a natural key because two genuine
    liquidations can share symbol, side, price, size and millisecond, and a
    content-only key would silently drop one. So the key is the content hash
    PLUS the position of the message in its connection: re-inserting the SAME
    message (a retried batch) collides and is skipped, while two distinct
    prints that look identical carry different sequence numbers and both land.
    """
    content = '|'.join(str(order.get(k, '')) for k in ('s', 'S', 'T', 'p', 'ap', 'q', 'z', 'X'))
    digest = hashlib.sha256(f'{connection_id}:{sequence}:{content}'.encode()).hexdigest()
    return digest


def parse_force_order(
    message: Mapping[str, Any],
    *,
    connection_id: str,
    sequence: int,
    watchlist: frozenset[str] | None = None,
) -> ForceOrderRecord | None:
    """One ``!forceOrder@arr`` event -> record, or ``None`` if it is not ours.

    Returns ``None`` for non-``forceOrder`` events and for symbols outside the
    watchlist (the stream is market-wide). Raises :class:`MalformedRow` for an
    event that claims to be a liquidation but is unusable.
    """
    if not isinstance(message, Mapping) or message.get('e') != 'forceOrder':
        return None
    order = message.get('o')
    if not isinstance(order, Mapping):
        raise MalformedRow('forceOrder event without an "o" object')
    symbol = _text(order.get('s'), 's')
    if watchlist is not None and symbol not in watchlist:
        return None
    side = _FORCE_SIDES.get(str(order.get('S')).upper())
    if side is None:
        raise MalformedRow(f'unknown liquidation side {order.get("S")!r}')
    traded_at = from_ms(order.get('T'))  # type: ignore[arg-type]
    event_ms = message.get('E')
    event_time = from_ms(event_ms) if event_ms is not None else None
    price = _decimal(order.get('p'), 'p')
    quantity = _decimal(order.get('q'), 'q')
    if price <= 0 or quantity < 0:
        raise MalformedRow('non-positive price or negative quantity')
    average = order.get('ap')
    filled = order.get('z')
    status = order.get('X')
    return ForceOrderRecord(
        coin=symbol,
        side=side,
        traded_at=traded_at,
        event_time=event_time,
        price=price,
        average_price=_decimal(average, 'ap') if average not in (None, '') else None,
        original_quantity=quantity,
        filled_quantity=_decimal(filled, 'z') if filled not in (None, '') else None,
        order_status=str(status) if status else None,
        dedup_key=force_order_dedup_key(connection_id, sequence, order),
        available_at=availability.force_order_available_at(traded_at),
    )


@dataclass
class BatchStats:
    """Bookkeeping for one fetched batch, carried into the report."""

    fetched: int = 0
    dropped_unclosed: int = 0
    rejected: int = 0
    requests: int = 0
    notes: list[str] = field(default_factory=list)
