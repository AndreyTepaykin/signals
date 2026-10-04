"""The statistics: one function, `summarize`, turns the observations of ONE cohort into a `CellSummary`.

Independent sample: first row of the group (the single code path)
------------------------------------------------------------------
`collapse_independent` keeps, for each `sample_group_id`, the row with the earliest `(decision_time, candidate_id)`. Every
statistic is computed on those rows only; the raw counts are reported next to them but never feed a statistic.

Why the first row and not the group mean:

* It is the trade the rules would have taken. "One decision per coin per horizon: the next signal waits until this one
  resolves" and "alt signals that share one BTC impulse count as one sample" both mean the first signal is the position
  and the later group members are the ones that would have been skipped.
* It is chosen from decision-time data only (`sample_group_id` is assigned at candidate creation; the order is the
  decision time). A mean over the group would also use later members' outcomes, and the representative would then be a
  synthetic number that no one traded.
* Stored R stays one real trade with its own costs, so profit factor, win rate and drawdown keep their meaning. A group
  mean is not a trade: a basket of three correlated alts is neither a win nor a loss in the same sense, and drawdown over
  baskets is not a drawdown anyone would have had.

Selection happens BEFORE `no_entry` rows are dropped (`no_entry` is a label outcome, so dropping first would make the
representative depend on how the fill went); a group whose first row is `no_entry` counts as a `no_entry` independent
sample and contributes no R. Rows without a label never reach this module.

The cost is variance: a group's later members are ignored. That is the conservative direction for a promotion floor.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.core.stats import params as P
from app.core.stats.observations import CohortKey, MixedCohortError, Observation
from app.models import enums as E


# ---------------------------------------------------------------------------
# Independent sample
# ---------------------------------------------------------------------------
def collapse_independent(observations: Iterable[Observation]) -> list[Observation]:
    """First row of every sample group, ordered by `(decision_time, candidate_id)`."""
    first: dict[str, Observation] = {}
    for observation in observations:
        current = first.get(observation.sample_group_id)
        if current is None or _order(observation) < _order(current):
            first[observation.sample_group_id] = observation
    return sorted(first.values(), key=_order)


def _order(observation: Observation) -> tuple[Any, int]:
    return (observation.decision_time, observation.candidate_id)


def require_single_cohort(observations: Sequence[Observation], cohort: CohortKey) -> None:
    for observation in observations:
        if observation.cohort != cohort:
            raise MixedCohortError(
                f'candidate {observation.candidate_id} is cohort {observation.cohort.label}, '
                f'asked to pool it into {cohort.label}'
            )


# ---------------------------------------------------------------------------
# Pure math over a list of R values
# ---------------------------------------------------------------------------
def mean_r(values: Sequence[float]) -> float | None:
    return float(np.mean(values)) if len(values) else None


def bootstrap_interval(
    values: Sequence[float],
    *,
    confidence: float = P.INTERVAL_CONFIDENCE,
    resamples: int = P.BOOTSTRAP_RESAMPLES,
    seed: int = P.BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Percentile bootstrap interval of the mean. None below two values (one value has no spread to resample)."""
    n = len(values)
    if n < 2:
        return None
    data = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = data[rng.integers(0, n, size=(resamples, n))].mean(axis=1)
    tail = (1.0 - confidence) / 2.0
    low, high = np.quantile(means, [tail, 1.0 - tail])
    return float(low), float(high)


