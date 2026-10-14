"""Vision zip selection and the CSV rows the importer hands to the live parsers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.worker import vision_import as V
from app.worker.feeds import availability

UTC = timezone.utc
LAG = timedelta(seconds=300)

KLINE = (
    'open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n'
    '1767225600000,87608.30,87791.90,87600.00,87705.00,752.615,1767226499999,65994654.30960,15400,447.032,39204593.95500,0\n'
)

METRICS = (
    'create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,'
    'sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n'
    '2026-01-01 00:00:00,BTCUSDT,94262.030,8264112415.551,1,1,1,1\n'
    '2026-01-01 00:05:00,BTCUSDT,94300.000,8265000000.000,1,1,1,1\n'
    '2026-01-01 01:00:00,BTCUSDT,94402.851,8286168359.706,1,1,1,1\n'
)


def test_a_monthly_zip_covers_its_month_and_the_daily_files_of_that_month_are_dropped():
    monthly = [Path('BTCUSDT-15m-2026-01.zip'), Path('BTCUSDT-15m-2026-09.zip')]
    daily = [
        Path('BTCUSDT-15m-2026-01-15.zip'),
        Path('BTCUSDT-15m-2026-10-01.zip'),
        Path('BTCUSDT-15m-2026-10-02.zip'),
    ]
    selected = V.select_kline_zips(monthly, daily)
    assert [path.name for path in selected] == [
        'BTCUSDT-15m-2026-01.zip',
        'BTCUSDT-15m-2026-09.zip',
        'BTCUSDT-15m-2026-10-01.zip',
        'BTCUSDT-15m-2026-10-02.zip',
    ]


def test_kline_csv_uses_the_live_parser_so_the_bar_is_knowable_at_its_close():
    record = V.parse_vision_klines(KLINE, coin='BTCUSDT', market='perp', timeframe='15m')[0]
    assert record.open_time == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert record.close_time == datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
    assert record.available_at == record.close_time
    assert record.close_price == Decimal('87705.00')
    assert record.trade_count == 15400
    assert record.taker_buy_base_volume == Decimal('447.032')


def test_spot_klines_publish_microseconds_and_land_on_the_same_close():
    spot = (
        '1767225600000000,87648.21000000,87816.00,87600.00,87705.00,1.5,'
        '1767226499999999,100.0,10,0.4,40.0,0\n'
    )
    record = V.parse_vision_klines(spot, coin='BTCUSDT', market='spot', timeframe='15m')[0]
    assert record.open_time == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert record.close_time == datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
    assert record.available_at == record.close_time


def test_metrics_keep_only_the_hour_boundary_and_add_the_publication_lag():
    records = V.parse_vision_hourly_oi(METRICS, coin='BTCUSDT', publication_lag=LAG)
    assert [record.poll_time for record in records] == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    ]
    assert records[0].timeframe == '1h'
    assert records[0].open_interest_base == Decimal('94262.030')
    assert records[0].available_at == availability.open_interest_available_at(records[0].poll_time, LAG)
    assert records[0].available_at == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
