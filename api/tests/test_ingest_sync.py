"""Range synchronisation: resumable, idempotent, honest about what it covered."""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.worker.feeds import sync
from app.worker.feeds.http import BinanceUnavailable, GeoBlocked, RetryPolicy
from ingest_helpers import NOW, FakeExchange, at, make_settings, ms

FUNDING_LAG = timedelta(seconds=make_settings().feed_funding_publication_lag_seconds)
UTC = timezone.utc
FIFTEEN = timedelta(minutes=15)
TOLERANCE = timedelta(hours=8, minutes=5)


def kline_sync(session, client, **kwargs):
    params = dict(coin='BTCUSDT', market='perp', timeframe='15m', start=at(14, 0), end=at(16, 45))
    params.update(kwargs)
    return sync.sync_klines(session, client, **params)


def stored_opens(session, market='perp', timeframe='15m', coin='BTCUSDT'):
    rows = session.execute(
        text('select open_time from candle where coin=:c and market=:m and timeframe=:t order by open_time'),
        {'c': coin, 'm': market, 't': timeframe},
    ).scalars().all()
    return [r.astimezone(UTC) for r in rows]


# ---------------------------------------------------------------------------
# klines
# ---------------------------------------------------------------------------
def test_klines_land_with_the_right_available_at_and_no_unclosed_bar(session):
    exchange = FakeExchange()
    report = kline_sync(session, exchange.client(), end=at(17, 0))  # asks for a range running past "now"
    assert stored_opens(session) == [at(14, 0) + n * FIFTEEN for n in range(11)]  # 14:00 .. 16:30
    assert report.written == 11 and report.expected == 11 and report.stored == 11
    assert (report.leading_gap, report.interior_gap, report.trailing_gap) == (0, 0, 0)
    # the proof, in SQL: no stored bar closes after the exchange's clock, and available_at == bar close for every row
    assert session.execute(text('select count(*) from candle where close_time > :now'), {'now': NOW}).scalar_one() == 0
    assert session.execute(text('select count(*) from candle where available_at <> open_time + interval \'15 minutes\'')).scalar_one() == 0
    assert session.execute(text('select count(*) from candle where available_at > :now'), {'now': NOW}).scalar_one() == 0


def test_a_just_closed_bar_is_not_stored_while_the_exchange_still_serves_it_provisionally(session):
    # 16:45:03 - the 16:30 bar closed 3 s ago; the fake (like Binance) still serves provisional values for it.
    early = FakeExchange(now=at(16, 45) + timedelta(seconds=3))
    kline_sync(session, early.client(), end=at(16, 45))
    assert at(16, 30) not in stored_opens(session)
    assert at(16, 15) in stored_opens(session)
    # once the settle margin has passed the bar is stored with its FINAL values, never the provisional ones
    late = FakeExchange(now=at(16, 45) + timedelta(seconds=40))
    kline_sync(session, late.client(), end=at(16, 45))
    row = session.execute(text("select close_price, base_volume from candle where open_time = :t and market = 'perp' and timeframe = '15m'"), {'t': at(16, 30)}).one()
    final = FakeExchange.kline_row(ms(at(16, 30)), 900_000, sum(map(ord, 'BTCUSDT')) % 5)
    assert row.close_price == Decimal(final[4])
    assert row.base_volume == Decimal(final[5])


def test_the_forming_bar_is_never_even_requested_by_a_range_sync(session):
    exchange = FakeExchange()
    kline_sync(session, exchange.client(), end=at(17, 0))
    last_requested_end = max(int(r.url.params['endTime']) for r in exchange.kline_requests())
    assert last_requested_end == ms(at(16, 30))  # the 16:45 bar (still forming) is outside the target


def test_rerunning_the_same_backfill_writes_nothing_and_asks_for_no_klines(session):
    exchange = FakeExchange()
    client = exchange.client()
    first = kline_sync(session, client)
    requests_after_first = len(exchange.kline_requests())
    second = kline_sync(session, client)
    assert first.written > 0
    assert second.written == 0 and second.fetched == 0 and second.requests == 0
    assert second.missing_before == 0
    assert len(exchange.kline_requests()) == requests_after_first  # resumed: nothing left to fetch


def test_an_interior_hole_is_found_and_only_the_hole_is_refetched(session):
    exchange = FakeExchange()
    client = exchange.client()
    kline_sync(session, client)
    session.execute(text("delete from candle where open_time = :t"), {'t': at(15, 0)})
    session.commit()
    before = len(exchange.kline_requests())
    report = kline_sync(session, client)
    new_requests = exchange.kline_requests()[before:]
    assert report.missing_before == 1 and report.written == 1 and report.interior_gap == 0
    assert len(new_requests) == 1
    assert int(new_requests[0].url.params['startTime']) == int(new_requests[0].url.params['endTime']) == ms(at(15, 0))


