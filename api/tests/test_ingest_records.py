"""Millisecond conversion, the available-at rule per feed kind, and row validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.worker.feeds import availability
from app.worker.feeds import records as R

UTC = timezone.utc
FIFTEEN = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# milliseconds -> aware UTC
# ---------------------------------------------------------------------------
def test_epoch_milliseconds_become_aware_utc_exactly():
    instant = R.from_ms(1790958600000)
    assert instant == datetime(2026, 10, 2, 16, 30, tzinfo=UTC)
    assert instant.tzinfo is not None and instant.utcoffset() == timedelta(0)


def test_conversion_keeps_millisecond_precision_without_float_rounding():
    instant = R.from_ms(1790899200002)  # a real Binance funding stamp, 2 ms after the boundary
    assert instant.microsecond == 2000
    assert R.to_ms(instant) == 1790899200002


@pytest.mark.parametrize('ms', [1790958600000, 1, 1790958599999, 1790958600001])
def test_ms_round_trip(ms):
    assert R.to_ms(R.from_ms(ms)) == ms


@pytest.mark.parametrize('bad', [1.5, '1790958600000', None, True, 0, -5])
def test_from_ms_refuses_anything_but_a_positive_int(bad):
    with pytest.raises(R.MalformedRow):
        R.from_ms(bad)


def test_to_ms_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        R.to_ms(datetime(2026, 10, 2, 16, 30))


def test_a_record_cannot_be_built_with_a_naive_datetime():
    with pytest.raises(R.MalformedRow):
        R.OpenInterestRecord(
            coin='BTCUSDT', timeframe='15m', poll_time=datetime(2026, 10, 2, 16, 30),
            open_interest_base=Decimal(1), open_interest_quote=None, available_at=datetime(2026, 10, 2, 16, 30, tzinfo=UTC),
        )


def test_a_non_utc_aware_datetime_is_normalised_to_utc():
    plus3 = timezone(timedelta(hours=3))
    record = R.OpenInterestRecord(
        coin='BTCUSDT', timeframe='15m', poll_time=datetime(2026, 10, 2, 19, 30, tzinfo=plus3),
        open_interest_base=Decimal(1), open_interest_quote=None, available_at=datetime(2026, 10, 2, 19, 30, tzinfo=plus3),
    )
    assert record.poll_time == datetime(2026, 10, 2, 16, 30, tzinfo=UTC)
    assert record.poll_time.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# the available-at rule per feed kind (the spec's "Feeds" table)
# ---------------------------------------------------------------------------
OPEN = datetime(2026, 10, 2, 16, 30, tzinfo=UTC)
KLINE = [1790958600000, '100.0', '102.0', '99.0', '101.0', '10.5', 1790959499999, '1050.0', 77, '4.2', '420.0', '0']


def test_kline_is_available_at_bar_close_not_bar_open():
    record = R.parse_kline_row(KLINE, coin='BTCUSDT', market='perp', timeframe='15m')
    assert record.open_time == OPEN
    assert record.available_at == OPEN + FIFTEEN
    assert record.close_time == record.available_at  # the closing boundary, exclusive
    assert availability.kline_available_at(OPEN, FIFTEEN) == OPEN + FIFTEEN


@pytest.mark.parametrize('timeframe, length', [('15m', timedelta(minutes=15)), ('1h', timedelta(hours=1)), ('4h', timedelta(hours=4))])
def test_kline_available_at_is_open_plus_timeframe_for_every_timeframe(timeframe, length):
    open_ms = R.to_ms(datetime(2026, 10, 2, 16, 0, tzinfo=UTC))
    row = [open_ms, '1', '2', '1', '2', '3', open_ms + R.timeframe_delta(timeframe) // timedelta(milliseconds=1) - 1, '3', 1, '1', '1', '0']
    record = R.parse_kline_row(row, coin='ETHUSDT', market='spot', timeframe=timeframe)
    assert record.available_at - record.open_time == length


OI_LAG = timedelta(seconds=180)


def test_open_interest_is_available_at_the_poll_close_plus_the_publication_lag():
    record = R.parse_open_interest_row(
        {'symbol': 'BTCUSDT', 'sumOpenInterest': '96446.315', 'sumOpenInterestValue': '8226619909.081', 'timestamp': 1790956800000},
        coin='BTCUSDT', period='1h', publication_lag=OI_LAG,
    )
    assert record.poll_time == datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
    assert record.available_at == record.poll_time + OI_LAG == datetime(2026, 10, 2, 16, 3, tzinfo=UTC)
    assert availability.open_interest_available_at(record.poll_time, OI_LAG) == record.available_at
    assert record.available_at > record.poll_time  # never the optimistic boundary
    assert record.available_at.utcoffset() == timedelta(0)


def test_the_publication_lag_has_no_default_and_cannot_be_negative():
    with pytest.raises(TypeError):
        availability.open_interest_available_at(datetime(2026, 10, 2, 16, 0, tzinfo=UTC))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        R.parse_open_interest_row({'sumOpenInterest': '1', 'timestamp': 1790956800000}, coin='BTCUSDT', period='1h')  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        availability.open_interest_available_at(datetime(2026, 10, 2, 16, 0, tzinfo=UTC), timedelta(seconds=-1))


def test_a_zero_lag_is_allowed_and_is_exactly_the_boundary():
    assert availability.open_interest_available_at(datetime(2026, 10, 2, 16, 0, tzinfo=UTC), timedelta(0)) == datetime(2026, 10, 2, 16, 0, tzinfo=UTC)


def test_settled_funding_is_available_at_the_print_plus_publication_lag_keeping_exchange_jitter():
    lag = timedelta(seconds=30)
    record = R.parse_funding_settled_row({'symbol': 'BTCUSDT', 'fundingTime': 1790899200002, 'fundingRate': '0.00002693', 'markPrice': '84829.6'}, publication_lag=lag)
    assert record.kind == 'settled'
    assert record.funding_time == R.from_ms(1790899200002)
    assert record.available_at == record.funding_time + lag
    with pytest.raises(TypeError):  # the lag has no default
        R.parse_funding_settled_row({'symbol': 'BTCUSDT', 'fundingTime': 1, 'fundingRate': '0', 'markPrice': '1'})
    with pytest.raises(ValueError):
        R.parse_funding_settled_row({'symbol': 'BTCUSDT', 'fundingTime': 1, 'fundingRate': '0', 'markPrice': '1'}, publication_lag=timedelta(seconds=-1))
    assert record.interval_hours is None  # the history endpoint does not say; never guessed
    assert record.funding_rate == Decimal('0.00002693')


def test_predicted_funding_is_available_when_observed_not_when_it_settles():
    row = {
        'symbol': 'BTCUSDT', 'markPrice': '85224.2', 'indexPrice': '85242.9', 'lastFundingRate': '0.00000863',
        'nextFundingTime': 1790985600000, 'time': 1790959536002,
    }
    record = R.parse_funding_predicted_row(row, interval_hours=8)
    assert record.kind == 'predicted'
    assert record.funding_time == R.from_ms(1790985600000)  # the FUTURE settlement it applies to
    assert record.available_at == R.from_ms(1790959536002)  # the moment it was stamped
    assert record.available_at < record.funding_time
    assert record.interval_hours == 8


def _force_message(**order):
    base = {'s': 'BTCUSDT', 'S': 'SELL', 'o': 'LIMIT', 'f': 'IOC', 'q': '0.014', 'p': '9910', 'ap': '9910', 'X': 'FILLED', 'l': '0.014', 'z': '0.014', 'T': 1790958600123}
    base.update(order)
    return {'e': 'forceOrder', 'E': 1790958600456, 'o': base}


def test_force_order_is_available_at_its_print_time_not_the_event_time():
    record = R.parse_force_order(_force_message(), connection_id='c', sequence=1)
    assert record is not None
    assert record.traded_at == R.from_ms(1790958600123)
    assert record.available_at == record.traded_at
    assert record.event_time == R.from_ms(1790958600456)
    assert record.event_time > record.available_at
    assert record.side == 'sell'


# ---------------------------------------------------------------------------
# kline validation
# ---------------------------------------------------------------------------
def test_kline_values_are_exact_decimals():
    record = R.parse_kline_row(KLINE, coin='BTCUSDT', market='perp', timeframe='15m')
    assert record.close_price == Decimal('101.0') and isinstance(record.close_price, Decimal)
    assert record.taker_buy_base_volume == Decimal('4.2')
    assert record.trade_count == 77


@pytest.mark.parametrize(
    'mutation, why',
    [
        (lambda r: r.__setitem__(0, r[0] + 1000), 'open not on a 15m boundary'),
        (lambda r: r.__setitem__(6, r[6] + 1), 'closeTime is not open + 15m - 1ms'),
        (lambda r: r.__setitem__(9, '99'), 'taker buy volume above total volume'),
        (lambda r: r.__setitem__(1, 100.0), 'a binary float price'),
        (lambda r: r.__setitem__(3, '-1'), 'non-positive price'),
        (lambda r: r.__setitem__(2, '98'), 'high below low'),
        (lambda r: r.__setitem__(4, 'NaN'), 'not finite'),
    ],
)
def test_malformed_klines_are_rejected(mutation, why):
    row = list(KLINE)
    mutation(row)
    with pytest.raises(R.MalformedRow):
        R.parse_kline_row(row, coin='BTCUSDT', market='perp', timeframe='15m')


def test_short_kline_row_is_rejected():
    with pytest.raises(R.MalformedRow):
        R.parse_kline_row(KLINE[:5], coin='BTCUSDT', market='perp', timeframe='15m')


def test_unsupported_timeframe_and_market_are_refused():
    with pytest.raises(ValueError):
        R.parse_kline_row(KLINE, coin='BTCUSDT', market='perp', timeframe='5m')
    with pytest.raises(ValueError):
        R.parse_kline_row(KLINE, coin='BTCUSDT', market='cme', timeframe='15m')


def test_open_interest_sample_off_the_period_grid_is_rejected():
    with pytest.raises(R.MalformedRow):
        R.parse_open_interest_row({'sumOpenInterest': '1', 'timestamp': 1790956800000 + 1}, coin='BTCUSDT', period='1h', publication_lag=OI_LAG)


# ---------------------------------------------------------------------------
# force-order parsing and dedup keys
# ---------------------------------------------------------------------------
def test_force_orders_outside_the_watchlist_and_other_events_are_ignored():
    assert R.parse_force_order(_force_message(s='XRPUSDT'), connection_id='c', sequence=1, watchlist=frozenset({'BTCUSDT'})) is None
    assert R.parse_force_order({'e': 'markPriceUpdate'}, connection_id='c', sequence=1) is None


def test_unusable_force_orders_are_rejected():
    with pytest.raises(R.MalformedRow):
        R.parse_force_order(_force_message(S='SIDEWAYS'), connection_id='c', sequence=1)
    with pytest.raises(R.MalformedRow):
        R.parse_force_order(_force_message(p='0'), connection_id='c', sequence=1)


def test_retrying_the_same_message_gives_the_same_key_but_identical_looking_prints_get_distinct_keys():
    first = R.parse_force_order(_force_message(), connection_id='conn', sequence=1)
    same_message_again = R.parse_force_order(_force_message(), connection_id='conn', sequence=1)
    look_alike = R.parse_force_order(_force_message(), connection_id='conn', sequence=2)
    other_connection = R.parse_force_order(_force_message(), connection_id='other', sequence=1)
    assert first.dedup_key == same_message_again.dedup_key
    assert first.dedup_key != look_alike.dedup_key  # two genuine identical liquidations must both land
    assert first.dedup_key != other_connection.dedup_key
