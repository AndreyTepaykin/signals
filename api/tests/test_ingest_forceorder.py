"""The liquidation-stream collector: reconnection, gap honesty, filtering. No network, no real sleeping."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

from app.config import Settings
from app.worker.feeds import forceorder as FO
from ingest_helpers import at

T0 = at(12, 0)
WATCH = frozenset({'BTCUSDT', 'ETHUSDT'})


def event(symbol='BTCUSDT', *, t=1790958600123, price='9910', side='SELL'):
    return json.dumps(
        {'e': 'forceOrder', 'E': t + 333, 'o': {'s': symbol, 'S': side, 'q': '0.014', 'p': price, 'ap': price, 'X': 'FILLED', 'z': '0.014', 'T': t}}
    )


class Clock:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class Script:
    """Scripted websocket server.

    Each connection is a list of items: a ``str``/``bytes`` is a frame to deliver, a number advances the fake
    clock, an exception is raised from ``recv`` (the connection dies). A connection whose script runs out just
    stays open and idle. When there are no more connections the next attempt stops the collector.
    """

    def __init__(self, clock: Clock, stop: asyncio.Event, *connections) -> None:
        self.clock, self.stop = clock, stop
        self.connections = [c if isinstance(c, BaseException) else list(c) for c in connections]
        self.urls: list[str] = []

    @asynccontextmanager
    async def __call__(self, url: str):
        self.urls.append(url)
        if not self.connections:
            self.stop.set()
            raise ConnectionError('no more scripted connections')
        items = self.connections.pop(0)
        if isinstance(items, BaseException):  # a connect failure
            raise items

        class Connection:
            async def recv(inner):
                while items:
                    item = items.pop(0)
                    if isinstance(item, BaseException):
                        raise item
                    if isinstance(item, (int, float)):
                        self.clock.advance(item)
                        continue
                    return item
                await asyncio.sleep(0.005)
                raise asyncio.TimeoutError  # idle: lets the collector's own timeout/flush logic run

        yield Connection()


class Recorder(FO.Hooks):
    def __init__(self) -> None:
        self.connected: list[tuple[str, datetime, FO.Gap | None]] = []
        self.disconnected: list[tuple[str, str]] = []
        self.heartbeats: list[datetime] = []

    def on_connected(self, connection_id, at_, gap, stats):
        self.connected.append((connection_id, at_, gap))

    def on_heartbeat(self, at_, stats):
        self.heartbeats.append(at_)

    def on_disconnected(self, connection_id, at_, reason, stats):
        self.disconnected.append((connection_id, reason))


def build(*connections, sink=None, watchlist=WATCH, **options):
    clock, stop = Clock(), asyncio.Event()
    script = Script(clock, stop, *connections)
    delays: list[float] = []
    stored: list = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        clock.advance(seconds)
        await asyncio.sleep(0)

    def default_sink(batch):
        stored.extend(batch)
        return len(batch)

    hooks = Recorder()
    collector = FO.ForceOrderCollector(
        base_url='wss://example.test',
        watchlist=watchlist,
        sink=sink or default_sink,
        connector=script,
        hooks=hooks,
        backoff=FO.Backoff(base=1, cap=60, jitter=0.25, stable_after=60, rand=lambda: 0.5),  # rand .5 = no jitter
        now=clock,
        sleep=sleep,
        flush_interval=0.01,
        heartbeat_interval=30,
        **options,
    )
    return collector, stop, script, delays, stored, hooks, clock


async def run(collector, stop):
    await asyncio.wait_for(collector.run(stop), timeout=10)


# ---------------------------------------------------------------------------
async def test_the_url_is_the_market_wide_force_order_stream():
    collector, stop, script, *_ = build()
    await run(collector, stop)
    assert script.urls[0] == 'wss://example.test/ws/!forceOrder@arr'
    assert Settings().binance_futures_ws_url == 'wss://fstream.binance.com'  # live default


async def test_only_watchlist_liquidations_are_kept_and_everything_else_is_counted():
    frames = [
        event('BTCUSDT'),
        event('DOGEUSDT'),  # not on the watchlist
        json.dumps({'e': 'aggTrade', 's': 'BTCUSDT'}),  # not a liquidation
        'not json at all',
        json.dumps([json.loads(event('ETHUSDT', t=1790958600200)), json.loads(event('SOLUSDT'))]),  # array frame
        event('BTCUSDT', side='SIDEWAYS'),  # claims to be a liquidation, is unusable
        event('BTCUSDT', t=1790958600300).encode(),  # bytes frame
        ConnectionError('server went away'),
    ]
    collector, stop, _, _, stored, *_ = build(frames)
    await run(collector, stop)
    assert [r.coin for r in stored] == ['BTCUSDT', 'ETHUSDT', 'BTCUSDT']
    stats = collector.stats
    assert stats.written == 3
    assert stats.malformed == 2  # the garbage frame and the bad side
    assert stats.ignored == 3  # DOGE, aggTrade, SOL


async def test_available_at_is_print_time_for_everything_the_collector_emits():
    collector, stop, _, _, stored, *_ = build([event(), ConnectionError('x')])
    await run(collector, stop)
    assert stored and all(r.available_at == r.traded_at for r in stored)
    assert collector.stats.last_traded_at == stored[-1].traded_at == collector.stats.last_available_at


async def test_rows_from_the_same_connection_get_distinct_sequence_numbers():
    collector, stop, _, _, stored, *_ = build([event(), event(), ConnectionError('x')])
    await run(collector, stop)
    keys = {R_key for R_key in (r.dedup_key for r in stored)}
    assert len(stored) == 2 and len(keys) == 2  # two identical liquidations are two rows


# ---------------------------------------------------------------------------
# reconnection
# ---------------------------------------------------------------------------
async def test_backoff_doubles_between_consecutive_failures():
    drops = [ConnectionError('a'), ConnectionError('b'), ConnectionError('c')]
    collector, stop, script, delays, *_ = build(*[[d] for d in drops])
    await run(collector, stop)
    assert delays[:3] == [1, 2, 4]
    assert len(script.urls) == 4  # three scripted drops, then the one that ends the test


async def test_a_failed_connect_is_retried_the_same_way():
    collector, stop, script, delays, stored, hooks, _ = build(OSError('refused'), [event(), ConnectionError('x')])
    await run(collector, stop)
    assert delays[0] == 1 and len(stored) == 1
    assert len(hooks.connected) == 1  # the refused attempt never counted as a connection


async def test_backoff_is_capped():
    collector, stop, _, delays, *_ = build(*[[ConnectionError('x')] for _ in range(9)])
    await run(collector, stop)
    assert max(delays) == 60 and delays[:7] == [1, 2, 4, 8, 16, 32, 60]


async def test_backoff_resets_only_after_a_connection_stayed_up():
    flaky = [ConnectionError('instant')]
    healthy = [event(), 120, ConnectionError('after two minutes')]
    collector, stop, _, delays, *_ = build(flaky, flaky, healthy, flaky)
    await run(collector, stop)
    assert delays[:4] == [1, 2, 1, 2]  # 1, 2 for the quick drops; the long-lived one resets it to 1


async def test_a_connection_that_drops_immediately_never_resets_the_backoff():
    collector, stop, _, delays, *_ = build(*[[ConnectionError('x')] for _ in range(4)])
    await run(collector, stop)
    assert delays[:4] == [1, 2, 4, 8]


async def test_a_gap_is_recorded_between_a_drop_and_the_next_connection():
    collector, stop, _, delays, _, hooks, _ = build([event(), 30, ConnectionError('dropped')], [event(), 1, ConnectionError('again')])
    await run(collector, stop)
    gaps = collector.stats.gaps
    assert gaps[0].seconds == 0.0  # start-up: connected immediately
    assert gaps[1].seconds == pytest.approx(delays[0])  # exactly the reconnect delay: those prints are lost
    assert hooks.connected[1][2] is gaps[1]  # the hook is told about the gap that preceded it


async def test_still_disconnected_at_shutdown_leaves_an_open_gap_on_record():
    collector, stop, *_ = build([ConnectionError('x')])
    await run(collector, stop)  # the second attempt finds nothing scripted, stops the run
    assert collector.stats.gaps[-1].ended_at is None


async def test_planned_reconnect_before_the_24_hour_server_cut():
    long_run = 23.5 * 3600
    collector, stop, script, _, stored, hooks, _ = build([event(), long_run, event(t=1790958600999)], [event(t=1790958601999), ConnectionError('x')])
    await run(collector, stop)
    reasons = [reason for _, reason in hooks.disconnected]
    assert reasons[0].startswith('planned reconnect')
    assert len(script.urls) >= 2 and len(stored) == 3  # the connection was replaced, nothing buffered was lost
    assert FO.MAX_CONNECTION_AGE == timedelta(hours=23, minutes=30)


async def test_every_connection_gets_its_own_id():
    collector, stop, _, _, stored, hooks, _ = build([event(), ConnectionError('a')], [event(), ConnectionError('b')])
    await run(collector, stop)
    ids = [cid for cid, _, _ in hooks.connected]
    assert len(ids) == len(set(ids)) == 2
    assert len({r.dedup_key for r in stored}) == 2  # same liquidation on two connections: still two keys


# ---------------------------------------------------------------------------
# the sink
# ---------------------------------------------------------------------------
async def test_a_failing_sink_keeps_the_rows_and_retries_them():
    calls: list[int] = []

    def flaky(batch):
        calls.append(len(batch))
        if len(calls) == 1:
            raise RuntimeError('database restarting')
        return len(batch)

    collector, stop, *_ = build([event(), ConnectionError('a')], [ConnectionError('b')], sink=flaky)
    await run(collector, stop)
    assert calls[:2] == [1, 1]  # same row offered again
    assert collector.stats.sink_failures == 1 and collector.stats.written == 1


async def test_the_buffer_is_bounded_when_the_sink_stays_down():
    def down(batch):
        raise RuntimeError('down')

    collector, stop, *_ = build([event(t=1790958600001), event(t=1790958600002), event(t=1790958600003), ConnectionError('x')], sink=down, max_buffer=2)
    await run(collector, stop)
    assert collector.stats.dropped_on_overflow == 1 and collector.stats.written == 0


# ---------------------------------------------------------------------------
# heartbeat & stop
# ---------------------------------------------------------------------------
async def test_heartbeat_fires_on_its_own_interval():
    collector, stop, _, _, _, hooks, _ = build([event(), 31, event(t=1790958600500), ConnectionError('x')])
    await run(collector, stop)
    assert len(hooks.heartbeats) == 1


async def test_setting_the_stop_event_ends_an_idle_connection_promptly():
    collector, stop, _, _, _, hooks, _ = build([])  # connects, then idles forever
    task = asyncio.create_task(collector.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=3)
    assert hooks.disconnected and hooks.disconnected[-1][1] == 'stopped'


async def test_default_backoff_has_jitter_within_bounds():
    backoff = FO.Backoff(base=2, cap=100, jitter=0.25)
    first = backoff.next_delay()
    assert 1.5 <= first <= 2.5