def test_a_run_that_dies_between_pages_keeps_its_pages_and_resumes_after_them(session):
    exchange = FakeExchange()
    client = exchange.client(kline_page_limit=4)
    client.futures.retry = RetryPolicy(max_attempts=2, base_delay=0, jitter=0)
    failing = {'on': False}

    def flaky(request: httpx.Request):
        if failing['on'] and request.url.path == '/fapi/v1/klines' and int(request.url.params['startTime']) >= ms(at(15, 0)):
            return httpx.Response(503)
        return None

    exchange.interceptor = flaky
    failing['on'] = True
    broken = kline_sync(session, client, start=at(14, 0), end=at(16, 45))
    assert broken.error and 'BinanceUnavailable' in broken.error
    kept = stored_opens(session)
    assert kept == [at(14, 0) + n * FIFTEEN for n in range(4)]  # page 1 survived the crash (commit per page)
    assert broken.interior_gap == 0 and broken.trailing_gap > 0

    failing['on'] = False
    resumed = kline_sync(session, client, start=at(14, 0), end=at(16, 45))
    assert resumed.error is None and resumed.written == 11 - 4
    assert len(stored_opens(session)) == 11
    resume_request = [r for r in exchange.kline_requests() if r.url.path == '/fapi/v1/klines'][-2]  # not a restart from 14:00
    assert int(resume_request.url.params['startTime']) >= ms(at(15, 0))


def test_a_series_that_starts_later_than_requested_reports_a_leading_gap(session):
    exchange = FakeExchange(listing_start={'BTCUSDT': at(15, 0)})
    report = kline_sync(session, exchange.client())
    assert report.leading_gap == 4 and report.interior_gap == 0
    assert report.first_stored == at(15, 0)
    assert any('starts later than requested' in note for note in report.notes)


def test_a_range_with_no_closed_bar_is_reported_as_skipped_not_as_success(session):
    report = kline_sync(session, FakeExchange().client(), start=at(16, 45), end=at(17, 30))
    assert report.skipped and report.written == 0 and report.expected is None


def test_dry_run_reports_the_gap_and_touches_nothing(session):
    exchange = FakeExchange()
    report = kline_sync(session, exchange.client(), dry_run=True)
    assert report.missing_before == 11 and report.written == 0 and report.requests == 0
    assert exchange.kline_requests() == [] and stored_opens(session) == []


def test_spot_and_perp_are_separate_series(session):
    exchange = FakeExchange()
    client = exchange.client()
    kline_sync(session, client, market='perp')
    kline_sync(session, client, market='spot')
    assert len(stored_opens(session, 'perp')) == len(stored_opens(session, 'spot')) == 11


def test_an_unknown_symbol_is_a_reported_error_not_a_crash(session):
    report = kline_sync(session, FakeExchange().client(), coin='NOPEUSDT')
    assert report.error and 'SymbolNotFound' in report.error and report.written == 0


def test_a_geo_block_stops_the_run_instead_of_failing_every_series(session):
    exchange = FakeExchange()
    exchange.interceptor = lambda request: httpx.Response(451) if request.url.path == '/fapi/v1/klines' else None
    with pytest.raises(GeoBlocked):
        kline_sync(session, exchange.client())


def test_all_three_timeframes_sync(session):
    client = FakeExchange().client()
    for timeframe, start in (('15m', at(14, 0)), ('1h', at(8, 0)), ('4h', at(0, 0, day=1))):
        report = kline_sync(session, client, timeframe=timeframe, start=start, end=at(17, 0))
        assert report.ok and report.written > 0 and report.interior_gap == 0
    assert session.execute(text('select count(*) from candle where close_time > :now'), {'now': NOW}).scalar_one() == 0


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------
def funding_sync(session, client, **kwargs):
    params = dict(coin='BTCUSDT', start=at(0, 0, day=1), end=at(17, 0), tolerance=TOLERANCE)
    params.update(kwargs)
    return sync.sync_funding_settled(session, client, **params)


def test_settled_funding_lands_at_each_print_and_never_in_the_future(session):
    report = funding_sync(session, FakeExchange().client())
    assert report.written == 6 and report.stored == 6 and report.interior_gap == 0
    rows = session.execute(text("select funding_time, available_at, kind from funding order by funding_time")).all()
    assert all(r.available_at == r.funding_time + FUNDING_LAG and r.kind == 'settled' for r in rows)
    assert all(r.available_at <= NOW for r in rows)
    assert all(r.funding_time <= NOW for r in rows)


def test_funding_rerun_is_idempotent_and_makes_one_trailing_request(session):
    exchange = FakeExchange()
    client = exchange.client()
    funding_sync(session, client)
    before = len(exchange.requests_to('/fapi/v1/fundingRate'))
    again = funding_sync(session, client)
    assert again.written == 0 and again.requests == 1  # the empty tail answer is counted, not hidden
    assert len(exchange.requests_to('/fapi/v1/fundingRate')) - before == 1  # only the tail after the newest print


