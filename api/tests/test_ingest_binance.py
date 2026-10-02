"""The Binance client against a fake exchange: closed bars only, pagination, the OI window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.worker.feeds import records as R
from app.worker.feeds.binance import (
    BinanceClient,
    ServerClock,
    ceil_to_grid,
    clamp_open_interest_start,
    floor_to_grid,
    futures_kline_weight,
)
from ingest_helpers import NOW, FakeExchange, at, make_client, ms

UTC = timezone.utc
FIFTEEN = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# THE unclosed-bar rule
# ---------------------------------------------------------------------------
def test_the_in_progress_candle_is_dropped_and_counted():
    exchange = FakeExchange()  # now = 16:50:30, so the 16:45 bar is still forming
    batch = exchange.client().klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 0), end=at(16, 45))
    opens = [r.open_time for r in batch.records]
    assert opens == [at(16, 0), at(16, 15), at(16, 30)]
    assert batch.dropped_unclosed == 1  # the 16:45 candle the exchange returned and we refused
    assert batch.fetched == 4
    assert all(r.available_at <= NOW for r in batch.records)
    assert all(r.close_time <= NOW for r in batch.records)


def test_the_exchange_really_did_send_the_unclosed_bar():
    """Guard against a vacuous test: the fake must hand back a bar that closes in the future."""
    exchange = FakeExchange()
    exchange.client().klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 45), end=at(16, 45))
    payload = httpx.Client(transport=httpx.MockTransport(exchange.handler)).get(
        'https://fapi.test/fapi/v1/klines',
        params={'symbol': 'BTCUSDT', 'interval': '15m', 'startTime': ms(at(16, 45)), 'endTime': ms(at(16, 45)), 'limit': 1},
    ).json()
    assert payload and payload[0][6] > ms(NOW)


def test_only_unclosed_bar_requested_yields_nothing():
    batch = FakeExchange().client().klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 45), end=at(16, 45))
    assert batch.records == [] and batch.dropped_unclosed == 1


@pytest.mark.parametrize('timeframe, forming_open', [('1h', at(16, 0)), ('4h', at(16, 0))])
def test_the_rule_holds_for_every_timeframe(timeframe, forming_open):
    batch = FakeExchange().client().klines(coin='ETHUSDT', market='spot', timeframe=timeframe, start=at(8, 0), end=forming_open)
    assert batch.dropped_unclosed == 1
    assert batch.records and max(r.close_time for r in batch.records) <= NOW


def test_a_bar_is_not_called_closed_inside_the_settle_margin():
    # 16:45:00.500 - the 16:30 bar closed half a second ago; its values are still provisional.
    just_after = datetime(2026, 10, 2, 16, 45, 0, 500_000, tzinfo=UTC)
    batch = FakeExchange(now=just_after).client().klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 15), end=at(16, 30))
    assert [r.open_time for r in batch.records] == [at(16, 15)]
    assert batch.dropped_unclosed == 1
    # ...and the same bar is accepted once the margin has passed.
    later = datetime(2026, 10, 2, 16, 45, 0, tzinfo=UTC) + FakeExchange().client().settle
    assert later - at(16, 45) >= timedelta(seconds=10)  # the margin is well above the ~5 s measured finalisation
    batch = FakeExchange(now=later).client().klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 15), end=at(16, 30))
    assert [r.open_time for r in batch.records] == [at(16, 15), at(16, 30)]


def test_a_fast_local_clock_cannot_make_a_forming_bar_look_closed():
    exchange = FakeExchange(now=datetime(2026, 10, 2, 16, 44, 58, tzinfo=UTC))  # the exchange: 2s before the close
    fast_local = datetime(2026, 10, 2, 16, 45, 3, tzinfo=UTC)  # our machine: 3s after it
    client = exchange.client()
    client.clock = ServerClock(lambda: ms(exchange.now), local_now=lambda: fast_local)
    assert client.clock.offset == timedelta(0)
    assert client.now() == datetime(2026, 10, 2, 16, 44, 58, tzinfo=UTC)  # corrected back to exchange time
    batch = client.klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 15), end=at(16, 30))
    assert [r.open_time for r in batch.records] == [at(16, 15)]  # the 16:30 bar is NOT closed yet
    assert batch.dropped_unclosed == 1


def test_latest_closed_open_is_the_newest_settled_bar():
    client = FakeExchange().client()
    assert client.latest_closed_open('15m') == at(16, 30)
    assert client.latest_closed_open('1h') == at(15, 0)
    assert client.latest_closed_open('4h') == at(12, 0)  # the 12:00-16:00 bar closed at 16:00


# ---------------------------------------------------------------------------
# routing, weight, malformed rows
# ---------------------------------------------------------------------------
def test_perp_and_spot_hit_different_hosts_and_endpoints():
    exchange = FakeExchange()
    client = exchange.client()
    client.klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 0), end=at(16, 15))
    client.klines(coin='BTCUSDT', market='spot', timeframe='15m', start=at(16, 0), end=at(16, 15))
    assert [(r.url.host, r.url.path) for r in exchange.kline_requests()] == [
        ('fapi.test', '/fapi/v1/klines'),
        ('api.test', '/api/v3/klines'),
    ]


@pytest.mark.parametrize('limit, weight', [(1, 1), (99, 1), (100, 2), (499, 2), (500, 5), (1000, 5), (1001, 10), (1500, 10)])
def test_futures_kline_weight_follows_the_documented_table(limit, weight):
    assert futures_kline_weight(limit) == weight


def test_a_malformed_bar_is_rejected_not_fatal(monkeypatch):
    original = FakeExchange.kline_row

    def bad_row(open_ms, length_ms, symbol_seed=0):
        row = original(open_ms, length_ms, symbol_seed)
        if open_ms == ms(at(16, 15)):
            row[9] = '999999'  # taker buy volume above total volume
        return row

    monkeypatch.setattr(FakeExchange, 'kline_row', staticmethod(bad_row))
    batch = FakeExchange().client().klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(16, 0), end=at(16, 30))
    assert [r.open_time for r in batch.records] == [at(16, 0), at(16, 30)]
    assert batch.rejected == 1 and batch.notes

# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------
def test_pagination_across_a_page_boundary_returns_every_bar_once_in_order():
    exchange = FakeExchange()
    client = exchange.client(kline_page_limit=10)
    start, end = at(0, 0, day=1), at(6, 0, day=1) + timedelta(minutes=0)  # 25 fifteen-minute bars
    batch = client.klines(coin='BTCUSDT', market='perp', timeframe='15m', start=start, end=end)
    opens = [r.open_time for r in batch.records]
    assert len(opens) == 25 and opens == sorted(set(opens))
    assert opens[0] == start and opens[-1] == end
    requests = exchange.kline_requests()
    assert len(requests) == 3  # 10 + 10 + 5
    starts = [int(r.url.params['startTime']) for r in requests]
    assert starts[1] == ms(start) + 10 * 900_000  # the cursor moved one bar past the last row of page 1
    assert starts[2] == ms(start) + 20 * 900_000
    assert [int(r.url.params['limit']) for r in requests] == [10, 10, 5]


def test_a_range_that_is_an_exact_multiple_of_the_page_does_not_loop():
    exchange = FakeExchange()
    client = exchange.client(kline_page_limit=10)
    batch = client.klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(0, 0, day=1), end=at(0, 0, day=1) + 19 * FIFTEEN)
    assert len(batch.records) == 20
    assert len(exchange.kline_requests()) == 2


def test_overlapping_pages_do_not_duplicate_bars():
    exchange = FakeExchange()
    client = exchange.client(kline_page_limit=5)

    def overlapping(request: httpx.Request):
        # Every page after the first also repeats the bar just before its startTime.
        if request.url.path == '/fapi/v1/klines' and len(exchange.kline_requests()) > 1:
            params = dict(request.url.params)
            earlier = FakeExchange.kline_row(int(params['startTime']) - 900_000, 900_000, sum(map(ord, 'BTCUSDT')) % 5)
            return httpx.Response(200, json=[earlier] + exchange._klines(params).json())
        return None

    exchange.interceptor = overlapping
    batch = client.klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(0, 0, day=1), end=at(0, 0, day=1) + 11 * FIFTEEN)
    opens = [r.open_time for r in batch.records]
    assert len(opens) == len(set(opens)) == 12

def test_iter_klines_yields_page_by_page_so_callers_can_commit_each_one():
    client = FakeExchange().client(kline_page_limit=4)
    pages = list(client.iter_klines(coin='BTCUSDT', market='perp', timeframe='15m', start=at(0, 0, day=1), end=at(0, 0, day=1) + 9 * FIFTEEN))
    assert [len(p.records) for p in pages] == [4, 4, 2]


def test_grid_helpers():
    assert floor_to_grid(at(16, 44), FIFTEEN) == at(16, 30)
    assert floor_to_grid(at(16, 45), FIFTEEN) == at(16, 45)
    assert ceil_to_grid(at(16, 31), FIFTEEN) == at(16, 45)
    assert ceil_to_grid(at(16, 30), FIFTEEN) == at(16, 30)
    assert floor_to_grid(at(18, 59), timedelta(hours=4)) == at(16, 0)


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------
def test_funding_history_paginates_and_stamps_each_print_at_its_own_time():
    exchange = FakeExchange()
    client = exchange.client(funding_page_limit=3)
    batch = client.funding_history(coin='BTCUSDT', start=at(0, 0, day=1), end=at(0, 0, day=3))
    times = [r.funding_time for r in batch.records]
    assert len(times) == len(set(times)) == 6  # Oct 1 00:00 .. Oct 2 16:00 (the Oct 3 00:00 print has not settled)
    assert times == sorted(times)
    assert all(r.kind == 'settled' and r.available_at == r.funding_time + client.funding_lag for r in batch.records)
    assert all(r.available_at <= NOW for r in batch.records)
    assert len(exchange.requests_to('/fapi/v1/fundingRate')) == 3  # 3 + 3, then an empty page proves the end


def test_funding_history_cursor_does_not_repeat_the_last_print():
    exchange = FakeExchange()
    client = exchange.client(funding_page_limit=2)
    client.funding_history(coin='BTCUSDT', start=at(0, 0, day=1), end=at(0, 0, day=3))
    starts = [int(r.url.params['startTime']) for r in exchange.requests_to('/fapi/v1/fundingRate')]
    last_time_of_page_one = ms(at(8, 0, day=1)) + 2
    assert starts[1] == last_time_of_page_one + 1


def test_a_settled_print_inside_the_publication_lag_is_dropped():
    # the print settled 10 s ago: real, but inside the 30 s allowance, so not yet knowable
    exchange = FakeExchange(now=at(8, 0) + timedelta(seconds=10))
    exchange.interceptor = lambda request: (
        httpx.Response(200, json=[{'symbol': 'BTCUSDT', 'fundingTime': ms(at(8, 0)) + 2, 'fundingRate': '0.0001', 'markPrice': '1'}])
        if request.url.path == '/fapi/v1/fundingRate' else None
    )
    batch = exchange.client().funding_history(coin='BTCUSDT', start=at(0, 0), end=at(9, 0))
    assert batch.records == [] and batch.dropped_unclosed == 1
    later = FakeExchange(now=at(8, 0) + timedelta(seconds=40))
    later.interceptor = exchange.interceptor
    assert len(later.client().funding_history(coin='BTCUSDT', start=at(0, 0), end=at(9, 0)).records) == 1


def test_a_settled_print_in_the_future_is_dropped():
    exchange = FakeExchange(now=at(7, 0))  # before the 08:00 settlement
    inner = exchange.handler

    def future_print(request: httpx.Request):
        if request.url.path == '/fapi/v1/fundingRate':
            return httpx.Response(200, json=[{'symbol': 'BTCUSDT', 'fundingTime': ms(at(8, 0)) + 2, 'fundingRate': '0.0001', 'markPrice': '1'}])
        return None

    exchange.interceptor = future_print
    batch = exchange.client().funding_history(coin='BTCUSDT', start=at(0, 0), end=at(9, 0))
    assert batch.records == [] and batch.dropped_unclosed == 1


def test_predicted_funding_comes_from_the_premium_index_with_the_symbols_interval():
    exchange = FakeExchange()
    batch = exchange.client().predicted_funding(frozenset({'BTCUSDT', 'LPTUSDT', 'SOLUSDT'}))
    by_coin = {r.coin: r for r in batch.records}
    assert set(by_coin) == {'BTCUSDT', 'LPTUSDT'}  # SOL is not in the exchange's answer
    assert by_coin['BTCUSDT'].interval_hours == 8  # absent from fundingInfo: Binance's documented default
    assert by_coin['LPTUSDT'].interval_hours == 4
    assert all(r.kind == 'predicted' for r in batch.records)
    assert by_coin['BTCUSDT'].available_at == NOW - timedelta(milliseconds=1500)
    assert by_coin['BTCUSDT'].funding_time > NOW  # the settlement it predicts is still ahead


# ---------------------------------------------------------------------------
# open interest: the 30-day window and the "last `limit` rows" quirk
# ---------------------------------------------------------------------------
def test_open_interest_windows_are_sliced_so_the_last_limit_quirk_cannot_truncate():
    exchange = FakeExchange()
    client = exchange.client(open_interest_page_limit=5)
    start, end = at(0, 0), at(11, 0)  # twelve hourly samples
    batch = client.open_interest_history(coin='BTCUSDT', period='1h', start=start, end=end)
    stamps = [r.poll_time for r in batch.records]
    assert stamps == [at(h, 0) for h in range(12)]  # none missing, none repeated
    assert len(exchange.requests_to('/futures/data/openInterestHist')) == 3  # 5 + 5 + 2 hours per window
    for request in exchange.requests_to('/futures/data/openInterestHist'):
        window = int(request.url.params['endTime']) - int(request.url.params['startTime']) + 1
        assert window <= 5 * 3_600_000  # a window never holds more periods than one page


def test_a_naive_single_call_would_have_lost_samples():
    """Documents WHY the slicing exists: the fake (like Binance) returns the tail of a long window."""
    exchange = FakeExchange()
    response = httpx.Client(transport=httpx.MockTransport(exchange.handler)).get(
        'https://fapi.test/futures/data/openInterestHist',
        params={'symbol': 'BTCUSDT', 'period': '1h', 'limit': 5, 'startTime': ms(at(0, 0)), 'endTime': ms(at(11, 0))},
    )
    stamps = [row['timestamp'] for row in response.json()]
    assert stamps == [ms(at(h, 0)) for h in range(7, 12)]  # only the last five of twelve


def test_open_interest_start_is_clamped_to_the_30_day_window_and_the_exchange_accepts_it():
    exchange = FakeExchange()
    client = exchange.client()
    start, clamped = clamp_open_interest_start(NOW - timedelta(days=90), client.now())
    assert clamped is True
    assert NOW - start < timedelta(days=30)
    batch = client.open_interest_history(coin='BTCUSDT', period='4h', start=start, end=at(12, 0))
    assert batch.records  # no -1130
    lag = client.open_interest_lag
    assert all(r.available_at == r.poll_time + lag for r in batch.records)
    for request in exchange.requests_to('/futures/data/openInterestHist'):
        assert ms(NOW) - int(request.url.params['startTime']) < 30 * 86_400_000


def test_a_sample_the_exchange_has_not_published_yet_is_never_returned():
    exchange = FakeExchange()  # serves a sample 90 s after its boundary; NOW is 16:50:30
    client = exchange.client()
    assert [r.poll_time for r in client.open_interest_history(coin='BTCUSDT', period='15m', start=at(16, 15), end=at(16, 45)).records][-1] == at(16, 45)
    exchange.now = at(16, 45) + timedelta(seconds=89)  # one second before the exchange publishes the 16:45 sample
    assert at(16, 45) not in [r.poll_time for r in exchange.client().open_interest_history(coin='BTCUSDT', period='15m', start=at(16, 15), end=at(16, 45)).records]


def test_the_allowance_is_held_back_even_after_the_exchange_has_published():
    exchange = FakeExchange()
    client = exchange.client()
    lag = client.open_interest_lag
    assert lag > timedelta(seconds=exchange.oi_publication_lag_s)  # the allowance is conservative against the fake
    exchange.now = at(16, 45) + timedelta(seconds=exchange.oi_publication_lag_s + 1)  # served, but inside the allowance
    served = exchange.client().open_interest_history(coin='BTCUSDT', period='15m', start=at(16, 30), end=at(16, 45))
    assert [r.poll_time for r in served.records] == [at(16, 30)]  # the 16:45 sample is dropped...
    assert served.dropped_unclosed == 1  # ...and counted, not silently lost
    exchange.now = at(16, 45) + lag
    exchange_client = exchange.client()
    assert [r.poll_time for r in exchange_client.open_interest_history(coin='BTCUSDT', period='15m', start=at(16, 30), end=at(16, 45)).records] == [at(16, 30), at(16, 45)]


def test_latest_open_interest_poll_is_the_newest_boundary_that_is_already_knowable():
    client = FakeExchange().client()
    lag = client.open_interest_lag
    assert client.latest_open_interest_poll('15m', at(16, 45) + lag) == at(16, 45)
    assert client.latest_open_interest_poll('15m', at(16, 45) + lag - timedelta(milliseconds=1)) == at(16, 30)
    assert client.latest_open_interest_poll('1h', at(17, 0) + lag - timedelta(seconds=1)) == at(16, 0)
    assert client.latest_open_interest_poll('4h', at(16, 0) + lag) == at(16, 0)


def test_an_unclamped_old_start_is_what_binance_refuses():
    from app.worker.feeds.http import BinanceAPIError

    client = FakeExchange().client()
    with pytest.raises(BinanceAPIError) as caught:
        client.open_interest_history(coin='BTCUSDT', period='1h', start=NOW - timedelta(days=40), end=NOW - timedelta(days=39))
    assert caught.value.code == -1130


def test_a_recent_start_is_not_clamped():
    start, clamped = clamp_open_interest_start(NOW - timedelta(days=3), NOW)
    assert clamped is False and start == NOW - timedelta(days=3)
