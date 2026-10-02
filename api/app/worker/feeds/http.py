"""HTTP transport for Binance's public market-data endpoints.

What actually breaks ingest, and what this module does about it:

* **Request weight.** Binance budgets by IP per minute (futures 2400, spot 6000)
  and a few endpoints carry their own request-count windows. :class:`SlidingWindow`
  keeps a local ledger and sleeps BEFORE a request would cross a soft limit,
  and folds in the ``X-MBX-USED-WEIGHT-1M`` header when the server reports more
  than the ledger knows (another process on the same IP, e.g. a backfill running
  beside the worker).
* **HTTP 429 / 418.** 429 means back off: honour ``Retry-After``. 418 means the
  IP is banned; the module raises at once and refuses further requests until the
  ban lifts rather than hammering a banned endpoint.
* **5xx, timeouts, connection resets, undecodable bodies.** Retried with
  exponential backoff and jitter, up to a bound, then surfaced as
  :class:`BinanceUnavailable`.
* **HTTP 451** (geo-block, e.g. a US IP) and **403** (WAF) are not retried. 451
  is a deployment fact, not a transient fault: the spec puts the VPS in
  Germany or Finland for exactly this reason.
* Any other 4xx is a bug in our request or a bad symbol and is raised with
  Binance's error code. Code ``-1121`` is :class:`SymbolNotFound`.

Public endpoints only. Nothing here signs a request or sends a key.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Binance's per-IP, per-minute request weight limits (their documented values).
FUTURES_WEIGHT_LIMIT_1M = 2400
SPOT_WEIGHT_LIMIT_1M = 6000
#: Stay under this fraction of a limit before sleeping.
SOFT_LIMIT_FRACTION = 0.8


class BinanceError(Exception):
    """Base class for everything this module raises."""


class BinanceAPIError(BinanceError):
    def __init__(self, status: int, code: int | None, message: str, *, path: str = '') -> None:
        super().__init__(f'{path} -> HTTP {status}, code {code}: {message}')
        self.status, self.code, self.message, self.path = status, code, message, path


class SymbolNotFound(BinanceAPIError):
    """Binance error -1121: the symbol does not exist on this market."""


class GeoBlocked(BinanceError):
    """HTTP 451. Binance refuses this IP's jurisdiction. A deployment note, not a fault."""


class RateLimited(BinanceError):
    """429/418, or a ban still in force. ``retry_after`` is seconds."""

    def __init__(self, message: str, *, retry_after: float | None = None, banned: bool = False) -> None:
        super().__init__(message)
        self.retry_after, self.banned = retry_after, banned


