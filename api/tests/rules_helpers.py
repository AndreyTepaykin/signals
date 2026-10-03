"""Builders for the M4 rule tests.

``make_inputs`` returns a ``RuleInputs`` that passes every gate as a long BTC
breakout candidate (trend family, swing-break event, chandelier stop 1.5 ATR below a
96,500 close, aggressor and OI agreeing). A test overrides exactly the field it is
about, so a failing assertion names the single fact that changed.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.core.rules.inputs import RuleInputs
from app.models import enums as E
from app.models.features import FeatureRow

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)  # a Monday, on the 15m grid

#: Stop 95,000; 1.5 ATR of 1,000 above it is the 96,500 reference close; 1R = 1,500.
PASSING_COLUMNS: dict[str, Any] = {
    'trend_family': 'trend',
    'swing_break_state': True,
    'swing_break_event': True,
    'swing_break_direction': 'long',
    'chandelier_stop_price': Decimal('95000'),
    'chandelier_stop_distance_atr': Decimal('1.5'),
    'atr_price': Decimal('1000'),
    'aggressor_imbalance_z': 1.2,
    'oi_price_quadrant': 'price_up_oi_up',
    'funding_z': 0.0,
    'perp_basis_z': 0.0,
    'squeeze_on': False,
    'pullback_reset': False,
    'session_vwap_distance_atr': 0.0,
    'cvd_divergence': False,
    'cvd_divergence_against': None,
    'event_flag': False,
    'perp_spot_disagreement_atr': 0.1,
}


def make_inputs(**overrides: Any) -> RuleInputs:
    values: dict[str, Any] = {
        'coin': 'BTCUSDT',
        'horizon': 'swing',
        'source': 'live',
        'bar_close': T0,
        'feature_hash': 'f' * 64,
        'is_reference_coin': True,
        **PASSING_COLUMNS,
    }
    values.update(overrides)
    return RuleInputs(**values)


def short_overrides() -> dict[str, Any]:
    """The mirror-image short breakout: stop 98,000 above a 96,500 close."""
    return {
        'swing_break_direction': 'short',
        'chandelier_stop_price': Decimal('98000'),
        'aggressor_imbalance_z': -1.2,
        'oi_price_quadrant': 'price_down_oi_up',
    }


def feature_row(
    coin: str = 'BTCUSDT',
    horizon: str = 'swing',
    bar_close: datetime = T0,
    source: str = E.SOURCE_LIVE,
    **columns: Any,
) -> FeatureRow:
    """A ``FeatureRow`` carrying the passing columns plus overrides (not yet added to a session)."""
    values: dict[str, Any] = {
        'coin': coin,
        'horizon': horizon,
        'signal_timeframe': '15m',
        'bar_close': bar_close,
        'source': source,
        'feature_hash': hashlib.sha256(f'{coin}|{horizon}|{bar_close.isoformat()}|{source}'.encode()).hexdigest(),
        'code_version': 'testver',
        **PASSING_COLUMNS,
    }
    values.update(columns)
    return FeatureRow(**values)


def add_feature_rows(session, coin: str, bar_close: datetime, *, source: str = E.SOURCE_LIVE, horizons=E.HORIZONS, **columns):
    rows = []
    for horizon in horizons:
        row = feature_row(coin, horizon, bar_close, source, **columns)
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def bars(n: int, start: datetime = T0, step: timedelta = timedelta(minutes=15)) -> list[datetime]:
    return [start + step * i for i in range(n)]
