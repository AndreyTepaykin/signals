"""Test doubles for the Milestone 2 ingest tests. No live network, ever.

``FakeExchange`` is an ``httpx.MockTransport`` handler that behaves like the parts
of Binance the client depends on, including the two behaviours that bite:

* the klines response ends with the IN-PROGRESS candle (``closeTime`` in the future);
* ``openInterestHist`` returns the LAST ``limit`` rows of a ``startTime``/``endTime``
  window, and rejects a ``startTime`` older than 30 days with ``-1130``.

Everything is deterministic from the instant ``now``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.worker.feeds import health
from app.worker.feeds import records as R
from app.worker.feeds.binance import BinanceClient, ServerClock
from app.worker.feeds.http import BinanceHttp, RetryPolicy
from app.worker.jobs import WorkerContext

UTC = timezone.utc
#: 16:50:30 - the 16:45 15m bar is in progress, the 16:30 bar is the newest closed one.
NOW = datetime(2026, 10, 2, 16, 50, 30, tzinfo=UTC)
MS = timedelta(milliseconds=1)


def ms(instant: datetime) -> int:
    return R.to_ms(instant)


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


class FakeExchange:
    """Answers the endpoints the client calls. Records every request."""

    def __init__(self, now: datetime = NOW, *, listing_start: dict[str, datetime] | None = None) -> None:
        self.now = now
        self.requests: list[httpx.Request] = []
        #: first instant for which a symbol has data (default: forever)
        self.listing_start = listing_start or {}
        #: callable(request) -> httpx.Response | None; return a response to override the normal answer
        self.interceptor: Callable[[httpx.Request], httpx.Response | None] | None = None
        self.funding_interval_overrides = {'LPTUSDT': 4}
        #: how long after its boundary the endpoint starts serving an open-interest sample (seconds)
        self.oi_publication_lag_s = 90
        #: Binance serves a just-closed bar with PROVISIONAL values for this long after its boundary (measured ~2-5 s)
        self.kline_finalization_s = 5
        #: Binance serves a settled funding print this long after it settles (measured ~5 s, n=1)
        self.funding_publication_lag_s = 5

    # -- plumbing --------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.interceptor is not None:
            override = self.interceptor(request)
            if override is not None:
                return override
        path = request.url.path
        params = dict(request.url.params)
        if path in ('/fapi/v1/time', '/api/v3/time'):
            return httpx.Response(200, json={'serverTime': ms(self.now)})
        if path in ('/fapi/v1/klines', '/api/v3/klines'):
            return self._klines(params)
        if path == '/futures/data/openInterestHist':
            return self._open_interest(params)
        if path == '/fapi/v1/fundingRate':
            return self._funding(params)
        if path == '/fapi/v1/fundingInfo':
            return httpx.Response(
                200,
                json=[
                    {'symbol': s, 'fundingIntervalHours': h, 'adjustedFundingRateCap': '0.02', 'adjustedFundingRateFloor': '-0.02'}
                    for s, h in self.funding_interval_overrides.items()
                ],
            )
        if path == '/fapi/v1/premiumIndex':
            return self._premium_index()
        return httpx.Response(404, json={'code': -1, 'msg': f'unexpected path {path}'})

    def requests_to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def kline_requests(self) -> list[httpx.Request]:
        return self.requests_to('/fapi/v1/klines') + self.requests_to('/api/v3/klines')

    def client(self, **kwargs: Any) -> BinanceClient:
        return make_client(self, **kwargs)

    # -- klines ----------------------------------------------------------
    def _listing(self, symbol: str) -> datetime | None:
        return self.listing_start.get(symbol)

    @staticmethod
    def kline_row(open_ms: int, length_ms: int, symbol_seed: int = 0) -> list[Any]:
        base = Decimal(100 + (open_ms // length_ms) % 50 + symbol_seed)
        volume = Decimal('10') + Decimal((open_ms // length_ms) % 7)
        return [
            open_ms,
            str(base),
            str(base + 2),
            str(base - 1),
            str(base + 1),
            str(volume),
            open_ms + length_ms - 1,
            str(volume * base),
            100 + (open_ms // length_ms) % 13,
            str(volume / 2),
            str(volume * base / 2),
            '0',
        ]

    def _klines(self, params: dict[str, str]) -> httpx.Response:
        symbol, interval = params['symbol'], params['interval']
        if symbol.startswith('NOPE'):
            return httpx.Response(400, json={'code': -1121, 'msg': 'Invalid symbol.'})
        length_ms = R.timeframe_delta(interval) // MS
        start, end, limit = int(params['startTime']), int(params['endTime']), int(params['limit'])
        listing = self._listing(symbol)
        first = ((start + length_ms - 1) // length_ms) * length_ms
        if listing is not None:
            first = max(first, ((ms(listing) + length_ms - 1) // length_ms) * length_ms)
        rows = []
        cursor = first
        now_ms = ms(self.now)
        while cursor <= end and cursor <= now_ms and len(rows) < limit:  # the in-progress bar (open <= now) is included
            row = self.kline_row(cursor, length_ms, sum(map(ord, symbol)) % 5)
            closes_at = cursor + length_ms
            if closes_at <= now_ms < closes_at + self.kline_finalization_s * 1000:  # closed, not yet final
                row[4] = str(Decimal(row[4]) + Decimal('0.5'))  # a provisional close...
                row[5] = str(Decimal(row[5]) - Decimal('1'))  # ...and volume
            rows.append(row)
            cursor += length_ms
        return httpx.Response(200, json=rows)

    # -- open interest ---------------------------------------------------
    def _open_interest(self, params: dict[str, str]) -> httpx.Response:
        period = params['period']
        length_ms = R.period_delta(period) // MS
        limit = int(params.get('limit', 30))
        now_ms = ms(self.now)
        earliest = now_ms - 30 * 86_400_000
        start = int(params.get('startTime', now_ms - limit * length_ms))
        end = int(params.get('endTime', now_ms))
        if start <= earliest:
            return httpx.Response(400, json={'code': -1130, 'msg': "parameter 'startTime' is invalid."})
        stamps = []
        cursor = ((start + length_ms - 1) // length_ms) * length_ms
        while cursor <= min(end, now_ms - self.oi_publication_lag_s * 1000):  # not served before boundary + lag
            stamps.append(cursor)
            cursor += length_ms
        stamps = stamps[-limit:]  # THE QUIRK: the last `limit` rows of the window, not the first
        return httpx.Response(
            200,
            json=[
                {
                    'symbol': params['symbol'],
                    'sumOpenInterest': f'{1000 + (t // length_ms) % 100}.00000000',
                    'sumOpenInterestValue': f'{9_000_000 + (t // length_ms) % 1000}.00000000',
                    'timestamp': t,
                }
                for t in stamps
            ],
        )

    # -- funding ---------------------------------------------------------
    FUNDING_STEP_MS = 8 * 3_600_000

    def _funding(self, params: dict[str, str]) -> httpx.Response:
        symbol = params['symbol']
        listing = self._listing(symbol)
        start = int(params.get('startTime', 0))
        end = min(int(params.get('endTime', ms(self.now))), ms(self.now) - self.funding_publication_lag_s * 1000)
        limit = int(params.get('limit', 100))
        first = ((start + self.FUNDING_STEP_MS - 1) // self.FUNDING_STEP_MS) * self.FUNDING_STEP_MS
        if listing is not None:
            first = max(first, ((ms(listing) + self.FUNDING_STEP_MS - 1) // self.FUNDING_STEP_MS) * self.FUNDING_STEP_MS)
        rows = []
        cursor = first
        while cursor + 2 <= end and len(rows) < limit:  # +2: Binance stamps prints a couple of ms late
            rows.append(
                {
                    'symbol': symbol,
                    'fundingTime': cursor + 2,
                    'fundingRate': f'0.0000{(cursor // self.FUNDING_STEP_MS) % 90 + 10}',
                    'markPrice': '85000.10000000',
                    'rateType': 'Regular',
                }
            )
            cursor += self.FUNDING_STEP_MS
        return httpx.Response(200, json=rows)

    def _premium_index(self) -> httpx.Response:
        next_funding = ((ms(self.now) // self.FUNDING_STEP_MS) + 1) * self.FUNDING_STEP_MS
        return httpx.Response(
            200,
            json=[
                {
                    'symbol': symbol,
                    'markPrice': '85224.24803623',
                    'indexPrice': '85242.93652174',
                    'estimatedSettlePrice': '85201.85184674',
                    'lastFundingRate': '0.00000863',
                    'interestRate': '0.00010000',
                    'nextFundingTime': next_funding,
                    'time': ms(self.now) - 1500,
                }
                for symbol in ('BTCUSDT', 'ETHUSDT', 'LPTUSDT')
            ],
        )


class Sleeper:
    """Records requested sleeps instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


