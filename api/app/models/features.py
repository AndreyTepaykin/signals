"""The decision-time feature store.

One row per coin, horizon and signal bar. Everything on it was computable from
prints whose ``available_at`` was at or before ``bar_close``, because the feature
engine reads feeds only through :func:`app.models.as_of.as_of`.

Three rules shape this table.

1. **Units are in the names.** ``*_atr`` is a distance in ATR units, ``*_z`` is a
   90-day z-score, ``*_pct`` is a 90-day quantile in 0-1, ``*_bps`` is basis
   points, ``*_price`` is a price. A raw funding print and a strike 2% away are
   not comparable across a 1% day and a 6% day, so neither is stored raw.
2. **Live metrics get typed columns; shadow metrics get the jsonb bag.** A
   metric whose feed exists but which has not passed the promotion gate is
   logged from day one in ``shadow_metrics``. There is no typed column for it,
   so a card renderer or a rule cannot reach it by attribute access by accident.
3. **Feeds that do not exist yet still get columns, and they stay NULL.** A
   missing print is NULL. It is never zero-filled: the spec says of CME basis
   "If the print is missing, omit the feature. Do not fill zero."

There is deliberately no ``available_at`` column here. Only raw feed rows have
one; a feature row's validity comes from how it was built, and adding an
``available_at`` would invite someone to gate features instead of feeds.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums as E
from app.models.base import (
    AtrUnits,
    Base,
    Bps,
    CreatedAt,
    Price,
    Quantile,
    Rate,
    Ratio,
    Sign,
    Size,
    SurrogateKey,
    ZScore,
)

#: Keys the shadow bag is expected to carry once their feeds exist. Documented
#: here so the bag is a known vocabulary rather than a junk drawer, and so M3
#: can be tested against it. Nothing in this list may ever be promoted to a
#: typed column without passing the promotion gate first.
SHADOW_METRIC_KEYS = (
    'liquidation_distance_atr',  # rank 4, estimated from force orders, shadow on every coin
    'liquidation_distance_estimated',  # always true while it comes from the partial stream
    'liquidation_print_rate_per_min',  # indicator: coins liquidated per minute
    'iv_rank',  # indicator: implied vol versus its own year
    'iv_term_slope',  # indicator: front month minus back month
    'top_of_book_imbalance',  # indicator: needs a depth feed we do not have
    'exchange_netflow_z',  # slow flow, needs a vendor
    'stablecoin_net_issuance_z',  # slow flow, needs a vendor
)


class FeatureRow(SurrogateKey, CreatedAt, Base):
    """Decision-time features for one coin, horizon and signal bar."""

    __tablename__ = 'feature_row'

    coin: Mapped[str] = mapped_column(nullable=False)
    horizon: Mapped[str] = mapped_column(nullable=False)
    signal_timeframe: Mapped[str] = mapped_column(
        nullable=False,
        comment='Timeframe of the bar whose close this row is keyed on.',
    )
    bar_close: Mapped[datetime] = mapped_column(
        nullable=False,
        comment='Close of the signal bar. Every feed row behind this one had available_at <= this.',
    )
    source: Mapped[str] = mapped_column(nullable=False, comment='live | replay')

    feature_hash: Mapped[str] = mapped_column(nullable=False)
    code_version: Mapped[str] = mapped_column(nullable=False)

    # ---- ATR basis: what every *_atr column on this row was divided by ----
    atr_price: Mapped[Price | None] = mapped_column(nullable=True)
    atr_timeframe: Mapped[str | None] = mapped_column(nullable=True)

    # ---- Rank 1: aggressor imbalance ----
    aggressor_imbalance: Mapped[Ratio | None] = mapped_column(
        nullable=True,
        comment='Taker-buy volume over total volume, raw.',
    )
    aggressor_imbalance_z: Mapped[ZScore | None] = mapped_column(nullable=True)

    # ---- Rank 2: OI-price quadrant ----
    oi_price_quadrant: Mapped[str | None] = mapped_column(nullable=True)
    oi_change_z: Mapped[ZScore | None] = mapped_column(
        nullable=True,
        comment='Inside the dead zone the quadrant reports *_oi_flat; the z-score is still stored.',
    )

    # ---- Rank 3: funding ----
    funding_z: Mapped[ZScore | None] = mapped_column(nullable=True, comment='Settled prints versus 90 days.')
    funding_rate_current: Mapped[Rate | None] = mapped_column(
        nullable=True,
        comment='Current (predicted) rate the live carry band reads, per interval.',
    )
    funding_carry_bps_horizon: Mapped[Bps | None] = mapped_column(
        nullable=True,
        comment='Signed carry over this horizon. The card band is this figure and its sign flip.',
    )

    # ---- Rank 5: trend efficiency ----
    trend_efficiency: Mapped[Ratio | None] = mapped_column(nullable=True)
    trend_family: Mapped[str | None] = mapped_column(
        nullable=True,
        comment='trend | chop. Between the bands the previous family is carried forward.',
    )

    # ---- Rank 6: realized vol ----
    realized_vol_yz: Mapped[Ratio | None] = mapped_column(nullable=True, comment='Yang-Zhang realized vol.')
    realized_vol_pct: Mapped[Quantile | None] = mapped_column(nullable=True, comment='Versus 90 days.')

    # ---- Rank 11: perp basis ----
    perp_basis_bps: Mapped[Bps | None] = mapped_column(nullable=True)
    perp_basis_z: Mapped[ZScore | None] = mapped_column(nullable=True)

    # ---- Ranks 12 and 13, and the US-session option books: no feed yet ----
    # These columns exist from day one so that no code change is needed when a
    # feed arrives, and they stay NULL until it does. NULL means "no print",
    # never zero.
    cme_basis_z: Mapped[ZScore | None] = mapped_column(
        nullable=True,
        comment='Rank 12. No licensed feed in Phase 1; stays NULL. Omit the feature, never fill zero.',
    )
    etf_net_flow_z: Mapped[ZScore | None] = mapped_column(
        nullable=True,
        comment='Rank 13. Timestamped at publisher time when a feed exists; stays NULL until then.',
    )
    ibit_dealer_gamma_sign: Mapped[Sign | None] = mapped_column(
        nullable=True,
        comment='US-session gamma, a separate book from Deribit. Stays NULL until a feed exists.',
    )
    cme_dealer_gamma_sign: Mapped[Sign | None] = mapped_column(
        nullable=True,
        comment='Never averaged with the Deribit sign.',
    )

    # ---- Cross-asset: lagged beta and the live residual gate ----
    beta_vs_btc: Mapped[Ratio | None] = mapped_column(
        nullable=True,
        comment='90-day rolling OLS beta, fit on returns ending BEFORE this bar. NULL for BTC.',
    )
    beta_fit_end: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment='Last return used in the beta fit. Proves the fit ended before bar_close.',
    )
    alt_residual_z_signal_bar: Mapped[ZScore | None] = mapped_column(
        nullable=True,
        comment='Residual strength ON the signal bar - the live gate. Residual over the HOLD is label-only.',
    )


    # ---- Indicators: filter ----
    cvd_delta: Mapped[Size | None] = mapped_column(nullable=True, comment='20-bar 15m taker-volume delta.')
    cvd_divergence: Mapped[bool | None] = mapped_column(nullable=True)
    cvd_divergence_against: Mapped[str | None] = mapped_column(
        nullable=True,
        comment='long | short: the direction this divergence argues against.',
    )

    # ---- Indicators: location ----
    session_vwap_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)
    swing_vwap_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)
    swing_vwap_anchor_time: Mapped[datetime | None] = mapped_column(nullable=True, comment='Last 4h swing.')
    poc_price: Mapped[Price | None] = mapped_column(nullable=True)
    poc_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)
    value_area_high: Mapped[Price | None] = mapped_column(nullable=True)
    value_area_low: Mapped[Price | None] = mapped_column(nullable=True)
    value_area_high_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)
    value_area_low_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)

    # ---- Indicators: triggers ----
    squeeze_on: Mapped[bool | None] = mapped_column(nullable=True, comment='Bollinger band inside the Keltner channel.')
    squeeze_bandwidth_ratio: Mapped[Ratio | None] = mapped_column(
        nullable=True,
        comment='Bollinger width over Keltner width; below 1 is a squeeze.',
    )
    swing_break: Mapped[bool | None] = mapped_column(nullable=True, comment='4h close beyond the last 2-bar fractal.')
    swing_break_direction: Mapped[str | None] = mapped_column(nullable=True, comment='long | short')
    last_swing_high: Mapped[Price | None] = mapped_column(nullable=True)
    last_swing_low: Mapped[Price | None] = mapped_column(nullable=True)
    swing_anchor_time: Mapped[datetime | None] = mapped_column(nullable=True)
    rsi: Mapped[Ratio | None] = mapped_column(nullable=True, comment='0-100, period from constants.')
    pullback_reset: Mapped[bool | None] = mapped_column(
        nullable=True,
        comment='Only meaningful while the efficiency family is trend.',
    )

    # ---- Indicators: stop ----
    chandelier_stop_price: Mapped[Price | None] = mapped_column(
        nullable=True,
        comment='Extreme since the swing, minus/plus the ATR multiple. This is the pre-options 1R.',
    )
    chandelier_stop_distance_atr: Mapped[AtrUnits | None] = mapped_column(nullable=True)

    # ---- Context available at decision time ----
    session: Mapped[str | None] = mapped_column(nullable=True, comment='Regime-slice session: asia | europe | us.')
    session_bucket: Mapped[str | None] = mapped_column(nullable=True, comment='Raw clock bucket, late included.')
    session_date: Mapped[date | None] = mapped_column(nullable=True, comment='Attributed UTC date (late folds forward).')
    is_weekend: Mapped[bool | None] = mapped_column(nullable=True)
    event_flag: Mapped[bool | None] = mapped_column(nullable=True, comment='An event stand-down window is active.')
    active_event_kinds: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment='Kinds whose window contains bar_close, as a json array.',
    )
    perp_spot_disagreement_atr: Mapped[AtrUnits | None] = mapped_column(
        nullable=True,
        comment='Last completed perp bar versus spot bar, in ATR. Over 1 ATR is a stand-down.',
    )

    # ---- Shadow bag ----
    shadow_metrics: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment='Metrics logged from the day their feed exists but barred from a live card. See SHADOW_METRIC_KEYS.',
    )

    __table_args__ = (
        UniqueConstraint(
            'coin',
            'horizon',
            'signal_timeframe',
            'bar_close',
            'source',
            name='uq_feature_row_series_bar_close',
        ),
        # Target of candidate's composite foreign key. Redundant as a key (id is
        # already unique) and deliberately so: it lets the database refuse a
        # candidate whose feature row belongs to a different coin, horizon,
        # signal bar or source.
        UniqueConstraint(
            'id',
            'coin',
            'horizon',
            'bar_close',
            'source',
            name='uq_feature_row_identity',
        ),
        E.one_of('horizon', E.HORIZONS),
        E.one_of('signal_timeframe', E.TIMEFRAMES),
        E.one_of('source', E.SOURCES),
        E.one_of('trend_family', E.TREND_FAMILIES),
        E.one_of('oi_price_quadrant', E.OI_PRICE_QUADRANTS),
        E.one_of('cvd_divergence_against', E.DIRECTIONS, name='cvd_divergence_against'),
        E.one_of('swing_break_direction', E.DIRECTIONS, name='swing_break_direction'),
        E.one_of('session', E.REGIME_SESSIONS),
        E.one_of('session_bucket', E.SESSION_BUCKETS),
        E.in_range('realized_vol_pct', 0, 1),
        E.in_range('aggressor_imbalance', 0, 1),
        E.in_range('rsi', 0, 100),
        CheckConstraint('ibit_dealer_gamma_sign IN (-1, 0, 1)', name='ibit_dealer_gamma_sign'),
        CheckConstraint('cme_dealer_gamma_sign IN (-1, 0, 1)', name='cme_dealer_gamma_sign'),
        CheckConstraint("jsonb_typeof(shadow_metrics) = 'object'", name='shadow_metrics_is_object'),
        CheckConstraint(
            "active_event_kinds IS NULL OR jsonb_typeof(active_event_kinds) = 'array'",
            name='active_event_kinds_is_array',
        ),
        CheckConstraint(
            'beta_fit_end IS NULL OR beta_fit_end <= bar_close',
            name='beta_fit_ends_before_signal_bar',
        ),
        Index('ix_feature_row_coin_bar_close', 'coin', 'bar_close'),
        Index('ix_feature_row_feature_hash', 'feature_hash'),
    )


__all__ = ['SHADOW_METRIC_KEYS', 'FeatureRow']
