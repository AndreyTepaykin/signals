"""ntfy notification channel for stand-down transitions and live alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.time import ensure_utc, utc_now
from app.models import enums as E
from app.models.decisions import Candidate
from app.worker.stand_down import StandDownState, current_stand_down, stand_down_to_dict

log = logging.getLogger(__name__)


@dataclass
class NotifyState:
    stand_down: StandDownState | None = None
    last_live_alert_id: int = 0
    pending_stand_down_message: str | None = None


def _post_ntfy(settings: Settings, *, title: str, message: str, priority: str, tags: str) -> bool:
    if not settings.ntfy_url:
        log.info('ntfy disabled: %s — %s', title, message)
        return True
    headers = {'Title': title, 'Priority': priority, 'Tags': tags}
    if settings.ntfy_token:
        headers['Authorization'] = f'Bearer {settings.ntfy_token}'
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(settings.ntfy_url, content=message.encode('utf-8'), headers=headers)
            response.raise_for_status()
        return True
    except Exception:
        log.exception('ntfy POST failed')
        return False


def _format_stand_down_start(state: StandDownState) -> str:
    lines = [f'Stand down active: {", ".join(state.kinds)}']
    for b in state.feed_breaches[:5]:
        lines.append(f'Feed {b["feed"]} {b.get("coin") or ""}: {b["reason"]}')
    for w in state.active_event_windows[:5]:
        lines.append(f'Event {w.kind} until {w.window_end}')
    return '\n'.join(lines)


def _format_alert(c: Candidate) -> str:
    entry = c.entry_price
    stop = c.stop_price
    parts = [
        f'{c.coin} {c.direction} {c.horizon}',
        f'verdict={c.options_verdict}',
    ]
    if entry is not None:
        parts.append(f'entry={float(entry):.4g}')
    if stop is not None:
        parts.append(f'stop={float(stop):.4g}')
    return ' | '.join(parts)


def init_notify_state(session: Session) -> NotifyState:
    """Initialize without sending (worker restart must not flood)."""
    state = current_stand_down(session, utc_now())
    max_alert = session.scalar(
        select(func.max(Candidate.id)).where(Candidate.source == E.SOURCE_LIVE, Candidate.status == E.STATUS_ALERT)
    )
    return NotifyState(stand_down=state, last_live_alert_id=int(max_alert or 0))


def run_notify(session: Session, settings: Settings, ctx_state: NotifyState) -> dict[str, Any]:
    now = utc_now()
    if ctx_state.pending_stand_down_message:
        if _post_ntfy(settings, title='Stand down', message=ctx_state.pending_stand_down_message, priority='high', tags='warning'):
            ctx_state.pending_stand_down_message = None

    new_state = current_stand_down(session, now)
    old = ctx_state.stand_down
    if old is not None:
        if not old.active and new_state.active:
            msg = _format_stand_down_start(new_state)
            if not _post_ntfy(settings, title='Stand down started', message=msg, priority='high', tags='warning'):
                ctx_state.pending_stand_down_message = msg
        elif old.active and not new_state.active:
            if not _post_ntfy(settings, title='Stand down ended', message='Feeds and event windows clear.', priority='default', tags='white_check_mark'):
                ctx_state.pending_stand_down_message = 'Stand down ended (retry)'
        elif old.active and new_state.active and old.kinds != new_state.kinds:
            msg = _format_stand_down_start(new_state)
            if not _post_ntfy(settings, title='Stand down changed', message=msg, priority='high', tags='warning'):
                ctx_state.pending_stand_down_message = msg
    ctx_state.stand_down = new_state

    alerts = session.execute(
        select(Candidate)
        .where(
            Candidate.source == E.SOURCE_LIVE,
            Candidate.status == E.STATUS_ALERT,
            Candidate.id > ctx_state.last_live_alert_id,
        )
        .order_by(Candidate.id)
    ).scalars().all()
    sent = 0
    for c in alerts:
        _post_ntfy(settings, title='Live alert', message=_format_alert(c), priority='high', tags='chart_with_upwards_trend')
        ctx_state.last_live_alert_id = c.id
        sent += 1

    return {'stand_down_active': new_state.active, 'alerts_notified': sent}
