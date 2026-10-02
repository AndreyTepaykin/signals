"""Options book snapshots and the figures derived from them.

``venue`` is part of the key of both tables, never a mixed-in attribute: the
spec says Deribit and IBIT "are scored separately and never blended", so there
must be no way to write a row that averages two books, and no way to read one
without naming the venue.

A derived row is keyed by ``horizon`` as well, because almost every figure here
is defined against "the expiry nearest the hold" and that expiry differs for a
4h scalp and a 72h position.

Both tables are feed rows: the options check at decision time reads them through
:func:`app.models.as_of.as_of`, so a snapshot that arrived after the signal bar
closed cannot influence that bar's card.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums as E
from app.models.base import AtrUnits, Base, FeedRow, Hours, Price, Quantile, Ratio, Sign, Size


class OptionsSnapshot(FeedRow, Base):
    """One raw public-book snapshot, stored verbatim.

    ``snapshot_time`` is the venue's snapshot instant (economic); ``available_at``
    is when we had it. The 15-minute staleness rule that turns a verdict into
    ``ignore`` is measured from ``snapshot_time``, and the constant for it lives
    in ``app.core.constants.OPTIONS_SNAPSHOT_MAX_AGE_MINUTES``.
    """

    __tablename__ = 'options_snapshot'

    venue: Mapped[str] = mapped_column(nullable=False)
    coin: Mapped[str] = mapped_column(nullable=False)
    snapshot_time: Mapped[datetime] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        nullable=False,
        comment='Raw venue response, unmodified, so a metric can be recomputed later.',
    )
    instrument_count: Mapped[int | None] = mapped_column(nullable=True)
    total_open_interest: Mapped[Size | None] = mapped_column(
        nullable=True,
        comment='Book depth for the per-coin OI floor; also feeds options_confidence.',
    )

    __table_args__ = (
        UniqueConstraint('venue', 'coin', 'snapshot_time', name='uq_options_snapshot_venue_coin_snapshot_time'),
        E.one_of('venue', E.OPTIONS_VENUES, name='venue'),
        CheckConstraint('available_at >= snapshot_time', name='available_after_snapshot'),
        Index('ix_options_snapshot_venue_coin_available_at', 'venue', 'coin', 'available_at'),
    )


class OptionsMetric(FeedRow, Base):
    """Figures derived from one snapshot, for one horizon's nearest expiry.

    Ranks 7-10 of the metric table plus max pain and OI concentration. These are
    modifiers: the options check reads them, and may delay, resize, widen the
    stop or cap the target. None of them touches P.

    Every distance is in ATR units and every historical comparison is a 90-day
    quantile, as the column names say. ``atr_price_used`` records the ATR the
    conversion used so a stored distance can be audited back to a price.
    """

    __tablename__ = 'options_metric'

    snapshot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey('options_snapshot.id', ondelete='CASCADE'),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(nullable=False)
    coin: Mapped[str] = mapped_column(nullable=False)
    snapshot_time: Mapped[datetime] = mapped_column(nullable=False)
    horizon: Mapped[str] = mapped_column(
        nullable=False,
        comment='Hold the nearest expiry was chosen for: scalp | swing | position.',
    )

    # Rank 7 - dealer gamma sign and flip distance.
    dealer_gamma_sign: Mapped[Sign | None] = mapped_column(
        nullable=True,
        comment='-1 dealers short gamma, +1 dealers long gamma, 0 undetermined.',
    )
    dealer_gamma_estimated: Mapped[bool] = mapped_column(
        nullable=False,
        server_default='true',
        comment='Public data gives OI by strike, not customer direction. Always an estimate so far.',
    )
    dealer_gamma_proxy: Mapped[str | None] = mapped_column(
        nullable=True,
        comment='Name of the documented proxy that produced the sign.',
    )
    gamma_flip_price: Mapped[Price | None] = mapped_column(nullable=True)
    gamma_flip_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)

    # Rank 8 - expiry-weighted path strikes.
    nearest_path_strike: Mapped[Price | None] = mapped_column(nullable=True)
    nearest_path_strike_side: Mapped[str | None] = mapped_column(nullable=True, comment='put | call')
    nearest_path_strike_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)
    nearest_path_strike_oi_share: Mapped[Ratio | None] = mapped_column(
        nullable=True,
        comment="That strike's share of near-dated OI (the headwind test).",
    )

    # Rank 9 - 25-delta skew.
    skew_25d: Mapped[Ratio | None] = mapped_column(nullable=True, comment='Put IV minus call IV at 25 delta.')
    skew_25d_pct: Mapped[Quantile | None] = mapped_column(nullable=True, comment='90-day quantile of skew_25d.')

    # Rank 10 - same-tenor IV minus realized.
    atm_iv: Mapped[Ratio | None] = mapped_column(nullable=True, comment='ATM implied vol of the nearest expiry.')
    same_tenor_rv: Mapped[Ratio | None] = mapped_column(nullable=True, comment='Yang-Zhang RV of that same length.')
    iv_minus_rv: Mapped[Ratio | None] = mapped_column(nullable=True)
    iv_minus_rv_pct: Mapped[Quantile | None] = mapped_column(
        nullable=True,
        comment='90-day quantile; drives the vol_rich / vol_cheap tags.',
    )

    # expiry_pin inputs and book depth.
    max_pain_strike: Mapped[Price | None] = mapped_column(
        nullable=True,
        comment='Not a signal on its own. Enters only as expiry_pin.',
    )
    top_strike_oi_share: Mapped[Ratio | None] = mapped_column(
        nullable=True,
        comment="Largest strike's share of THAT expiry's OI (the expiry_pin test).",
    )
    expiry_time: Mapped[datetime | None] = mapped_column(nullable=True, comment='Expiry nearest the hold.')
    hours_to_expiry: Mapped[Hours | None] = mapped_column(nullable=True)
    expiry_open_interest: Mapped[Size | None] = mapped_column(nullable=True)
    near_dated_open_interest: Mapped[Size | None] = mapped_column(
        nullable=True,
        comment='Headwind denominator. What counts as near-dated is M5\'s to define.',
    )
    total_open_interest: Mapped[Size | None] = mapped_column(nullable=True)

    atr_price_used: Mapped[Price | None] = mapped_column(
        nullable=True,
        comment='ATR in price terms used to convert every *_atr column here.',
    )

    __table_args__ = (
        UniqueConstraint(
            'venue',
            'coin',
            'snapshot_time',
            'horizon',
            name='uq_options_metric_venue_coin_snapshot_time_horizon',
        ),
        E.one_of('venue', E.OPTIONS_VENUES, name='venue'),
        E.one_of('horizon', E.HORIZONS),
        E.one_of('nearest_path_strike_side', E.OPTION_SIDES, name='path_strike_side'),
        CheckConstraint('dealer_gamma_sign IN (-1, 0, 1)', name='dealer_gamma_sign'),
        E.in_range('skew_25d_pct', 0, 1),
        E.in_range('iv_minus_rv_pct', 0, 1),
        E.in_range('nearest_path_strike_oi_share', 0, 1),
        E.in_range('top_strike_oi_share', 0, 1),
        CheckConstraint('available_at >= snapshot_time', name='available_after_snapshot'),
        Index('ix_options_metric_venue_coin_horizon_available_at', 'venue', 'coin', 'horizon', 'available_at'),
        Index('ix_options_metric_snapshot_id', 'snapshot_id'),
    )


__all__ = ['OptionsMetric', 'OptionsSnapshot']
