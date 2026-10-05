"""Deribit ingest: parsing from a fixture, availability, the client (mock transport, no network), storage, health, the job.

Nothing here touches the network: the transport is ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.core import constants as C
from app.core.features.series import Bar
from app.core.features.stats import trailing_percentile
from app.core.features.volatility import yang_zhang_vol
from app.core.options.book import PAYLOAD_INSTRUMENT_KEYS, book_from_payload
from app.models.feeds import Candle, EventCalendar, FeedHealth
from app.models.options import OptionsMetric, OptionsSnapshot
from app.worker import options_ingest as OI
from app.worker.feeds import availability, health
from app.worker.feeds.deribit import DeribitClient, DeribitError, SnapshotRecord, parse_book_summary
from app.worker.feeds.http import BinanceAPIError, BinanceHttp, RetryPolicy
from app.worker.feeds.records import from_ms, to_ms
from app.worker.jobs import OPTIONS_JOBS, JobOutcome, WorkerContext, job_deribit_book, options_job_intervals
from app.worker.scheduler import build_scheduler
from ingest_helpers import make_settings
from tests.options_helpers import LAG, deribit_row, expiry, record, response

UTC = timezone.utc
FIXTURE = Path(__file__).parent / 'fixtures' / 'deribit_book_summary_btc.json'
SNAP = from_ms(1772452740008)  # the fixture's median creation_timestamp: 2026-03-02 11:59:00.008
FETCHED = SNAP + timedelta(seconds=1)


def fixture_body() -> dict:
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def parsed(**kwargs) -> SnapshotRecord:
    return parse_book_summary(fixture_body(), 'BTC', fetched_at=kwargs.pop('fetched_at', FETCHED), publication_lag=kwargs.pop('lag', LAG))


# ---------------------------------------------------------------------------
# parsing a response shaped like the real endpoint
# ---------------------------------------------------------------------------
def test_the_fixture_parses_into_a_snapshot_record():
    r = parsed()
    assert (r.coin, r.currency) == ('BTCUSDT', 'BTC')  # Binance symbol in every table, Deribit currency on the wire
    assert r.snapshot_time == SNAP  # lower median of the ten accepted creation_timestamps
    assert r.index_price == 96510.2  # estimated_delivery_price: the spot index
    assert r.instrument_count == 10 and r.rejected == 4
    assert r.total_open_interest == 335.0
    assert r.notes == ['4 of 14 rows rejected']


def test_the_payload_keeps_only_the_needed_keys_and_drops_zero_open_interest():
    payload = parsed().payload
    assert set(payload) == {'currency', 'index_price', 'instruments'}
    rows = payload['instruments']
    assert len(rows) == 9  # the zero-OI call is counted in instrument_count but not stored
    assert all(set(row) == set(PAYLOAD_INSTRUMENT_KEYS) for row in rows)  # no bid/ask/volume/high/low
    assert all(row['open_interest'] > 0 for row in rows)
    assert [row['instrument_name'] for row in rows] == sorted(row['instrument_name'] for row in rows)  # input order never reaches storage
    assert 'BTC-3MAR26-99000-C' not in {row['instrument_name'] for row in rows}


def test_the_stored_payload_rebuilds_the_book_with_iv_as_a_fraction():
    r = parsed()
    book = book_from_payload(r.payload, r.snapshot_time)
    assert book.currency == 'BTC' and book.index_price == 96510.2 and book.total_open_interest == 335.0
    call = next(q for q in book.quotes if q.name == 'BTC-3MAR26-96500-C')
    assert call.iv == pytest.approx(0.50) and call.forward == 96523.1 and call.expiry_time == expiry(3, 3)


def test_a_row_the_parser_rejects_is_counted_and_never_stored():
    names = {row['instrument_name'] for row in parsed().payload['instruments']}
    assert not names & {'ETH-3MAR26-2000-C', 'BTC-PERPETUAL', 'BTC-3MAR26-97500-C', 'BTC-3MAR26-97600-C'}


@pytest.mark.parametrize(
    'body',
    [{}, {'result': []}, {'result': 'nope'}, {'error': {'code': 10009, 'message': 'x'}}, {'result': [{'instrument_name': 'BTC-PERPETUAL'}]}, [1, 2]],
)
def test_a_response_with_no_valid_row_is_an_error_not_an_empty_book(body):
    with pytest.raises(DeribitError):
        parse_book_summary(body, 'BTC', fetched_at=FETCHED, publication_lag=LAG)


@pytest.mark.parametrize(
    'override',
    [{'open_interest': '5'}, {'open_interest': True}, {'underlying_price': 0}, {'estimated_delivery_price': None}, {'creation_timestamp': 1.5}, {'creation_timestamp': 0}, {'open_interest': float('nan')}],
)
def test_malformed_numbers_are_rejected_row_by_row(override):
    good = deribit_row('BTC', expiry(3, 3), 100, 'call', 5.0)
    bad = deribit_row('BTC', expiry(3, 3), 110, 'call', 5.0, **override)
    r = record([good, bad])
    assert r.rejected == 1 and r.instrument_count == 1


def test_one_stale_or_future_row_cannot_move_the_snapshot_instant():
    rows = [deribit_row('BTC', expiry(3, 3), 100 + i, 'call', 1.0, created=SNAP) for i in range(5)]
    rows.append(deribit_row('BTC', expiry(3, 3), 200, 'call', 1.0, created=SNAP - timedelta(hours=3)))
    rows.append(deribit_row('BTC', expiry(3, 3), 210, 'call', 1.0, created=SNAP + timedelta(hours=3)))
    assert record(rows, snapshot=None, fetched_at=SNAP + timedelta(seconds=1)).snapshot_time == SNAP


def test_an_even_number_of_rows_uses_the_lower_median():
    rows = [deribit_row('BTC', expiry(3, 3), 100, 'call', 1.0, created=SNAP), deribit_row('BTC', expiry(3, 3), 110, 'call', 1.0, created=SNAP + timedelta(milliseconds=40))]
    assert record(rows, fetched_at=SNAP + timedelta(seconds=1)).snapshot_time == SNAP


def test_the_other_currency_is_not_mixed_in():
    btc_only = response([deribit_row('BTC', expiry(3, 3), 100, 'call', 5.0)])
    with pytest.raises(DeribitError):
        parse_book_summary(btc_only, 'ETH', fetched_at=FETCHED, publication_lag=LAG)  # no ETH row: not an empty ETH book
    r = parse_book_summary(response([deribit_row('ETH', expiry(3, 3), 2000, 'call', 5.0, index=2049.0)]), 'ETH', fetched_at=FETCHED, publication_lag=LAG)
    assert r.coin == 'ETHUSDT' and r.payload['currency'] == 'ETH'

# ---------------------------------------------------------------------------
# available_at: measured lag, floored at the moment we received the book
# ---------------------------------------------------------------------------
def test_available_at_is_the_later_of_snapshot_plus_lag_and_the_time_we_received_it():
    fn = availability.options_snapshot_available_at
    assert fn(SNAP, SNAP + timedelta(seconds=1), LAG) == SNAP + LAG  # received quickly: the lag margin applies
    assert fn(SNAP, SNAP + timedelta(seconds=40), LAG) == SNAP + timedelta(seconds=40)  # received late: never earlier than that
    assert fn(SNAP, SNAP, timedelta(0)) == SNAP
    with pytest.raises(ValueError):
        fn(SNAP, SNAP, timedelta(seconds=-1))


def test_the_parsed_record_uses_the_rule():
    assert parsed().available_at == SNAP + LAG
    assert parsed(fetched_at=SNAP + timedelta(minutes=2)).available_at == SNAP + timedelta(minutes=2)
    assert parsed().available_at >= parsed().snapshot_time


def test_the_lag_is_configuration_not_a_rule_constant():
    assert Settings(_env_file=None).feed_deribit_publication_lag_seconds == 10
    assert not any('DERIBIT' in name or 'PUBLICATION' in name for name in dir(C) if name.isupper())  # never enters rule_hash
    with pytest.raises(ValueError):
        Settings(_env_file=None, feed_deribit_publication_lag_seconds=-1)


def test_deribit_config_defaults_and_bounds():
    s = Settings(_env_file=None)
    assert (s.deribit_url, s.deribit_enabled, s.feed_deribit_poll_seconds, s.deribit_write_expiry_calendar) == ('https://www.deribit.com', True, 300, False)
    for bad in (0, 29, 301):
        with pytest.raises(ValueError):
            Settings(_env_file=None, feed_deribit_poll_seconds=bad)


# ---------------------------------------------------------------------------
# the client, on a mock transport
# ---------------------------------------------------------------------------
def make_client(handler, **settings) -> tuple[DeribitClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = BinanceHttp(
        'https://deribit.test',
        weight_limit_1m=120,
        client=httpx.Client(transport=httpx.MockTransport(wrapped)),
        retry=RetryPolicy(max_attempts=1, base_delay=0, jitter=0),
    )
    return DeribitClient(http=http, settings=make_settings(**settings), clock=lambda: FETCHED), seen


def test_the_client_makes_one_unauthenticated_get_for_the_option_book():
    client, seen = make_client(lambda request: httpx.Response(200, json=fixture_body()))
    r = client.book_summary('BTC')
    (request,) = seen
    assert request.method == 'GET' and request.content == b''
    assert request.url.path == '/api/v2/public/get_book_summary_by_currency'
    assert dict(request.url.params) == {'currency': 'BTC', 'kind': 'option'}
    assert not {'authorization', 'x-mbx-apikey', 'deribit-client-id'} & {k.lower() for k in request.headers}
    assert r.snapshot_time == SNAP and r.fetched_at == FETCHED and r.available_at == SNAP + LAG


def test_the_client_uses_the_configured_lag():
    client, _ = make_client(lambda request: httpx.Response(200, json=fixture_body()), feed_deribit_publication_lag_seconds=45)
    assert client.book_summary('BTC').available_at == SNAP + timedelta(seconds=45)


def test_the_client_has_no_book_for_a_coin_deribit_does_not_list():
    client, seen = make_client(lambda request: httpx.Response(200, json=fixture_body()))
    with pytest.raises(KeyError):
        client.book_summary('SOL')
    assert seen == []  # not even a request


def test_an_http_error_and_a_json_rpc_error_both_surface_and_store_nothing():
    client, _ = make_client(lambda request: httpx.Response(400, json={'error': {'code': 10001, 'message': 'bad'}}))
    with pytest.raises(BinanceAPIError):
        client.book_summary('BTC')
    client, _ = make_client(lambda request: httpx.Response(200, json={'error': {'code': 13004, 'message': 'invalid'}}))
    with pytest.raises(DeribitError):
        client.book_summary('BTC')


# ---------------------------------------------------------------------------
# storage and the derived metrics
# ---------------------------------------------------------------------------
def add_perp_candles(session, coin='BTCUSDT', *, hours=80, last_close=datetime(2026, 3, 2, 11, 0, tzinfo=UTC), extra_open=None):
    """Hourly (and 4h) perp candles closing at or before ``last_close``; optionally one more bar that closes later."""
    bars: list[Bar] = []
    price = 96000.0
    for i in range(hours):
        opened = last_close - timedelta(hours=hours - i)
        o, c = price, price * (1.0 + (0.004 if i % 2 == 0 else -0.003))
        bars.append(Bar(opened, opened + timedelta(hours=1), o, max(o, c) * 1.002, min(o, c) * 0.998, c, 10.0, 5.0, opened + timedelta(hours=1)))
        price = c
    if extra_open is not None:
        o, c = price, price * 1.01
        bars.append(Bar(extra_open, extra_open + timedelta(hours=1), o, c * 1.002, o * 0.998, c, 10.0, 5.0, extra_open + timedelta(hours=1)))
    for bar in bars:
        session.add(Candle(coin=coin, market='perp', timeframe='1h', open_time=bar.open_time, close_time=bar.close_time, open_price=Decimal(repr(bar.open)), high_price=Decimal(repr(bar.high)), low_price=Decimal(repr(bar.low)), close_price=Decimal(repr(bar.close)), base_volume=Decimal('10'), taker_buy_base_volume=Decimal('5'), available_at=bar.available_at))
    four = last_close.replace(hour=(last_close.hour // 4) * 4)
    price = 95000.0
    for i in range(30):
        opened = four - timedelta(hours=4 * (30 - i))
        c = price * (1.01 if i % 2 == 0 else 0.995)
        high, low = max(price, c) * 1.004, min(price, c) * 0.996
        session.add(Candle(coin=coin, market='perp', timeframe='4h', open_time=opened, close_time=opened + timedelta(hours=4), open_price=Decimal(repr(price)), high_price=Decimal(repr(high)), low_price=Decimal(repr(low)), close_price=Decimal(repr(c)), base_volume=Decimal('10'), taker_buy_base_volume=Decimal('5'), available_at=opened + timedelta(hours=4)))
        price = c
    session.flush()
    return bars


def metrics_of(session, snapshot_id):
    rows = session.execute(select(OptionsMetric).where(OptionsMetric.snapshot_id == snapshot_id)).scalars().all()
    return {m.horizon: m for m in rows}


def test_ingest_stores_the_snapshot_and_one_derived_row_per_horizon(session):
    bars = add_perp_candles(session)
    r = parsed()
    result = OI.ingest_snapshot(session, r)
    assert result.inserted and result.metrics_written == 3 and result.calendar_written == 0
    snap = session.get(OptionsSnapshot, result.snapshot_id)
    assert (snap.venue, snap.coin, snap.snapshot_time, snap.available_at) == ('deribit', 'BTCUSDT', SNAP, SNAP + LAG)
    assert (snap.instrument_count, snap.total_open_interest) == (10, Decimal('335'))
    assert snap.payload == r.payload

    by_horizon = metrics_of(session, snap.id)
    assert set(by_horizon) == set(C.HORIZONS)
    swing = by_horizon['swing']
    assert swing.available_at == snap.available_at and swing.snapshot_time == SNAP  # a figure is known when its book is
    assert swing.expiry_time == expiry(3, 3)
    assert float(swing.hours_to_expiry) == pytest.approx(20.0 + 59.992 / 3600.0, abs=1e-4)
    assert swing.expiry_open_interest == Decimal('115') and swing.near_dated_open_interest == Decimal('235') and swing.total_open_interest == Decimal('335')
    assert swing.max_pain_strike == Decimal('96500')  # worked by hand: pain(96500) = 0
    assert float(swing.top_strike_oi_share) == pytest.approx(70 / 115)
    assert float(swing.atm_iv) == pytest.approx(0.51)  # (call 50% + put 52%) / 2 at the strike nearest the 96,523.1 forward
    assert swing.dealer_gamma_estimated is True and swing.dealer_gamma_proxy == C.OPTIONS_GAMMA_PROXY
    assert swing.nearest_path_strike is None and swing.nearest_path_strike_distance_atr is None  # recorded on the candidate, not here
    assert by_horizon['position'].expiry_time == expiry(5, 3) and by_horizon['scalp'].expiry_time == expiry(3, 3)


def test_realised_vol_and_atr_come_from_the_perp_candles_visible_at_the_snapshot(session):
    bars = add_perp_candles(session, extra_open=datetime(2026, 3, 2, 11, 0, tzinfo=UTC))  # a bar that closes at 12:00, after the book
    result = OI.ingest_snapshot(session, parsed())
    swing = metrics_of(session, result.snapshot_id)['swing']
    visible = [b for b in bars if b.available_at <= SNAP + LAG]
    assert len(visible) == len(bars) - 1
    expected = yang_zhang_vol(visible[-25:], timedelta(hours=1))  # ~20h to expiry -> max(24, ceil(20.02)) = 24 bars + the reference
    assert float(swing.same_tenor_rv) == pytest.approx(expected, abs=1e-6)
    assert float(swing.iv_minus_rv) == pytest.approx(0.51 - expected, abs=1e-8)
    assert swing.atr_price_used is not None and swing.atr_price_used > 0


def test_flip_distance_is_in_atr_units_signed_index_minus_flip(session, monkeypatch):
    add_perp_candles(session)
    from app.core.options import metrics as M

    real = M.derive_horizon_metrics

    def with_flip(*args, **kwargs):
        from dataclasses import replace

        return replace(real(*args, **kwargs), gamma_flip_price=96000.0, dealer_gamma_sign=-1)

    monkeypatch.setattr(OI.M, 'derive_horizon_metrics', with_flip)
    result = OI.ingest_snapshot(session, parsed())
    swing = metrics_of(session, result.snapshot_id)['swing']
    assert float(swing.gamma_flip_distance_atr) == pytest.approx((96510.2 - 96000.0) / float(swing.atr_price_used), abs=1e-5)


def test_without_candles_the_perp_dependent_columns_are_null_not_zero(session):
    result = OI.ingest_snapshot(session, parsed())
    swing = metrics_of(session, result.snapshot_id)['swing']
    assert swing.same_tenor_rv is None and swing.iv_minus_rv is None and swing.iv_minus_rv_pct is None
    assert swing.atr_price_used is None and swing.gamma_flip_distance_atr is None
    assert any('no ATR' in note for note in result.notes)
    assert swing.atm_iv is not None  # the book-only figures are still there


def test_ingesting_the_same_snapshot_twice_changes_nothing(session):
    OI.ingest_snapshot(session, parsed())
    again = OI.ingest_snapshot(session, parsed(fetched_at=FETCHED + timedelta(seconds=5)))
    assert again.inserted is False and again.metrics_written == 0 and again.notes == ['snapshot already stored']
    assert session.scalar(select(func.count()).select_from(OptionsSnapshot)) == 1
    assert session.scalar(select(func.count()).select_from(OptionsMetric)) == 3
    assert session.scalar(select(OptionsSnapshot.available_at)) == SNAP + LAG  # the first arrival's availability stands


def test_the_calendar_row_is_opt_in_because_it_changes_what_event_flag_means(session):
    OI.ingest_snapshot(session, parsed())
    assert session.scalar(select(func.count()).select_from(EventCalendar)) == 0  # default: no calendar row at all


def test_the_expiry_calendar_marks_large_expiries_by_open_interest_share(session):
    result = OI.ingest_snapshot(session, parsed(), write_calendar=True)
    assert result.calendar_written == 3  # 3 MAR, 4 MAR, 5 MAR settle within 72h; 6 MAR does not
    rows = {r.event_time: r for r in session.execute(select(EventCalendar)).scalars()}
    assert set(rows) == {expiry(3, 3), expiry(4, 3), expiry(5, 3)}
    assert all(r.kind == 'expiry' and r.coin == 'BTCUSDT' and r.feed_source == 'deribit' and r.available_at == SNAP + LAG for r in rows.values())
    assert rows[expiry(3, 3)].is_large is True  # 115 of 335 = 34%
    assert rows[expiry(4, 3)].is_large is True  # 20 of 335 = 6.0% >= 5%
    assert rows[expiry(3, 3)].notional_usd == Decimal(repr(115.0 * 96510.2))
    again = OI.ingest_snapshot(session, parsed(), write_calendar=True)
    assert again.calendar_written == 0


# ---------------------------------------------------------------------------
# the 90-day percentile history
# ---------------------------------------------------------------------------
def test_history_is_thinned_to_one_sample_per_hour_and_hides_late_arrivals(session):
    start = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    for i in range(24):  # every 5 minutes for two hours
        taken = start + timedelta(minutes=5 * i)
        OI.ingest_snapshot(session, record([deribit_row('BTC', expiry(3, 3), 100, 'call', 5.0, forward=96523.1)], snapshot=taken, fetched_at=taken + timedelta(seconds=1)))
    late = start + timedelta(hours=2, minutes=5)
    OI.ingest_snapshot(session, record([deribit_row('BTC', expiry(3, 3), 100, 'call', 5.0, forward=96523.1)], snapshot=late, fetched_at=late + timedelta(minutes=30)))
    now = start + timedelta(hours=3)
    kwargs = dict(at=now, snapshot_time=now)
    samples = OI.history_samples(session, 'BTCUSDT', 'swing', 'atm_iv', **kwargs)
    assert [s.time for s in samples] == [start, start + timedelta(hours=1), late]  # the first sample of each hour
    visible = OI.history_samples(session, 'BTCUSDT', 'swing', 'atm_iv', at=late + timedelta(minutes=10), snapshot_time=now)
    assert late not in [s.time for s in visible]  # it was only received 30 minutes after it was taken
    assert late in [s.time for s in OI.history_samples(session, 'BTCUSDT', 'swing', 'atm_iv', at=late + timedelta(minutes=31), snapshot_time=now)]
    assert OI.history_samples(session, 'ETHUSDT', 'swing', 'atm_iv', **kwargs) == []  # per coin
    assert OI.history_samples(session, 'BTCUSDT', 'swing', 'atm_iv', at=now, snapshot_time=start) == []  # strictly before the sample instant


def test_the_percentile_needs_the_production_history_and_is_a_midrank_quantile():
    from app.core.features.series import Sample

    base = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [Sample(base + timedelta(hours=6 * i), base + timedelta(hours=6 * i), float(i)) for i in range(40)]  # 0..39 over 10 days
    now = base + timedelta(hours=6 * 40)
    assert OI._percentile(samples, 30.5, snapshot_time=now, at=now) == pytest.approx(31 / 40)  # 31 of the 40 prior samples are below
    assert OI._percentile(samples, 30.0, snapshot_time=now, at=now) == pytest.approx((30 + 0.5) / 40)  # a tie counts half
    assert OI._percentile(samples[:20], 5.0, snapshot_time=now, at=now) is None  # fewer than 30 samples
    short = [Sample(base + timedelta(hours=i), base + timedelta(hours=i), float(i)) for i in range(40)]
    assert OI._percentile(short, 5.0, snapshot_time=base + timedelta(hours=40), at=base + timedelta(hours=40)) is None  # spans under 7 days
    assert OI._percentile(samples, None, snapshot_time=now, at=now) is None
    assert trailing_percentile(samples, now, 30.5, now, OI.PRODUCTION_POLICY).value == pytest.approx(31 / 40)


# ---------------------------------------------------------------------------
# feed_health registration
# ---------------------------------------------------------------------------
NOW = SNAP + timedelta(minutes=2)


def deribit_breaches(session, now, settings=None, **kwargs):
    return [b for b in health.stand_down_breaches(session, now, settings or make_settings(), include_options=True, **kwargs) if b.feed == health.FEED_DERIBIT_BOOK]


def test_the_deribit_feed_is_registered_for_btc_and_eth_only_with_the_spec_limit():
    settings = make_settings(watchlist='BTCUSDT,ETHUSDT,SOLUSDT')
    specs = [s for s in health.expected_feeds(settings, include_options=True) if s.feed == health.FEED_DERIBIT_BOOK]
    assert sorted(s.coin for s in specs) == ['BTCUSDT', 'ETHUSDT']
    assert {s.max_staleness_seconds for s in specs} == {15 * 60} and all(s.gating for s in specs)
    assert health.FEED_DERIBIT_BOOK == 'deribit_options_book'


def test_the_default_feed_set_is_unchanged_by_the_options_flag():
    settings = make_settings()
    base = health.expected_feeds(settings)
    assert all(s.feed != health.FEED_DERIBIT_BOOK for s in base)
    assert len(health.expected_feeds(settings, include_options=True)) == len(base) + 2


def test_a_deribit_feed_that_never_ran_is_a_breach_only_when_asked(session):
    assert len(deribit_breaches(session, NOW)) == 2 and all('never run' in b.reason for b in deribit_breaches(session, NOW))
    assert [b for b in health.stand_down_breaches(session, NOW, make_settings()) if b.feed == health.FEED_DERIBIT_BOOK] == []


def test_fresh_books_are_ok_and_the_limit_is_fifteen_minutes_from_available_at(session):
    for coin, currency in (('BTCUSDT', 'BTC'), ('ETHUSDT', 'ETH')):
        r = record([deribit_row(currency, expiry(3, 3), 100, 'call', 5.0)], currency=currency, snapshot=SNAP, fetched_at=FETCHED)
        OI.ingest_snapshot(session, r)
    settings = make_settings()
    statuses = health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=settings, include_options=True)
    mine = [s for s in statuses if s.feed == health.FEED_DERIBIT_BOOK]
    assert {s.coin for s in mine} == {'BTCUSDT', 'ETHUSDT'} and {s.status for s in mine} == {'ok'}
    assert all(s.max_staleness_seconds == 900 and s.last_print_at == SNAP and s.last_available_at == SNAP + LAG for s in mine)
    stored = session.execute(select(FeedHealth).where(FeedHealth.feed == health.FEED_DERIBIT_BOOK)).scalars().all()
    assert {(row.coin, row.status) for row in stored} == {('BTCUSDT', 'ok'), ('ETHUSDT', 'ok')}
    available = SNAP + LAG
    assert deribit_breaches(session, available + timedelta(seconds=900), settings) == []  # exactly at the limit
    late = deribit_breaches(session, available + timedelta(seconds=901), settings)
    assert sorted(b.coin for b in late) == ['BTCUSDT', 'ETHUSDT'] and 'limit 900s' in late[0].reason


def test_a_stale_book_is_a_gating_breach_for_the_health_rule_but_not_for_the_default_stand_down(session):
    OI.ingest_snapshot(session, parsed())
    health.refresh_feed_health(session, now=NOW, attempts=health.AttemptLog(), settings=make_settings(watchlist='BTCUSDT'), include_options=True)
    later = SNAP + timedelta(hours=1)
    assert [b.coin for b in deribit_breaches(session, later, make_settings(watchlist='BTCUSDT'))] == ['BTCUSDT']
    default = health.stand_down_breaches(session, later, make_settings(watchlist='BTCUSDT'))
    assert all(b.feed != health.FEED_DERIBIT_BOOK for b in default)  # the rules job stays Binance-only: stale options mean `ignore`


# ---------------------------------------------------------------------------
# the job and the scheduler
# ---------------------------------------------------------------------------
def make_context(client, **settings) -> WorkerContext:
    return WorkerContext(
        settings=make_settings(**settings), client=SimpleNamespace(now=lambda: NOW), session_factory=None,  # type: ignore[arg-type]
        attempts=health.AttemptLog(), code_version='testver', deribit=client,
    )


def test_the_job_stores_each_coins_book_and_a_failure_on_one_coin_keeps_the_other(session, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params['currency'] == 'ETH':
            return httpx.Response(400, json={'error': {'code': 1, 'message': 'boom'}})
        return httpx.Response(200, json=fixture_body())

    client, seen = make_client(handler)
    ctx = make_context(client)
    outcome = job_deribit_book(session, ctx)
    assert isinstance(outcome, JobOutcome) and len(seen) == 2
    assert len(outcome.errors) == 1 and 'ETHUSDT' in outcome.errors[0]
    assert outcome.detail['stored'] == 1 and outcome.rows_touched == 1 + 3
    assert session.scalar(select(func.count()).select_from(OptionsSnapshot)) == 1
    assert ctx.attempts.get(health.FEED_DERIBIT_BOOK, 'BTCUSDT').last_success_at is not None
    assert ctx.attempts.get(health.FEED_DERIBIT_BOOK, 'ETHUSDT').last_error is not None


def test_a_storage_failure_for_one_coin_rolls_back_only_that_coin(session, monkeypatch):
    client, _ = make_client(lambda request: httpx.Response(200, json=fixture_body()))
    real = OI.metric_row

    def failing(session_, record_, *args, **kwargs):
        if record_.coin == 'ETHUSDT':
            raise RuntimeError('derived-metric failure')
        return real(session_, record_, *args, **kwargs)

    # the fixture book is BTC; relabel the ETH call to the same body so both coins reach storage
    body = fixture_body()
    eth_body = json.loads(json.dumps(body).replace('BTC-', 'ETH-').replace('"BTC"', '"ETH"'))
    client, _ = make_client(lambda request: httpx.Response(200, json=eth_body if request.url.params['currency'] == 'ETH' else body))
    monkeypatch.setattr(OI, 'metric_row', failing)
    outcome = job_deribit_book(session, make_context(client))
    assert len(outcome.errors) == 1 and 'ETHUSDT' in outcome.errors[0]
    coins = set(session.scalars(select(OptionsSnapshot.coin)))
    assert coins == {'BTCUSDT'}  # ETH's snapshot insert was undone with its failed metrics


def test_the_job_is_a_no_op_without_a_client_and_skips_alts(session):
    ctx = make_context(None)
    assert job_deribit_book(session, ctx).skipped is True
    client, seen = make_client(lambda request: httpx.Response(200, json=fixture_body()))
    job_deribit_book(session, make_context(client, watchlist='SOLUSDT,BTCUSDT'))
    assert [r.url.params['currency'] for r in seen] == ['BTC']  # SOL has no Deribit book: never requested


def test_the_scheduler_registers_the_deribit_job_only_when_a_client_exists():
    without = build_scheduler(make_context(None))
    assert health.FEED_DERIBIT_BOOK not in {job.id for job in without.get_jobs()}
    client, _ = make_client(lambda request: httpx.Response(200, json=fixture_body()))
    with_client = build_scheduler(make_context(client))
    jobs = {job.id: job for job in with_client.get_jobs()}
    assert health.FEED_DERIBIT_BOOK in jobs and jobs[health.FEED_DERIBIT_BOOK].trigger.interval == timedelta(seconds=300)
    assert options_job_intervals(make_settings()) == {health.FEED_DERIBIT_BOOK: 300} and set(OPTIONS_JOBS) == {health.FEED_DERIBIT_BOOK}
    assert len({job.id for job in without.get_jobs()}) == 6  # Binance jobs + notify, unchanged Deribit gating


def test_to_ms_and_from_ms_round_trip_the_fixture_instant():
    assert to_ms(SNAP) == 1772452740008
