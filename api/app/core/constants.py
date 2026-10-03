"""Frozen thresholds for the Signals loop.

Every numeric rule from ``self-learning-crypto-signals.md`` lives here and
nowhere else. Later milestones import from this module; they never write a
threshold literal. ``app.core.hashing.rule_hash`` hashes the public names in
this module, so moving any value is a visible, hash-changing event.

Conventions
-----------
* Ratios, z-scores, quantiles, ATR multiples, R multiples and size
  multipliers are plain ``float``.
* Costs in basis points are ``decimal.Decimal`` (never float) so that cost
  arithmetic is exact.
* Counts and hours are ``int``. Durations are ``datetime.timedelta`` only
  where noted; otherwise whole minutes/hours as ``int``.
* Only UPPERCASE names that do not start with ``_`` are part of the frozen
  set. Containers are immutable (``tuple`` / ``MappingProxyType``).
* Allowed value types (the canonical serializer rejects anything else):
  ``str``, ``int``, ``float``, ``bool``, ``Decimal``, ``tuple``, mappings.

Changing a value here changes ``rule_hash()``. Bump ``RULE_VERSION`` when the
*meaning* of a rule changes without a numeric change.
"""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

# ---------------------------------------------------------------------------
# Rule version (hashed together with the constants below).
# ---------------------------------------------------------------------------
RULE_VERSION = 'phase1-v3'

# ---------------------------------------------------------------------------
# Horizons and vertical barriers
# Spec: "Label contract" (Vertical barrier: scalp 4h, swing 24h, position 72h).
# ---------------------------------------------------------------------------
HORIZON_SCALP = 'scalp'
HORIZON_SWING = 'swing'
HORIZON_POSITION = 'position'
HORIZONS = (HORIZON_SCALP, HORIZON_SWING, HORIZON_POSITION)

VERTICAL_BARRIER_HOURS = MappingProxyType(
    {
        HORIZON_SCALP: 4,
        HORIZON_SWING: 24,
        HORIZON_POSITION: 72,
    }
)
# "embargo equal to the horizon" (promotion gate) and "embargo equal to the
# longest horizon" (weekly purged retrain, phase 2).
LONGEST_HORIZON_HOURS = 72

# ---------------------------------------------------------------------------
# Costs (frozen, printed on the card)
# Spec: "Label contract" - "4 bps taker per side, 2 bps slippage per side".
# Basis points are Decimal. Funding is NOT here: labels use realized funding.
# ---------------------------------------------------------------------------
TAKER_FEE_BPS_PER_SIDE = Decimal('4')
SLIPPAGE_BPS_PER_SIDE = Decimal('2')
SIDES_PER_ROUND_TRIP = 2
ROUND_TRIP_COST_BPS = (TAKER_FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) * SIDES_PER_ROUND_TRIP  # 12 bps, fees+slippage only

# ---------------------------------------------------------------------------
# Lookbacks
# Spec: "Every rate is a 90-day z-score or percentile".
# ---------------------------------------------------------------------------
LOOKBACK_DAYS = 90

# ---------------------------------------------------------------------------
# Trend efficiency (rank 5)
# Spec: "24-bar efficiency on 1h closes. Trend at or above 0.40. Chop at or
# below 0.25. Between those, keep the previous family. Only that family may fire".
# ---------------------------------------------------------------------------
EFFICIENCY_BARS = 24
EFFICIENCY_TIMEFRAME = '1h'
EFFICIENCY_TREND_MIN = 0.40  # >= -> trend family
EFFICIENCY_CHOP_MAX = 0.25  # <= -> chop family
FAMILY_TREND = 'trend'
FAMILY_CHOP = 'chop'
FAMILIES = (FAMILY_TREND, FAMILY_CHOP)

# ---------------------------------------------------------------------------
# OI-price quadrant (rank 2)
# Spec: "A dead zone ignores OI changes inside 0.25 sigma".
# ---------------------------------------------------------------------------
OI_CHANGE_DEAD_ZONE_SIGMA = 0.25

# ---------------------------------------------------------------------------
# Beta and alt residual gate
# Spec: "Conditioners - Residual versus BTC" and "Anti-noise".
# Beta = 90-day rolling OLS of the alt on BTC, fit on returns that END BEFORE
# the signal bar. Gate: lagged-beta residual z-score on the signal bar >= 1.0.
# ---------------------------------------------------------------------------
BETA_LOOKBACK_DAYS = 90
BETA_FIT_ENDS_BEFORE_SIGNAL_BAR = True
ALT_RESIDUAL_Z_GATE = 1.0

