"""WebSocket push tests."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routers import ws as ws_module
from conftest import BAR_CLOSE, make_candidate
from tests.test_stats_job import add_label, cand

WORKER = Path(__file__).resolve().parent.parent / 'app' / 'worker'


@pytest.fixture
def ws_client(session):
    @contextmanager
    def factory():
        yield session

    ws_module.snapshot_session_factory = factory
    ws_module.ws_poll_seconds_override = 0.05
    with TestClient(create_app()) as client:
        yield client
    ws_module.snapshot_session_factory = None
    ws_module.ws_poll_seconds_override = None


def test_stand_down_identity_ignores_staleness():
    base = {
        'active': True,
        'kinds': ['feed_stale'],
        'feed_breaches': [{'feed': 'binance_kline_perp_15m', 'coin': 'BTCUSDT', 'staleness_seconds': 10, 'reason': 'old'}],
        'active_event_windows': [{'kind': 'cpi', 'event_time': '2026-01-01T12:00:00+00:00'}],
    }
    other = {
        **base,
        'feed_breaches': [{'feed': 'binance_kline_perp_15m', 'coin': 'BTCUSDT', 'staleness_seconds': 999, 'reason': 'older'}],
    }
    assert ws_module.stand_down_identity(base) == ws_module.stand_down_identity(other)


def test_ws_pushes_candidates_and_labels(ws_client, session):
    with ws_client.websocket_connect('/ws') as ws:
        hello = ws.receive_json()
        assert hello['type'] == 'hello'
        make_candidate(session, bar_close=BAR_CLOSE + timedelta(minutes=99))
        session.flush()
        got_candidates = False
        for _ in range(40):
            msg = ws.receive_json()
            if msg.get('type') == 'candidates':
                got_candidates = True
                break
        assert got_candidates
        c = cand(session, 120)
        add_label(session, c)
        session.flush()
        for _ in range(40):
            msg = ws.receive_json()
            if msg.get('type') == 'labels':
                break
        else:
            pytest.fail('expected labels event after new label')


def test_notify_post_only_in_post_ntfy_and_uses_settings_url():
    tree = ast.parse((WORKER / 'notify.py').read_text(encoding='utf-8'))
    posts_outside: list[int] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == '_post_ntfy':
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == 'post':
                    url_arg = sub.args[0] if sub.args else None
                    assert isinstance(url_arg, ast.Attribute) and url_arg.attr == 'ntfy_url'
            return
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == 'post':
                    posts_outside.append(sub.lineno)
    assert posts_outside == []