class BinanceUnavailable(BinanceError):
    """Retries exhausted on 5xx / timeouts / connection errors."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    #: +/- fraction of the delay, so a fleet of retries does not synchronise.
    jitter: float = 0.25
    #: Longest ``Retry-After`` we will sleep through instead of raising.
    max_retry_after: float = 120.0

    def delay(self, attempt: int, rand: Callable[[], float]) -> float:
        """Backoff before retry number ``attempt`` (1 = first retry)."""
        raw = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return max(0.0, raw * (1 + self.jitter * (2 * rand() - 1)))


class SlidingWindow:
    """A thread-safe ledger of cost spent in the last ``window_seconds``.

    ``acquire(cost)`` returns the seconds it had to wait (it sleeps first, using
    the injected ``sleep``), then records the spend. ``observe(total)`` raises
    the ledger to a server-reported total.
    """

    def __init__(
        self,
        limit: float,
        window_seconds: float,
        *,
        soft_fraction: float = SOFT_LIMIT_FRACTION,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.soft_limit = limit * soft_fraction
        self.window_seconds = window_seconds
        self._monotonic, self._sleep = monotonic, sleep
        self._entries: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        while self._entries and now - self._entries[0][0] >= self.window_seconds:
            self._entries.popleft()

    def spent(self) -> float:
        with self._lock:
            self._purge(self._monotonic())
            return sum(cost for _t, cost in self._entries)

    def acquire(self, cost: float) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = self._monotonic()
                self._purge(now)
                spent = sum(c for _t, c in self._entries)
                if spent + cost <= self.soft_limit or not self._entries:
                    self._entries.append((now, cost))
                    return waited
                wait = self._entries[0][0] + self.window_seconds - now
            wait = max(wait, 0.001)
            log.info('throttling %.1fs: %.0f/%.0f spent in %.0fs window', wait, spent, self.soft_limit, self.window_seconds)
            self._sleep(wait)
            waited += wait

    def observe(self, server_total: float) -> None:
        """Server says ``server_total`` is used; add the part our ledger missed."""
        with self._lock:
            now = self._monotonic()
            self._purge(now)
            missing = server_total - sum(c for _t, c in self._entries)
            if missing > 0:
                self._entries.append((now, missing))


class BinanceHttp:
    """One base URL, with weight accounting, retry and rate-limit handling."""

    def __init__(
        self,
        base_url: str,
        *,
        weight_limit_1m: int,
        client: httpx.Client | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
        extra_windows: Mapping[str, SlidingWindow] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self._client = client or httpx.Client(timeout=timeout, headers={'User-Agent': 'signals-worker/0.1'})
        self._owns_client = client is None
        self.retry = retry or RetryPolicy()
        self._sleep, self._monotonic, self._rand = sleep, monotonic, rand
        self.weight = SlidingWindow(weight_limit_1m, 60.0, monotonic=monotonic, sleep=sleep)
        #: Endpoint families with their own request-count budget (name -> window).
        self.windows: dict[str, SlidingWindow] = dict(extra_windows or {})
        self._blocked_until: float = 0.0
        self.request_count = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- ban state -------------------------------------------------------
    def _check_ban(self) -> None:
        remaining = self._blocked_until - self._monotonic()
        if remaining > 0:
            raise RateLimited(
                f'{self.base_url} is in a rate-limit/ban cool-down for another {remaining:.0f}s',
                retry_after=remaining,
                banned=True,
            )

    # -- the request -----------------------------------------------------
    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        weight: int = 1,
        windows: Sequence[str] = (),
    ) -> Any:
        """GET ``path`` and return the decoded JSON body.

        ``weight`` is the request's documented cost; ``windows`` names extra
        per-endpoint request-count budgets to charge one unit each.
        """
        last_error: Exception | None = None
        for attempt in range(1, self.retry.max_attempts + 1):
            self._check_ban()
            self.weight.acquire(weight)
            for name in windows:
                self.windows[name].acquire(1)
            self.request_count += 1
            try:
                response = self._client.get(self.base_url + path, params=dict(params or {}))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning('%s transport error (%s) attempt %d/%d', path, type(exc).__name__, attempt, self.retry.max_attempts)
                self._backoff(attempt)
                continue

            used = response.headers.get('x-mbx-used-weight-1m')
            if used is not None and used.isdigit():
                self.weight.observe(float(used))

            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    last_error = exc
                    log.warning('%s returned an undecodable body, attempt %d/%d', path, attempt, self.retry.max_attempts)
                    self._backoff(attempt)
                    continue
            if status == 451:
                raise GeoBlocked(
                    f'{path} -> HTTP 451: Binance refuses this IP\'s jurisdiction. Run the worker from an '
                    'allowed region (the spec deploys to Germany or Finland).'
                )
            if status == 418:
                retry_after = self._retry_after(response)
                self._blocked_until = self._monotonic() + (retry_after or 120.0)
                raise RateLimited(f'{path} -> HTTP 418: IP banned', retry_after=retry_after, banned=True)
            if status == 429:
                retry_after = self._retry_after(response)
                last_error = RateLimited(f'{path} -> HTTP 429', retry_after=retry_after)
                if retry_after is not None and retry_after > self.retry.max_retry_after:
                    self._blocked_until = self._monotonic() + retry_after
                    raise last_error
                log.warning('%s -> 429, waiting %.1fs', path, retry_after if retry_after is not None else self.retry.delay(attempt, self._rand))
                if retry_after is not None:
                    self._sleep(retry_after)
                else:
                    self._backoff(attempt)
                continue
            if status >= 500:
                last_error = BinanceAPIError(status, None, response.text[:200], path=path)
                log.warning('%s -> HTTP %d, attempt %d/%d', path, status, attempt, self.retry.max_attempts)
                self._backoff(attempt)
                continue
            raise self._api_error(status, response, path)

        if isinstance(last_error, RateLimited):
            raise last_error
        raise BinanceUnavailable(f'{path}: gave up after {self.retry.max_attempts} attempts ({last_error!r})')

    # -- helpers ---------------------------------------------------------
    def _backoff(self, attempt: int) -> None:
        if attempt >= self.retry.max_attempts:
            return  # no point sleeping before giving up
        self._sleep(self.retry.delay(attempt, self._rand))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get('retry-after')
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None

    @staticmethod
    def _api_error(status: int, response: httpx.Response, path: str) -> BinanceAPIError:
        code: int | None = None
        message = response.text[:200]
        try:
            body = response.json()
            if isinstance(body, dict):
                code = body.get('code') if isinstance(body.get('code'), int) else None
                message = str(body.get('msg', message))
        except ValueError:
            pass
        if code == -1121:
            return SymbolNotFound(status, code, message, path=path)
        return BinanceAPIError(status, code, message, path=path)