# ---------------------------------------------------------------------------
# CVD divergence (filter)
# Spec: "20 bars of 15m taker-volume delta. Price makes a 20-bar high or low
# and CVD does not."
# ---------------------------------------------------------------------------
CVD_BARS = 20
CVD_TIMEFRAME = '15m'
CVD_PRICE_EXTREME_BARS = 20

# ---------------------------------------------------------------------------
# Volume profile (location)
# Spec: "Session profile, 50 bins, 70% value area."
# ---------------------------------------------------------------------------
VOLUME_PROFILE_BINS = 50
VOLUME_PROFILE_VALUE_AREA = 0.70

# ---------------------------------------------------------------------------
# Volatility squeeze (trigger)
# Spec: "Bollinger 20, 2 sigma, inside Keltner 20, 1.5x ATR."
# ---------------------------------------------------------------------------
SQUEEZE_TIMEFRAME = '15m'
BOLLINGER_PERIOD = 20
BOLLINGER_SIGMA = 2.0
KELTNER_PERIOD = 20
KELTNER_ATR_MULT = 1.5

# ---------------------------------------------------------------------------
# Swing break and chandelier stop
# Spec: "A 4h close beyond the last 2-bar fractal swing" and "Extreme since
# that swing, minus 3x ATR (plus 3x ATR for shorts). This stop is 1R."
# ---------------------------------------------------------------------------
SWING_TIMEFRAME = '4h'
SWING_FRACTAL_BARS = 2
CHANDELIER_ATR_MULT = 3.0

# ATR period is NOT stated in the spec. Owner-confirmed rather than assumption (M3 brief):
# Wilder ATR over 14 bars of the timeframe the stop/gate is computed on.
ATR_PERIOD = 14

# ---------------------------------------------------------------------------
# Pullback reset (trigger, trend family only)
# Spec: "RSI(14) returning toward 40-50 (long) or 50-60 (short)".
# ---------------------------------------------------------------------------
PULLBACK_RSI_PERIOD = 14
PULLBACK_RSI_LONG_BAND = (40.0, 50.0)
PULLBACK_RSI_SHORT_BAND = (50.0, 60.0)

# ---------------------------------------------------------------------------
# Options influence check
# Spec: "Options influence check" precedence table.
# ---------------------------------------------------------------------------
OPTIONS_SNAPSHOT_MAX_AGE_MINUTES = 15  # older -> verdict `ignore`

# expiry_pin
EXPIRY_PIN_WINDOW_HOURS = 48
EXPIRY_PIN_MIN_STRIKE_OI_SHARE = 0.15  # largest strike share of THAT expiry's OI

# headwind
HEADWIND_MIN_STRIKE_OI_SHARE = 0.10  # share of near-dated OI
HEADWIND_MIN_ROOM_R = 0.5  # skip if room to the cap is under this (also CVD/headwind cancel rule)

# amplify
AMPLIFY_FLIP_MAX_ATR = 1.0  # flip inside 1 ATR
AMPLIFY_STOP_WIDEN_ATR = 0.5  # widen stop by 0.5 ATR, then recompute 1R
AMPLIFY_SIZE_MULTIPLIER = 1.15

# dampen
DAMPEN_SIZE_MULTIPLIER = 0.70
DAMPEN_TARGET_CAP_R = 1.0  # targets tighten to the nearer of model target and +1R

# align
ALIGN_SIZE_MULTIPLIER = 1.00

# vol_rich / vol_cheap: same-tenor ATM IV minus Yang-Zhang RV, 90-day percentile.
VOL_RICH_QUANTILE = 0.80  # spread at or above -> vol_rich
VOL_CHEAP_QUANTILE = 0.20  # spread at or below -> vol_cheap
VOL_SPREAD_LOOKBACK_DAYS = LOOKBACK_DAYS

# Precedence, first match wins. `vol_rich` / `vol_cheap` are tags, not rungs.
OPTIONS_VERDICT_PRECEDENCE = ('ignore', 'expiry_pin', 'headwind', 'amplify', 'dampen', 'align')
OPTIONS_VOL_TAGS = ('vol_rich', 'vol_cheap')
OPTIONS_EXPRESSIONS = ('perp', 'spot', 'defined_risk_option', 'wait_for_expiry')

