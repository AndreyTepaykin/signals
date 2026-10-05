"""WebSocket push channel for dashboard refresh hints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core.db_sync import get_sync_engine
from app.core.hashing import rule_hash as current_rule_hash
from app.core.time import utc_now
from app.models.decisions import Candidate
from app.models.feeds import FeedHealth
from app.routers.stats_cache import fingerprint
from app.worker.stand_down import current_stand_down, stand_down_to_dict
from app.worker.stats_job import readonly_session

log = logging.getLogger(__name__)
router = APIRouter(tags=['websocket'])

# Test hooks: override session factory and poll interval.
SnapshotSessionFactory = Callable[[], AbstractContextManager[Session]]
snapshot_session_factory: SnapshotSessionFactory | None = None
ws_poll_seconds_override: float | None = None


def _session_factory() -> AbstractContextManager[Session]:
    if snapshot_session_factory is not None:
        return snapshot_session_factory()
    return readonly_session(get_sync_engine())


def poll_interval_seconds() -> float:
    if ws_poll_seconds_override is not None:
        return ws_poll_seconds_override
    return float(get_settings().ws_poll_seconds)


def stand_down_identity(stand_down: dict[str, Any]) -> tuple[Any, ...]:
    """Stable key for push deduplication (staleness_seconds excluded)."""
    breaches = stand_down.get('feed_breaches') or []
    breach_key = tuple(sorted((b.get('feed'), b.get('coin')) for b in breaches))
    windows = stand_down.get('active_event_windows') or []
    window_key = tuple(sorted((w.get('kind'), w.get('event_time')) for w in windows))
    kinds = tuple(sorted(stand_down.get('kinds') or []))
    return (bool(stand_down.get('active')), kinds, breach_key, window_key)


def poll_snapshot(source: str = 'live') -> dict[str, Any]:
    with _session_factory() as session:
        fp = fingerprint(session, source)
        max_candidate = session.scalar(select(func.max(Candidate.id)).where(Candidate.source == source))
        stand = stand_down_to_dict(current_stand_down(session, utc_now()))
        max_feed_checked = session.scalar(select(func.max(FeedHealth.last_checked_at)))
        return {
            'fingerprint': {
                'rule_hash': current_rule_hash(),
                'source': source,
                'label_count': fp[0],
                'max_label_id': fp[1],
                'max_candidate_id': max_candidate,
                'estimate_count': fp[3],
            },
            'stand_down': stand,
            'max_feed_health_checked_at': max_feed_checked.isoformat() if max_feed_checked else None,
        }


@router.websocket('/ws')
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    poll_seconds = poll_interval_seconds()
    source = 'live'
    try:
        snap = await run_in_threadpool(poll_snapshot, source)
        await websocket.send_json({'type': 'hello', **snap})
        prev_fp = snap['fingerprint']
        prev_stand_id = stand_down_identity(snap['stand_down'])
        prev_feed = snap['max_feed_health_checked_at']
        while True:
            await asyncio.sleep(poll_seconds)
            snap = await run_in_threadpool(poll_snapshot, source)
            fp = snap['fingerprint']
            if (
                fp['max_candidate_id'] != prev_fp['max_candidate_id']
                or fp['label_count'] != prev_fp['label_count']
                or fp['max_label_id'] != prev_fp['max_label_id']
                or fp['estimate_count'] != prev_fp['estimate_count']
            ):
                await websocket.send_json({'type': 'candidates', 'fingerprint': fp})
                if fp['label_count'] != prev_fp['label_count'] or fp['max_label_id'] != prev_fp['max_label_id']:
                    await websocket.send_json({'type': 'labels', 'fingerprint': fp})
                prev_fp = fp
            stand_id = stand_down_identity(snap['stand_down'])
            if stand_id != prev_stand_id:
                await websocket.send_json({'type': 'stand_down', 'stand_down': snap['stand_down']})
                prev_stand_id = stand_id
            if snap['max_feed_health_checked_at'] != prev_feed:
                await websocket.send_json({'type': 'feed_health', 'max_feed_health_checked_at': snap['max_feed_health_checked_at']})
                prev_feed = snap['max_feed_health_checked_at']
    except WebSocketDisconnect:
        return
    except Exception:
        log.exception('websocket poll failed')
        await websocket.close()
