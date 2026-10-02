"""feed_health: the per-feed staleness limits, their derivation, and the stand-down query."""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.models.feeds import FeedHealth
from app.worker.feeds import health, store, sync
from ingest_helpers import NOW, FakeExchange, at, make_settings
from test_ingest_store import candle, funding, oi

UTC = timezone.utc
SETTINGS = make_settings(watchlist='BTCUSDT')  # poll 60/120/300/60, grace 120, funding interval 8h
COINS = ('BTCUSDT',)


def spec(feed, coin='BTCUSDT'):
    return {(s.feed, s.coin): s for s in health.expected_feeds(SETTINGS, COINS)}[(feed, coin)]


# ---------------------------------------------------------------------------
# the limits (period + poll interval + grace)
# ---------------------------------------------------------------------------
def test_the_limit_is_period_plus_poll_plus_grace():
    assert health.staleness_limit_seconds(period_seconds=900, poll_seconds=60, grace_seconds=120) == 1080
    with pytest.raises(ValueError):
        health.staleness_limit_seconds(period_seconds=-1, poll_seconds=0, grace_seconds=0)


@pytest.mark.parametrize(
    'feed, limit',
    [
        ('binance_kline_perp_15m', 900 + 60 + 120),
        ('binance_kline_spot_15m', 900 + 60 + 120),
        ('binance_kline_perp_1h', 3600 + 60 + 120),
        ('binance_kline_spot_4h', 14400 + 60 + 120),
        ('binance_open_interest_15m', 900 + 120 + 120),
        ('binance_open_interest_1h', 3600 + 120 + 120),
        ('binance_open_interest_4h', 14400 + 120 + 120),
        ('binance_funding_settled', 8 * 3600 + 300 + 120),
        ('binance_funding_predicted', 0 + 60 + 120),
    ],
)
def test_each_feeds_limit_is_derived_from_its_own_cadence(feed, limit):
    assert spec(feed).max_staleness_seconds == limit


def test_limits_move_with_configuration_not_with_constants():
    slower = make_settings(watchlist='BTCUSDT', feed_kline_poll_seconds=120, feed_staleness_grace_seconds=300)
    limits = {(s.feed, s.coin): s.max_staleness_seconds for s in health.expected_feeds(slower, COINS)}
    assert limits[('binance_kline_perp_15m', 'BTCUSDT')] == 900 + 120 + 300


def test_every_gating_feed_has_a_numeric_limit_and_the_force_order_feed_has_none():
    specs = health.expected_feeds(SETTINGS, COINS)
    gating = [s for s in specs if s.gating]
    assert all(isinstance(s.max_staleness_seconds, int) and s.max_staleness_seconds > 0 for s in gating)
    force = [s for s in specs if s.feed == health.FEED_FORCE_ORDER]
    assert len(force) == 1 and force[0].gating is False and force[0].max_staleness_seconds is None and force[0].coin is None


def test_the_feed_set_covers_both_markets_every_timeframe_and_every_coin():
    two = health.expected_feeds(make_settings(watchlist='BTCUSDT,ETHUSDT'))
    kline_feeds = {s.feed for s in two if s.feed.startswith('binance_kline_')}
    assert kline_feeds == {f'binance_kline_{m}_{t}' for m in ('perp', 'spot') for t in ('15m', '1h', '4h')}
    assert len([s for s in two if s.coin == 'ETHUSDT']) == len([s for s in two if s.coin == 'BTCUSDT'])


def test_staleness_seconds_is_whole_seconds_since_available_at():
    assert health.staleness_seconds(at(16, 30), NOW) == 20 * 60 + 30
    assert health.staleness_seconds(None, NOW) is None
    assert health.staleness_seconds(NOW + timedelta(seconds=5), NOW) == 0  # never negative


@pytest.mark.parametrize(
    'staleness, limit, failing, expected',
    [
        (None, 100, False, 'unknown'),  # never produced a row
        (50, None, False, 'unknown'),  # no rule: NOT fresh
        (100, 100, False, 'ok'),  # exactly at the limit is still inside it
        (101, 100, False, 'stale'),
        (101, 100, True, 'down'),
        (10, 100, True, 'ok'),  # a failed fetch while data is still in date is not yet an outage
    ],
)
def test_classification(staleness, limit, failing, expected):
    assert health.classify(staleness=staleness, limit=limit, last_attempt_failed=failing) == expected


# ---------------------------------------------------------------------------
# computing it from stored data
# ---------------------------------------------------------------------------
def load_fresh_btc(session, *, newest_15m_open=at(16, 30)):
    """Everything BTC needs to be healthy at NOW (16:50:30)."""
    for market in ('perp', 'spot'):
        store.write_candles(session, [candle(newest_15m_open, market=market)], now=NOW)
        store.write_candles(session, [candle(at(15, 0), market=market, timeframe='1h', close_time=at(16, 0), available_at=at(16, 0))], now=NOW)
        store.write_candles(session, [candle(at(12, 0), market=market, timeframe='4h', close_time=at(16, 0), available_at=at(16, 0))], now=NOW)
    for period in ('15m', '1h', '4h'):
        store.write_open_interest(session, [oi(at(16, 45) if period == '15m' else at(16, 0), timeframe=period, available_at=at(16, 45) if period == '15m' else at(16, 0))], now=NOW)
    store.write_funding(session, [funding('settled', at(16, 0))], now=NOW)
    store.write_funding(session, [funding('predicted', at(0, 0, day=3), available_at=NOW - timedelta(seconds=30))], now=NOW)
    session.commit()


