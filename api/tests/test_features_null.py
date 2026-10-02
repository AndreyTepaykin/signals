"""Missing is NULL, never zero.

An input that is unavailable must give a NULL feature with a stated reason. It
must not give 0.0, False, an empty string, a stale value reused from the
previous bar, NaN or infinity. And a feature that is genuinely zero must stay
zero (the opposite mistake: NULLing a real reading).
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from app.core import constants as C
from app.core.features import columns
from app.core.features.assemble import compute_features
from app.core.features.reading import Reading, have, missing
from app.core.features.series import CalendarEvent

from feature_helpers import M15, dt, synthetic_inputs

END = dt(2026, 6, 1)  # a 4h boundary, so every timeframe has its bar at T


@pytest.fixture(scope='module')
def alt():
    return synthetic_inputs(with_alt=True)


@pytest.fixture(scope='module')
def base(alt):
    return compute_features(alt)


def _null(result, *names):
    for name in names:
        assert result.values[name] is None, f'{name} should be NULL, got {result.values[name]!r}'
        assert result.reasons.get(name), f'{name} is NULL but gives no reason'


def _present(result, *names):
    for name in names:
        assert result.values[name] is not None, f'{name} is unexpectedly NULL: {result.reasons.get(name)}'


ATR_DISTANCES = (
    'session_vwap_distance_atr',
    'swing_vwap_distance_atr',
    'poc_distance_atr',
    'value_area_high_distance_atr',
    'value_area_low_distance_atr',
    'chandelier_stop_distance_atr',
    'perp_spot_disagreement_atr',
)


def test_control_a_complete_market_fills_what_it_can(base):
    # The columns below must be non-NULL in the complete synthetic market, so the NULL tests that
    # follow are meaningful: each NULL they demand is caused by the input they remove.
    _present(
        base,
        'atr_price', 'atr_timeframe', 'aggressor_imbalance', 'aggressor_imbalance_z', 'oi_price_quadrant',
        'oi_change_z', 'funding_z', 'trend_efficiency', 'trend_family', 'realized_vol_yz', 'realized_vol_pct',
        'perp_basis_bps', 'perp_basis_z', 'beta_vs_btc', 'beta_fit_end', 'alt_residual_z_signal_bar', 'cvd_delta',
        'cvd_divergence', 'session_vwap_distance_atr', 'poc_price', 'value_area_high', 'value_area_low',
        'poc_distance_atr', 'squeeze_on', 'squeeze_bandwidth_ratio', 'rsi', 'perp_spot_disagreement_atr', 'session',
        'session_bucket', 'session_date', 'is_weekend',
    )


def test_the_four_columns_with_no_phase_1_feed_are_always_null(base, alt):
    for result in (base, compute_features(replace(alt, source='live'))):
        _null(result, *columns.ALWAYS_NULL)
        assert all(result.values[name] is None for name in columns.ALWAYS_NULL)


def test_no_open_interest_gives_null_quadrant_and_z(alt):
    result = compute_features(replace(alt, oi_1h=[]))
    _null(result, 'oi_price_quadrant', 'oi_change_z')
    _present(result, 'funding_z', 'perp_basis_z')  # nothing else was disturbed


def test_stale_open_interest_is_null_not_the_last_value(alt):
    """OI that stopped 3 hours before the bar is not 'current OI'."""
    stopped = [p for p in alt.oi_1h if p.available_at <= END - timedelta(hours=3)]
    result = compute_features(replace(alt, oi_1h=stopped))
    _null(result, 'oi_price_quadrant', 'oi_change_z')


def test_no_settled_funding_gives_null_z(alt):
    _null(compute_features(replace(alt, funding_settled=[])), 'funding_z')


def test_stale_funding_is_null_not_the_last_print(alt):
    old = [p for p in alt.funding_settled if p.available_at <= END - timedelta(hours=20)]
    _null(compute_features(replace(alt, funding_settled=old)), 'funding_z')


def test_no_spot_gives_null_basis_and_disagreement(alt):
    result = compute_features(replace(alt, spot_15m=[], spot_1h=[]))
    _null(result, 'perp_basis_bps', 'perp_basis_z', 'perp_spot_disagreement_atr')
    _present(result, 'atr_price', 'rsi')


def test_no_4h_series_nulls_the_atr_and_everything_divided_by_it(alt):
    result = compute_features(replace(alt, perp_4h=[]))
    _null(result, 'atr_price', 'atr_timeframe', 'swing_break', 'swing_break_direction', 'last_swing_high',
          'last_swing_low', 'swing_anchor_time', 'swing_vwap_anchor_time', 'chandelier_stop_price', *ATR_DISTANCES)
    # levels that are prices, not ATR multiples, do not need the ATR
    _present(result, 'poc_price', 'value_area_high', 'value_area_low', 'rsi', 'squeeze_on', 'cvd_delta')


def test_a_stale_4h_series_is_null_not_the_previous_bars_values(alt):
    """The newest 4h bar is missing: the swing state belongs to the PREVIOUS 4h bar and must not be reused."""
    behind = [b for b in alt.perp_4h if b.close_time < END]
    result = compute_features(replace(alt, perp_4h=behind))
    _null(result, 'atr_price', 'swing_break', 'last_swing_high', 'chandelier_stop_price')


def test_no_signal_bar_nulls_everything_that_is_about_the_signal_bar(alt):
    without = [b for b in alt.perp_15m if b.close_time != END]
    result = compute_features(replace(alt, perp_15m=without))
    _null(result, 'aggressor_imbalance', 'cvd_delta', 'rsi', 'squeeze_on', 'session_vwap_distance_atr', 'poc_distance_atr')


def test_short_history_gives_null_z_scores_but_keeps_the_raw_readings(alt):
    start = END - timedelta(days=3)
    short = replace(
        alt,
        perp_15m=[b for b in alt.perp_15m if b.open_time >= start],
        spot_15m=[b for b in alt.spot_15m if b.open_time >= start],
        perp_1h=[b for b in alt.perp_1h if b.open_time >= start],
        spot_1h=[b for b in alt.spot_1h if b.open_time >= start],
        btc_perp_15m=[b for b in alt.btc_perp_15m if b.open_time >= start],
        oi_1h=[p for p in alt.oi_1h if p.poll_time >= start],
        funding_settled=[p for p in alt.funding_settled if p.funding_time >= start],
    )
    result = compute_features(short)
    _null(result, 'aggressor_imbalance_z', 'oi_change_z', 'funding_z', 'realized_vol_pct', 'perp_basis_z', 'beta_vs_btc',
          'alt_residual_z_signal_bar', 'beta_fit_end')
    _present(result, 'aggressor_imbalance', 'atr_price', 'rsi', 'realized_vol_yz', 'perp_basis_bps', 'trend_efficiency')
    for name in ('aggressor_imbalance_z', 'funding_z', 'perp_basis_z'):
        assert 'need' in result.reasons[name]  # the reason names the shortfall


def test_a_hole_in_the_1h_series_nulls_what_needs_consecutive_bars(alt):
    holed = [b for b in alt.perp_1h if b.close_time != END - timedelta(hours=5)]
    result = compute_features(replace(alt, perp_1h=holed))
    _null(result, 'trend_efficiency', 'trend_family', 'realized_vol_yz', 'realized_vol_pct')
    _present(result, 'perp_basis_bps', 'atr_price')


def test_a_1h_series_that_stopped_is_null_not_stale(alt):
    behind = [b for b in alt.perp_1h if b.close_time < END]
    result = compute_features(replace(alt, perp_1h=behind))
    _null(result, 'trend_efficiency', 'trend_family', 'realized_vol_yz', 'realized_vol_pct')


def test_zero_volume_gives_null_not_zero(alt):
    day = END - timedelta(days=1)
    dead = [replace(b, volume=0.0, taker_buy_volume=0.0) if b.open_time >= day else b for b in alt.perp_15m]
    result = compute_features(replace(alt, perp_15m=dead))
    _null(result, 'aggressor_imbalance', 'session_vwap_distance_atr', 'poc_price', 'value_area_high', 'value_area_low')
    assert result.values['aggressor_imbalance'] is None  # 0/0 is not 0.5 and not 0.0


def test_a_flat_market_gives_null_z_not_zero_or_infinity(alt):
    flat = [replace(b, open=100.0, high=100.0, low=100.0, close=100.0, volume=10.0, taker_buy_volume=5.0) for b in alt.perp_15m]
    result = compute_features(replace(alt, perp_15m=flat, spot_15m=flat, btc_perp_15m=flat))
    _null(result, 'aggressor_imbalance_z')  # zero variance in the baseline
    assert result.values['aggressor_imbalance'] == 0.5  # the raw reading is a real number
    for name, value in result.values.items():
        assert not (isinstance(value, float) and (math.isnan(value) or math.isinf(value))), name


def test_btc_has_no_beta_to_itself_and_an_alt_without_btc_has_none(alt, base):
    btc = compute_features(replace(alt, is_reference_coin=True, btc_perp_15m=None))
    _null(btc, 'beta_vs_btc', 'beta_fit_end', 'alt_residual_z_signal_bar')
    orphan = compute_features(replace(alt, btc_perp_15m=None))
    _null(orphan, 'beta_vs_btc', 'beta_fit_end', 'alt_residual_z_signal_bar')
    _present(base, 'beta_vs_btc')  # with the reference series it exists


def test_an_empty_calendar_is_unknown_not_no_event(alt):
    unloaded = compute_features(replace(alt, calendar_loaded=False))
    _null(unloaded, 'event_flag', 'active_event_kinds')
    loaded_empty = compute_features(replace(alt, calendar_loaded=True, events=()))
    assert loaded_empty.values['event_flag'] is False and loaded_empty.values['active_event_kinds'] == []
    kind = next(iter(C.EVENT_WINDOW_MINUTES))
    near = CalendarEvent(kind, END + timedelta(minutes=5), END - timedelta(days=2), True)
    flagged = compute_features(replace(alt, calendar_loaded=True, events=(near,)))
    assert flagged.values['event_flag'] is True and flagged.values['active_event_kinds'] == [kind]
    unpublished = CalendarEvent(kind, END + timedelta(minutes=5), END + timedelta(minutes=1), True)  # not yet known at T
    hidden = compute_features(replace(alt, calendar_loaded=True, events=(unpublished,)))
    assert hidden.values['event_flag'] is False


def test_no_force_orders_gives_an_empty_shadow_bag_not_zeros(base):
    assert base.values['shadow_metrics'] == {}
    assert base.reasons.get('shadow_metrics')


def test_a_replay_row_has_null_carry_band_with_a_reason(base):
    _null(base, 'funding_rate_current', 'funding_carry_bps_horizon')


def test_a_live_row_without_a_predicted_print_has_null_carry_band(alt):
    _null(compute_features(replace(alt, source='live')), 'funding_rate_current', 'funding_carry_bps_horizon')


# ---------------------------------------------------------------------------
# The opposite mistake: a genuine zero is NOT turned into NULL
# ---------------------------------------------------------------------------
def test_a_real_zero_is_kept(alt):
    """Balanced taker flow over the 20-bar window: cvd_delta is exactly 0.0, which is a reading."""
    start = END - M15 * 30
    balanced = [replace(b, taker_buy_volume=b.volume / 2.0) if b.open_time >= start else b for b in alt.perp_15m]
    result = compute_features(replace(alt, perp_15m=balanced))
    assert result.values['cvd_delta'] == 0.0 and result.values['cvd_delta'] is not None
    assert result.values['cvd_divergence'] is False  # False is a real answer, not a missing one
    assert 'cvd_delta' not in result.reasons


# ---------------------------------------------------------------------------
# Invariants over every variant above and the representation itself
# ---------------------------------------------------------------------------
def test_every_null_with_a_reason_is_none_and_nothing_is_nan(alt, base):
    variants = [
        base,
        compute_features(replace(alt, oi_1h=[], funding_settled=[], spot_15m=[], spot_1h=[])),
        compute_features(replace(alt, perp_4h=[], perp_1h=[], perp_15m=[])),
        compute_features(replace(alt, source='live')),
    ]
    for result in variants:
        assert set(result.values) == set(columns.FEATURE_COLUMNS)
        for name, reason in result.reasons.items():
            assert reason
            assert result.values[name] in (None, {}), f'{name} has a reason ({reason}) but value {result.values[name]!r}'
        for name, value in result.values.items():
            if isinstance(value, float):
                assert math.isfinite(value), name
        assert len(result.reasons) == sum(1 for v in result.values.values() if v is None or v == {})


def test_an_entirely_empty_market_gives_a_row_of_nulls_not_an_error_or_zeros(alt):
    empty = replace(alt, perp_15m=[], spot_15m=[], perp_1h=[], spot_1h=[], perp_4h=[], btc_perp_15m=[], oi_1h=[], funding_settled=[])
    result = compute_features(empty)
    nulls = {name for name, value in result.values.items() if value is None}
    # only the clock-derived context columns can exist with no market data at all
    assert {'session', 'session_bucket', 'session_date', 'is_weekend'}.isdisjoint(nulls)
    assert nulls == set(columns.FEATURE_COLUMNS) - {'session', 'session_bucket', 'session_date', 'is_weekend', 'shadow_metrics'}
    assert not any(v == 0.0 for v in result.values.values() if isinstance(v, float))


def test_reading_cannot_spell_missing_as_zero_or_nan():
    with pytest.raises(ValueError):
        Reading(None)  # missing needs a reason
    with pytest.raises(ValueError):
        Reading(1.0, 'because')  # a value and a reason together
    with pytest.raises(ValueError):
        have(float('nan'))
    with pytest.raises(ValueError):
        have(float('inf'))
    assert have(0.0).ok and not missing('x').ok
    assert missing('no history').value is None


def test_a_column_will_not_store_nan_inf_or_an_overflow():
    for bad in (float('nan'), float('inf'), -float('inf')):
        with pytest.raises(columns.ColumnValueError):
            columns.normalise_value('rsi', bad)
    with pytest.raises(columns.ColumnValueError):
        columns.normalise_value('trend_efficiency', 1.0e4)  # numeric(12,8) holds < 10^4
    assert columns.normalise_value('rsi', None) is None


def test_a_forgotten_column_is_an_error_not_a_default():
    partial = {name: None for name in columns.FEATURE_COLUMNS}
    del partial['rsi']
    with pytest.raises(columns.ColumnValueError):
        columns.normalise_mapping(partial)
    partial['rsi'] = None
    partial['not_a_column'] = 1
    with pytest.raises(columns.ColumnValueError):
        columns.normalise_mapping(partial)
