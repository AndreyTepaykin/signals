"""Live stand-down state for the dashboard banner and ntfy notifier.

Perp-vs-spot disagreement is evaluated per signal bar on ``feature_row`` and
stand-downs individual candidates in the rules job; it is **not** included in
this banner (only feed staleness and macro/expiry event windows).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core import constants as C
from app.core.time import active_event_windows, ensure_utc, event_window, utc_now
from app.models.as_of import as_of
from app.models.feeds import EventCalendar
from app.worker.feeds.health import FeedBreach, stand_down_breaches


@dataclass(frozen=True)
class EventWindowActive:
    kind: str
    coin: str | None
    event_time: str
    window_start: str
    window_end: str
    title: str | None


@dataclass(frozen=True)
class StandDownState:
    active: bool
    kinds: tuple[str, ...]
    feed_breaches: tuple[dict[str, Any], ...]
    active_event_windows: tuple[EventWindowActive, ...]
    upcoming_event_windows: tuple[EventWindowActive, ...]


def _calendar_events_for_stand_down(session: Session, now: datetime) -> list[tuple[str, datetime, str | None, str | None, bool | None]]:
    """Visible calendar rows that can participate in event-window stand-down."""
    now = ensure_utc(now)
    reader = as_of(now)
    lo = now - timedelta(days=1)
    hi = now + timedelta(days=1)
    stmt = reader.select(EventCalendar).where(EventCalendar.event_time >= lo, EventCalendar.event_time <= hi)
    rows = session.execute(stmt.order_by(EventCalendar.event_time, EventCalendar.id)).scalars().all()
    out: list[tuple[str, datetime, str | None, str | None, bool | None]] = []
    for row in rows:
        if row.kind not in C.EVENT_WINDOW_MINUTES:
            continue
        if row.kind == C.EVENT_KIND_EXPIRY and row.is_large is False:
            continue
        out.append((row.kind, row.event_time, row.coin, row.title, row.is_large))
    return out


def _event_window_rows(
    events: list[tuple[str, datetime, str | None, str | None, bool | None]],
    *,
    now: datetime,
    upcoming_hours: float | None,
) -> tuple[list[EventWindowActive], list[EventWindowActive]]:
    """Split active windows at ``now`` from upcoming windows in the next ``upcoming_hours``."""
    now = ensure_utc(now)
    active: list[EventWindowActive] = []
    upcoming: list[EventWindowActive] = []
    windowed = [(kind, at) for kind, at, *_ in events]
    active_kinds = {e.kind for e in active_event_windows(now, windowed)}
    horizon = now + timedelta(hours=upcoming_hours) if upcoming_hours is not None else now

    for kind, at, coin, title, _is_large in events:
        start, end = event_window(kind, at)
        row = EventWindowActive(
            kind=kind,
            coin=coin,
            event_time=at.isoformat(),
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            title=title,
        )
        if start <= now <= end:
            active.append(row)
        elif upcoming_hours is not None and now < start <= horizon:
            upcoming.append(row)
    return active, upcoming


def current_stand_down(
    session: Session,
    now: datetime | None = None,
    *,
    settings: Settings | None = None,
) -> StandDownState:
    settings = settings or get_settings()
    now = ensure_utc(now or utc_now())
    breaches = stand_down_breaches(session, now, settings=settings, coins=settings.watchlist_symbols)
    events = _calendar_events_for_stand_down(session, now)
    active_windows, upcoming = _event_window_rows(events, now=now, upcoming_hours=24.0)

    kinds: list[str] = []
    if breaches:
        kinds.append(C.STAND_DOWN_FEED_BREACH)
    if active_windows:
        kinds.append(C.STAND_DOWN_EVENT_WINDOW)

    breach_dicts = tuple(
        {
            'feed': b.feed,
            'coin': b.coin,
            'reason': b.reason,
            'staleness_seconds': b.staleness_seconds,
            'max_staleness_seconds': b.max_staleness_seconds,
        }
        for b in breaches
    )
    return StandDownState(
        active=bool(kinds),
        kinds=tuple(kinds),
        feed_breaches=breach_dicts,
        active_event_windows=tuple(active_windows),
        upcoming_event_windows=tuple(upcoming),
    )


def stand_down_to_dict(state: StandDownState) -> dict[str, Any]:
    return {
        'active': state.active,
        'kinds': list(state.kinds),
        'feed_breaches': list(state.feed_breaches),
        'active_event_windows': [w.__dict__ for w in state.active_event_windows],
        'upcoming_event_windows': [w.__dict__ for w in state.upcoming_event_windows],
    }
