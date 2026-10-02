"""Retry, backoff, rate limiting and the failure modes that actually break ingest."""

from __future__ import annotations

import httpx
import pytest

from app.worker.feeds.http import (
    BinanceAPIError,
    BinanceHttp,
    BinanceUnavailable,
    GeoBlocked,
    RateLimited,
    RetryPolicy,
    SlidingWindow,
    SymbolNotFound,
)
from ingest_helpers import FAST_RETRY, Sleeper, make_http


class Script:
    """A handler that plays back a list of responses (or exceptions), then repeats the last one."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return step


def ok(body=None):
    return httpx.Response(200, json=[] if body is None else body)


# ---------------------------------------------------------------------------
# transient failures: 5xx, timeouts, bad bodies
# ---------------------------------------------------------------------------
def test_a_5xx_is_retried_with_exponential_backoff_then_succeeds():
    script = Script(httpx.Response(503), httpx.Response(502), ok([1]))
    http, sleeper = make_http(script)
    assert http.get('/x') == [1]
    assert script.calls == 3
    assert sleeper.calls == [1.0, 2.0]  # base 1s, doubled; jitter pinned to zero


def test_backoff_is_capped():
    policy = RetryPolicy(max_attempts=10, base_delay=1.0, max_delay=8.0, jitter=0.0)
    assert [policy.delay(n, lambda: 0.5) for n in range(1, 7)] == [1, 2, 4, 8, 8, 8]


def test_backoff_jitter_stays_inside_its_band():
    policy = RetryPolicy(base_delay=4.0, jitter=0.25)
    assert policy.delay(1, lambda: 0.0) == pytest.approx(3.0)
    assert policy.delay(1, lambda: 1.0) == pytest.approx(5.0)


def test_timeouts_are_retried_and_exhaustion_raises_unavailable():
    script = Script(httpx.ReadTimeout('slow'))
    http, sleeper = make_http(script)
    with pytest.raises(BinanceUnavailable):
        http.get('/x')
    assert script.calls == FAST_RETRY.max_attempts
    assert len(sleeper.calls) == FAST_RETRY.max_attempts - 1  # no pointless sleep before giving up


def test_connection_errors_are_retried():
    script = Script(httpx.ConnectError('reset'), ok([2]))
    http, _ = make_http(script)
    assert http.get('/x') == [2]


def test_an_undecodable_200_body_is_retried():
    script = Script(httpx.Response(200, text='<html>gateway</html>'), ok([3]))
    http, _ = make_http(script)
    assert http.get('/x') == [3]


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------
def test_429_honours_retry_after_then_succeeds():
    script = Script(httpx.Response(429, headers={'Retry-After': '3'}), ok([4]))
    http, sleeper = make_http(script)
    assert http.get('/x') == [4]
    assert sleeper.calls == [3.0]


def test_429_without_retry_after_falls_back_to_backoff():
    script = Script(httpx.Response(429), ok([5]))
    http, sleeper = make_http(script)
    assert http.get('/x') == [5]
    assert sleeper.calls == [1.0]


def test_429_with_a_huge_retry_after_raises_instead_of_sleeping_and_blocks_further_calls():
    script = Script(httpx.Response(429, headers={'Retry-After': '900'}))
    http, sleeper = make_http(script)
    with pytest.raises(RateLimited):
        http.get('/x')
    calls_after_first = script.calls
    with pytest.raises(RateLimited):
        http.get('/x')
    assert script.calls == calls_after_first  # the cool-down means no second request
    assert sleeper.calls == []


def test_418_is_a_ban_and_stops_all_further_requests():
    script = Script(httpx.Response(418, headers={'Retry-After': '600'}))
    http, _ = make_http(script)
    with pytest.raises(RateLimited) as caught:
        http.get('/x')
    assert caught.value.banned is True and caught.value.retry_after == 600
    with pytest.raises(RateLimited):
        http.get('/y')
    assert script.calls == 1


def test_sliding_window_sleeps_before_crossing_the_soft_limit():
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    window = SlidingWindow(10, 60, soft_fraction=0.8, monotonic=lambda: now[0], sleep=sleep)  # soft limit 8
    assert window.acquire(5) == 0
    now[0] = 10.0
    waited = window.acquire(5)  # 5 + 5 > 8: must wait for the first spend to age out (at t=60)
    assert waited == pytest.approx(50.0)
    assert slept == [pytest.approx(50.0)]
    assert window.spent() == 5  # the old spend left the window


def test_the_servers_used_weight_header_raises_our_ledger():
    now = [0.0]
    window = SlidingWindow(100, 60, soft_fraction=0.8, monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    window.acquire(1)
    window.observe(79)  # another process on this IP has spent 78 of our minute
    assert window.spent() == 79
    assert window.acquire(5) > 0  # 79 + 5 > 80


def test_the_governor_is_charged_per_request_and_reads_the_header():
    script = Script(httpx.Response(200, json=[], headers={'x-mbx-used-weight-1m': '2300'}))
    http, sleeper = make_http(script, weight_limit=2400)  # soft limit 1920
    http.get('/x', weight=5)
    assert http.weight.spent() >= 2300
    # monotonic is the real clock here, so the next request would block; we only check the ledger above.


# ---------------------------------------------------------------------------
# errors that must NOT be retried
# ---------------------------------------------------------------------------
def test_451_raises_geoblocked_once():
    script = Script(httpx.Response(451, json={'msg': 'Service unavailable from a restricted location'}))
    http, sleeper = make_http(script)
    with pytest.raises(GeoBlocked):
        http.get('/x')
    assert script.calls == 1 and sleeper.calls == []


def test_unknown_symbol_is_symbol_not_found_and_not_retried():
    script = Script(httpx.Response(400, json={'code': -1121, 'msg': 'Invalid symbol.'}))
    http, _ = make_http(script)
    with pytest.raises(SymbolNotFound) as caught:
        http.get('/x')
    assert caught.value.code == -1121 and script.calls == 1


def test_other_4xx_raise_the_binance_error_code_without_retry():
    script = Script(httpx.Response(400, json={'code': -1130, 'msg': "parameter 'startTime' is invalid."}))
    http, _ = make_http(script)
    with pytest.raises(BinanceAPIError) as caught:
        http.get('/x')
    assert caught.value.code == -1130 and script.calls == 1


def test_a_403_waf_block_is_not_retried():
    script = Script(httpx.Response(403, text='WAF'))
    http, _ = make_http(script)
    with pytest.raises(BinanceAPIError):
        http.get('/x')
    assert script.calls == 1


def test_no_signed_or_trading_request_can_be_built():
    """The transport only ever GETs a path it is handed; assert the client sends no key or signature."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok([])

    http, _ = make_http(handler)
    http.get('/fapi/v1/klines', {'symbol': 'BTCUSDT'})
    request = seen[0]
    assert request.method == 'GET'
    assert 'x-mbx-apikey' not in {k.lower() for k in request.headers}
    assert 'signature' not in dict(request.url.params)