FAST_RETRY = RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=30.0, jitter=0.0)


def make_http(handler: Callable[[httpx.Request], httpx.Response], *, base_url: str = 'https://fapi.test', retry: RetryPolicy = FAST_RETRY,
               sleeper: Sleeper | None = None, weight_limit: int = 2400) -> tuple[BinanceHttp, Sleeper]:
    sleeper = sleeper or Sleeper()
    http = BinanceHttp(
        base_url,
        weight_limit_1m=weight_limit,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry=retry,
        sleep=sleeper,
        rand=lambda: 0.5,  # jitter factor exactly 1
    )
    return http, sleeper


def make_client(exchange: FakeExchange, *, kline_page_limit: int = 1000, funding_page_limit: int = 1000,
                open_interest_page_limit: int = 500, settings: Settings | None = None) -> BinanceClient:
    """A BinanceClient wired to the fake exchange, with no sleeping and the clock pinned to the exchange."""
    from app.worker.feeds.http import SlidingWindow

    futures, _ = make_http(exchange.handler, base_url='https://fapi.test')
    futures.windows = {'funding': SlidingWindow(500, 300), 'open_interest_hist': SlidingWindow(1000, 300)}
    spot, _ = make_http(exchange.handler, base_url='https://api.test', weight_limit=6000)
    clock = ServerClock(None, local_now=lambda: exchange.now)
    return BinanceClient(
        futures=futures,
        spot=spot,
        clock=clock,
        settings=settings or make_settings(),
        kline_page_limit=kline_page_limit,
        funding_page_limit=funding_page_limit,
        open_interest_page_limit=open_interest_page_limit,
    )


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {'watchlist': 'BTCUSDT,ETHUSDT'}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# database fixtures shared by the ingest tests (imported by name into each module)
# ---------------------------------------------------------------------------
@pytest.fixture
def session_factory(session: Session) -> sessionmaker[Session]:
    """Sessions that all join the test's rolled-back transaction (one at a time)."""
    return sessionmaker(bind=session.get_bind(), join_transaction_mode='create_savepoint', expire_on_commit=False)


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def worker_context(session_factory: sessionmaker[Session], exchange: FakeExchange) -> WorkerContext:
    return WorkerContext(
        settings=make_settings(),
        client=exchange.client(),
        session_factory=session_factory,
        attempts=health.AttemptLog(),
        code_version='testver',
    )
