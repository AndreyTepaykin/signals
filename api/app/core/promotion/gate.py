"""The shadow promotion gate: one verdict per layer, cohort and horizon. Pure: no I/O, no clock (``as_of`` is passed in).

Spec ("Shadow mode and promotion"): promote a layer only when the shadow window has run at least four weeks, the locked
book has at least 50 independent paired outcomes (same candidates with and without the layer, costs on, embargo equal
to the horizon), the mean paired difference in stored R is positive, and the 90% interval on that difference lies
above zero. Otherwise the layer stays in shadow, and the rejection is logged with everything it was judged by.

The gate only REPORTS. Nothing here changes a live decision; promoting a layer into the rules is a later, human
``RULE_VERSION`` bump.

One cell, in this order
-----------------------
1. **Cell.** One cohort (`CohortKey`: source, options path, lookback; a foreign row raises `MixedCohortError`) and one
   horizon (a foreign horizon raises ``ValueError``). Cohorts and horizons are never pooled.
2. **Point in time.** Rows resolved after ``as_of`` are dropped (their outcome did not exist yet).
3. **Locked book.** Only ``locked_book is True``; a row whose flag is unknown is counted and dropped.
4. **Independent sample.** First row per ``sample_group_id`` (`summary.collapse_independent`, the stats engine's only
   path), then ``no_entry`` labels are dropped, as in every R statistic.
5. **Pair.** ``layer.pair(observation, counterfactual)``; ``None`` = not in the paired population.
6. **Embargo.** Within one coin, in decision order, a pair whose decision time is earlier than the last KEPT pair's
   decision time plus the horizon is dropped.
7. **Floors and verdict.** Shadow window = first kept pair's decision time to ``as_of``, at least
   `params.MIN_SHADOW_DAYS` days; at least `params.MIN_PAIRED_OUTCOMES` pairs. If a floor fails: ``insufficient``.
   Otherwise ``promote`` when the mean difference and the lower end of the percentile-bootstrap interval are both
   above zero, else ``reject``. Every failed criterion is listed.
8. **Binding.** Only a ``live`` cell binds. A replay cell's verdict is prefixed ``preview_`` and is never ``promote``
   (as the kill rule's replay preview in `app.core.stats.report`).

Difference = R with the layer minus R without it. For a filter audit, a negative interval means the filter blocked
winners ("Filters that block winners come out"); the reason list says so.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.core import constants as C
from app.core.promotion import params as P
from app.core.promotion.layers import Counterfactual, Layer
from app.core.stats.observations import CohortKey, Observation, split_by_cohort
from app.core.stats.summary import bootstrap_interval, collapse_independent, mean_r, require_single_cohort

VERDICT_PROMOTE = 'promote'
VERDICT_REJECT = 'reject'
VERDICT_INSUFFICIENT = 'insufficient'
PREVIEW_PREFIX = 'preview_'
BINDING_VERDICTS = (VERDICT_PROMOTE, VERDICT_REJECT, VERDICT_INSUFFICIENT)
PREVIEW_VERDICTS = tuple(PREVIEW_PREFIX + v for v in BINDING_VERDICTS)
VERDICTS = BINDING_VERDICTS + PREVIEW_VERDICTS

REASON_WINDOW = 'shadow_window_below_floor'
REASON_PAIRS = 'paired_outcomes_below_floor'
REASON_MEAN = 'mean_diff_not_positive'
REASON_INTERVAL = 'interval_not_above_zero'
REASON_NO_INTERVAL = 'no_interval'
REASON_BLOCKS_WINNERS = 'interval_below_zero_layer_hurts'
REASON_ALL_MET = 'all_criteria_met'
REASON_ADVISORY = 'advisory_replay'

_DAY = timedelta(days=1)


@dataclass(frozen=True)
class PairedOutcome:
    candidate_id: int
    coin: str
    decision_time: datetime
    r_with: float
    r_without: float
    blocked: bool  # the layer suppressed this row (False: a pass-through, difference 0)

    @property
    def diff(self) -> float:
        return self.r_with - self.r_without


@dataclass(frozen=True)
class GateCell:
    """One verdict. ``params`` carries every parameter it was judged by; ``counts`` says where the rows went."""

    layer: str
    cohort: CohortKey
    horizon: str
    verdict: str
    binding: bool
    n_pairs: int
    mean_diff_r: float | None
    ci_low: float | None
    ci_high: float | None
    shadow_start: datetime | None
    as_of: datetime
    window_days: float
    reasons: tuple[str, ...]
    params: Mapping[str, Any]
    counts: Mapping[str, int]
    pairs: tuple[PairedOutcome, ...] = field(default=(), repr=False)

    @property
    def n_blocked_pairs(self) -> int:
        return sum(1 for p in self.pairs if p.blocked)


def gate_params(layer: Layer, horizon: str) -> dict[str, Any]:
    """Everything a verdict was judged by, so the log is self-describing."""
    return {
        'layer': {'name': layer.name, 'description': layer.description, **dict(layer.definition)},
        'min_shadow_weeks': P.MIN_SHADOW_WEEKS,
        'min_shadow_days': P.MIN_SHADOW_DAYS,
        'min_paired_outcomes': P.MIN_PAIRED_OUTCOMES,
        'interval_confidence': P.INTERVAL_CONFIDENCE,
        'mean_diff_min_exclusive': P.MEAN_DIFF_MIN_EXCLUSIVE,
        'bootstrap': {'method': P.BOOTSTRAP_METHOD, 'resamples': P.BOOTSTRAP_RESAMPLES, 'seed': P.BOOTSTRAP_SEED},
        'embargo_hours': dict(P.EMBARGO_HOURS),
        'embargo_hours_this_cell': P.EMBARGO_HOURS[horizon],
        'embargo_scope': 'per coin within the cell, measured from the last kept pair',
        'population': 'locked_book only',
        'independence': 'first row per sample_group_id by (decision_time, candidate_id), before no_entry is dropped',
        'no_entry': 'excluded',
        'difference': 'r_with_layer - r_without_layer',
        'binding_source': P.BINDING_SOURCE,
        'bin_edges': {'lookback_thin_before': P.LOOKBACK_THIN_BEFORE.isoformat()},
    }


def embargo(pairs: Sequence[PairedOutcome], horizon: str) -> list[PairedOutcome]:
    """Keep a pair only if it is at least one horizon after the last KEPT pair on the same coin."""
    hold = timedelta(hours=P.EMBARGO_HOURS[horizon])
    last_kept: dict[str, datetime] = {}
    kept: list[PairedOutcome] = []
    for pair in sorted(pairs, key=lambda p: (p.decision_time, p.candidate_id)):
        previous = last_kept.get(pair.coin)
        if previous is not None and pair.decision_time < previous + hold:
            continue
        last_kept[pair.coin] = pair.decision_time
        kept.append(pair)
    return kept


def evaluate_cell(
    layer: Layer,
    observations: Sequence[Observation],
    *,
    cohort: CohortKey,
    horizon: str,
    as_of: datetime,
    counterfactuals: Mapping[int, Counterfactual] | None = None,
) -> GateCell:
    """The verdict for one layer in one cohort and one horizon. Refuses rows from another cohort or horizon."""
    if horizon not in C.HORIZONS:
        raise ValueError(f'unknown horizon {horizon!r}')
    require_single_cohort(observations, cohort)
    for observation in observations:
        if observation.horizon != horizon:
            raise ValueError(f'candidate {observation.candidate_id} is horizon {observation.horizon!r}, cell is {horizon!r}')
    facts = counterfactuals or {}

    resolved = [o for o in observations if o.resolved_at <= as_of]
    locked = [o for o in resolved if o.locked_book is True]
    independent = collapse_independent(locked)
    filled = [o for o in independent if not o.is_no_entry]
    paired: list[PairedOutcome] = []
    for observation in filled:
        result = layer.pair(observation, facts.get(observation.candidate_id))
        if result is not None:
            paired.append(
                PairedOutcome(
                    observation.candidate_id,
                    observation.coin,
                    observation.decision_time,
                    result[0],
                    result[1],
                    layer.blocks(observation),
                )
            )
    kept = embargo(paired, horizon)
    counts = {
        'rows': len(observations),
        'resolved_after_as_of': len(observations) - len(resolved),
        'not_locked_book': sum(1 for o in resolved if o.locked_book is False),
        'locked_book_unknown': sum(1 for o in resolved if o.locked_book is None),
        'locked_book': len(locked),
        'independent': len(independent),
        'no_entry': len(independent) - len(filled),
        'blocked_by_layer': sum(1 for o in filled if layer.blocks(o)),
        'blocked_paired': sum(1 for p in paired if p.blocked),
        'not_in_pair': len(filled) - len(paired),
        'paired_before_embargo': len(paired),
        'embargoed': len(paired) - len(kept),
        'pairs': len(kept),
    }
    return _verdict(layer, cohort, horizon, as_of, kept, counts)


def _verdict(
    layer: Layer,
    cohort: CohortKey,
    horizon: str,
    as_of: datetime,
    pairs: list[PairedOutcome],
    counts: dict[str, int],
) -> GateCell:
    diffs = [p.diff for p in pairs]
    n = len(diffs)
    start = pairs[0].decision_time if pairs else None
    window_days = 0.0 if start is None else max((as_of - start) / _DAY, 0.0)
    mean = mean_r(diffs)
    interval = bootstrap_interval(
        diffs, confidence=P.INTERVAL_CONFIDENCE, resamples=P.BOOTSTRAP_RESAMPLES, seed=P.BOOTSTRAP_SEED
    )

    reasons: list[str] = []
    if window_days < P.MIN_SHADOW_DAYS:
        reasons.append(f'{REASON_WINDOW}: {window_days:.2f} days < {P.MIN_SHADOW_DAYS}')
    if n < P.MIN_PAIRED_OUTCOMES:
        reasons.append(f'{REASON_PAIRS}: {n} < {P.MIN_PAIRED_OUTCOMES}')
    if reasons:
        verdict = VERDICT_INSUFFICIENT
    else:
        floor = P.MEAN_DIFF_MIN_EXCLUSIVE
        if mean is None or not mean > floor:
            reasons.append(f'{REASON_MEAN}: mean {"n/a" if mean is None else f"{mean:+.4f}"} R <= {floor}')
        if interval is None:
            reasons.append(REASON_NO_INTERVAL)
        elif not interval[0] > floor:
            reasons.append(f'{REASON_INTERVAL}: {P.INTERVAL_CONFIDENCE:.0%} interval [{interval[0]:+.4f}, {interval[1]:+.4f}] R')
        if interval is not None and interval[1] < floor:
            reasons.append(f'{REASON_BLOCKS_WINNERS}: the whole interval is below zero, the layer costs R')
        verdict = VERDICT_REJECT if reasons else VERDICT_PROMOTE
        if not reasons:
            reasons.append(REASON_ALL_MET)

    binding = cohort.source == P.BINDING_SOURCE
    if not binding:
        verdict = PREVIEW_PREFIX + verdict
        reasons.append(f'{REASON_ADVISORY}: {cohort.source} is a backtest; the gate binds on {P.BINDING_SOURCE} only')

    return GateCell(
        layer=layer.name,
        cohort=cohort,
        horizon=horizon,
        verdict=verdict,
        binding=binding,
        n_pairs=n,
        mean_diff_r=mean,
        ci_low=None if interval is None else interval[0],
        ci_high=None if interval is None else interval[1],
        shadow_start=start,
        as_of=as_of,
        window_days=window_days,
        reasons=tuple(reasons),
        params=gate_params(layer, horizon),
        counts=counts,
        pairs=tuple(pairs),
    )


def run_gate(
    layer: Layer,
    observations: Iterable[Observation],
    *,
    as_of: datetime,
    counterfactuals: Mapping[int, Counterfactual] | None = None,
) -> list[GateCell]:
    """One cell per (cohort, horizon) present in ``observations``, cohorts in key order, horizons in `constants.HORIZONS` order."""
    cells: list[GateCell] = []
    for cohort, rows in split_by_cohort(list(observations)).items():
        for horizon in C.HORIZONS:
            subset = [o for o in rows if o.horizon == horizon]
            if subset:
                cells.append(
                    evaluate_cell(layer, subset, cohort=cohort, horizon=horizon, as_of=as_of, counterfactuals=counterfactuals)
                )
    return cells
