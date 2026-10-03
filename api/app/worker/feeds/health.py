"""Feed health and the staleness rule behind the "feed past its available-at" stand-down.

The spec says "Stand down when a feed is past its available-at rule" and gives
exactly one number (Deribit, 15 minutes). Milestone 0 deliberately left the rest
undefined; this module defines them, from each feed's own cadence.

The rule
--------
A feed is **stale** when ``now - last_available_at`` exceeds

    max_staleness = period + poll_interval + grace

* ``period`` - how often the feed *produces* a new row. A 15m kline is
  legitimately up to 15 minutes old right up until the next bar closes.
* ``poll_interval`` - our own polling cadence; the new row can sit on the
  exchange for up to one poll interval before we fetch it.
* ``grace`` - ``Settings.feed_staleness_grace_seconds`` (default 120 s): exchange
  publication lag plus one missed poll.

This is the same thing as "the newest bar is more than ``poll + grace`` overdue":
at the instant bar N+1 should have been written, the newest row is bar N and its
age is exactly ``period``; the limit then allows ``poll + grace`` of slack. So a
1h series is declared stale about three minutes after a missing close, not an
hour after, and the limit does not loosen as the timeframe grows.

All of it is configuration (``app/config.py``), never ``constants.py``: these
are operating parameters, and changing one must not change the rule hash of
every historical label.

Open interest and its publication lag
-------------------------------------
An open-interest row is stored with ``available_at = poll boundary + publication lag``
(``Settings.feed_open_interest_publication_lag_seconds``) and, like a kline, is only
stored once ``now >= available_at``. The limit above is measured from
``available_at``, so the lag is ALREADY inside it: consecutive rows are exactly one
period apart in ``available_at`` and the next one is fetched within one poll
interval, whatever the lag is. Adding the lag to the limit again would double-count
it and loosen the stand-down by that much for no reason. What the lag does change is
the failure mode when Binance is SLOWER than the allowance on some boundary: the
row is fetched late, and that excess (not the whole lag) must fit inside ``grace``.
The allowance is chosen above the slowest measured publication for that reason.

The same reasoning holds for settled funding (``available_at = funding_time + 30 s``): the
interval between consecutive prints is unchanged, so its limit is unchanged. Klines are
judged from bar close, which is still their ``available_at``; the settle margin delays only
the WRITE (about 30 s), well inside ``grace``.

Staleness is judged by the CLOCK at query time
----------------------------------------------
``feed_health.status`` is what the worker last wrote. A dead worker writes
nothing, so its last status would read ``ok`` forever. The stand-down must
therefore compare ``now`` against ``last_available_at`` itself. That is
:func:`stand_down_breaches`, and in SQL it is::

    SELECT feed, coin FROM feed_health
    WHERE  last_available_at IS NULL
       OR  max_staleness_seconds IS NULL                -- no rule: NOT fresh
       OR  EXTRACT(EPOCH FROM (:now - last_available_at)) > max_staleness_seconds;

plus any expected (feed, coin) with no row at all, which only the Python
function sees (it knows what should exist).

The force-order feed is exempt: it is partial, estimated and shadow-only, so it
cannot change a live alert, and a quiet market produces no prints at all, which
is indistinguishable from a dead stream by data alone. Its liveness is tracked
through ``status`` and ``last_success_at`` (connection heartbeat) instead.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core import constants as C
from app.core.time import ensure_utc
from app.models import enums as E
from app.models.as_of import as_of
from app.models.feeds import Candle, FeedHealth, Funding, OpenInterest
from app.models.options import OptionsSnapshot
from app.worker.feeds import records as R

FEED_FUNDING_SETTLED = 'binance_funding_settled'
FEED_FUNDING_PREDICTED = 'binance_funding_predicted'
FEED_FORCE_ORDER = 'binance_force_order'
#: The Deribit public book, one row per options coin (BTC, ETH). Added to the expected feeds only on request
#: (`include_options`): see `expected_feeds`.
FEED_DERIBIT_BOOK = 'deribit_options_book'

#: Feeds that do not take part in the stand-down rule (see module docstring).
NON_GATING_FEEDS = frozenset({FEED_FORCE_ORDER})

FORCE_ORDER_DETAIL = (
    'Partial stream: Binance pushes at most the latest liquidation per symbol per second. '
    'Anything derived from it is an ESTIMATE and stays in shadow. Live collection only; '
    'there is no historical endpoint, so it cannot be backfilled.'
)


def kline_feed(market: str, timeframe: str) -> str:
    return f'binance_kline_{market}_{timeframe}'


def open_interest_feed(period: str) -> str:
    return f'binance_open_interest_{period}'


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------
def staleness_limit_seconds(*, period_seconds: int, poll_seconds: int, grace_seconds: int) -> int:
    """``period + poll interval + grace``. See the module docstring for why."""
    for name, value in (('period', period_seconds), ('poll', poll_seconds), ('grace', grace_seconds)):
        if value < 0:
            raise ValueError(f'{name} must not be negative, got {value}')
    return period_seconds + poll_seconds + grace_seconds


def staleness_seconds(last_available_at: datetime | None, now: datetime) -> int | None:
    """Whole seconds since ``last_available_at`` (``None`` if the feed never produced a row)."""
    if last_available_at is None:
        return None
    return max(0, int((ensure_utc(now) - ensure_utc(last_available_at)).total_seconds()))


@dataclass(frozen=True)
class FeedSpec:
    feed: str
    coin: str | None
    period_seconds: int
    poll_seconds: int
    max_staleness_seconds: int | None
    gating: bool = True
    reason: str = ''  # how the limit was derived, for humans


def expected_feeds(
    settings: Settings | None = None,
    coins: Iterable[str] | None = None,
    *,
    include_options: bool = False,
) -> list[FeedSpec]:
    """Every (feed, coin) the live worker is supposed to keep fresh, with its limit.

    ``include_options`` adds the Deribit book (BTC and ETH only, limit = the spec's 15 minutes). It is
    opt-in because a Deribit outage must not stand down alts or the Binance-only gates: the options check
    turns a stale book into ``ignore`` by itself. Callers that want the feed treated as a gating feed
    (``refresh_feed_health``, ``stand_down_breaches``) pass the flag.
    """
    settings = settings or get_settings()
    coins = tuple(coins) if coins is not None else settings.watchlist_symbols
    grace = settings.feed_staleness_grace_seconds
    specs: list[FeedSpec] = []

    for market in E.CANDLE_MARKETS:
        for timeframe in E.TIMEFRAMES:
            period = int(R.timeframe_delta(timeframe).total_seconds())
            poll = settings.feed_kline_poll_seconds
            for coin in coins:
                specs.append(
                    FeedSpec(
                        kline_feed(market, timeframe),
                        coin,
                        period,
                        poll,
                        staleness_limit_seconds(period_seconds=period, poll_seconds=poll, grace_seconds=grace),
                        reason=f'{timeframe} bar + {poll}s poll + {grace}s grace',
                    )
                )
    for oi_period in settings.open_interest_periods:
        period = int(R.period_delta(oi_period).total_seconds())
        poll = settings.feed_open_interest_poll_seconds
        for coin in coins:
            specs.append(
                FeedSpec(
                    open_interest_feed(oi_period),
                    coin,
                    period,
                    poll,
                    staleness_limit_seconds(period_seconds=period, poll_seconds=poll, grace_seconds=grace),
                    reason=f'{oi_period} sample + {poll}s poll + {grace}s grace (measured from available_at, which already includes the publication lag)',
                )
            )
    settled_period = settings.feed_funding_interval_hours * 3600
    settled_poll = settings.feed_funding_settled_poll_seconds
    predicted_poll = settings.feed_funding_predicted_poll_seconds
    for coin in coins:
        specs.append(
            FeedSpec(
                FEED_FUNDING_SETTLED,
                coin,
                settled_period,
                settled_poll,
                staleness_limit_seconds(period_seconds=settled_period, poll_seconds=settled_poll, grace_seconds=grace),
                reason=f'{settings.feed_funding_interval_hours}h settlement + {settled_poll}s poll + {grace}s grace',
            )
        )
        specs.append(
            FeedSpec(
                FEED_FUNDING_PREDICTED,
                coin,
                predicted_poll,  # a rolling estimate: it is "produced" every poll
                predicted_poll,
                staleness_limit_seconds(period_seconds=0, poll_seconds=predicted_poll, grace_seconds=grace),
                reason=f'rolling estimate: {predicted_poll}s poll + {grace}s grace',
            )
        )
    if include_options:
        poll = settings.feed_deribit_poll_seconds
        limit = C.OPTIONS_SNAPSHOT_MAX_AGE_MINUTES * 60
        for coin in coins:
            if coin in C.OPTIONS_CURRENCY_BY_COIN:
                specs.append(
                    FeedSpec(
                        FEED_DERIBIT_BOOK,
                        coin,
                        poll,
                        poll,
                        limit,
                        reason=(
                            f'spec: a Deribit snapshot older than {C.OPTIONS_SNAPSHOT_MAX_AGE_MINUTES} minutes is stale '
                            f'(measured from available_at = snapshot + {settings.feed_deribit_publication_lag_seconds}s lag)'
                        ),
                    )
                )
    specs.append(
        FeedSpec(
            FEED_FORCE_ORDER,
            None,
            0,
            0,
            None,
            gating=False,
            reason='partial, estimated, shadow-only: no stand-down rule; liveness via status/last_success_at',
        )
    )
    return specs


# ---------------------------------------------------------------------------
# attempts (written by jobs, merged into feed_health by the single health writer)
# ---------------------------------------------------------------------------
@dataclass
class Attempt:
    last_checked_at: datetime
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None


class AttemptLog:
    """Thread-safe record of the latest fetch outcome per (feed, coin)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str | None], Attempt] = {}
        self._lock = threading.Lock()

    def success(self, feed: str, coin: str | None, at: datetime) -> None:
        with self._lock:
            item = self._items.setdefault((feed, coin), Attempt(at))
            item.last_checked_at, item.last_success_at = at, at

    def error(self, feed: str, coin: str | None, at: datetime, message: str) -> None:
        with self._lock:
            item = self._items.setdefault((feed, coin), Attempt(at))
            item.last_checked_at, item.last_error_at, item.last_error = at, at, message[:500]

    def get(self, feed: str, coin: str | None) -> Attempt | None:
        with self._lock:
            item = self._items.get((feed, coin))
            return None if item is None else Attempt(**vars(item))


