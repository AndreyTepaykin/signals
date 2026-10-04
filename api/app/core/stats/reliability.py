"""Reliability diagram dataset.

Input is the stated probability on each candidate (`candidate.p_upper_before_stop`, "the confidence on the card") and what
happened. The spec: "Confidence is trusted only when bins with at least 20 outcomes hit about as often as they claim,
higher bins show higher average R, and Brier score sits beside expectancy." and "Bins under 20 outcomes are hidden."

* Bins hold INDEPENDENT outcomes (first row of each sample group, chosen before `no_entry` and missing probabilities are
  dropped), counted by the same `collapse_independent` as everything else.
* A bin under `params.MIN_BIN_OUTCOMES` independent outcomes is returned with `hidden=True` and every value withheld
  (None): the dataset itself does not carry the number, so a chart cannot draw it by accident. Its counts stay.
* "Hit" is `first_barrier == 'upper'`; a timeout (`vertical`) or a stop is a miss (owner decision, M8.1: the same definition the pooled
  estimate uses for `p_upper_before_stop`).
* Nothing here invents a probability. With none stored the dataset is returned empty with an explanatory note.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.stats import params as P
from app.core.stats.observations import CohortKey
from app.core.stats.summary import collapse_independent, require_single_cohort
from app.models import enums as E


@dataclass(frozen=True)
class ReliabilityBin:
    low: float
    high: float
    n_raw: int
    n_independent: int
    hidden: bool
    mean_probability: float | None
    observed_rate: float | None
    mean_stored_r: float | None


@dataclass(frozen=True)
class ReliabilityDataset:
    cohort: str
    bin_edges: tuple[float, ...]
    min_bin_outcomes: int
    n_independent_filled: int
    n_independent_with_probability: int
    available: bool
    note: str
    bins: tuple[ReliabilityBin, ...]
    visible_bins: int
    max_abs_gap: float | None  # over visible bins: |mean probability - observed rate|
    r_non_decreasing: bool | None  # over visible bins: does mean stored R rise with the bin?

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out['bins'] = [dict(b.__dict__) for b in self.bins]
        return out


def _bin_index(probability: float, edges: Sequence[float]) -> int:
    last = len(edges) - 2
    for index in range(last + 1):
        if probability < edges[index + 1]:
            return index
    return last  # exactly the top edge


def reliability_dataset(
    observations: Sequence[Any],
    cohort: CohortKey,
    *,
    edges: Sequence[float] = P.RELIABILITY_BIN_EDGES,
    min_bin_outcomes: int = P.MIN_BIN_OUTCOMES,
) -> ReliabilityDataset:
    require_single_cohort(observations, cohort)
    independent = [o for o in collapse_independent(observations) if not o.is_no_entry]
    with_p = [o for o in independent if o.p_upper is not None]
    raw_with_p = [o for o in observations if not o.is_no_entry and o.p_upper is not None]

    n_bins = len(edges) - 1
    members: list[list[Any]] = [[] for _ in range(n_bins)]
    raw_counts = [0] * n_bins
    for o in with_p:
        members[_bin_index(o.p_upper, edges)].append(o)
    for o in raw_with_p:
        raw_counts[_bin_index(o.p_upper, edges)] += 1

    bins: list[ReliabilityBin] = []
    for index in range(n_bins):
        rows = members[index]
        hidden = len(rows) < min_bin_outcomes
        bins.append(
            ReliabilityBin(
                low=edges[index],
                high=edges[index + 1],
                n_raw=raw_counts[index],
                n_independent=len(rows),
                hidden=hidden,
                mean_probability=None if hidden else sum(o.p_upper for o in rows) / len(rows),
                observed_rate=None if hidden else sum(1 for o in rows if o.first_barrier == E.BARRIER_UPPER) / len(rows),
                mean_stored_r=None if hidden else sum(o.stored_r for o in rows) / len(rows),
            )
        )

    visible = [b for b in bins if not b.hidden]
    if not with_p:
        note = 'no probability stored on these candidates (p_upper_before_stop is NULL): nothing to plot'
    elif not visible:
        note = f'every bin has fewer than {min_bin_outcomes} independent outcomes: all hidden'
    else:
        note = f'{len(visible)} of {n_bins} bins visible (at least {min_bin_outcomes} independent outcomes each)'
    gaps = [abs(b.mean_probability - b.observed_rate) for b in visible]
    means = [b.mean_stored_r for b in visible]
    return ReliabilityDataset(
        cohort=cohort.label,
        bin_edges=tuple(edges),
        min_bin_outcomes=min_bin_outcomes,
        n_independent_filled=len(independent),
        n_independent_with_probability=len(with_p),
        available=bool(with_p),
        note=note,
        bins=tuple(bins),
        visible_bins=len(visible),
        max_abs_gap=max(gaps) if gaps else None,
        r_non_decreasing=(all(a <= b for a, b in zip(means, means[1:])) if len(means) >= 2 else None),
    )
