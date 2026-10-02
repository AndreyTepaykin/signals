"""UTC time and session logic.

Every timestamp in this system is UTC. Functions here are pure: they take
instants and return answers, with no database or clock access (except
``utc_now``). Naive datetimes are rejected rather than guessed at.

Interval conventions (all stated once, here):

* Sessions, the weekend and the US cash session are half-open: ``[start, end)``.
* Event stand-down windows are CLOSED: ``[event - before, event + after]``
  (the conservative choice for a stand-down).

Note: this module is named ``time`` to match the brief. It never imports the
standard-library ``time`` module, and nothing should run with ``app/core`` as
the working directory or on ``sys.path``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

from app.core import constants as C

UTC = timezone.utc
_NEW_YORK = ZoneInfo(C.US_CASH_TIMEZONE)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    """Current instant as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(instant: datetime) -> datetime:
    """Return ``instant`` as an aware UTC datetime.

    Raises ``ValueError`` for naive datetimes: this system never guesses a zone.
    """
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('naive datetime rejected: all timestamps must be timezone-aware (UTC)')
    return instant.astimezone(UTC)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionInfo:
    """Result of :func:`classify_session`.

    ``bucket``       raw clock bucket: ``asia`` | ``europe`` | ``us`` | ``late``.
    ``session``      the session used as the regime-slice value: ``asia`` |
                     ``europe`` | ``us``. A folded ``late`` instant reports ``asia``.
    ``session_date`` the UTC date the session is attributed to. For an
                     instant folded from ``late`` this is the NEXT calendar
                     date (the Asia session it joins); otherwise the
                     instant's own UTC date.
    ``folded``       True when a ``late`` instant was folded into the next Asia date.
    """

    bucket: str
    session: str
    session_date: date
    folded: bool


def raw_session_bucket(instant: datetime) -> str:
    """Raw UTC clock bucket (``asia``/``europe``/``us``/``late``), no folding."""
    hour = ensure_utc(instant).hour
    for name, start, end in C.SESSION_BOUNDARIES_UTC_HOURS:
        if start <= hour < end:
            return name
    raise AssertionError('SESSION_BOUNDARIES_UTC_HOURS does not cover 0-24h')  # pragma: no cover


def classify_session(instant: datetime) -> SessionInfo:
    """Classify a UTC instant into a session and its attributed date.

    Asia 00:00-07:00, Europe 07:00-13:00, US 13:00-21:00. ``late`` (21:00-24:00)
    folds into the NEXT calendar date's Asia session unless the US cash session
    is open at that instant, in which case it stays ``us`` on its own date.
    """
    utc = ensure_utc(instant)
    bucket = raw_session_bucket(utc)
    if bucket == C.SESSION_LATE:
        if is_us_cash_open(utc):
            return SessionInfo(bucket, C.SESSION_US, utc.date(), False)
        return SessionInfo(bucket, C.SESSION_ASIA, utc.date() + timedelta(days=1), True)
    return SessionInfo(bucket, bucket, utc.date(), False)


# ---------------------------------------------------------------------------
# Weekend
# ---------------------------------------------------------------------------
def _week_offset(utc: datetime) -> timedelta:
    return timedelta(
        days=utc.weekday(),
        hours=utc.hour,
        minutes=utc.minute,
        seconds=utc.second,
        microseconds=utc.microsecond,
    )


def is_weekend(instant: datetime) -> bool:
    """True from Friday 21:00 UTC (inclusive) to Sunday 21:00 UTC (exclusive)."""
    offset = _week_offset(ensure_utc(instant))
    start = timedelta(days=C.WEEKEND_START_WEEKDAY, hours=C.WEEKEND_START_HOUR_UTC)
    end = timedelta(days=C.WEEKEND_END_WEEKDAY, hours=C.WEEKEND_END_HOUR_UTC)
    return start <= offset < end