def test_funding_hole_is_detected_and_refilled(session):
    exchange = FakeExchange()
    client = exchange.client()
    funding_sync(session, client)
    session.execute(text("delete from funding where funding_time between :a and :b"), {'a': at(7, 0, day=1), 'b': at(9, 0, day=2)})
    session.commit()
    report = funding_sync(session, client)
    assert report.written >= 1 and report.interior_gap == 0 and report.stored == 6


def test_funding_that_starts_later_than_requested_says_so(session):
    exchange = FakeExchange(listing_start={'BTCUSDT': at(0, 0, day=2)})
    report = funding_sync(session, exchange.client())
    assert any('no earlier history' in note for note in report.notes)


def test_predicted_funding_is_stored_as_a_distinct_kind_available_at_observation(session):
    client = FakeExchange().client()
    report = sync.poll_predicted_funding(session, client, ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
    assert report.written == 2 and report.coins_seen == ['BTCUSDT', 'ETHUSDT']
    assert any('SOLUSDT' in note for note in report.notes)
    row = session.execute(text("select kind, funding_time, available_at from funding where coin='BTCUSDT'")).one()
    assert row.kind == 'predicted' and row.available_at < NOW < row.funding_time


# ---------------------------------------------------------------------------
# open interest
# ---------------------------------------------------------------------------
def test_open_interest_lands_at_poll_close_plus_lag_and_reports_the_30_day_clamp(session):
    exchange = FakeExchange()
    report = sync.sync_open_interest(
        session, exchange.client(), coin='BTCUSDT', period='4h', start=NOW - timedelta(days=90), end=NOW
    )
    assert any('~30 days only' in note and 'cannot be backfilled' in note for note in report.notes)
    assert report.effective_start > NOW - timedelta(days=30)
    assert report.written > 150 and report.interior_gap == 0
    lag = make_settings().feed_open_interest_publication_lag_seconds
    assert session.execute(text('select count(*) from open_interest where available_at <> poll_time + make_interval(secs => :s)'), {'s': lag}).scalar_one() == 0
    newest = session.execute(text('select max(poll_time) from open_interest')).scalar_one()
    assert newest + timedelta(seconds=lag) <= NOW  # nothing is stored before it is knowable
    oldest = session.execute(text('select min(poll_time) from open_interest')).scalar_one()
    assert NOW - oldest < timedelta(days=30)


def test_open_interest_rerun_is_idempotent(session):
    client = FakeExchange().client()
    kwargs = dict(coin='BTCUSDT', period='1h', start=NOW - timedelta(days=2), end=NOW)
    first = sync.sync_open_interest(session, client, **kwargs)
    second = sync.sync_open_interest(session, client, **kwargs)
    assert first.written > 0 and second.written == 0 and second.requests == 0


# ---------------------------------------------------------------------------
# gap arithmetic
# ---------------------------------------------------------------------------
def test_missing_runs_and_gap_classification():
    grid = sync.grid_points(at(0, 0), at(2, 0), timedelta(hours=1))  # 0,1,2
    assert sync.missing_runs(grid, set()) == [(at(0, 0), at(2, 0))]
    assert sync.missing_runs(grid, {at(0, 0), at(1, 0), at(2, 0)}) == []
    assert sync.missing_runs(grid, {at(1, 0)}) == [(at(0, 0), at(0, 0)), (at(2, 0), at(2, 0))]
    wide = sync.grid_points(at(0, 0), at(6, 0), timedelta(hours=1))  # 0..6
    assert sync.classify_gaps(wide, {at(2, 0), at(3, 0), at(5, 0)}) == (2, 1, 1)
    assert sync.classify_gaps(wide, set()) == (7, 0, 0)


def test_funding_runs_covers_leading_interior_and_trailing():
    stored = [at(8, 0), at(16, 0), at(23, 59) + timedelta(hours=8)]
    runs = sync.funding_runs(stored, at(0, 0), at(23, 0, day=3), TOLERANCE)
    assert (at(0, 0), at(8, 0) - timedelta(milliseconds=1)) not in runs  # exactly 8h to the first print: within tolerance
    assert any(first == at(16, 0) + timedelta(milliseconds=1) for first, _ in runs)  # the interior gap
    assert runs[-1][0] == stored[-1] + timedelta(milliseconds=1)  # the tail is always asked about
    assert sync.funding_runs([], at(0, 0), at(5, 0), TOLERANCE) == [(at(0, 0), at(5, 0))]


def test_unavailable_exchange_is_a_reported_error(session):
    exchange = FakeExchange()
    exchange.interceptor = lambda request: httpx.Response(500) if request.url.path == '/fapi/v1/klines' else None
    client = exchange.client()
    client.futures.retry = RetryPolicy(max_attempts=2, base_delay=0, jitter=0)
    report = kline_sync(session, client)
    assert report.error and BinanceUnavailable.__name__ in report.error
    assert report.written == 0 and report.leading_gap == 11
