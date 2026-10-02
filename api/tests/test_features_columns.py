"""The feature registry matches the ``feature_row`` model, and column naming carries units.

A column added, renamed or resized in ``app/models/features.py`` must fail here
rather than go unfilled or overflow in production.
"""

from __future__ import annotations

import datetime as dt_module
from dataclasses import replace

import pytest
from sqlalchemy import Boolean, Date, DateTime, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB

from app.core.features import columns
from app.core.features.assemble import compute_features
from app.core.features.series import visible_bars
from app.models.features import SHADOW_METRIC_KEYS, FeatureRow

from feature_helpers import synthetic_inputs

TABLE = FeatureRow.__table__


def test_registry_covers_every_feature_column_and_nothing_else():
    model_columns = set(TABLE.columns.keys())
    registry = set(columns.FEATURE_COLUMNS)
    non_feature = set(columns.NON_FEATURE_COLUMNS)
    assert registry | non_feature == model_columns
    assert registry & non_feature == set()
    assert len(model_columns) == 62 and len(registry) == 53 and len(non_feature) == 9


def test_registry_order_is_the_model_order():
    model_order = [name for name in TABLE.columns.keys() if name not in columns.NON_FEATURE_COLUMNS]
    assert list(columns.FEATURE_COLUMNS) == model_order


@pytest.mark.parametrize('name', sorted(columns.FEATURE_COLUMNS))
def test_every_column_has_the_registered_type(name):
    spec = columns.FEATURE_COLUMNS[name]
    column = TABLE.columns[name]
    assert column.nullable or name == 'shadow_metrics'
    kind = spec.kind
    if kind == 'float':
        assert isinstance(column.type, Numeric)
        assert (column.type.precision, column.type.scale) == (spec.precision, spec.scale), name
    elif kind == 'sign':
        assert isinstance(column.type, SmallInteger)
    elif kind == 'bool':
        assert isinstance(column.type, Boolean)
    elif kind == 'str':
        assert isinstance(column.type, String)
    elif kind == 'datetime':
        assert isinstance(column.type, DateTime)
    elif kind == 'date':
        assert isinstance(column.type, Date)
    elif kind in ('str_list', 'json_object'):
        assert isinstance(column.type, JSONB)
    else:  # pragma: no cover
        pytest.fail(f'unknown kind {kind}')


def test_the_four_unfed_columns_are_exactly_the_documented_ones():
    assert set(columns.ALWAYS_NULL) == {'cme_basis_z', 'etf_net_flow_z', 'ibit_dealer_gamma_sign', 'cme_dealer_gamma_sign'}
    assert set(columns.ALWAYS_NULL) <= set(columns.FEATURE_COLUMNS)


# ---------------------------------------------------------------------------
# ATR-unit naming
# ---------------------------------------------------------------------------
def test_every_column_named_atr_is_an_atr_multiple_column():
    """A column whose name ends ``_atr`` is in ATR units and has the ATR-unit type; no other column does."""
    atr_named = {name for name in columns.FEATURE_COLUMNS if name.endswith('_atr')}
    assert atr_named == {
        'session_vwap_distance_atr', 'swing_vwap_distance_atr', 'poc_distance_atr', 'value_area_high_distance_atr',
        'value_area_low_distance_atr', 'chandelier_stop_distance_atr', 'perp_spot_disagreement_atr',
    }
    for name in atr_named:
        assert columns.FEATURE_COLUMNS[name] == columns.ATR_UNITS, name


def test_price_and_atr_columns_cannot_be_confused():
    """``atr_price`` is the ATR in PRICE units (what the _atr columns are divided by); it is not an _atr column."""
    assert columns.FEATURE_COLUMNS['atr_price'] == columns.PRICE
    assert not 'atr_price'.endswith('_atr')
    for name in ('atr_price', 'atr_timeframe'):
        assert name in columns.FEATURE_COLUMNS


def test_atr_columns_are_price_gap_over_the_row_s_own_atr_price():
    """Recompute each distance from the row's own stored levels: (price - level) / atr_price, in ATR units."""
    inputs = synthetic_inputs()
    row = compute_features(inputs).values
    price = visible_bars(inputs.perp_15m, inputs.bar_close)[-1].close
    atr = row['atr_price']
    assert atr is not None and atr > 0
    for distance, level in (
        ('poc_distance_atr', 'poc_price'),
        ('value_area_high_distance_atr', 'value_area_high'),
        ('value_area_low_distance_atr', 'value_area_low'),
    ):
        assert row[distance] == pytest.approx((price - row[level]) / atr, abs=2e-6), distance
    # the chandelier distance, long or short, is measured from price to the stop in the same unit
    if row['chandelier_stop_price'] is not None:
        gap = abs(price - row['chandelier_stop_price']) / atr
        assert abs(row['chandelier_stop_distance_atr']) == pytest.approx(gap, abs=2e-6)
    assert row['atr_timeframe'] == '4h'


