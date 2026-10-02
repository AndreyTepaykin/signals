"""The writers: idempotence, no corruption of existing rows, and the unclosed-bar lock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.core.time import ensure_utc
from app.models.as_of import as_of
from app.models.feeds import Candle, ForceOrder, Funding, OpenInterest
from app.worker.feeds import records as R
from app.worker.feeds import store
from ingest_helpers import NOW, at

UTC = timezone.utc
FIFTEEN = timedelta(minutes=15)
OI_LAG = timedelta(seconds=180)  # an arbitrary allowance: these tests are about the writer, not the number


def candle(open_time=None, **overrides):
    open_time = open_time or at(16, 15)
    values = dict(
        coin='BTCUSDT', market='perp', timeframe='15m', open_time=open_time, close_time=open_time + FIFTEEN,
        open_price=Decimal('100'), high_price=Decimal('102'), low_price=Decimal('99'), close_price=Decimal('101'),
        base_volume=Decimal('10'), quote_volume=Decimal('1000'), taker_buy_base_volume=Decimal('4'),
        taker_buy_quote_volume=Decimal('400'), trade_count=5, available_at=open_time + FIFTEEN,
    )
    values.update(overrides)
    return R.CandleRecord(**values)


def count(session, model):
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# candles
# ---------------------------------------------------------------------------
def test_reinserting_an_identical_candle_writes_nothing_and_keeps_one_row(session):
    first = store.write_candles(session, [candle()], now=NOW)
    second = store.write_candles(session, [candle()], now=NOW)
    assert (first.written, first.already_present) == (1, 0)
    assert (second.written, second.already_present) == (0, 1)
    assert count(session, Candle) == 1


def test_an_overlapping_window_cannot_change_a_stored_row(session):
    store.write_candles(session, [candle(close_price=Decimal('101'))], now=NOW)
    result = store.write_candles(session, [candle(close_price=Decimal('555')), candle(at(16, 0), close_price=Decimal('7'))], now=NOW)
    assert result.written == 1  # only the genuinely new 16:00 bar
    stored = session.execute(text("select close_price from candle where open_time = :t"), {'t': at(16, 15)}).scalar_one()
    assert stored == Decimal('101')  # the original value, untouched
    assert count(session, Candle) == 2


def test_duplicates_inside_one_batch_are_collapsed(session):
    result = store.write_candles(session, [candle(), candle(), candle()], now=NOW)
    assert result.submitted == 3 and result.written == 1 and count(session, Candle) == 1


def test_the_writer_refuses_a_bar_that_has_not_closed(session):
    forming = candle(at(16, 45))  # closes 17:00, after NOW
    with pytest.raises(store.UnclosedBarError):
        store.write_candles(session, [candle(at(16, 30)), forming], now=NOW)
    assert count(session, Candle) == 0  # not even the closed one: the whole batch is refused


def test_a_bar_closing_exactly_now_is_accepted_and_one_millisecond_later_is_not(session):
    closes_now = candle(at(16, 35) - FIFTEEN)  # opens 16:20, closes 16:35
    store.write_candles(session, [closes_now], now=at(16, 35))
    with pytest.raises(store.UnclosedBarError):
        store.write_candles(session, [candle(at(16, 20), coin='ETHUSDT')], now=at(16, 35) - timedelta(milliseconds=1))


def test_candle_instants_come_back_as_aware_utc_with_available_at_at_bar_close(session):
    store.write_candles(session, [candle()], now=NOW)
    row = session.execute(as_of(NOW).select(Candle)).scalar_one()
    # psycopg hands back instants in the SESSION time zone (Europe/Moscow on this machine): still aware,
    # still the same instant. Callers that read .hour or .date() must go through ensure_utc first.
    for value in (row.open_time, row.close_time, row.available_at):
        assert value.tzinfo is not None
        assert ensure_utc(value).utcoffset() == timedelta(0)
    assert row.open_time == at(16, 15)
    assert row.available_at == row.close_time == row.open_time + FIFTEEN == at(16, 30)
    assert isinstance(row.close_price, Decimal) and row.close_price == Decimal('101')


def test_the_as_of_gate_hides_a_candle_until_its_bar_has_closed(session):
    store.write_candles(session, [candle(at(16, 15))], now=NOW)
    before_close = at(16, 30) - timedelta(milliseconds=1)
    assert session.execute(as_of(before_close).select(Candle)).scalars().all() == []
    assert len(session.execute(as_of(at(16, 30)).select(Candle)).scalars().all()) == 1


# ---------------------------------------------------------------------------
# open interest
# ---------------------------------------------------------------------------
def oi(poll_time=None, **overrides):
    poll_time = poll_time or at(16, 0)
    values = dict(coin='BTCUSDT', timeframe='1h', poll_time=poll_time, open_interest_base=Decimal('96446.3'),
                  open_interest_quote=Decimal('8226619909.08'), available_at=poll_time + OI_LAG)
    values.update(overrides)
    return R.OpenInterestRecord(**values)


def test_open_interest_is_idempotent_and_available_at_the_poll_close_plus_the_lag(session):
    assert store.write_open_interest(session, [oi()], now=NOW).written == 1
    assert store.write_open_interest(session, [oi(open_interest_base=Decimal('1'))], now=NOW).written == 0
    row = session.execute(as_of(NOW).select(OpenInterest)).scalar_one()
    assert row.available_at == row.poll_time + OI_LAG and row.open_interest_base == Decimal('96446.3')


def test_the_writer_refuses_an_open_interest_sample_that_is_not_yet_knowable(session):
    sample = oi(at(16, 45))  # boundary 16:45 + 3 min = 16:48, but NOW is 16:50:30 -> fine
    assert store.write_open_interest(session, [sample], now=NOW).written == 1
    too_new = oi(at(16, 45), coin='ETHUSDT')
    with pytest.raises(store.UnclosedBarError):
        store.write_open_interest(session, [oi(at(16, 45), coin='SOLUSDT'), too_new], now=at(16, 48) - timedelta(seconds=1))  # 1s short of 16:48
    assert count(session, OpenInterest) == 1  # the whole batch was refused
    assert store.write_open_interest(session, [too_new], now=at(16, 48)).written == 1  # exactly at availability is fine


def test_the_as_of_gate_hides_open_interest_until_boundary_plus_lag(session):
    store.write_open_interest(session, [oi(at(16, 0))], now=NOW)
    assert session.execute(as_of(at(16, 0)).select(OpenInterest)).scalars().all() == []  # the bar close itself: not knowable yet
    assert session.execute(as_of(at(16, 3) - timedelta(milliseconds=1)).select(OpenInterest)).scalars().all() == []
    assert len(session.execute(as_of(at(16, 3)).select(OpenInterest)).scalars().all()) == 1


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------
def funding(kind='settled', funding_time=None, rate='0.0001', available_at=None, **overrides):
    funding_time = funding_time or at(8, 0)
    values = dict(coin='BTCUSDT', kind=kind, funding_time=funding_time, funding_rate=Decimal(rate), interval_hours=None,
                  mark_price=None, index_price=None, available_at=available_at or funding_time)
    values.update(overrides)
    return R.FundingRecord(**values)


def test_a_settled_print_is_written_once_and_never_updated(session):
    assert store.write_funding(session, [funding(rate='0.0001')], now=NOW).written == 1
    assert store.write_funding(session, [funding(rate='0.9999')], now=NOW).written == 0
    row = session.execute(as_of(NOW).select(Funding)).scalar_one()
    assert row.funding_rate == Decimal('0.000100000000') and row.available_at == row.funding_time


def test_settled_and_predicted_rows_for_the_same_instant_are_distinct_rows(session):
    store.write_funding(session, [funding('settled', at(8, 0)), funding('predicted', at(8, 0), available_at=at(7, 59))], now=NOW)
    assert count(session, Funding) == 2


def test_a_predicted_rate_is_refreshed_only_by_a_newer_observation(session):
    settlement = at(16, 0, day=3)
    store.write_funding(session, [funding('predicted', settlement, '0.0001', available_at=at(16, 10))], now=NOW)
    newer = store.write_funding(session, [funding('predicted', settlement, '0.0002', available_at=at(16, 20))], now=NOW)
    stale = store.write_funding(session, [funding('predicted', settlement, '0.0009', available_at=at(16, 5))], now=NOW)
    same = store.write_funding(session, [funding('predicted', settlement, '0.0003', available_at=at(16, 20))], now=NOW)
    assert (newer.written, stale.written, same.written) == (1, 0, 0)
    row = session.execute(as_of(NOW).select(Funding)).scalar_one()
    assert row.funding_rate == Decimal('0.000200000000') and row.available_at == at(16, 20)
    assert count(session, Funding) == 1


def test_a_predicted_row_may_point_at_a_future_settlement(session):
    # the schema CHECK exempts predicted rows from available_at >= funding_time
    store.write_funding(session, [funding('predicted', at(23, 0), available_at=at(16, 0))], now=NOW)
    assert count(session, Funding) == 1


def test_many_observations_of_one_predicted_key_in_a_single_batch_do_not_error(session):
    batch = [funding('predicted', at(23, 0), f'0.000{n}', available_at=at(16, n)) for n in range(1, 6)]
    store.write_funding(session, batch, now=NOW)
    row = session.execute(as_of(NOW).select(Funding)).scalar_one()
    assert row.available_at == at(16, 5)


# ---------------------------------------------------------------------------
# force orders
# ---------------------------------------------------------------------------
def test_the_writer_refuses_a_settled_funding_print_that_is_not_yet_knowable(session):
    settled = funding('settled', at(16, 0), available_at=at(16, 0) + timedelta(seconds=30))
    with pytest.raises(store.UnclosedBarError):
        store.write_funding(session, [settled], now=at(16, 0) + timedelta(seconds=29))
    assert count(session, Funding) == 0
    assert store.write_funding(session, [settled], now=at(16, 0) + timedelta(seconds=30)).written == 1
    # the gate agrees: invisible before settlement + lag, visible at it
    assert session.execute(as_of(at(16, 0) + timedelta(seconds=29)).select(Funding)).all() == []
    assert len(session.execute(as_of(at(16, 0) + timedelta(seconds=30)).select(Funding)).all()) == 1

def force(sequence=1, connection='c1', **order):
    base = {'s': 'BTCUSDT', 'S': 'SELL', 'q': '0.014', 'p': '9910', 'ap': '9910', 'X': 'FILLED', 'z': '0.014', 'T': 1790958600123}
    base.update(order)
    return R.parse_force_order({'e': 'forceOrder', 'E': 1790958600456, 'o': base}, connection_id=connection, sequence=sequence)


def test_a_retried_force_order_batch_is_idempotent(session):
    batch = [force(1), force(2, p='9911')]
    assert store.write_force_orders(session, batch).written == 2
    assert store.write_force_orders(session, batch).written == 0
    assert count(session, ForceOrder) == 2


def test_two_genuinely_identical_liquidations_are_both_kept(session):
    assert store.write_force_orders(session, [force(1), force(2)]).written == 2
    assert count(session, ForceOrder) == 2


def test_force_order_is_available_at_its_print_time_in_the_database(session):
    store.write_force_orders(session, [force(1)])
    row = session.execute(as_of(at(16, 31)).select(ForceOrder)).scalar_one()
    assert row.available_at == row.traded_at == R.from_ms(1790958600123)
    assert row.side == 'sell' and row.price == Decimal('9910')
    assert session.execute(as_of(row.traded_at - timedelta(milliseconds=1)).select(ForceOrder)).scalars().all() == []
