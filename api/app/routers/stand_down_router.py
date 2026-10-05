"""Stand-down banner endpoint (live only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.time import utc_now
from app.routers.deps import ReadonlySession
from app.worker.stand_down import current_stand_down, stand_down_to_dict

router = APIRouter(prefix='/stand-down', tags=['stand-down'])


@router.get('')
def stand_down(session: ReadonlySession) -> dict[str, Any]:
    state = current_stand_down(session, utc_now())
    return stand_down_to_dict(state)