# Per-coin open-interest floor for `ignore`. The spec says "a per-coin floor
# you set by hand". NO VALUE HAS BEEN SET: empty means "no floor configured",
# and Milestone 5 / the owner must fill it. Not invented here.
OPTIONS_OI_FLOOR_BY_COIN = MappingProxyType({})

# ---------------------------------------------------------------------------
# Pools, edge line, abstain rule, calibration, interval
# Spec: "Abstain", "Edge line", "Label contract" (bins), "Shadow mode".
# ---------------------------------------------------------------------------
POOL_MIN_INDEPENDENT_OUTCOMES = 30  # edge line and abstain rule
POOL_WINDOW_DAYS = 90  # "30 independent outcomes in 90 days"
CALIBRATION_BIN_MIN_OUTCOMES = 20  # bins under this are hidden
CONFIDENCE_TRUST_MIN_BIN_OUTCOMES = 20  # "trusted only when bins with at least 20 outcomes..."
INTERVAL_CONFIDENCE = 0.90  # "the 90% interval" (edge line and promotion gate)

# Pool walk-up order, most specific first. Walk until the cell has
# POOL_MIN_INDEPENDENT_OUTCOMES independent outcomes in POOL_WINDOW_DAYS.
# Each entry is (level_name, dimensions that define the cell at that level).
POOL_DIM_SETUP_FAMILY = 'setup_family'
POOL_DIM_TREND_FAMILY = 'trend_family'
POOL_DIM_VOL_PERCENTILE = 'vol_percentile'
POOL_DIM_SESSION = 'session'
POOL_DIM_EVENT_FLAG = 'event_flag'
POOL_WALK_UP_LEVELS = (
    (
        'full_slice',
        (POOL_DIM_SETUP_FAMILY, POOL_DIM_TREND_FAMILY, POOL_DIM_VOL_PERCENTILE, POOL_DIM_SESSION, POOL_DIM_EVENT_FLAG),
    ),
    (
        'drop_event_flag',
        (POOL_DIM_SETUP_FAMILY, POOL_DIM_TREND_FAMILY, POOL_DIM_VOL_PERCENTILE, POOL_DIM_SESSION),
    ),
    ('drop_session', (POOL_DIM_SETUP_FAMILY, POOL_DIM_TREND_FAMILY, POOL_DIM_VOL_PERCENTILE)),
    ('trend_x_vol', (POOL_DIM_SETUP_FAMILY, POOL_DIM_TREND_FAMILY, POOL_DIM_VOL_PERCENTILE)),
    ('setup_family', (POOL_DIM_SETUP_FAMILY,)),
)
# NOTE (spec ambiguity, see BUILD-LOG): the spec's walk-up reads "full slice,
# then drop the event flag, then session, then trend x vol, then the setup
# family". The regime slice is trend x vol x session x event, so dropping the
# session already yields trend x vol, and the `drop_session` and `trend_x_vol`
# levels have identical dimension sets. They are kept as two named levels, in
# spec order, so nothing is silently merged; the stats engine (M8) may skip a
# level whose cell is identical to the previous one. The setup family is
# treated as part of the key at every level (the edge belongs to a family).

# ---------------------------------------------------------------------------
# Model floors
# Spec: "Models" - LightGBM at 300, sequence models at 2,000; kill rule at 300.
# ---------------------------------------------------------------------------
LIGHTGBM_MIN_INDEPENDENT_LABELS = 300
SEQUENCE_MODEL_MIN_INDEPENDENT_LABELS = 2000
KILL_RULE_INDEPENDENT_LABELS = 300

# ---------------------------------------------------------------------------
# Event windows (stand-down)
# Spec: "Signal sources" - 30 min before CPI/FOMC until 30 min after; 15 min
# before a large expiry until 15 min after. Windows are closed intervals.
# ---------------------------------------------------------------------------
EVENT_KIND_CPI = 'cpi'
EVENT_KIND_FOMC = 'fomc'
EVENT_KIND_EXPIRY = 'expiry'
EVENT_WINDOW_MINUTES = MappingProxyType(
    {
        # kind: (minutes before, minutes after)
        EVENT_KIND_CPI: (30, 30),
        EVENT_KIND_FOMC: (30, 30),
        EVENT_KIND_EXPIRY: (15, 15),
    }
)