def profit_factor(values: Sequence[float]) -> tuple[float | None, str | None]:
    """Gross wins over gross losses. `(None, why)` when it is undefined (no losing outcome)."""
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if losses > 0:
        return wins / losses, None
    if wins > 0:
        return None, 'no losing outcome: undefined'
    return None, 'no winning or losing outcome'


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative R curve, as a non-negative magnitude. The curve starts at 0."""
    peak = 0.0
    total = 0.0
    worst = 0.0
    for value in values:
        total += value
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def win_rate(values: Sequence[float]) -> float | None:
    """Share with stored R above zero (a miss is stored R <= 0). A companion to expectancy, never a headline."""
    return None if not len(values) else sum(1 for v in values if v > 0) / len(values)


def brier_score(pairs: Sequence[tuple[float, bool]]) -> float | None:
    """Mean squared gap between the stated probability and what happened (0/1). None with no pairs."""
    if not pairs:
        return None
    return sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in pairs) / len(pairs)


def sample_note(n_independent: int) -> tuple[bool, str]:
    """`(insufficient, text)` for a statistic computed over `n_independent` independent filled outcomes."""
    if n_independent == 0:
        return True, 'n=0 independent: nothing to conclude'
    if n_independent < P.MIN_INDEPENDENT_TO_CONCLUDE:
        return True, f'n={n_independent} independent: too small to conclude (need at least {P.MIN_INDEPENDENT_TO_CONCLUDE})'
    return False, f'n={n_independent} independent'


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CellSummary:
    """Every number for one cell of one cohort. `n_*_raw` are label rows; `n_*_independent` are sample groups."""

    cohort: str
    # --- counts (raw next to independent) ---
    n_labels_raw: int
    n_labels_independent: int
    n_no_entry_raw: int
    n_no_entry_independent: int
    no_entry_rate_raw: float | None
    no_entry_rate_independent: float | None
    n_filled_raw: int
    n_filled_independent: int  # the n behind every statistic below
    # --- sample-size honesty ---
    insufficient_sample: bool
    sample_note: str
    # --- expectancy (stored R, independent filled outcomes only) ---
    expectancy_r: float | None
    interval_low: float | None
    interval_high: float | None
    interval_confidence: float
    profit_factor: float | None
    profit_factor_note: str | None
    max_drawdown_r: float
    win_rate: float | None  # companion
    barrier_counts_independent: dict[str, int]
    # --- Brier: needs a stored probability ---
    brier: float | None
    n_with_probability: int
    brier_note: str
    # --- label quality (share among FILLED labels, no_entry excluded) ---
    n_ambiguous_raw: int
    ambiguous_share_raw: float | None
    n_ambiguous_independent: int
    ambiguous_share_independent: float | None
    ambiguous_flag: bool
    # --- funding_partial ---
    n_funding_partial_raw: int
    funding_partial_share_raw: float | None
    n_funding_partial_independent: int
    expectancy_r_ex_funding_partial: float | None  # funding-sensitive: drops funding_partial rows
    n_ex_funding_partial: int
    funding_r_mean_complete: float | None  # funding-sensitive: mean funding_r over funding-complete rows only

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _share(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def summarize(observations: Sequence[Observation], cohort: CohortKey) -> CellSummary:
    """Statistics for one cell. `cohort` is mandatory and every observation must belong to it: pooling is a TypeError-level
    mistake here, not a convention. An empty list is a valid, all-zero cell."""
    require_single_cohort(observations, cohort)
    independent = collapse_independent(observations)

    raw_filled = [o for o in observations if not o.is_no_entry]
    ind_filled = [o for o in independent if not o.is_no_entry]
    r_values = [_stored_r(o) for o in ind_filled]  # already in decision order; drawdown re-sorts by resolution below
    by_resolution = sorted(ind_filled, key=lambda o: (o.resolved_at, o.candidate_id))
    curve = [_stored_r(o) for o in by_resolution]

    insufficient, note = sample_note(len(ind_filled))
    interval = bootstrap_interval(r_values)
    pf, pf_note = profit_factor(r_values)

    pairs = [(o.p_upper, o.first_barrier == E.BARRIER_UPPER) for o in ind_filled if o.p_upper is not None]
    brier = brier_score(pairs)

    ambiguous_raw = sum(1 for o in raw_filled if o.barrier_ambiguous)
    ambiguous_ind = sum(1 for o in ind_filled if o.barrier_ambiguous)
    share_raw = _share(ambiguous_raw, len(raw_filled))

    partial_raw = sum(1 for o in raw_filled if o.funding_partial)
    complete = [o for o in ind_filled if not o.funding_partial]
    complete_r = [_stored_r(o) for o in complete]
    complete_funding = [o.funding_r for o in complete if o.funding_r is not None]

    return CellSummary(
        cohort=cohort.label,
        n_labels_raw=len(observations),
        n_labels_independent=len(independent),
        n_no_entry_raw=len(observations) - len(raw_filled),
        n_no_entry_independent=len(independent) - len(ind_filled),
        no_entry_rate_raw=_share(len(observations) - len(raw_filled), len(observations)),
        no_entry_rate_independent=_share(len(independent) - len(ind_filled), len(independent)),
        n_filled_raw=len(raw_filled),
        n_filled_independent=len(ind_filled),
        insufficient_sample=insufficient,
        sample_note=note,
        expectancy_r=mean_r(r_values),
        interval_low=None if interval is None else interval[0],
        interval_high=None if interval is None else interval[1],
        interval_confidence=P.INTERVAL_CONFIDENCE,
        profit_factor=pf,
        profit_factor_note=pf_note,
        max_drawdown_r=max_drawdown(curve),
        win_rate=win_rate(r_values),
        barrier_counts_independent=dict(sorted(Counter(o.first_barrier for o in ind_filled).items())),
        brier=brier,
        n_with_probability=len(pairs),
        brier_note=_brier_note(len(pairs), len(ind_filled)),
        n_ambiguous_raw=ambiguous_raw,
        ambiguous_share_raw=share_raw,
        n_ambiguous_independent=ambiguous_ind,
        ambiguous_share_independent=_share(ambiguous_ind, len(ind_filled)),
        ambiguous_flag=share_raw is not None and share_raw > P.AMBIGUOUS_SHARE_FLAG,
        n_funding_partial_raw=partial_raw,
        funding_partial_share_raw=_share(partial_raw, len(raw_filled)),
        n_funding_partial_independent=sum(1 for o in ind_filled if o.funding_partial),
        expectancy_r_ex_funding_partial=mean_r(complete_r),
        n_ex_funding_partial=len(complete),
        funding_r_mean_complete=mean_r(complete_funding),
    )


def _stored_r(observation: Observation) -> float:
    if observation.stored_r is None:  # the label CHECK forbids it for a filled label; fail loudly, never impute
        raise ValueError(f'filled candidate {observation.candidate_id} has no stored R')
    return observation.stored_r


def _brier_note(n_with_p: int, n_filled: int) -> str:
    if n_with_p == 0:
        return 'no probability stored on these candidates (p_upper_before_stop is NULL): Brier not computed'
    if n_with_p < n_filled:
        return f'Brier over {n_with_p} of {n_filled} independent outcomes that carry a probability'
    return f'Brier over all {n_with_p} independent outcomes'

