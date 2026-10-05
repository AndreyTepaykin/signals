"""Stats parameters (Milestone 8).

The hash reads ``app.core.constants`` and ``app.core.features.params`` and nothing else. Values defined here only decide
how outcomes are *reported*. Everything the pooled estimate depends on (floors, interval, bootstrap, vol buckets, thin
lookback) is read from ``constants`` because the abstain rule (M10) turns that estimate into a decision; those names are
aliases, never restated numbers.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core import constants as C

# ---------------------------------------------------------------------------
# Floors, read from the frozen constants
# ---------------------------------------------------------------------------
#: A statistic over fewer independent filled outcomes than this is flagged `insufficient_sample` (spec: "30 independent
#: outcomes" for the edge line and the abstain rule).
MIN_INDEPENDENT_TO_CONCLUDE: int = C.POOL_MIN_INDEPENDENT_OUTCOMES
#: A reliability bin with fewer independent outcomes than this is hidden (spec: "bins under 20 outcomes are hidden").
MIN_BIN_OUTCOMES: int = C.CALIBRATION_BIN_MIN_OUTCOMES
#: The kill rule is evaluated only from this many independent labels (spec: 300).
KILL_RULE_MIN_INDEPENDENT: int = C.KILL_RULE_INDEPENDENT_LABELS
#: Two-sided confidence of every expectancy interval ("the 90% interval").
INTERVAL_CONFIDENCE: float = C.INTERVAL_CONFIDENCE

# ---------------------------------------------------------------------------
# Label-quality flags (owner requirements from M6)
# ---------------------------------------------------------------------------
#: Above this share of ambiguous (stop and target in one 15m bar) among FILLED labels the report raises a flag.
#: The fix (on-demand Binance 1m klines for those bars only) is deliberately not built.
AMBIGUOUS_SHARE_FLAG: float = 0.05

# ---------------------------------------------------------------------------
# Thin lookback (owner requirement from M7)
# ---------------------------------------------------------------------------
#: The backfill that feeds the replay starts 2026-09-04, so features for signal bars in roughly the first 20 days run on
#: less history than the engine's 90-day z-score and percentile windows. Signal bars strictly before this instant
#: (2026-09-04 + 20 days) form the `thin` lookback cohort; later ones are `full`. It is a reporting split only.
BACKFILL_START: datetime = datetime(2026, 9, 4, tzinfo=timezone.utc)
THIN_LOOKBACK_DAYS: int = 20
THIN_LOOKBACK_CUTOFF: datetime = datetime.fromisoformat(C.ESTIMATE_THIN_LOOKBACK_CUTOFF_UTC).astimezone(timezone.utc)

# ---------------------------------------------------------------------------
# Pool cells
# ---------------------------------------------------------------------------
#: The vol-percentile pool dimension needs buckets; the spec names the dimension but no edges. Terciles of the
#: realized-vol percentile (stored 0..1): `low` below the first edge, `high` from the second.
VOL_BUCKET_EDGES: tuple[float, float] = C.ESTIMATE_VOL_BUCKET_EDGES
VOL_BUCKET_NAMES: tuple[str, str, str] = C.ESTIMATE_VOL_BUCKET_NAMES
UNKNOWN = 'unknown'

# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------
#: Percentile bootstrap over the independent outcomes, fixed seed so a rerun prints the same interval.
BOOTSTRAP_RESAMPLES: int = C.ESTIMATE_BOOTSTRAP_RESAMPLES
BOOTSTRAP_SEED: int = C.ESTIMATE_BOOTSTRAP_SEED

# ---------------------------------------------------------------------------
# Reliability diagram
# ---------------------------------------------------------------------------
#: Ten equal-width bins on [0, 1]; the last bin includes 1.0. Printed verbatim in the dataset.
RELIABILITY_BIN_EDGES: tuple[float, ...] = tuple(i / 10.0 for i in range(11))