# ---------------------------------------------------------------------------
# Feed and data stand-downs
# Spec: "Anti-noise" - feed past its available-at rule; last completed perp and
# spot bars disagree by more than 1 ATR.
# ---------------------------------------------------------------------------
PERP_SPOT_MAX_DISAGREEMENT_ATR = 1.0  # strictly greater -> stand down
# Per-feed staleness limits are NOT numeric in the spec (only Deribit: 15 min,
# above). Deferred to M2 (feed_health); deliberately not invented here.

# ---------------------------------------------------------------------------
# Frozen rules (Milestone 4): direction, triggers, setup families, stand-downs
# Spec: "Indicators that improve the signal", "Anti-noise", "Metrics that move
# the estimate" (trend efficiency: "Only that family may fire").
# Everything below is an interpretation the spec leaves open and is listed as a
# numbered Ambiguity in the M4 BUILD-LOG entry; none of it is optimised.
# ---------------------------------------------------------------------------
TRIGGER_SWING_BREAK = 'swing_break'
TRIGGER_PULLBACK_RESET = 'pullback_reset'
TRIGGER_SQUEEZE = 'squeeze'
TRIGGER_VWAP_TAG = 'vwap_tag'
# When two triggers fire on one bar the first in this order is recorded (the
# candidate has one `trigger`). Must hold exactly the `TRIGGERS` vocabulary.
TRIGGER_PRIORITY = (TRIGGER_SWING_BREAK, TRIGGER_PULLBACK_RESET, TRIGGER_SQUEEZE, TRIGGER_VWAP_TAG)

# "Only that family may fire": which triggers each efficiency family may use.
# The break and the pullback are trend-following; the VWAP tag is the chop
# mean-reversion entry ("In chop, stretched distance"); a squeeze may time a
# break in either family.
TRIGGERS_BY_FAMILY = MappingProxyType(
    {
        FAMILY_TREND: (TRIGGER_SWING_BREAK, TRIGGER_PULLBACK_RESET, TRIGGER_SQUEEZE),
        FAMILY_CHOP: (TRIGGER_SQUEEZE, TRIGGER_VWAP_TAG),
    }
)

# The swing-break TRIGGER fires on the EVENT (the 15m bar that closes the first
# 4h close beyond the swing), never on the STATE. The state is only the
# direction permission for every trigger. See the M4 BUILD-LOG entry.
SWING_BREAK_TRIGGER_FIRES_ON = 'event'

# Frozen setup-family vocabulary: the `setup_family` pool key (M8's last walk-up
# level). One family per trigger; the efficiency family is a separate pool
# dimension (`trend_family`), so a squeeze in a trend and in chop pool apart
# until the walk-up merges them.
SETUP_FAMILY_BREAKOUT = 'breakout'
SETUP_FAMILY_PULLBACK = 'pullback'
SETUP_FAMILY_SQUEEZE = 'squeeze'
SETUP_FAMILY_REVERSION = 'reversion'
SETUP_FAMILIES = (SETUP_FAMILY_BREAKOUT, SETUP_FAMILY_PULLBACK, SETUP_FAMILY_SQUEEZE, SETUP_FAMILY_REVERSION)
SETUP_FAMILY_BY_TRIGGER = MappingProxyType(
    {
        TRIGGER_SWING_BREAK: SETUP_FAMILY_BREAKOUT,
        TRIGGER_PULLBACK_RESET: SETUP_FAMILY_PULLBACK,
        TRIGGER_SQUEEZE: SETUP_FAMILY_SQUEEZE,
        TRIGGER_VWAP_TAG: SETUP_FAMILY_REVERSION,
    }
)

# Direction vote ("Directional sources vote on Long / Short / Watch"). The 4h
# swing break supplies the direction (it "allows the direction"); the
# positioning sources then vote for it or against it. A source with a NULL
# input abstains. The candidate is confirmed when (votes for - votes against)
# >= DIRECTION_VOTE_MIN_NET; otherwise it is a `watch`.
# |z| >= DIRECTION_VOTE_Z_MIN is a decisive reading of a z-scored source
# (aggressor imbalance; funding and perp basis read as CROWDING, so contrarian).
# 1.0 is the one z-score the spec freezes (the alt residual gate), reused here.
DIRECTION_VOTE_Z_MIN = 1.0
DIRECTION_VOTE_MIN_NET = 0

