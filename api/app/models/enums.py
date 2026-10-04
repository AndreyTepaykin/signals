"""Closed vocabularies and the ``CHECK`` helpers that enforce them.

Why ``text`` + ``CHECK`` and not a Postgres native ``ENUM``
----------------------------------------------------------
Native enums put the vocabulary in the database catalog, where it becomes a
second source of truth beside ``app.core.constants``. Widening one is
``ALTER TYPE ... ADD VALUE``, which Alembic's autogenerate does not detect, and
narrowing or renaming a value requires creating a new type, rewriting every
column that uses it and dropping the old one. ``text`` plus a named ``CHECK``
is a two-statement migration (drop the constraint, add the new one), is fully
visible to autogenerate as a constraint diff, and keeps the authoritative list
in Python where ``rule_hash`` can see the values that came from
``constants.py``. The cost is one byte of storage per value over the enum's
four, and no cross-table type reuse. That trade is worth it here.

A ``CHECK (col IN (...))`` passes when ``col`` is NULL, because ``IN`` yields
NULL and a NULL check result is not a violation. Nullability is therefore
controlled by ``nullable=`` alone, exactly as intended.

Values sourced from ``app.core.constants`` are re-exported here, never
restated. Values defined in this module are structural vocabulary (names for
states the spec describes in prose but never numbers); each is flagged below
and is listed in the build log so a later milestone can challenge it.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import CheckConstraint

from app.core import constants as C

# ---------------------------------------------------------------------------
# Sourced from app.core.constants (hash-visible; do not restate)
# ---------------------------------------------------------------------------
HORIZONS = C.HORIZONS  # scalp | swing | position
TREND_FAMILIES = C.FAMILIES  # trend | chop
OPTIONS_VERDICTS = C.OPTIONS_VERDICT_PRECEDENCE  # ignore | expiry_pin | headwind | amplify | dampen | align
OPTIONS_VOL_TAGS = C.OPTIONS_VOL_TAGS  # vol_rich | vol_cheap
OPTIONS_EXPRESSIONS = C.OPTIONS_EXPRESSIONS  # perp | spot | defined_risk_option | wait_for_expiry
SETUP_FAMILIES = C.SETUP_FAMILIES  # breakout | pullback | squeeze | reversion (frozen by M4)
POOL_LEVELS = tuple(level for level, _dims in C.POOL_WALK_UP_LEVELS)
#: Sessions as stored in the regime slice. ``late`` folds into ``asia`` (or
#: stays ``us`` while US cash is open), so it is never a stored slice value -
#: see ``app.core.time.classify_session``.
REGIME_SESSIONS = (C.SESSION_ASIA, C.SESSION_EUROPE, C.SESSION_US)
#: Raw clock buckets, including ``late``, for diagnostics.
SESSION_BUCKETS = tuple(name for name, _start, _end in C.SESSION_BOUNDARIES_UTC_HOURS)
#: Timeframes the Phase 1 feature engine reads, derived from the constants
#: that name them. Ingesting any other timeframe needs a migration that widens
#: this CHECK - deliberately, so a ``'m15'`` typo cannot silently fork a series.
TIMEFRAMES = tuple(
    sorted({C.CVD_TIMEFRAME, C.EFFICIENCY_TIMEFRAME, C.SQUEEZE_TIMEFRAME, C.SWING_TIMEFRAME})
)
#: Event kinds that stand the book down; the window lengths live in constants.
STAND_DOWN_EVENT_KINDS = tuple(C.EVENT_WINDOW_MINUTES)

# ---------------------------------------------------------------------------
# Defined here (structural vocabulary, not thresholds) - FLAGGED in BUILD-LOG
# ---------------------------------------------------------------------------
#: Spec: "Funding settlement and unlocks are slice features", i.e. they are on
#: the calendar but carry no stand-down window.
EVENT_KIND_UNLOCK = 'unlock'
EVENT_KIND_FUNDING_SETTLEMENT = 'funding_settlement'
SLICE_ONLY_EVENT_KINDS = (EVENT_KIND_UNLOCK, EVENT_KIND_FUNDING_SETTLEMENT)
EVENT_KINDS = STAND_DOWN_EVENT_KINDS + SLICE_ONLY_EVENT_KINDS

#: Spec: "Directional sources vote on Long / Short / Watch." A candidate always
#: has a direction; ``watch`` is a status, not a direction.
DIRECTION_LONG = 'long'
DIRECTION_SHORT = 'short'
DIRECTIONS = (DIRECTION_LONG, DIRECTION_SHORT)

#: Spec distinguishes an alert, a Watch card, a cancelled candidate, a delayed
#: candidate and an abstain. Every one of them is resolved on the same barriers
#: and every one of them counts in the promotion pair.
STATUS_ALERT = 'alert'
STATUS_WATCH = 'watch'
STATUS_CANCELLED = 'cancelled'
STATUS_DELAYED = 'delayed'
STATUS_ABSTAINED = 'abstained'
CANDIDATE_STATUSES = (STATUS_ALERT, STATUS_WATCH, STATUS_CANCELLED, STATUS_DELAYED, STATUS_ABSTAINED)
#: Statuses that must carry a reason: the card has to say why it did not fire.
STATUSES_REQUIRING_REASON = (STATUS_WATCH, STATUS_CANCELLED, STATUS_DELAYED, STATUS_ABSTAINED)

#: Live rows and replay rows share every table; the promotion gate reads only
#: ``live``.
SOURCE_LIVE = 'live'
SOURCE_REPLAY = 'replay'
SOURCES = (SOURCE_LIVE, SOURCE_REPLAY)

#: Triggers from "Indicators that improve the signal": the 4h swing break is
#: the setup, and a squeeze, VWAP tag or pullback reset times it.
TRIGGERS = ('swing_break', 'squeeze', 'vwap_tag', 'pullback_reset')

#: Which barrier traded first. ``vertical`` is the horizon ending.
BARRIER_UPPER = 'upper'
BARRIER_LOWER = 'lower'
BARRIER_VERTICAL = 'vertical'
#: Not a barrier: the next open was at or through the stop (or through the cap), so the setup was invalid before the
#: fill and there is no entry and no realized R. M8 excludes these from expectancy and reports their rate apart.
BARRIER_NO_ENTRY = 'no_entry'
FIRST_BARRIERS = (BARRIER_UPPER, BARRIER_LOWER, BARRIER_VERTICAL, BARRIER_NO_ENTRY)

#: Four OI-price states plus the dead zone. Spec: "Four states, plus the
#: OI-change z-score. A dead zone ignores OI changes inside 0.25 sigma" - a
#: reading inside the dead zone is ``price_*_oi_flat``, not a fifth quadrant.
OI_PRICE_QUADRANTS = (
    'price_up_oi_up',
    'price_up_oi_down',
    'price_down_oi_up',
    'price_down_oi_down',
    'price_up_oi_flat',
    'price_down_oi_flat',
)

#: Option venues are scored separately and never blended, so the venue is part
#: of every options key. ``ibit`` and ``cme`` have no feed in Phase 1; their
#: rows simply do not exist yet.
OPTIONS_VENUE_DERIBIT = 'deribit'
OPTIONS_VENUE_IBIT = 'ibit'
OPTIONS_VENUE_CME = 'cme'
OPTIONS_VENUES = (OPTIONS_VENUE_DERIBIT, OPTIONS_VENUE_IBIT, OPTIONS_VENUE_CME)

#: Which side of the book a path strike or wall sits on.
OPTION_SIDES = ('put', 'call')

#: Dealer gamma sign: customers long options means dealers short gamma (-1),
#: customers short options means dealers long gamma (+1), 0 = undetermined.
DEALER_GAMMA_SIGNS = (-1, 0, 1)

#: "Which of spot, perp, or CME moved first on the signal bar".
LEAD_VENUES = ('spot', 'perp', 'cme')

#: Candle series we actually ingest. Both are needed: the perp series drives
#: every feature, and the spot series is required by the perp-basis z-score and
#: by the "last completed perp and spot bars disagree by more than 1 ATR"
#: stand-down. There is no CME candle feed in Phase 1, so ``cme`` is a possible
#: ``lead_venue`` without being a possible candle market.
MARKET_PERP = 'perp'
MARKET_SPOT = 'spot'
CANDLE_MARKETS = (MARKET_PERP, MARKET_SPOT)

#: Funding rows: a settled print (the one the z-score uses) or the current
#: predicted rate (the one the live carry band reads).
FUNDING_SETTLED = 'settled'
FUNDING_PREDICTED = 'predicted'
FUNDING_KINDS = (FUNDING_SETTLED, FUNDING_PREDICTED)

#: Liquidation print side as Binance reports it.
FORCE_ORDER_SIDES = ('buy', 'sell')

#: How a candidate's ``sample_group_id`` was decided. See ``app.models.decisions``.
SAMPLE_GROUP_NEW = 'new'
SAMPLE_GROUP_OVERLAP = 'overlap'
SAMPLE_GROUP_BTC_IMPULSE = 'btc_impulse'
SAMPLE_GROUP_REASONS = (SAMPLE_GROUP_NEW, SAMPLE_GROUP_OVERLAP, SAMPLE_GROUP_BTC_IMPULSE)

JOB_STATUSES = ('running', 'ok', 'error', 'skipped')
FEED_STATUSES = ('ok', 'stale', 'down', 'unknown')
PROMOTION_DECISIONS = ('promoted', 'rejected')


# ---------------------------------------------------------------------------
# CHECK helpers
# ---------------------------------------------------------------------------
def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def one_of(column: str, values: Iterable[str], *, name: str | None = None) -> CheckConstraint:
    """``CHECK (column IN (...))`` over a closed vocabulary.

    Passes for NULL, so nullability stays the business of ``nullable=``.
    """
    rendered = ', '.join(_literal(value) for value in values)
    if not rendered:
        raise ValueError(f'refusing to build an empty CHECK for {column!r}')
    return CheckConstraint(f'{column} IN ({rendered})', name=name or column)


def in_range(
    column: str,
    low: Decimal | int | float,
    high: Decimal | int | float,
    *,
    name: str | None = None,
) -> CheckConstraint:
    """``CHECK (low <= column <= high)``, inclusive at both ends."""
    return CheckConstraint(f'{column} >= {low} AND {column} <= {high}', name=name or f'{column}_range')


def non_negative(column: str, *, name: str | None = None) -> CheckConstraint:
    """``CHECK (column >= 0)``."""
    return CheckConstraint(f'{column} >= 0', name=name or f'{column}_non_negative')