# ---------------------------------------------------------------------------
# refresh: the single writer of data-derived feed_health columns
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FeedStatus:
    feed: str
    coin: str | None
    status: str
    last_print_at: datetime | None
    last_available_at: datetime | None
    staleness_seconds: int | None
    max_staleness_seconds: int | None
    rows_last_window: int | None = None
    window_minutes: int | None = None
    detail: str | None = None


def classify(
    *, staleness: int | None, limit: int | None, last_attempt_failed: bool
) -> str:
    """``unknown`` (no data / no rule), ``ok``, ``stale`` (late but fetches succeed) or ``down`` (late and failing)."""
    if staleness is None or limit is None:
        return 'unknown'
    if staleness <= limit:
        return 'ok'
    return 'down' if last_attempt_failed else 'stale'


def _latest_print(session: Session, now: datetime, spec: FeedSpec) -> tuple[datetime | None, datetime | None]:
    """``(economic time, available_at)`` of the newest stored row for the spec."""
    reader = as_of(now)
    coin = spec.coin
    if spec.feed.startswith('binance_kline_'):
        _, _, market, timeframe = spec.feed.split('_', 3)
        statement = (
            reader.select(Candle.open_time, Candle.available_at)
            .where(Candle.coin == coin, Candle.market == market, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
    elif spec.feed.startswith('binance_open_interest_'):
        period = spec.feed.removeprefix('binance_open_interest_')
        statement = (
            reader.select(OpenInterest.poll_time, OpenInterest.available_at)
            .where(OpenInterest.coin == coin, OpenInterest.timeframe == period)
            .order_by(OpenInterest.poll_time.desc())
            .limit(1)
        )
    elif spec.feed == FEED_FUNDING_SETTLED:
        statement = (
            reader.select(Funding.funding_time, Funding.available_at)
            .where(Funding.coin == coin, Funding.kind == E.FUNDING_SETTLED)
            .order_by(Funding.funding_time.desc())
            .limit(1)
        )
    elif spec.feed == FEED_FUNDING_PREDICTED:
        statement = (
            reader.select(Funding.funding_time, Funding.available_at)
            .where(Funding.coin == coin, Funding.kind == E.FUNDING_PREDICTED)
            .order_by(Funding.available_at.desc())
            .limit(1)
        )
    elif spec.feed == FEED_DERIBIT_BOOK:
        statement = (
            reader.select(OptionsSnapshot.snapshot_time, OptionsSnapshot.available_at)
            .where(OptionsSnapshot.venue == E.OPTIONS_VENUE_DERIBIT, OptionsSnapshot.coin == coin)
            .order_by(OptionsSnapshot.snapshot_time.desc())
            .limit(1)
        )
    else:
        raise ValueError(f'no reader for feed {spec.feed!r}')
    row = session.execute(statement).first()
    return (None, None) if row is None else (ensure_utc(row[0]), ensure_utc(row[1]))


def _rows_in_window(session: Session, now: datetime, spec: FeedSpec, window: timedelta) -> int:
    since = now - window
    if spec.feed.startswith('binance_kline_'):
        _, _, market, timeframe = spec.feed.split('_', 3)
        where = [Candle.coin == spec.coin, Candle.market == market, Candle.timeframe == timeframe, Candle.available_at > since]
        stmt = as_of(now).gate(sa_select(func.count(Candle.id)).where(*where))
    elif spec.feed.startswith('binance_open_interest_'):
        period = spec.feed.removeprefix('binance_open_interest_')
        where = [OpenInterest.coin == spec.coin, OpenInterest.timeframe == period, OpenInterest.available_at > since]
        stmt = as_of(now).gate(sa_select(func.count(OpenInterest.id)).where(*where))
    elif spec.feed == FEED_DERIBIT_BOOK:
        where = [OptionsSnapshot.venue == E.OPTIONS_VENUE_DERIBIT, OptionsSnapshot.coin == spec.coin, OptionsSnapshot.available_at > since]
        stmt = as_of(now).gate(sa_select(func.count(OptionsSnapshot.id)).where(*where))
    else:
        kind = E.FUNDING_SETTLED if spec.feed == FEED_FUNDING_SETTLED else E.FUNDING_PREDICTED
        where = [Funding.coin == spec.coin, Funding.kind == kind, Funding.available_at > since]
        stmt = as_of(now).gate(sa_select(func.count(Funding.id)).where(*where))
    return int(session.execute(stmt).scalar() or 0)


def refresh_feed_health(
    session: Session,
    *,
    now: datetime,
    attempts: AttemptLog,
    settings: Settings | None = None,
    coins: Iterable[str] | None = None,
    include_options: bool = False,
) -> list[FeedStatus]:
    """Recompute and store the health row of every gating feed. Does not commit.

    This is the only writer of the data-derived columns for these feeds (the
    force-order collector owns its own row), so there is no read-modify-write
    race between jobs: jobs only report attempts in memory.
    """
    now = ensure_utc(now)
    settings = settings or get_settings()
    existing: dict[tuple[str, str | None], FeedHealth] = {
        (row.feed, row.coin): row for row in session.execute(sa_select(FeedHealth)).scalars()
    }
    statuses: list[FeedStatus] = []
    values: list[dict[str, Any]] = []

    for spec in expected_feeds(settings, coins, include_options=include_options):
        if not spec.gating:
            continue
        printed, available = _latest_print(session, now, spec)
        age = staleness_seconds(available, now)
        attempt = attempts.get(spec.feed, spec.coin)
        old = existing.get((spec.feed, spec.coin))

        last_success = _latest(attempt.last_success_at if attempt else None, old.last_success_at if old else None)
        last_error = _latest(attempt.last_error_at if attempt else None, old.last_error_at if old else None)
        failing = last_error is not None and (last_success is None or last_error > last_success)

        status = classify(staleness=age, limit=spec.max_staleness_seconds, last_attempt_failed=failing)
        window_minutes = max(60, spec.period_seconds // 60)
        rows = _rows_in_window(session, now, spec, timedelta(minutes=window_minutes))
        detail = _detail(status, age, spec, attempt.last_error if (attempt and failing) else None)

        statuses.append(
            FeedStatus(spec.feed, spec.coin, status, printed, available, age, spec.max_staleness_seconds, rows, window_minutes, detail)
        )
        values.append(
            {
                'feed': spec.feed,
                'coin': spec.coin,
                'last_print_at': printed,
                'last_available_at': available,
                'last_checked_at': now,
                'last_success_at': last_success,
                'last_error_at': last_error,
                'rows_last_window': rows,
                'window_minutes': window_minutes,
                'staleness_seconds': age,
                'max_staleness_seconds': spec.max_staleness_seconds,
                'status': status,
                'detail': detail,
            }
        )

    _upsert(session, values)
    return statuses


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    candidates = [x for x in (a, b) if x is not None]
    return max(candidates) if candidates else None


def _detail(status: str, age: int | None, spec: FeedSpec, last_error: str | None) -> str:
    if age is None:
        base = 'no row has ever been stored for this feed'
    else:
        base = f'newest row is {age}s old; limit {spec.max_staleness_seconds}s ({spec.reason})'
    if last_error and status in ('stale', 'down'):
        base += f'; last fetch error: {last_error}'
    return base[:900]


def _upsert(session: Session, values: Sequence[dict[str, Any]]) -> None:
    for start in range(0, len(values), 200):
        chunk = list(values[start : start + 200])
        insert = pg_insert(FeedHealth).values(chunk)
        columns = {key: getattr(insert.excluded, key) for key in chunk[0] if key not in ('feed', 'coin')}
        columns['updated_at'] = func.now()
        session.execute(insert.on_conflict_do_update(index_elements=['feed', 'coin'], set_=columns))


def write_force_order_health(
    session: Session,
    *,
    now: datetime,
    connected: bool,
    last_traded_at: datetime | None,
    last_available_at: datetime | None,
    rows: int,
    window_minutes: int,
    error: str | None = None,
) -> None:
    """Upsert the force-order stream's row. Liveness is the connection, not the prints."""
    now = ensure_utc(now)
    detail = FORCE_ORDER_DETAIL if error is None else f'{FORCE_ORDER_DETAIL} Last error: {error[:300]}'
    values: dict[str, Any] = {
        'feed': FEED_FORCE_ORDER,
        'coin': None,
        'last_print_at': last_traded_at,
        'last_available_at': last_available_at,
        'last_checked_at': now,
        'rows_last_window': rows,
        'window_minutes': window_minutes,
        'staleness_seconds': staleness_seconds(last_available_at, now),
        'max_staleness_seconds': None,
        'status': 'ok' if connected else 'down',
        'detail': detail,
    }
    if connected:
        values['last_success_at'] = now
    else:
        values['last_error_at'] = now
    insert = pg_insert(FeedHealth).values(values)
    columns = {key: getattr(insert.excluded, key) for key in values if key not in ('feed', 'coin')}
    columns['updated_at'] = func.now()
    session.execute(insert.on_conflict_do_update(index_elements=['feed', 'coin'], set_=columns))


# ---------------------------------------------------------------------------
# the stand-down query
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FeedBreach:
    feed: str
    coin: str | None
    reason: str
    staleness_seconds: int | None
    max_staleness_seconds: int | None


def stand_down_breaches(
    session: Session,
    now: datetime,
    settings: Settings | None = None,
    coins: Iterable[str] | None = None,
    *,
    include_options: bool = False,
) -> list[FeedBreach]:
    """Gating feeds that are past their available-at rule at ``now``. Empty means clear.

    ``include_options=True`` also judges the Deribit book (BTC and ETH, 15 minutes). The rules job does NOT pass
    it: a stale book is an ``ignore`` verdict for that coin, not a stand-down of every candidate.

    A feed counts as a breach when it has no ``feed_health`` row at all, never
    produced a row, has no staleness rule configured (NULL is "no rule", never
    "fresh"), or is older than its limit **by the clock at ``now``** - not by the
    status the worker last wrote, which a dead worker can no longer update.
    """
    now = ensure_utc(now)
    rows: Mapping[tuple[str, str | None], FeedHealth] = {
        (row.feed, row.coin): row for row in session.execute(sa_select(FeedHealth)).scalars()
    }
    breaches: list[FeedBreach] = []
    for spec in expected_feeds(settings, coins, include_options=include_options):
        if not spec.gating:
            continue
        row = rows.get((spec.feed, spec.coin))
        if row is None:
            breaches.append(FeedBreach(spec.feed, spec.coin, 'no feed_health row: the feed has never run', None, spec.max_staleness_seconds))
            continue
        age = staleness_seconds(row.last_available_at, now)
        if age is None:
            breaches.append(FeedBreach(spec.feed, spec.coin, 'the feed has never produced a row', None, row.max_staleness_seconds))
        elif row.max_staleness_seconds is None:
            breaches.append(FeedBreach(spec.feed, spec.coin, 'no staleness rule configured', age, None))
        elif age > row.max_staleness_seconds:
            breaches.append(
                FeedBreach(spec.feed, spec.coin, f'{age}s old, limit {row.max_staleness_seconds}s', age, row.max_staleness_seconds)
            )
    return breaches
