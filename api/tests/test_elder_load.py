"""Loader: row conversion, and (when Postgres is up) the read-only period cut."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.models import enums as E
from app.models.feeds import Candle, Funding
from app.research.elder.load import (
    HISTORY_4H_START,
    HISTORY_START,
    latest_common_end,
    load_coin,
    readonly_connection,
    rows_to_ohlcv,
)
from app.research.elder.resample import STEP_1H, STEP_4H, STEP_15M

UTC = timezone.utc
STEP = STEP_15M


def _row(opened, step=STEP, **overrides):
    values = {
        'open_time': opened,
        'close_time': opened + step,
        'open_price': Decimal('100.5'),
        'high_price': Decimal('101'),
        'low_price': Decimal('99.25'),
        'close_price': Decimal('100.25'),
        'base_volume': Decimal('12.5'),
    }
    values.update(overrides)
    return values


def test_rows_to_ohlcv_converts_decimal_and_checks_exclusive_close():
    opened = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    nxt = opened + STEP
    bars = rows_to_ohlcv([_row(opened), _row(nxt, close_price=Decimal('101'))], STEP)
    assert [(bar.open_time, bar.close_time) for bar in bars] == [
        (opened, opened + STEP),
        (nxt, nxt + STEP),
    ]
    assert bars[0].open == 100.5
    assert bars[0].high == 101.0
    assert bars[0].low == 99.25
    assert bars[0].close == 100.25
    assert bars[0].volume == 12.5
    assert bars[1].close == 101.0


def test_rows_to_ohlcv_rejects_binance_close_one_millisecond_early():
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    row = _row(opened, close_time=opened + STEP - timedelta(milliseconds=1))
    with pytest.raises(ValueError, match='exclusive end'):
        rows_to_ohlcv([row], STEP)


def test_rows_to_ohlcv_rejects_naive_and_unsorted_rows():
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match='timezone'):
        rows_to_ohlcv([_row(opened.replace(tzinfo=None))], STEP)
    with pytest.raises(ValueError, match='sorted'):
        rows_to_ohlcv([_row(opened + STEP), _row(opened)], STEP)


def _candle(coin, timeframe, opened, step, close_price, *, market='perp'):
    return Candle(
        coin=coin,
        market=market,
        timeframe=timeframe,
        open_time=opened,
        close_time=opened + step,
        available_at=opened + step,
        open_price=Decimal('100'),
        high_price=Decimal('110'),
        low_price=Decimal('90'),
        close_price=Decimal(str(close_price)),
        base_volume=Decimal('10'),
        taker_buy_base_volume=Decimal('4'),
    )


def test_loader_keeps_only_bars_that_close_by_period_end(session):
    end = datetime(2026, 3, 1, tzinfo=UTC)
    session.add_all([
        # 15m: on the end, after the end, and before the 15m history starts.
        _candle('BTCUSDT', '15m', end - STEP_15M, STEP_15M, 11),
        _candle('BTCUSDT', '15m', end, STEP_15M, 99),
        _candle('BTCUSDT', '15m', HISTORY_START - STEP_15M, STEP_15M, 7),
        # 1h rows exist in the table but the loader no longer reads them.
        _candle('BTCUSDT', '1h', end - STEP_1H, STEP_1H, 21),
        _candle('BTCUSDT', '1h', end, STEP_1H, 98),
        # 4h: history start kept, the bar before it dropped, a bar after the end dropped.
        _candle('BTCUSDT', '4h', HISTORY_4H_START, STEP_4H, 31),
        _candle('BTCUSDT', '4h', HISTORY_4H_START - STEP_4H, STEP_4H, 32),
        _candle('BTCUSDT', '4h', end, STEP_4H, 97),
        Funding(
            coin='BTCUSDT', kind=E.FUNDING_SETTLED, funding_time=datetime(2026, 2, 1, tzinfo=UTC),
            available_at=datetime(2026, 2, 1, 0, 1, tzinfo=UTC), funding_rate=Decimal('0.0001'),
            mark_price=Decimal('100'),
        ),
        Funding(
            coin='BTCUSDT', kind=E.FUNDING_SETTLED, funding_time=end,
            available_at=end, funding_rate=Decimal('0.0002'), mark_price=None,
        ),
        Funding(
            coin='BTCUSDT', kind=E.FUNDING_SETTLED, funding_time=end + timedelta(hours=8),
            available_at=end + timedelta(hours=8), funding_rate=Decimal('0.0003'),
        ),
        Funding(
            coin='BTCUSDT', kind=E.FUNDING_PREDICTED, funding_time=datetime(2026, 2, 1, tzinfo=UTC),
            available_at=datetime(2026, 1, 31, tzinfo=UTC), funding_rate=Decimal('0.0009'),
        ),
        Funding(
            coin='BTCUSDT', kind=E.FUNDING_SETTLED, funding_time=HISTORY_START - timedelta(days=1),
            available_at=HISTORY_START - timedelta(days=1), funding_rate=Decimal('0.0008'),
        ),
    ])
    session.flush()

    loaded = load_coin(session, 'BTCUSDT', end)
    assert [bar.close for bar in loaded.bars_15m] == [11.0]
    assert loaded.bars_15m[0].close_time == end
    assert not hasattr(loaded, 'bars_1h')
    assert [bar.close for bar in loaded.bars_4h] == [31.0]
    assert loaded.bars_4h[0].open_time == HISTORY_4H_START
    assert [(item.funding_time, item.rate, item.mark_price) for item in loaded.funding] == [
        (datetime(2026, 2, 1, tzinfo=UTC), float(Decimal('0.0001')), 100.0),
        (end, float(Decimal('0.0002')), None),
    ]
    assert all(bar.close_time <= end for bar in loaded.bars_15m + loaded.bars_4h)


def test_latest_common_end_is_the_minimum_across_coins(session):
    early = datetime(2026, 3, 15, tzinfo=UTC)
    late = datetime(2026, 4, 1, tzinfo=UTC)
    session.add_all([
        _candle('ETHUSDT', '15m', early - STEP_15M, STEP_15M, 1),
        _candle('BTCUSDT', '15m', early - STEP_15M, STEP_15M, 1),
        _candle('BTCUSDT', '15m', late - STEP_15M, STEP_15M, 2),
    ])
    session.flush()
    assert latest_common_end(session, ['BTCUSDT', 'ETHUSDT']) == early
    with pytest.raises(ValueError, match='SOLUSDT'):
        latest_common_end(session, ['BTCUSDT', 'SOLUSDT'])


def test_readonly_connection_rejects_ddl(db_engine):
    with readonly_connection(db_engine) as conn:
        with pytest.raises(SQLAlchemyError, match='[Rr]ead-?[Oo]nly'):
            conn.execute(text('CREATE TABLE elder_e5_should_not_exist (id integer)'))
    with db_engine.connect() as conn:
        found = conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = 'elder_e5_should_not_exist'")
        ).first()
    assert found is None
