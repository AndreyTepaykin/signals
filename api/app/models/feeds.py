"""Raw feed tables.

Every table here is a :class:`~app.models.base.FeedRow`: it stores the economic
instant it describes AND ``available_at``, the instant the print could first
have been known. Nothing in this module is ever read with a bare
``select(...)``; reads go through :func:`app.models.as_of.as_of`.

``feed_health`` is the one exception - it is a status table about the feeds, not
a feed row, so it carries no ``available_at`` and is not gated.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums as E
from app.models.base import Base, CreatedAt, FeedRow, Price, Rate, Size, SurrogateKey


class Candle(FeedRow, Base):
    """A closed OHLCV bar.

    ``open_time`` is the economic instant the bar starts; ``close_time`` is when
    it ends; ``available_at`` is when it could be known, which for a kline is
    the close. The CHECK below is the schema-level statement of that rule: a
    bar can never be available before it has closed.
    """

    __tablename__ = 'candle'

    coin: Mapped[str] = mapped_column(nullable=False)
    market: Mapped[str] = mapped_column(nullable=False, comment='perp | spot')
    timeframe: Mapped[str] = mapped_column(nullable=False)
    open_time: Mapped[datetime] = mapped_column(nullable=False)
    close_time: Mapped[datetime] = mapped_column(nullable=False)

    open_price: Mapped[Price] = mapped_column(nullable=False)
    high_price: Mapped[Price] = mapped_column(nullable=False)
    low_price: Mapped[Price] = mapped_column(nullable=False)
    close_price: Mapped[Price] = mapped_column(nullable=False)

    base_volume: Mapped[Size] = mapped_column(nullable=False)
    quote_volume: Mapped[Size | None] = mapped_column(nullable=True)
    taker_buy_base_volume: Mapped[Size] = mapped_column(
        nullable=False,
        comment='Aggressor imbalance (rank 1) is this over base_volume.',
    )
    taker_buy_quote_volume: Mapped[Size | None] = mapped_column(nullable=True)
    trade_count: Mapped[int | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint('coin', 'market', 'timeframe', 'open_time', name='uq_candle_series_open_time'),
        E.one_of('timeframe', E.TIMEFRAMES),
        E.one_of('market', E.CANDLE_MARKETS),
        CheckConstraint('close_time > open_time', name='close_after_open'),
        CheckConstraint('available_at >= close_time', name='available_after_close'),
        CheckConstraint('high_price >= low_price', name='high_above_low'),
        CheckConstraint('taker_buy_base_volume <= base_volume', name='taker_buy_within_volume'),
        E.non_negative('base_volume'),
        Index('ix_candle_series_available_at', 'coin', 'market', 'timeframe', 'available_at'),
    )


class OpenInterest(FeedRow, Base):
    """Open interest as of the close of a poll bar (rank 2, the OI-price quadrant)."""

    __tablename__ = 'open_interest'

    coin: Mapped[str] = mapped_column(nullable=False)
    #: Binance's open-interest history period. Its vocabulary is wider than the
    #: kline timeframes we read, so this column deliberately has no CHECK; M2
    #: owns which periods it polls.
    timeframe: Mapped[str] = mapped_column(nullable=False)
    poll_time: Mapped[datetime] = mapped_column(
        nullable=False,
        comment='Close of the poll bar the reading describes.',
    )

    open_interest_base: Mapped[Size] = mapped_column(nullable=False, comment='Contracts / base units.')
    open_interest_quote: Mapped[Size | None] = mapped_column(nullable=True, comment='Notional in quote terms.')

    __table_args__ = (
        UniqueConstraint('coin', 'timeframe', 'poll_time', name='uq_open_interest_series_poll_time'),
        CheckConstraint('available_at >= poll_time', name='available_after_poll'),
        E.non_negative('open_interest_base'),
        Index('ix_open_interest_series_available_at', 'coin', 'timeframe', 'available_at'),
    )


class Funding(FeedRow, Base):
    """A funding print.

    The clearest available-at case in the system: a settled print describes a
    period that ended at ``funding_time`` but could only be known once it
    settled, so ``available_at >= funding_time`` is enforced for
    ``kind = 'settled'``. A ``'predicted'`` row is the opposite - it is the
    current rate for a settlement that has not happened yet, so its
    ``funding_time`` is in the future and the CHECK deliberately exempts it.

    The funding z-score (rank 3) reads settled prints. The live carry band in
    the label contract reads the predicted rate. Labels use neither: they use
    the funding that actually printed during the hold.
    """

    __tablename__ = 'funding'

    coin: Mapped[str] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(nullable=False, comment='settled | predicted')
    funding_time: Mapped[datetime] = mapped_column(
        nullable=False,
        comment='Settlement instant the rate applies to (economic time, not available_at).',
    )
    funding_rate: Mapped[Rate] = mapped_column(nullable=False, comment='Per interval, not annualised.')
    interval_hours: Mapped[int | None] = mapped_column(
        nullable=True,
        comment='Settlement interval the venue used; NULL when the feed did not say.',
    )
    mark_price: Mapped[Price | None] = mapped_column(nullable=True)
    index_price: Mapped[Price | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint('coin', 'funding_time', 'kind', name='uq_funding_coin_funding_time_kind'),
        E.one_of('kind', E.FUNDING_KINDS, name='kind'),
        CheckConstraint(
            f"kind <> '{E.FUNDING_SETTLED}' OR available_at >= funding_time",
            name='settled_available_after_settlement',
        ),
        Index('ix_funding_coin_kind_available_at', 'coin', 'kind', 'available_at'),
    )


class ForceOrder(FeedRow, Base):
    """A liquidation print from the force-order stream.

    The stream is partial and carries no venue-side identifier, so there is no
    honest natural key: two genuine liquidations can share coin, side, price and
    size in the same millisecond. ``dedup_key`` is an optional ingest-side hash
    that lets M2 make an at-least-once reinsert idempotent; it is unique when
    present and NULL otherwise.

    Feeds the liquidation-distance estimate, which stays flagged estimated and
    in shadow. It cannot be backfilled, so replay leaves it null.
    """

    __tablename__ = 'force_order'

    coin: Mapped[str] = mapped_column(nullable=False)
    side: Mapped[str] = mapped_column(nullable=False, comment='buy | sell (the side of the liquidating order)')
    traded_at: Mapped[datetime] = mapped_column(nullable=False, comment='Venue trade time (economic).')
    event_time: Mapped[datetime | None] = mapped_column(nullable=True, comment='Venue stream event time.')

    price: Mapped[Price] = mapped_column(nullable=False)
    average_price: Mapped[Price | None] = mapped_column(nullable=True)
    original_quantity: Mapped[Size] = mapped_column(nullable=False)
    filled_quantity: Mapped[Size | None] = mapped_column(nullable=True)
    order_status: Mapped[str | None] = mapped_column(nullable=True, comment='Venue status text, stored verbatim.')
    dedup_key: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint('dedup_key', name='uq_force_order_dedup_key'),
        E.one_of('side', E.FORCE_ORDER_SIDES, name='side'),
        E.non_negative('original_quantity'),
        Index('ix_force_order_coin_available_at', 'coin', 'available_at'),
        Index('ix_force_order_coin_traded_at', 'coin', 'traded_at'),
    )


class EventCalendar(FeedRow, Base):
    """Locally kept calendar of CPI, FOMC, expiry, unlock and funding settlement.

    The stand-down window lengths live in ``app.core.constants`` and the
    arithmetic in ``app.core.time.event_window``; this table stores only the
    occurrences. ``kind`` values in ``STAND_DOWN_EVENT_KINDS`` stand the book
    down, the rest are slice features only.

    ``is_large`` and ``notional_usd`` exist because the spec stands the book down
    around "a large expiry" without ever defining large. NULL means nobody has
    decided yet, and must not be read as "not large".
    """

    __tablename__ = 'event_calendar'

    kind: Mapped[str] = mapped_column(nullable=False)
    coin: Mapped[str | None] = mapped_column(nullable=True, comment='NULL for macro events (CPI, FOMC).')
    event_time: Mapped[datetime] = mapped_column(nullable=False, comment='When the event happens (economic).')
    title: Mapped[str | None] = mapped_column(nullable=True)
    feed_source: Mapped[str | None] = mapped_column(nullable=True, comment='Where the entry came from.')
    is_large: Mapped[bool | None] = mapped_column(
        nullable=True,
        comment='Expiry size classification. NULL = undecided, never "not large".',
    )
    notional_usd: Mapped[Size | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            'kind',
            'coin',
            'event_time',
            name='uq_event_calendar_kind_coin_event_time',
            postgresql_nulls_not_distinct=True,
        ),
        E.one_of('kind', E.EVENT_KINDS, name='kind'),
        Index('ix_event_calendar_event_time_kind', 'event_time', 'kind'),
        Index('ix_event_calendar_available_at_event_time', 'available_at', 'event_time'),
    )


class FeedHealth(SurrogateKey, CreatedAt, Base):
    """Last-seen and staleness per feed, so the stand-down rule can query it.

    Not a feed row: it has no ``available_at`` and is not gated by ``as_of``. It
    describes the feeds rather than carrying a print.

    ``max_staleness_seconds`` is NULL until somebody configures a limit. The
    spec gives a number for exactly one feed (Deribit, 15 minutes, which lives
    in ``constants.OPTIONS_SNAPSHOT_MAX_AGE_MINUTES``); M2 owns the rest. A NULL
    limit means "no rule configured" and must not be read as "fresh".
    """

    __tablename__ = 'feed_health'

    feed: Mapped[str] = mapped_column(nullable=False, comment='Feed identifier, e.g. binance_kline_15m.')
    coin: Mapped[str | None] = mapped_column(nullable=True, comment='NULL for feeds that are not per-coin.')

    last_print_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment='Economic time of the newest row seen.',
    )
    last_available_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment='available_at of the newest row seen - what staleness is measured from.',
    )
    last_checked_at: Mapped[datetime] = mapped_column(nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(nullable=True)

    rows_last_window: Mapped[int | None] = mapped_column(nullable=True)
    window_minutes: Mapped[int | None] = mapped_column(nullable=True)
    staleness_seconds: Mapped[int | None] = mapped_column(
        nullable=True,
        comment='Whole seconds since last_available_at, as measured at last_checked_at.',
    )
    max_staleness_seconds: Mapped[int | None] = mapped_column(nullable=True)

    status: Mapped[str] = mapped_column(nullable=False, server_default='unknown')
    detail: Mapped[str | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint('feed', 'coin', name='uq_feed_health_feed_coin', postgresql_nulls_not_distinct=True),
        E.one_of('status', E.FEED_STATUSES, name='status'),
        Index('ix_feed_health_status_last_checked_at', 'status', 'last_checked_at'),
    )


__all__ = ['Candle', 'EventCalendar', 'FeedHealth', 'ForceOrder', 'Funding', 'OpenInterest']
