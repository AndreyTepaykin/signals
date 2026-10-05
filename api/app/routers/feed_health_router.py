"""Feed health endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.config import get_settings
from app.core.time import utc_now
from app.models.feeds import FeedHealth
from app.routers.deps import ReadonlySession
from app.worker.feeds.health import stand_down_breaches

router = APIRouter(prefix='/feed-health', tags=['feed-health'])


class FeedHealthRowOut(BaseModel):
    feed: str
    coin: str | None
    status: str
    last_print_at: datetime | None
    last_available_at: datetime | None
    last_checked_at: datetime | None
    staleness_seconds: int | None
    max_staleness_seconds: int | None
    detail: str | None


class FeedHealthOut(BaseModel):
    rows: list[FeedHealthRowOut]
    stand_down_breaches: list[dict[str, Any]]


@router.get('', response_model=FeedHealthOut)
def feed_health(session: ReadonlySession) -> FeedHealthOut:
    settings = get_settings()
    now = utc_now()
    rows = session.execute(select(FeedHealth).order_by(FeedHealth.feed, FeedHealth.coin)).scalars().all()
    breaches = stand_down_breaches(session, now, settings=settings, coins=settings.watchlist_symbols)
    return FeedHealthOut(
        rows=[
            FeedHealthRowOut(
                feed=r.feed,
                coin=r.coin,
                status=r.status,
                last_print_at=r.last_print_at,
                last_available_at=r.last_available_at,
                last_checked_at=r.last_checked_at,
                staleness_seconds=r.staleness_seconds,
                max_staleness_seconds=r.max_staleness_seconds,
                detail=r.detail,
            )
            for r in rows
        ],
        stand_down_breaches=[
            {
                'feed': b.feed,
                'coin': b.coin,
                'reason': b.reason,
                'staleness_seconds': b.staleness_seconds,
                'max_staleness_seconds': b.max_staleness_seconds,
            }
            for b in breaches
        ],
    )
