"""The slice of a ``feature_row`` the frozen rules read.

``RuleInputs`` is built from a stored ``feature_row`` (or any object with the
same attribute names). Prices stay ``Decimal`` so the stop and target are exact;
every z-score, ratio and distance is a ``float``. ``None`` means NULL on the row
and is never coerced to zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.time import ensure_utc


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


@dataclass(frozen=True)
class RuleInputs:
    coin: str
    horizon: str
    source: str
    bar_close: datetime
    feature_hash: str
    is_reference_coin: bool
    trend_family: str | None = None
    # swing break (M3.1)
    swing_break_state: bool | None = None
    swing_break_event: bool | None = None
    swing_break_direction: str | None = None
    chandelier_stop_price: Decimal | None = None
    chandelier_stop_distance_atr: Decimal | None = None
    atr_price: Decimal | None = None
    # direction vote
    aggressor_imbalance_z: float | None = None
    oi_price_quadrant: str | None = None
    funding_z: float | None = None
    perp_basis_z: float | None = None
    # triggers
    squeeze_on: bool | None = None
    pullback_reset: bool | None = None
    session_vwap_distance_atr: float | None = None
    # filters and gates
    cvd_divergence: bool | None = None
    cvd_divergence_against: str | None = None
    alt_residual_z_signal_bar: float | None = None
    event_flag: bool | None = None
    perp_spot_disagreement_atr: float | None = None

    @classmethod
    def from_feature_row(cls, row: Any, *, reference_coin: str) -> RuleInputs:
        """Read the rule columns off a ``FeatureRow`` (attribute access only)."""
        return cls(
            coin=row.coin,
            horizon=row.horizon,
            source=row.source,
            bar_close=ensure_utc(row.bar_close),
            feature_hash=row.feature_hash,
            is_reference_coin=row.coin == reference_coin,
            trend_family=row.trend_family,
            swing_break_state=row.swing_break_state,
            swing_break_event=row.swing_break_event,
            swing_break_direction=row.swing_break_direction,
            chandelier_stop_price=_decimal(row.chandelier_stop_price),
            chandelier_stop_distance_atr=_decimal(row.chandelier_stop_distance_atr),
            atr_price=_decimal(row.atr_price),
            aggressor_imbalance_z=_float(row.aggressor_imbalance_z),
            oi_price_quadrant=row.oi_price_quadrant,
            funding_z=_float(row.funding_z),
            perp_basis_z=_float(row.perp_basis_z),
            squeeze_on=row.squeeze_on,
            pullback_reset=row.pullback_reset,
            session_vwap_distance_atr=_float(row.session_vwap_distance_atr),
            cvd_divergence=row.cvd_divergence,
            cvd_divergence_against=row.cvd_divergence_against,
            alt_residual_z_signal_bar=_float(row.alt_residual_z_signal_bar),
            event_flag=row.event_flag,
            perp_spot_disagreement_atr=_float(row.perp_spot_disagreement_atr),
        )


__all__ = ['RuleInputs']