def test_the_atr_unit_changes_with_the_atr_not_with_the_price_gap():
    """Same price gap, bigger ATR: the column in ATR units shrinks. A column that held price units would not."""
    inputs = synthetic_inputs()
    wide = replace(
        inputs,
        perp_4h=[replace(b, high=b.high + (b.high - b.low) * 2.0, low=b.low - (b.high - b.low) * 2.0) for b in inputs.perp_4h],
    )
    base, bigger = compute_features(inputs).values, compute_features(wide).values
    assert bigger['atr_price'] > base['atr_price']
    assert abs(bigger['poc_distance_atr']) < abs(base['poc_distance_atr'])
    assert bigger['poc_price'] == base['poc_price']  # the price level itself did not move


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def test_floats_are_rounded_to_the_column_scale():
    assert columns.normalise_value('rsi', 55.123456789) == 55.12345679  # numeric(12, 8)
    assert columns.normalise_value('realized_vol_pct', 0.12345678) == 0.123457  # numeric(7, 6)
    assert columns.normalise_value('atr_price', 1.23456789012345) == 1.2345678901  # numeric(24, 10)
    assert columns.normalise_value('funding_z', -1.9999996) == -2.0  # numeric(14, 6)


def test_shadow_floats_round_and_none_items_are_dropped_and_keys_sort():
    out = columns.normalise_value('shadow_metrics', {'b': None, 'z': 1.123456789123, 'a': True, 'c': 'x'})
    assert out == {'a': True, 'c': 'x', 'z': 1.12345679}
    assert list(out) == ['a', 'c', 'z']
    assert columns.normalise_value('shadow_metrics', None) == {}


def test_event_kinds_are_sorted_and_deduplicated():
    assert columns.normalise_value('active_event_kinds', ['fomc', 'cpi', 'fomc']) == ['cpi', 'fomc']
    assert columns.normalise_value('active_event_kinds', []) == []


def test_type_confusion_is_an_error():
    with pytest.raises(columns.ColumnValueError):
        columns.normalise_value('rsi', True)  # a bool is not a float
    with pytest.raises(columns.ColumnValueError):
        columns.normalise_value('squeeze_on', 1)  # an int is not a bool
    with pytest.raises(columns.ColumnValueError):
        columns.normalise_value('session_date', dt_module.datetime(2026, 1, 1, tzinfo=dt_module.timezone.utc))
    with pytest.raises(columns.ColumnValueError):
        columns.normalise_value('ibit_dealer_gamma_sign', 2)
    assert columns.normalise_value('beta_fit_end', dt_module.datetime(2026, 1, 1, tzinfo=dt_module.timezone.utc)).tzinfo is not None


def test_every_computed_value_fits_its_column():
    values = compute_features(synthetic_inputs(with_alt=True)).values
    assert set(values) == set(columns.FEATURE_COLUMNS)
    for name, value in values.items():
        assert columns.normalise_value(name, value) == value, f'{name} is not already normalised'
        spec = columns.FEATURE_COLUMNS[name]
        if spec.kind == 'float' and value is not None:
            assert abs(value) < spec.limit


def test_check_constraints_ranges_hold_for_computed_values():
    values = compute_features(synthetic_inputs(with_alt=True)).values
    assert 0.0 <= values['rsi'] <= 100.0
    assert 0.0 <= values['aggressor_imbalance'] <= 1.0
    assert 0.0 <= values['realized_vol_pct'] <= 1.0

def test_shadow_keys_the_engine_may_write_are_in_the_documented_vocabulary():
    from app.core.features import liquidation

    assert {liquidation.KEY_DISTANCE, liquidation.KEY_ESTIMATED, liquidation.KEY_RATE} <= set(SHADOW_METRIC_KEYS)


def test_beta_fit_ends_strictly_before_the_signal_bar():
    """The lag is visible on the row: the last return in the fit is the one BEFORE the signal bar's return."""
    from datetime import timedelta

    inputs = synthetic_inputs(with_alt=True)
    values = compute_features(inputs).values
    assert values['beta_fit_end'] < inputs.bar_close  # the model's CHECK constraint, shown on real output
    assert values['beta_fit_end'] == inputs.bar_close - timedelta(minutes=15)