# ---------------------------------------------------------------------------
# US cash session (ETF flow, IBIT)
# ---------------------------------------------------------------------------
def is_us_dst(instant: datetime) -> bool:
    """True when the US is on daylight time at ``instant`` (America/New_York, zoneinfo)."""
    dst = ensure_utc(instant).astimezone(_NEW_YORK).dst()
    return dst is not None and dst != timedelta(0)


def _cash_hours(instant: datetime) -> tuple[tuple[int, int], tuple[int, int]]:
    return C.US_CASH_HOURS_UTC_DST if is_us_dst(instant) else C.US_CASH_HOURS_UTC_STANDARD


def us_cash_window_utc(day: date) -> tuple[datetime, datetime]:
    """UTC ``(open, close)`` of the US cash session for a UTC calendar date.

    Daylight time is decided for the instant of that day's cash open, via
    ``zoneinfo``. Weekends and exchange holidays are NOT considered: the spec
    gives clock hours only.
    """
    probe = datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)  # inside either window
    (open_h, open_m), (close_h, close_m) = _cash_hours(probe)
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return base + timedelta(hours=open_h, minutes=open_m), base + timedelta(hours=close_h, minutes=close_m)


def is_us_cash_open(instant: datetime) -> bool:
    """True inside ``[open, close)`` of the US cash session at ``instant``."""
    utc = ensure_utc(instant)
    open_at, close_at = us_cash_window_utc(utc.date())
    return open_at <= utc < close_at


# ---------------------------------------------------------------------------
# Horizons
# ---------------------------------------------------------------------------
def horizon_hours(horizon: str) -> int:
    """Vertical-barrier length in hours for ``scalp`` / ``swing`` / ``position``."""
    try:
        return C.VERTICAL_BARRIER_HOURS[horizon]
    except KeyError:
        raise ValueError(f'unknown horizon {horizon!r}; expected one of {C.HORIZONS}') from None


def vertical_barrier_deadline(entry_time: datetime, horizon: str) -> datetime:
    """Instant at which the vertical barrier expires: ``entry_time + horizon``.

    ``entry_time`` is the fill time (the next bar's open after the signal bar
    closes), not the signal bar's close.
    """
    return ensure_utc(entry_time) + timedelta(hours=horizon_hours(horizon))


# ---------------------------------------------------------------------------
# Event stand-down arithmetic (the calendar table is Milestone 1's job)
# ---------------------------------------------------------------------------
class EventTime(NamedTuple):
    """An event occurrence: ``kind`` is one of ``cpi`` / ``fomc`` / ``expiry``."""

    kind: str
    at: datetime


def event_window(kind: str, at: datetime) -> tuple[datetime, datetime]:
    """Closed stand-down window ``(start, end)`` for one event."""
    try:
        before, after = C.EVENT_WINDOW_MINUTES[kind]
    except KeyError:
        raise ValueError(f'unknown event kind {kind!r}; expected one of {tuple(C.EVENT_WINDOW_MINUTES)}') from None
    at_utc = ensure_utc(at)
    return at_utc - timedelta(minutes=before), at_utc + timedelta(minutes=after)


def active_event_windows(instant: datetime, events: Iterable[tuple[str, datetime]]) -> list[EventTime]:
    """Events whose closed stand-down window contains ``instant``."""
    utc = ensure_utc(instant)
    active: list[EventTime] = []
    for kind, at in events:
        start, end = event_window(kind, at)
        if start <= utc <= end:
            active.append(EventTime(kind, ensure_utc(at)))
    return active


def in_event_window(instant: datetime, events: Iterable[tuple[str, datetime]]) -> bool:
    """True when ``instant`` is inside any event's stand-down window.

    ``events`` is an iterable of ``(kind, event_time)``. Pass only events that
    stand the book down (CPI, FOMC, and large expiries); deciding which expiry
    is "large" belongs to the caller.
    """
    return bool(active_event_windows(instant, events))