def test_a_fully_fresh_set_of_feeds_is_ok(session):
    load_fresh_btc(session)
    statuses = health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)
    assert {s.status for s in statuses} == {'ok'}
    assert len(statuses) == 6 + 3 + 2


def test_a_late_bar_makes_exactly_that_series_stale(session):
    load_fresh_btc(session, newest_15m_open=at(16, 15))  # the 16:30 close is missing; 16:45 is not due yet
    # The 16:15 bar closed (became available) at 16:30; 1081s later it is 1s past its 1080s limit.
    now = at(16, 30) + timedelta(seconds=1081)
    statuses = {(s.feed, s.coin): s for s in health.refresh_feed_health(session, now=now, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)}
    assert statuses[('binance_kline_perp_15m', 'BTCUSDT')].status == 'stale'
    assert statuses[('binance_kline_perp_15m', 'BTCUSDT')].staleness_seconds == 1081
    assert statuses[('binance_kline_perp_15m', 'BTCUSDT')].max_staleness_seconds == 1080


def test_one_second_inside_the_limit_is_still_ok(session):
    load_fresh_btc(session, newest_15m_open=at(16, 15))
    now = at(16, 30) + timedelta(seconds=1080)
    statuses = {(s.feed, s.coin): s for s in health.refresh_feed_health(session, now=now, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)}
    assert statuses[('binance_kline_perp_15m', 'BTCUSDT')].status == 'ok'


def test_a_feed_with_no_rows_is_unknown(session):
    statuses = health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)
    assert {s.status for s in statuses} == {'unknown'}
    assert all(s.last_available_at is None and s.staleness_seconds is None for s in statuses)


def test_stale_and_failing_fetches_is_down_and_the_error_is_kept(session):
    load_fresh_btc(session, newest_15m_open=at(16, 15))
    attempts = health.AttemptLog()
    attempts.error('binance_kline_perp_15m', 'BTCUSDT', NOW, 'BinanceUnavailable: boom')
    now = at(16, 30) + timedelta(seconds=2000)
    statuses = {(s.feed, s.coin): s for s in health.refresh_feed_health(session, now=now, attempts=attempts, settings=SETTINGS, coins=COINS)}
    perp = statuses[('binance_kline_perp_15m', 'BTCUSDT')]
    assert perp.status == 'down' and 'boom' in perp.detail
    assert statuses[('binance_kline_spot_15m', 'BTCUSDT')].status == 'stale'  # late, but nothing is failing


def test_refreshing_twice_updates_rows_in_place(session):
    load_fresh_btc(session)
    for _ in range(2):
        health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)
    rows = session.execute(select(func.count()).select_from(FeedHealth)).scalar_one()
    assert rows == 11
    row = session.execute(select(FeedHealth).where(FeedHealth.feed == 'binance_kline_perp_15m')).scalar_one()
    assert row.last_available_at == at(16, 45) and row.last_print_at == at(16, 30)
    assert row.max_staleness_seconds == 1080 and row.status == 'ok' and row.rows_last_window == 1


def test_attempt_history_survives_a_refresh_that_has_no_new_attempt(session):
    load_fresh_btc(session)
    attempts = health.AttemptLog()
    attempts.success('binance_kline_perp_15m', 'BTCUSDT', at(16, 49))
    health.refresh_feed_health(session, now=NOW, attempts=attempts, settings=SETTINGS, coins=COINS)
    health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)  # fresh process, no memory
    row = session.execute(select(FeedHealth).where(FeedHealth.feed == 'binance_kline_perp_15m')).scalar_one()
    assert row.last_success_at == at(16, 49)


# ---------------------------------------------------------------------------
# the stand-down query
# ---------------------------------------------------------------------------
def test_no_breach_when_everything_is_fresh(session):
    load_fresh_btc(session)
    health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)
    assert health.stand_down_breaches(session, NOW, SETTINGS, COINS) == []


def test_a_feed_that_has_never_run_is_a_breach(session):
    breaches = health.stand_down_breaches(session, NOW, SETTINGS, COINS)
    assert {b.feed for b in breaches} == {s.feed for s in health.expected_feeds(SETTINGS, COINS) if s.gating}
    assert all('never run' in b.reason for b in breaches)


