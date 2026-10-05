"""Shared serializers for API responses (decimals as floats)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core import constants as C
from app.core.stats import params as P
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.features import FeatureRow

DRIVER_FIELDS: tuple[str, ...] = (
    'aggressor_imbalance_z',
    'oi_price_quadrant',
    'funding_z',
    'perp_basis_z',
    'realized_vol_pct',
    'trend_efficiency',
    'cvd_divergence',
    'alt_residual_z_signal_bar',
    'oi_change_z',
)


def fnum(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return float(value)


def vol_bucket(pct: float | None) -> str | None:
    if pct is None:
        return None
    low, high = P.VOL_BUCKET_EDGES
    if pct < low:
        return P.VOL_BUCKET_NAMES[0]
    if pct < high:
        return P.VOL_BUCKET_NAMES[1]
    return P.VOL_BUCKET_NAMES[2]


def drivers_from_feature(row: FeatureRow | None) -> list[dict[str, Any]]:
    if row is None:
        return []
    out: list[dict[str, Any]] = []
    for name in DRIVER_FIELDS:
        val = getattr(row, name)
        if val is None:
            continue
        if isinstance(val, Decimal):
            val = float(val)
        out.append({'name': name, 'value': val})
    return out


def card_action(candidate: Candidate) -> str:
    if candidate.status == E.STATUS_ALERT:
        return candidate.direction
    return 'watch'


def edge_hidden_block(candidate: Candidate) -> tuple[bool, str | None]:
    pool_n = candidate.pool_n
    floor = C.POOL_MIN_INDEPENDENT_OUTCOMES
    if pool_n is None:
        return True, f'no pool reached {floor} independent outcomes when this decision was taken'
    if pool_n < floor:
        return True, f'pool has {pool_n} of {floor} independent outcomes'
    return False, None


def cost_line(feature: FeatureRow | None) -> dict[str, Any]:
    band = None
    if feature is not None and feature.funding_carry_bps_horizon is not None:
        band = float(feature.funding_carry_bps_horizon)
    return {
        'taker_fee_bps_per_side': float(C.TAKER_FEE_BPS_PER_SIDE),
        'slippage_bps_per_side': float(C.SLIPPAGE_BPS_PER_SIDE),
        'round_trip_bps': float(C.ROUND_TRIP_COST_BPS),
        'funding_carry_bps_horizon': band,
    }
