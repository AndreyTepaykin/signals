"""Engine parameters that are NOT frozen spec thresholds.

``app.core.constants`` holds every number the spec states, and
``rule_hash()`` hashes it. The values below are choices the spec leaves open:
which timeframe a statistic is sampled on, how many bars of warm-up a
recursive indicator gets, and how much history a 90-day statistic needs before
it is allowed to speak. Each one is documented with the ambiguity it resolves
in the Milestone 3 entry of ``docs/BUILD-LOG.md``.

They are kept apart from ``constants.py`` (spec numbers vs engine choices),
but they ARE hashed: ``app.core.hashing.frozen_engine_params`` puts every
public UPPERCASE value here into the ``rule_hash()`` payload under its own
``engine_params`` key. Changing a value here changes both the feature values
and ``rule_hash``. A new public UPPERCASE name must be added to
``frozen_engine_params`` too, or ``tests/test_hashing.py`` fails.

Nothing here is read from the environment or the clock. The same code on the
same bars gives the same numbers in a live run and in a replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.core import constants as C

# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------
_UNIT_SECONDS = {'m': 60, 'h': 3600, 'd': 86400}


def timeframe_delta(timeframe: str) -> timedelta:
    """``'15m'`` -> 15 minutes, ``'1h'`` -> 1 hour, ``'4h'`` -> 4 hours."""
    if len(timeframe) < 2 or timeframe[-1] not in _UNIT_SECONDS or not timeframe[:-1].isdigit():
        raise ValueError(f'unrecognised timeframe {timeframe!r}')
    return timedelta(seconds=int(timeframe[:-1]) * _UNIT_SECONDS[timeframe[-1]])


#: Decisions are made on the close of a 15-minute bar ("A 15-minute squeeze,
#: VWAP tag, or pullback reset times it"). Every feature_row is keyed on it.
SIGNAL_TIMEFRAME = C.SQUEEZE_TIMEFRAME
assert SIGNAL_TIMEFRAME == C.CVD_TIMEFRAME  # both 15m in the spec; fail loudly if one moves

#: The timeframe of the ATR every ``*_atr`` column on a row is divided by. The
#: chandelier stop (1R) and the swing are 4h objects, so the unit of distance is
#: the unit of the stop. AMBIGUITY: the spec never says (see BUILD-LOG).
ATR_TIMEFRAME = C.SWING_TIMEFRAME

#: Yang-Zhang realized volatility and the trailing-percentile are sampled on 1h
#: bars, 24 bars (one day) per estimate. AMBIGUITY: the spec gives neither.
REALIZED_VOL_TIMEFRAME = '1h'
REALIZED_VOL_BARS = 24

#: Perp basis is sampled on 1h bars (rank 11 is "Hours-days").
BASIS_TIMEFRAME = '1h'

#: Open interest is read from the 1h series, one period change (rank 2 is
#: "Hours-days"). AMBIGUITY: the spec names no OI period.
OI_SERIES_TIMEFRAME = '1h'
OI_CHANGE_STEPS = 1

#: A "latest" reading older than this is stale, not current. Open interest
#: normally lags the bar close by 5 to 65 minutes (hourly boundary plus the
#: publication allowance), so two periods leaves room for one missed poll.
OI_MAX_AGE_PERIODS = 2
#: Settled funding prints are at most 8h apart; one missed settlement is 16h.
FUNDING_MAX_AGE = timedelta(hours=16)

# ---------------------------------------------------------------------------
# History needed before a 90-day statistic is allowed to be a number
# ---------------------------------------------------------------------------
HISTORY_DAYS = C.LOOKBACK_DAYS


@dataclass(frozen=True)
class HistoryPolicy:
    """How much history a rolling z-score or percentile needs.

    The spec says every rate is a 90-day z-score or percentile and gives no
    minimum. A z-score over 25 hours of data is not a 90-day z-score, and
    quoting it as one is exactly the kind of believable-but-wrong number this
    milestone exists to prevent. So a statistic needs BOTH enough span and
    enough samples; below that it is NULL, never computed "over what exists"
    and never zero.

    ``window_days`` is the look-back (the spec's 90).
    ``min_span_days``: the prior samples must span at least this long (a full
        weekly cycle, so weekends and all three sessions are represented).
    ``min_samples``: the prior samples must number at least this many.
    """

    window_days: int = HISTORY_DAYS
    min_span_days: float = 7.0
    min_samples: int = 30

    def __post_init__(self) -> None:
        if self.window_days <= 0 or self.min_span_days < 0 or self.min_samples < 2:
            raise ValueError('window_days must be > 0, min_span_days >= 0, min_samples >= 2')

    @property
    def window(self) -> timedelta:
        return timedelta(days=self.window_days)

    @property
    def min_span(self) -> timedelta:
        return timedelta(days=self.min_span_days)


PRODUCTION_POLICY = HistoryPolicy()

# ---------------------------------------------------------------------------
# Fixed history lengths for recursive indicators (determinism)
# ---------------------------------------------------------------------------
#: Wilder ATR / RSI are recursive: their value depends on where the recursion
#: starts. To make a live run and a replay agree to the last bit, both always
#: use the LAST ``*_HISTORY_BARS`` visible bars (or all of them when fewer
#: exist). Influence of the seed after 200 bars of period 14 is (13/14)^186.
ATR_HISTORY_BARS = 200
RSI_HISTORY_BARS = 200
#: Swings older than this many 4h bars (20 days) are not "the last swing".
SWING_HISTORY_BARS = 120
#: The trend-family state machine is replayed over this many hourly efficiency
#: readings (14 days). The family is the most recent DECISIVE reading inside
#: it, so it is a pure function of the data, not of process history.
FAMILY_MEMORY_BARS = 336

# ---------------------------------------------------------------------------
# Session-anchored indicators
# ---------------------------------------------------------------------------
#: A VWAP or volume profile anchored at the UTC day's 00:00 says almost
#: nothing in its first bars (a one-bar VWAP IS the price). Fewer bars -> NULL.
SESSION_MIN_BARS = 8

# ---------------------------------------------------------------------------
# Liquidation estimate (shadow)
# ---------------------------------------------------------------------------
LIQUIDATION_LOOKBACK_HOURS = 24
LIQUIDATION_MIN_PRINTS = 5
#: Width of a price bin, in ATR units.
LIQUIDATION_BIN_ATR = 0.5
#: The print rate is measured over the signal bar.
LIQUIDATION_RATE_WINDOW_MINUTES = 15

# ---------------------------------------------------------------------------
# Calendar year for annualising a volatility (crypto trades 24/7)
# ---------------------------------------------------------------------------
DAYS_PER_YEAR = 365
HOURS_PER_YEAR = DAYS_PER_YEAR * 24
