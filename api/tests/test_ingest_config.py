"""Where the watchlist and the feed knobs live, that they are not frozen rules, and that ingest is read-only."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app import config as config_module
from app.config import DEFAULT_WATCHLIST, WATCHLIST_MAX_COINS, Settings, get_settings, parse_symbols
from app.core import constants
from app.core.hashing import frozen_constants, rule_hash
from ingest_helpers import make_settings

WORKER = Path(__file__).resolve().parent.parent / 'app' / 'worker'


# ---------------------------------------------------------------------------
# the watchlist
# ---------------------------------------------------------------------------
def test_default_watchlist_is_10_to_20_unique_liquid_symbols_with_btc_and_eth_first():
    assert DEFAULT_WATCHLIST[:2] == ('BTCUSDT', 'ETHUSDT')
    assert 10 <= len(DEFAULT_WATCHLIST) <= 20 == WATCHLIST_MAX_COINS
    assert len(set(DEFAULT_WATCHLIST)) == len(DEFAULT_WATCHLIST)
    assert Settings(_env_file=None).watchlist_symbols == DEFAULT_WATCHLIST


def test_the_watchlist_is_not_in_constants_and_so_not_in_the_rule_hash():
    names = set(frozen_constants())
    assert not any(token in name for name in names for token in ('WATCHLIST', 'POLL', 'STALE', 'BINANCE', 'GRACE'))
    assert not hasattr(constants, 'WATCHLIST') and not hasattr(constants, 'DEFAULT_WATCHLIST')


def test_changing_the_watchlist_or_feed_knobs_cannot_change_the_rule_hash(monkeypatch):
    before = rule_hash()
    monkeypatch.setenv('WATCHLIST', 'BTCUSDT')
    monkeypatch.setenv('FEED_KLINE_POLL_SECONDS', '120')
    monkeypatch.setenv('FEED_STALENESS_GRACE_SECONDS', '600')
    get_settings.cache_clear()
    try:
        assert get_settings().watchlist_symbols == ('BTCUSDT',)
        assert get_settings().feed_kline_poll_seconds == 120
        assert rule_hash() == before
    finally:
        get_settings.cache_clear()


def test_the_watchlist_comes_from_the_environment():
    settings = make_settings(watchlist=' ethusdt , btcusdt,ETHUSDT ')
    assert settings.watchlist_symbols == ('ETHUSDT', 'BTCUSDT')  # normalised, de-duplicated, order kept


@pytest.mark.parametrize('bad', ['BTC', 'BTCUSD', 'BTC-USDT', 'btc/usdt', 'USDT', 'B USDT'])
def test_parse_symbols_rejects_things_that_are_not_binance_usdt_symbols(bad):
    with pytest.raises(ValueError):
        parse_symbols(bad)


def test_parse_symbols_ignores_empty_tokens():
    assert parse_symbols('BTCUSDT,, ,ETHUSDT,') == ('BTCUSDT', 'ETHUSDT')
    assert parse_symbols('') == ()


def test_more_than_20_coins_or_none_is_refused_when_the_watchlist_is_read():
    twenty_one = ','.join(f'COIN{n}USDT' for n in range(21))
    with pytest.raises(ValueError, match='at most 20'):
        make_settings(watchlist=twenty_one).watchlist_symbols
    with pytest.raises(ValueError, match='empty'):
        make_settings(watchlist=' , ').watchlist_symbols
    assert len(make_settings(watchlist=','.join(f'COIN{n}USDT' for n in range(20))).watchlist_symbols) == 20


# ---------------------------------------------------------------------------
# feed knobs
# ---------------------------------------------------------------------------
def test_default_polling_intervals_are_inside_the_specs_one_to_five_minutes():
    settings = Settings(_env_file=None)
    polls = [
        settings.feed_kline_poll_seconds,
        settings.feed_open_interest_poll_seconds,
        settings.feed_funding_settled_poll_seconds,
        settings.feed_funding_predicted_poll_seconds,
        settings.feed_health_poll_seconds,
    ]
    assert all(60 <= p <= 300 for p in polls)


@pytest.mark.parametrize('knob', ['feed_kline_poll_seconds', 'feed_open_interest_poll_seconds', 'feed_funding_settled_poll_seconds', 'feed_funding_predicted_poll_seconds'])
@pytest.mark.parametrize('value', [0, 5, 301, 3600])
def test_a_poll_interval_outside_the_safe_range_is_refused(knob, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{knob: value})


def test_open_interest_periods_are_configurable_and_deduplicated():
    assert make_settings(feed_open_interest_periods='1h, 4h,1h').open_interest_periods == ('1h', '4h')
    with pytest.raises(ValueError):
        make_settings(feed_open_interest_periods=' ').open_interest_periods


def test_binance_endpoints_default_to_the_public_hosts():
    settings = Settings(_env_file=None)
    assert settings.binance_futures_url == 'https://fapi.binance.com'
    assert settings.binance_spot_url == 'https://api.binance.com'
    assert settings.binance_futures_ws_url == 'wss://fstream.binance.com'


def test_config_has_no_credentials_for_the_exchange():
    fields = {name.lower() for name in Settings.model_fields}
    assert not any(token in name for name in fields for token in ('api_key', 'apikey', 'secret', 'signature'))


# ---------------------------------------------------------------------------
# there is no order endpoint in any phase
# ---------------------------------------------------------------------------
ALLOWED_PATHS = {
    '/fapi/v1/time',
    '/fapi/v1/klines',
    '/api/v3/klines',
    '/fapi/v1/fundingRate',
    '/fapi/v1/fundingInfo',
    '/fapi/v1/premiumIndex',
    '/futures/data/openInterestHist',
}
FORBIDDEN_IDENTIFIERS = {'signature', 'api_key', 'apikey', 'api_secret', 'secret', 'secret_key', 'hmac', 'listen_key', 'listenkey', 'post', 'put', 'delete', 'patch'}
FORBIDDEN_PATH = re.compile(r'/(api|fapi|sapi|dapi|papi|eapi)/v\d+/(order|batchOrders|allOpenOrders|listenKey|account|positionRisk|balance|leverage|marginType|userDataStream)', re.I)


def worker_trees():
    for path in sorted(WORKER.rglob('*.py')):
        yield path, ast.parse(path.read_text(encoding='utf-8'))


def test_every_binance_path_in_the_worker_is_a_public_market_data_endpoint():
    seen: set[str] = set()
    for path, tree in worker_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith('/') and re.match(r'^/(f?api|futures)/', node.value):
                seen.add(node.value)
                assert not FORBIDDEN_PATH.search(node.value), f'{path.name}: trading or account endpoint {node.value}'
    assert seen == ALLOWED_PATHS


def test_the_worker_has_no_signing_no_keys_and_only_read_verbs():
    problems: list[str] = []
    for path, tree in worker_trees():
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.keyword) and node.arg:
                names.append(node.arg)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.extend(alias.name.split('.')[-1] for alias in node.names)
            for name in names:
                if name.lower() in FORBIDDEN_IDENTIFIERS:
                    problems.append(f'{path.name}:{node.lineno} uses {name!r}')
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.upper() in {'POST', 'PUT', 'DELETE', 'PATCH'} or node.value.lower() in {'x-mbx-apikey', 'signature'}:
                    problems.append(f'{path.name}:{node.lineno} contains {node.value!r}')
    assert problems == []


def test_every_http_call_in_the_worker_is_one_httpx_get_inside_binancehttp():
    verbs = []
    for path, tree in worker_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                if node.func.attr in {'get', 'post', 'put', 'delete', 'patch', 'request', 'send', 'stream'} and isinstance(receiver, ast.Attribute) and receiver.attr == '_client':
                    verbs.append((path.name, node.func.attr))
    assert verbs == [('http.py', 'get')]  # the one egress point in the whole worker: a GET

def test_the_worker_reads_feed_tables_only_through_the_as_of_gate():
    """The repository-wide scan (tests/test_as_of.py) covers app/**; this pins that app/worker really is inside it."""
    offenders = []
    for path, tree in worker_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'select':
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in {'Candle', 'OpenInterest', 'Funding', 'ForceOrder'}:
                        offenders.append(f'{path.name}:{node.lineno}')
    assert offenders == []
    assert config_module.__file__
