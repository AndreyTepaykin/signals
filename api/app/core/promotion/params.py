"""Promotion-gate parameters (Phase 2 prep). Not hashed.

The gate reports and logs; it never changes a live decision (promoting a layer into the rules is a later, human
``RULE_VERSION`` bump), so nothing here may enter ``rule_hash``. The hash reads ``app.core.constants`` and
``app.core.features.params`` only. The spec's numbers already live in ``constants`` (``PROMOTION_*``) and are read
here, never restated; the bootstrap is the stats engine's own (same seed, same resamples).

Every value below is copied into each verdict's ``params`` so a logged decision names the floors it was judged by.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from app.core import constants as C
from app.core.stats import params as SP
from app.models import enums as E

#: "The shadow window has run at least four weeks."
MIN_SHADOW_WEEKS: int = C.PROMOTION_MIN_SHADOW_WEEKS
MIN_SHADOW_DAYS: int = 7 * MIN_SHADOW_WEEKS
#: "At least 50 independent paired outcomes."
MIN_PAIRED_OUTCOMES: int = C.PROMOTION_MIN_PAIRED_OUTCOMES
#: "The 90% interval on that difference lies above zero."
INTERVAL_CONFIDENCE: float = C.PROMOTION_INTERVAL_CONFIDENCE
#: The mean paired difference and the interval's lower bound must both be strictly above this.
MEAN_DIFF_MIN_EXCLUSIVE: float = C.PROMOTION_MEAN_DIFF_MIN_EXCLUSIVE

#: Percentile bootstrap of the mean paired difference: the stats engine's helper, seed and resample count.
BOOTSTRAP_METHOD = 'percentile'
BOOTSTRAP_RESAMPLES: int = SP.BOOTSTRAP_RESAMPLES
BOOTSTRAP_SEED: int = SP.BOOTSTRAP_SEED

if not C.PROMOTION_EMBARGO_EQUALS_HORIZON:  # pragma: no cover - the gate implements no other embargo
    raise RuntimeError('the promotion gate implements only "embargo equal to the horizon"')
#: "Embargo equal to the horizon": within one coin and horizon, a pair is dropped when its decision time is earlier
#: than the last kept pair's decision time plus this many hours.
EMBARGO_HOURS: Mapping[str, int] = MappingProxyType(dict(C.VERTICAL_BARRIER_HOURS))

#: A trade the layer stopped was not taken: it scores 0 R in the "with the layer" arm.
SKIPPED_TRADE_R: float = 0.0
#: The gate binds on live outcomes only; a replay cell gets an advisory verdict that can never be `promote`.
BINDING_SOURCE: str = E.SOURCE_LIVE

#: The gate bins nothing continuous: one cell per cohort and horizon. The only edge behind a cell is the stats
#: engine's thin-lookback cutoff (part of the cohort key), recorded verbatim in every verdict.
LOOKBACK_THIN_BEFORE: datetime = SP.THIN_LOOKBACK_CUTOFF
