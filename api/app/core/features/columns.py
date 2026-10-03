"""The ``feature_row`` columns this engine fills, and how each value is normalised.

``feature_row`` has 63 columns. Nine are identity or bookkeeping and are NOT
features (``id``, ``created_at``, ``coin``, ``horizon``, ``signal_timeframe``,
``bar_close``, ``source``, ``feature_hash``, ``code_version``); the other 54 are
listed here with a ``kind``. ``tests/test_features_columns.py`` compares this
registry with the SQLAlchemy model (names, numeric precision and scale), so a
column added or resized in ``app/models/features.py`` fails a test here instead
of silently going unfilled or overflowing.

Why the numbers are rounded BEFORE they are hashed
--------------------------------------------------
A ``numeric(14, 6)`` column keeps six decimals. If the hash were taken over the
raw float and the database kept the rounded one, the stored row could never
reproduce its own ``feature_hash``. So every float is rounded to its column's
scale first, and the mapping that is hashed is exactly the mapping that is
stored. ``round(x, s)`` yields the float nearest to the correctly rounded
decimal, and so does ``float(Decimal(...))`` on the way back from the database:
the two agree, which a database round-trip test confirms.

NULL is ``None`` in the mapping and in the hash. A missing value is a distinct
fact from 0.0 and hashes differently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.core.time import ensure_utc


@dataclass(frozen=True)
class Column:
    kind: str  # float | bool | str | datetime | date | sign | str_list | json_object
    precision: int | None = None
    scale: int | None = None

    @property
    def limit(self) -> float | None:
        """Exclusive absolute bound the database column can hold."""
        if self.precision is None or self.scale is None:
            return None
        return float(10 ** (self.precision - self.scale))


def _f(precision: int, scale: int) -> Column:
    return Column('float', precision, scale)


PRICE = _f(24, 10)
SIZE = _f(32, 10)
RATE = _f(18, 12)
ATR_UNITS = _f(14, 6)
Z_SCORE = _f(14, 6)
QUANTILE = _f(7, 6)
BPS = _f(14, 6)
RATIO = _f(12, 8)
SIGN = Column('sign')
BOOL = Column('bool')
STR = Column('str')
DATETIME = Column('datetime')
DATE = Column('date')
STR_LIST = Column('str_list')
JSON_OBJECT = Column('json_object')

#: Every feature column, in the order the model declares them.
FEATURE_COLUMNS: dict[str, Column] = {
    'atr_price': PRICE,
    'atr_timeframe': STR,
    'aggressor_imbalance': RATIO,
    'aggressor_imbalance_z': Z_SCORE,
    'oi_price_quadrant': STR,
    'oi_change_z': Z_SCORE,
    'funding_z': Z_SCORE,
    'funding_rate_current': RATE,
    'funding_carry_bps_horizon': BPS,
    'trend_efficiency': RATIO,
    'trend_family': STR,
    'realized_vol_yz': RATIO,
    'realized_vol_pct': QUANTILE,
    'perp_basis_bps': BPS,
    'perp_basis_z': Z_SCORE,
    'cme_basis_z': Z_SCORE,
    'etf_net_flow_z': Z_SCORE,
    'ibit_dealer_gamma_sign': SIGN,
    'cme_dealer_gamma_sign': SIGN,
    'beta_vs_btc': RATIO,
    'beta_fit_end': DATETIME,
    'alt_residual_z_signal_bar': Z_SCORE,
    'cvd_delta': SIZE,
    'cvd_divergence': BOOL,
    'cvd_divergence_against': STR,
    'session_vwap_distance_atr': ATR_UNITS,
    'swing_vwap_distance_atr': ATR_UNITS,
    'swing_vwap_anchor_time': DATETIME,
    'poc_price': PRICE,
    'poc_distance_atr': ATR_UNITS,
    'value_area_high': PRICE,
    'value_area_low': PRICE,
    'value_area_high_distance_atr': ATR_UNITS,
    'value_area_low_distance_atr': ATR_UNITS,
    'squeeze_on': BOOL,
    'squeeze_bandwidth_ratio': RATIO,
    'swing_break_state': BOOL,
    'swing_break_event': BOOL,
    'swing_break_direction': STR,
    'last_swing_high': PRICE,
    'last_swing_low': PRICE,
    'swing_anchor_time': DATETIME,
    'rsi': RATIO,
    'pullback_reset': BOOL,
    'chandelier_stop_price': PRICE,
    'chandelier_stop_distance_atr': ATR_UNITS,
    'session': STR,
    'session_bucket': STR,
    'session_date': DATE,
    'is_weekend': BOOL,
    'event_flag': BOOL,
    'active_event_kinds': STR_LIST,
    'perp_spot_disagreement_atr': ATR_UNITS,
    'shadow_metrics': JSON_OBJECT,
}

#: Columns on ``feature_row`` that are identity or bookkeeping, not features.
NON_FEATURE_COLUMNS = (
    'id',
    'created_at',
    'coin',
    'horizon',
    'signal_timeframe',
    'bar_close',
    'source',
    'feature_hash',
    'code_version',
)

#: Columns no Phase 1 feed can fill. Always NULL, never 0.
ALWAYS_NULL = ('cme_basis_z', 'etf_net_flow_z', 'ibit_dealer_gamma_sign', 'cme_dealer_gamma_sign')

SHADOW_FLOAT_DECIMALS = 8


class ColumnValueError(ValueError):
    """A computed value cannot be stored in its column."""


def normalise_value(name: str, value: Any) -> Any:
    """Round and validate one value for its column. ``None`` stays ``None``."""
    spec = FEATURE_COLUMNS[name]
    if name == 'shadow_metrics':
        return _normalise_shadow(value)
    if value is None:
        return None
    kind = spec.kind
    if kind == 'float':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ColumnValueError(f'{name}: expected a float, got {type(value).__name__}')
        number = float(value)
        if not math.isfinite(number):
            raise ColumnValueError(f'{name}: non-finite value {value!r} cannot be stored (should have been missing)')
        rounded = round(number, spec.scale or 0)
        if abs(rounded) >= (spec.limit or math.inf):
            raise ColumnValueError(f'{name}: {rounded!r} overflows numeric({spec.precision},{spec.scale})')
        return 0.0 if rounded == 0.0 else rounded  # fold -0.0
    if kind == 'bool':
        if not isinstance(value, bool):
            raise ColumnValueError(f'{name}: expected bool, got {type(value).__name__}')
        return value
    if kind == 'str':
        if not isinstance(value, str):
            raise ColumnValueError(f'{name}: expected str, got {type(value).__name__}')
        return value
    if kind == 'datetime':
        if not isinstance(value, datetime):
            raise ColumnValueError(f'{name}: expected datetime, got {type(value).__name__}')
        return ensure_utc(value)
    if kind == 'date':
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ColumnValueError(f'{name}: expected date, got {type(value).__name__}')
        return value
    if kind == 'sign':
        if isinstance(value, bool) or not isinstance(value, int) or value not in (-1, 0, 1):
            raise ColumnValueError(f'{name}: expected -1, 0 or 1')
        return value
    if kind == 'str_list':
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            raise ColumnValueError(f'{name}: expected a list of str')
        return sorted(set(value))
    raise ColumnValueError(f'{name}: unknown column kind {kind!r}')  # pragma: no cover


def _normalise_shadow(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ColumnValueError('shadow_metrics must be a dict')
    out: dict[str, Any] = {}
    for key in sorted(value):
        item = value[key]
        if item is None:
            continue  # absent key = unknown; a JSON null would be a second spelling of the same fact
        if isinstance(item, bool) or isinstance(item, str):
            out[key] = item
        elif isinstance(item, (int, float)):
            number = float(item)
            if not math.isfinite(number):
                raise ColumnValueError(f'shadow_metrics[{key!r}] is not finite')
            rounded = round(number, SHADOW_FLOAT_DECIMALS)
            out[key] = 0.0 if rounded == 0.0 else rounded
        else:
            raise ColumnValueError(f'shadow_metrics[{key!r}]: unsupported type {type(item).__name__}')
    return out


def normalise_mapping(raw: dict[str, Any]) -> dict[str, Any]:
    """Every feature column, normalised; a missing key is an error (no silent default)."""
    unknown = set(raw) - set(FEATURE_COLUMNS)
    missing = [name for name in FEATURE_COLUMNS if name not in raw]
    if unknown:
        raise ColumnValueError(f'not feature_row feature columns: {sorted(unknown)}')
    if missing:
        raise ColumnValueError(f'feature columns not provided (use None for NULL): {missing}')
    return {name: normalise_value(name, raw[name]) for name in FEATURE_COLUMNS}