def test_a_dead_worker_is_caught_by_the_clock_even_though_its_last_status_said_ok(session):
    load_fresh_btc(session)
    health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)
    assert session.execute(text("select count(*) from feed_health where status <> 'ok'")).scalar_one() == 0
    # The worker dies. Nothing updates feed_health again. An hour later:
    later = NOW + timedelta(hours=1)
    breaches = health.stand_down_breaches(session, later, SETTINGS, COINS)
    names = {b.feed for b in breaches}
    assert 'binance_kline_perp_15m' in names and 'binance_funding_predicted' in names and 'binance_open_interest_15m' in names
    assert 'binance_kline_perp_4h' not in names  # a 4h bar is allowed to be that old
    assert 'binance_funding_settled' not in names  # an 8h-cadence feed is fine after one hour
    assert all(b.staleness_seconds > b.max_staleness_seconds for b in breaches)
    # every status in the table still reads 'ok': proof that reading status alone would have been wrong
    assert session.execute(text("select count(*) from feed_health where status <> 'ok'")).scalar_one() == 0


def test_a_null_limit_is_a_breach_never_fresh(session):
    load_fresh_btc(session)
    health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)
    session.execute(text("update feed_health set max_staleness_seconds = NULL where feed = 'binance_kline_perp_15m'"))
    breaches = health.stand_down_breaches(session, NOW, SETTINGS, COINS)
    assert [b.reason for b in breaches] == ['no staleness rule configured']


def test_the_force_order_feed_never_triggers_the_stand_down(session):
    load_fresh_btc(session)
    health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)
    health.write_force_order_health(session, now=NOW, connected=False, last_traded_at=None, last_available_at=None, rows=0, window_minutes=60, error='down')
    assert health.stand_down_breaches(session, NOW + timedelta(days=3), SETTINGS, COINS) != []  # other feeds went stale...
    assert health.FEED_FORCE_ORDER not in {b.feed for b in health.stand_down_breaches(session, NOW + timedelta(days=3), SETTINGS, COINS)}


def test_force_order_health_is_one_row_updated_in_place_and_marks_the_estimate(session):
    health.write_force_order_health(session, now=NOW, connected=True, last_traded_at=at(16, 49), last_available_at=at(16, 49), rows=3, window_minutes=60)
    health.write_force_order_health(session, now=NOW + timedelta(seconds=60), connected=False, last_traded_at=at(16, 49), last_available_at=at(16, 49), rows=3, window_minutes=60, error='reset')
    rows = session.execute(select(FeedHealth).where(FeedHealth.feed == health.FEED_FORCE_ORDER)).scalars().all()
    assert len(rows) == 1  # coin IS NULL still hits the NULLS NOT DISTINCT key
    row = rows[0]
    assert row.coin is None and row.status == 'down' and row.max_staleness_seconds is None
    assert row.last_success_at == NOW and row.last_error_at == NOW + timedelta(seconds=60)
    assert 'ESTIMATE' in row.detail and 'shadow' in row.detail and 'cannot be backfilled' in row.detail


def test_refresh_after_a_real_sync_reads_what_the_sync_stored(session):
    exchange = FakeExchange()
    client = exchange.client()
    sync.sync_klines(session, client, coin='BTCUSDT', market='perp', timeframe='15m', start=at(14, 0), end=at(17, 0))
    statuses = {(s.feed, s.coin): s for s in health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS)}
    perp = statuses[('binance_kline_perp_15m', 'BTCUSDT')]
    assert perp.status == 'ok' and perp.last_available_at == at(16, 45) and perp.last_print_at == at(16, 30)
    assert statuses[('binance_kline_spot_15m', 'BTCUSDT')].status == 'unknown'


def test_the_open_interest_limit_does_not_include_the_publication_lag_because_available_at_already_does():
    """Rows are one period apart in available_at and fetched within one poll, whatever the lag is."""
    for lag in (0, 90, 300, 900):
        settings = make_settings(watchlist='BTCUSDT', feed_open_interest_publication_lag_seconds=lag)
        limits = {s.feed: s for s in health.expected_feeds(settings, COINS)}
        for period, seconds in (('15m', 900), ('1h', 3600), ('4h', 14400)):
            spec = limits[f'binance_open_interest_{period}']
            # worst healthy age: the next row's available_at (= this one's + period) plus one poll interval
            assert spec.max_staleness_seconds == seconds + 120 + 120
            worst_healthy_age = seconds + spec.poll_seconds
            assert worst_healthy_age <= spec.max_staleness_seconds


def test_open_interest_is_judged_from_available_at_not_from_the_boundary(session):
    lag = timedelta(seconds=make_settings().feed_open_interest_publication_lag_seconds)
    store.write_open_interest(session, [oi(at(16, 30), timeframe='15m', available_at=at(16, 30) + lag)], now=NOW)
    specs = [s for s in health.expected_feeds(SETTINGS, COINS) if s.feed == 'binance_open_interest_15m']
    (status,) = [s for s in health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=SETTINGS, coins=COINS) if s.feed == 'binance_open_interest_15m']
    assert status.last_print_at == at(16, 30) and status.last_available_at == at(16, 30) + lag
    assert status.staleness_seconds == int((NOW - at(16, 30) - lag).total_seconds())
    assert specs