# Chop mean-reversion entry: the signal-bar close is at least this many ATR from
# the session VWAP, on the side opposite the trade direction. The spec says
# "stretched distance" without a number.
VWAP_STRETCH_ATR = 1.0

# Stand-down inputs that can be unknown (NULL on the feature row). The event
# calendar is owner-loaded and optional in Phase 1, so an unloaded calendar does
# not stand the book down; a missing spot bar is a feed fault, so it does.
STAND_DOWN_ON_UNKNOWN_EVENT_CALENDAR = False
STAND_DOWN_ON_UNKNOWN_PERP_SPOT = True

# Machine-readable `candidate.status_reason` codes written by the rules.
STATUS_REASON_PENDING_OPTIONS_CHECK = 'pending_options_check'  # status `watch`: M4 passed, M5 has not run
STATUS_REASON_VOTE_CONFLICT = 'vote_conflict'  # status `watch`
STATUS_REASON_CVD_DIVERGENCE = 'cvd_divergence_against'  # status `cancelled`
STATUS_REASON_STAND_DOWN_PREFIX = 'stand_down:'  # status `cancelled`, then the kinds below joined by `+`
STAND_DOWN_FEED_BREACH = 'feed_stale'
STAND_DOWN_EVENT_WINDOW = 'event_window'
STAND_DOWN_PERP_SPOT = 'perp_spot_disagreement'

# ---------------------------------------------------------------------------
# Locked book and promotion gate
# Spec: "Shadow mode and promotion".
# ---------------------------------------------------------------------------
LOCKED_BOOK_ONE_IN = 5  # hash(coin, bar_time) % 5 selects the locked book
# The bucket is the first LOCKED_BOOK_HASH_BYTES bytes of HASH_ALGORITHM over the
# canonical text of {"bar_close", "coin"}, read as a big-endian integer, mod
# LOCKED_BOOK_ONE_IN. A candidate is in the locked book when that equals
# LOCKED_BOOK_SELECTED_BUCKET.
LOCKED_BOOK_HASH_BYTES = 8
LOCKED_BOOK_SELECTED_BUCKET = 0
PROMOTION_MIN_SHADOW_WEEKS = 4
PROMOTION_MIN_PAIRED_OUTCOMES = 50
PROMOTION_INTERVAL_CONFIDENCE = INTERVAL_CONFIDENCE
PROMOTION_MEAN_DIFF_MIN_EXCLUSIVE = 0.0  # mean paired diff and interval lower bound must be > 0
PROMOTION_EMBARGO_EQUALS_HORIZON = True

# ---------------------------------------------------------------------------
# Sessions (UTC)
# Spec: "Context the loop learns - Sessions (UTC)".
# Half-open [start, end) in whole UTC hours. `late` is folded into the NEXT
# Asia date unless US cash is open (see app.core.time).
# ---------------------------------------------------------------------------
SESSION_ASIA = 'asia'
SESSION_EUROPE = 'europe'
SESSION_US = 'us'
SESSION_LATE = 'late'
SESSION_BOUNDARIES_UTC_HOURS = (
    (SESSION_ASIA, 0, 7),
    (SESSION_EUROPE, 7, 13),
    (SESSION_US, 13, 21),
    (SESSION_LATE, 21, 24),
)
# Weekend: Friday 21:00 UTC (inclusive) through Sunday 21:00 UTC (exclusive).
# Weekday numbers follow datetime.weekday(): Monday=0 ... Sunday=6.
WEEKEND_START_WEEKDAY = 4
WEEKEND_START_HOUR_UTC = 21
WEEKEND_END_WEEKDAY = 6
WEEKEND_END_HOUR_UTC = 21

# US cash session (ETF flow, IBIT) in UTC as ((start_h, start_m), (end_h, end_m)).
# DST status comes from zoneinfo('America/New_York') for the instant in question.
US_CASH_TIMEZONE = 'America/New_York'
US_CASH_HOURS_UTC_DST = ((13, 30), (20, 0))
US_CASH_HOURS_UTC_STANDARD = ((14, 30), (21, 0))

# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
HASH_ALGORITHM = 'sha256'
