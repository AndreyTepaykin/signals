"""Read-only load of perp candles and settled funding for the Elder research.

The database ``close_time`` is already the exclusive end of the bar
(``open_time + step``). :func:`rows_to_ohlcv` recomputes that end and raises if
the stored value differs, so a Binance close (one millisecond earlier) cannot
slip through.

Lookahead guard: a bar is loaded only when ``close_time <= period_end``. Nothing
that closes after the period end is held in memory. Weekly history starts at
2024-10-01 (4h). 1h, 15m and settled funding start at 2025-10-01.

Writes are not this module's job. :func:`readonly_connection` opens a
transaction whose first statement is ``SET TRANSACTION READ ONLY``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import bindparam, text

from app.research.elder.resample import STEP_15M, STEP_1H, STEP_4H, OHLCV
from app.research.elder.simulate import FundingPrint

UTC = timezone.utc

COINS: tuple[str, ...] = (
    'BTCUSDT',
    'ETHUSDT',
    'SOLUSDT',
    'BNBUSDT',
    'XRPUSDT',
    'DOGEUSDT',
    'ADAUSDT',
    'AVAXUSDT',
    'LINKUSDT',
    'TRXUSDT',
)

#: Perp 4h history. The first full Monday week is 2024-10-07.
HISTORY_4H_START = datetime(2024, 10, 1, tzinfo=UTC)
#: 1h, 15m and settled funding.
HISTORY_START = datetime(2025, 10, 1, tzinfo=UTC)

TRAIN_START = datetime(2026, 1, 1, tzinfo=UTC)
#: Exclusive end of train and the open of holdout. Never load past this on the train path.
TRAIN_END = datetime(2026, 7, 1, tzinfo=UTC)
HOLDOUT_START = TRAIN_END

#: Train sub-windows. Trades are assigned by fill_time; the tuning objective uses the whole train.
SUBWINDOWS: tuple[tuple[str, datetime, datetime], ...] = (
    ('jan_apr', datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)),
    ('may_jun', datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
)

_CANDLE_SQL = text(
    """
    SELECT open_time, close_time, open_price, high_price, low_price, close_price, base_volume
    FROM candle
    WHERE coin = :coin
      AND market = 'perp'
      AND timeframe = :timeframe
      AND open_time >= :start
      AND close_time <= :period_end
    ORDER BY open_time
    """
)

_FUNDING_SQL = text(
    """
    SELECT funding_time, funding_rate, mark_price
    FROM funding
    WHERE coin = :coin
      AND kind = 'settled'
      AND funding_time >= :start
      AND funding_time <= :period_end
    ORDER BY funding_time
    """
)

_LATEST_SQL = text(
    """
    SELECT coin, max(close_time) AS latest
    FROM candle
    WHERE market = 'perp'
      AND timeframe = '15m'
      AND coin IN :coins
    GROUP BY coin
    """
).bindparams(bindparam('coins', expanding=True))


@dataclass(frozen=True)
class CoinData:
    """One coin, already truncated at the period end the loader was given."""

    coin: str
    bars_15m: list[OHLCV]
    bars_1h: list[OHLCV]
    bars_4h: list[OHLCV]
    funding: list[FundingPrint]


def _utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('timestamp must be timezone-aware')
    return instant.astimezone(UTC)


def _field(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    # A missing price is an error. mark_price is allowed to be absent; callers
    # that permit None check the key themselves.
    if len(names) == 1 or any(name in row for name in names):
        present = [name for name in names if name in row]
        if present and row[present[0]] is None:
            raise ValueError(f'{present[0]} is NULL')
    raise KeyError(f'row is missing {" / ".join(names)}')


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'not a number: {value!r}') from exc
    if not math.isfinite(number):
        raise ValueError(f'non-finite number: {value!r}')
    return number


def rows_to_ohlcv(rows: Sequence[Mapping[str, Any]], step: timedelta) -> list[OHLCV]:
    """Convert candle rows to :class:`OHLCV`.

    ``close_time`` is recomputed as ``open_time + step`` and must equal the
    stored ``close_time``. Numeric columns become floats. ``base_volume`` (or
    ``volume``) becomes ``volume``. Rows must be strictly sorted by open time.
    """
    if step <= timedelta(0):
        raise ValueError('step must be positive')
    bars: list[OHLCV] = []
    previous: datetime | None = None
    for row in rows:
        opened = _utc(_field(row, 'open_time'))
        stored_close = _utc(_field(row, 'close_time'))
        expected = opened + step
        if stored_close != expected:
            raise ValueError(
                f'close_time {stored_close.isoformat()} != open_time + {step} '
                f'({expected.isoformat()}); stored close_time must be the exclusive end'
            )
        if previous is not None and opened <= previous:
            raise ValueError(f'rows are not strictly sorted by open_time at {opened.isoformat()}')
        previous = opened
        bars.append(
            OHLCV(
                open_time=opened,
                close_time=expected,
                open=_num(_field(row, 'open_price', 'open')),
                high=_num(_field(row, 'high_price', 'high')),
                low=_num(_field(row, 'low_price', 'low')),
                close=_num(_field(row, 'close_price', 'close')),
                volume=_num(_field(row, 'base_volume', 'volume')),
            )
        )
    return bars


def _rows_to_funding(rows: Sequence[Mapping[str, Any]]) -> list[FundingPrint]:
    prints: list[FundingPrint] = []
    previous: datetime | None = None
    for row in rows:
        when = _utc(_field(row, 'funding_time'))
        if previous is not None and when <= previous:
            raise ValueError(f'funding rows are not strictly sorted at {when.isoformat()}')
        previous = when
        mark = row['mark_price'] if 'mark_price' in row else None
        prints.append(
            FundingPrint(
                funding_time=when,
                rate=_num(row['funding_rate']),
                mark_price=None if mark is None else _num(mark),
            )
        )
    return prints


def _candles(conn: Any, coin: str, timeframe: str, start: datetime, period_end: datetime, step: timedelta) -> list[OHLCV]:
    rows = conn.execute(
        _CANDLE_SQL,
        {'coin': coin, 'timeframe': timeframe, 'start': start, 'period_end': period_end},
    ).mappings().all()
    bars = rows_to_ohlcv(rows, step)
    if any(bar.close_time > period_end for bar in bars):
        raise RuntimeError(f'{coin} {timeframe} kept a bar that closes after {period_end.isoformat()}')
    return bars


def load_coin(conn: Any, coin: str, period_end: datetime) -> CoinData:
    """Load one coin. ``conn`` is a SQLAlchemy connection or session.

    Only ``SELECT``. 4h from 2024-10-01; 1h, 15m and settled funding from
    2025-10-01. Every bar satisfies ``close_time <= period_end``.
    """
    end = _utc(period_end)
    return CoinData(
        coin=coin,
        bars_15m=_candles(conn, coin, '15m', HISTORY_START, end, STEP_15M),
        bars_1h=_candles(conn, coin, '1h', HISTORY_START, end, STEP_1H),
        bars_4h=_candles(conn, coin, '4h', HISTORY_4H_START, end, STEP_4H),
        funding=_rows_to_funding(
            conn.execute(
                _FUNDING_SQL,
                {'coin': coin, 'start': HISTORY_START, 'period_end': end},
            ).mappings().all()
        ),
    )


def latest_common_end(conn: Any, coins: Sequence[str]) -> datetime:
    """Minimum, over ``coins``, of the latest stored perp 15m ``close_time``.

    Holdout uses this so every coin and every timeframe share one end. A coin
    with no perp 15m bars raises.
    """
    if not coins:
        raise ValueError('no coins')
    rows = conn.execute(_LATEST_SQL, {'coins': list(coins)}).mappings().all()
    found = {row['coin']: _utc(row['latest']) for row in rows}
    missing = [coin for coin in coins if coin not in found]
    if missing:
        raise ValueError(f'no perp 15m bars for {", ".join(missing)}')
    return min(found[coin] for coin in coins)


@contextmanager
def readonly_connection(engine: Any) -> Iterator[Any]:
    """Connection whose first transaction statement is ``SET TRANSACTION READ ONLY``.

    Rolled back on the way out. There is nothing to commit.
    """
    conn = engine.connect()
    try:
        trans = conn.begin()
        try:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            yield conn
        finally:
            trans.rollback()
    finally:
        conn.close()
