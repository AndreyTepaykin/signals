"""ntfy notify job tests."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.core import constants as C
from app.models import enums as E
from app.worker import notify as notify_mod
from conftest import BAR_CLOSE, make_candidate
from datetime import timedelta


def test_disabled_logs_only(caplog):
    settings = Settings(ntfy_url='')
    with caplog.at_level('INFO'):
        assert notify_mod._post_ntfy(settings, title='t', message='hello', priority='default', tags='x') is True
    assert any('ntfy disabled' in r.message for r in caplog.records)


def test_alert_only_live(session, monkeypatch):
    posts: list[str] = []

    def fake_post(settings, *, title, message, priority, tags):
        posts.append(message)
        return True

    monkeypatch.setattr(notify_mod, '_post_ntfy', fake_post)
    settings = Settings(ntfy_url='https://ntfy.example/topic')
    state = notify_mod.NotifyState(last_live_alert_id=0)
    make_candidate(session, source=E.SOURCE_REPLAY, status=E.STATUS_ALERT, bar_close=BAR_CLOSE + timedelta(minutes=1))
    live = make_candidate(session, source=E.SOURCE_LIVE, status=E.STATUS_ALERT, bar_close=BAR_CLOSE + timedelta(minutes=2))
    session.flush()
    notify_mod.run_notify(session, settings, state)
    assert len(posts) == 1
    assert live.coin in posts[0]
    assert state.last_live_alert_id == live.id


def test_stand_down_transition_posts(session, monkeypatch):
    posts: list[tuple[str, str]] = []

    def fake_post(settings, *, title, message, priority, tags):
        posts.append((title, priority))
        return True

    monkeypatch.setattr(notify_mod, '_post_ntfy', fake_post)
    settings = Settings(ntfy_url='https://ntfy.example/topic')
    inactive = notify_mod.StandDownState(active=False, kinds=(), feed_breaches=(), active_event_windows=(), upcoming_event_windows=())
    state = notify_mod.NotifyState(stand_down=inactive)

    monkeypatch.setattr(
        notify_mod,
        'current_stand_down',
        lambda s, now=None, settings=None: notify_mod.StandDownState(
            active=True,
            kinds=(C.STAND_DOWN_FEED_BREACH,),
            feed_breaches=({'feed': 'x', 'coin': 'BTCUSDT', 'reason': 'stale'},),
            active_event_windows=(),
            upcoming_event_windows=(),
        ),
    )
    notify_mod.run_notify(session, settings, state)
    assert posts
    assert posts[0][0] == 'Stand down started'
    assert posts[0][1] == 'high'


def test_init_does_not_notify(session, monkeypatch):
    monkeypatch.setattr(notify_mod, '_post_ntfy', lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not post')))
    notify_mod.init_notify_state(session)
