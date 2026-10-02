"""Pins the spec values. A failure here means a frozen threshold moved."""

from decimal import Decimal

from app.core import constants as C


def test_costs_are_decimal_bps():
    assert C.TAKER_FEE_BPS_PER_SIDE == Decimal('4')
    assert C.SLIPPAGE_BPS_PER_SIDE == Decimal('2')
    assert isinstance(C.TAKER_FEE_BPS_PER_SIDE, Decimal)
    assert C.ROUND_TRIP_COST_BPS == Decimal('12')


def test_vertical_barriers():
    assert dict(C.VERTICAL_BARRIER_HOURS) == {'scalp': 4, 'swing': 24, 'position': 72}


def test_efficiency_and_lookbacks():
    assert (C.EFFICIENCY_BARS, C.EFFICIENCY_TIMEFRAME) == (24, '1h')
    assert (C.EFFICIENCY_TREND_MIN, C.EFFICIENCY_CHOP_MAX) == (0.40, 0.25)
    assert C.LOOKBACK_DAYS == 90 and C.BETA_LOOKBACK_DAYS == 90
    assert C.OI_CHANGE_DEAD_ZONE_SIGMA == 0.25
    assert C.ALT_RESIDUAL_Z_GATE == 1.0


def test_indicator_defaults():
    assert (C.CVD_BARS, C.CVD_TIMEFRAME) == (20, '15m')
    assert (C.VOLUME_PROFILE_BINS, C.VOLUME_PROFILE_VALUE_AREA) == (50, 0.70)
    assert (C.BOLLINGER_PERIOD, C.BOLLINGER_SIGMA) == (20, 2.0)
    assert (C.KELTNER_PERIOD, C.KELTNER_ATR_MULT) == (20, 1.5)
    assert (C.SWING_TIMEFRAME, C.SWING_FRACTAL_BARS) == ('4h', 2)
    assert C.CHANDELIER_ATR_MULT == 3.0
    assert C.PULLBACK_RSI_LONG_BAND == (40.0, 50.0)
    assert C.PULLBACK_RSI_SHORT_BAND == (50.0, 60.0)


def test_options_check():
    assert C.OPTIONS_SNAPSHOT_MAX_AGE_MINUTES == 15
    assert (C.EXPIRY_PIN_WINDOW_HOURS, C.EXPIRY_PIN_MIN_STRIKE_OI_SHARE) == (48, 0.15)
    assert (C.HEADWIND_MIN_STRIKE_OI_SHARE, C.HEADWIND_MIN_ROOM_R) == (0.10, 0.5)
    assert (C.AMPLIFY_FLIP_MAX_ATR, C.AMPLIFY_STOP_WIDEN_ATR, C.AMPLIFY_SIZE_MULTIPLIER) == (1.0, 0.5, 1.15)
    assert C.DAMPEN_SIZE_MULTIPLIER == 0.70 and C.ALIGN_SIZE_MULTIPLIER == 1.00
    assert (C.VOL_RICH_QUANTILE, C.VOL_CHEAP_QUANTILE) == (0.80, 0.20)
    assert C.OPTIONS_VERDICT_PRECEDENCE == ('ignore', 'expiry_pin', 'headwind', 'amplify', 'dampen', 'align')


def test_floors_and_gates():
    assert C.POOL_MIN_INDEPENDENT_OUTCOMES == 30 and C.POOL_WINDOW_DAYS == 90
    assert C.CALIBRATION_BIN_MIN_OUTCOMES == 20 and C.CONFIDENCE_TRUST_MIN_BIN_OUTCOMES == 20
    assert (C.LIGHTGBM_MIN_INDEPENDENT_LABELS, C.SEQUENCE_MODEL_MIN_INDEPENDENT_LABELS) == (300, 2000)
    assert C.LOCKED_BOOK_ONE_IN == 5
    assert (C.PROMOTION_MIN_SHADOW_WEEKS, C.PROMOTION_MIN_PAIRED_OUTCOMES) == (4, 50)
    assert C.INTERVAL_CONFIDENCE == 0.90
    assert C.PERP_SPOT_MAX_DISAGREEMENT_ATR == 1.0


def test_event_windows():
    assert dict(C.EVENT_WINDOW_MINUTES) == {'cpi': (30, 30), 'fomc': (30, 30), 'expiry': (15, 15)}


def test_pool_walk_up_order_is_spec_order():
    assert [name for name, _ in C.POOL_WALK_UP_LEVELS] == [
        'full_slice',
        'drop_event_flag',
        'drop_session',
        'trend_x_vol',
        'setup_family',
    ]
    dims = {name: set(d) for name, d in C.POOL_WALK_UP_LEVELS}
    assert dims['full_slice'] > dims['drop_event_flag'] > dims['drop_session']
    assert 'event_flag' not in dims['drop_event_flag'] and 'session' not in dims['drop_session']


def test_session_boundaries_cover_the_day_without_gaps():
    spans = [(start, end) for _, start, end in C.SESSION_BOUNDARIES_UTC_HOURS]
    assert spans[0][0] == 0 and spans[-1][1] == 24
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert [n for n, *_ in C.SESSION_BOUNDARIES_UTC_HOURS] == ['asia', 'europe', 'us', 'late']
    assert C.US_CASH_HOURS_UTC_DST == ((13, 30), (20, 0))
    assert C.US_CASH_HOURS_UTC_STANDARD == ((14, 30), (21, 0))
