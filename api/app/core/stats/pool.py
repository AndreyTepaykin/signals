"""The spec's pooled estimate (Label contract table, "Edge line"): P(upper before stop), expected R, MAE, hours, interval.

A pure function of ``(history, target)``. No database, no clock: the target carries its own decision instant and the
history is whatever labeled outcomes the caller loaded.

What the estimate is
--------------------
For a candidate (the *target*), walk up the edge line (``constants.POOL_WALK_UP_LEVELS``: full slice, drop the event flag,
drop session, trend x vol, setup family; levels that would repeat the same dimensions are skipped) and stop at the first
level whose cell holds at least ``POOL_MIN_INDEPENDENT_OUTCOMES`` (30) independent outcomes inside the last
``POOL_WINDOW_DAYS`` (90) days. In that cell:

* ``p_upper_before_stop`` = share of pooled independent outcomes whose first barrier was ``upper`` (a ``vertical`` timeout is a
  miss, exactly like a stop). This is the confidence on the card. The win rate (stored R > 0) stays a companion in the
  report; it is not this number.
* ``expected_r`` = mean *stored* R (net of fees, slippage and realized funding) of the same outcomes.
* ``expected_mae_r`` = mean MAE in R until the first barrier; ``median_hours_to_resolution`` = median hours to it.
* ``expected_r_ci_low/high`` = seeded percentile bootstrap, 90%.

Below the floor at every level nothing is estimated: the result is a :class:`NoEstimate`, never a small-n number.

Barriers as labeled
-------------------
Pooled outcomes keep the barriers they were *labeled* with. A row whose stop or target the options layer modified is
pooled with its own modified-barrier outcome, not re-simulated on the unmodified ones. Per the spec, a capped win is "the
probability of that cap". The pool therefore mixes labeled barrier geometries by design; it estimates what rows of this
kind realized, not a counterfactual.

Pool keys (all of these hold at **every** level; none is ever walked away)
--------------------------------------------------------------------------
* ``horizon`` (a scalp and a swing outcome are never pooled: different vertical barriers),
* ``source`` (replay and live never share a pool) and ``options_path`` (the M7 rule: no pooling across options paths),
* ``lookback = full`` (rows labeled on thin lookback are excluded from pools; a thin *target* may still receive an estimate),
* not ``no_entry`` (no R, nothing to pool). Every candidate status (alert, watch, shadow) is pooled: the outcome of a
  trade the rules did not alert still says what the setup realizes, and the pool needs the sample.

Point in time (no lookahead)
----------------------------
A history row counts for a target only if it was *knowable* at the target's decision instant:

* it was decided strictly before the target (``decision_time < target.decision_time``; the target itself and anything
  decided later or at the same bar never enter),
* its outcome was resolved and visible by then: ``resolved_at + knowable_lag <= target.decision_time``, where the lag is
  the kline settle lag the ``as_of`` gate uses (the closing bar is served ``feed_kline_settle_seconds`` after close), and
* it resolved no more than ``POOL_WINDOW_DAYS`` before the decision.

Independence uses the same rule as the report: the first row of each ``sample_group_id`` (by decision time) is the
representative. The representative is chosen among the rows decided before the target **without looking at outcomes**
(an unresolved first member makes the group's representative unknowable; it is not replaced by a later member, which
would make the choice depend on the outcome). The window and knowability filters are applied to the representatives.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from app.core import constants as C
from app.core.stats import params as P
from app.core.stats.observations import LOOKBACK_FULL, Observation, classify_lookback, vol_bucket
from app.core.stats.summary import bootstrap_interval, collapse_independent
from app.models import enums as E


@dataclass(frozen=True)
class PoolTarget:
    """What the estimate is looked up for: a candidate's pool keys and its decision instant."""

    decision_time: datetime
    horizon: str
    source: str
    options_path: str
    setup_family: str
    trend_family: str | None
    vol_pct: float | None
    session: str | None
    event_flag: bool | None

    def dimension(self, name: str) -> str:
        unknown = P.UNKNOWN
        if name == C.POOL_DIM_SETUP_FAMILY:
            return self.setup_family
        if name == C.POOL_DIM_TREND_FAMILY:
            return unknown if self.trend_family is None else str(self.trend_family)
        if name == C.POOL_DIM_VOL_PERCENTILE:
            return vol_bucket(self.vol_pct)
        if name == C.POOL_DIM_SESSION:
            return unknown if self.session is None else str(self.session)
        if name == C.POOL_DIM_EVENT_FLAG:
            return unknown if self.event_flag is None else str(self.event_flag)
        raise KeyError(name)


@dataclass(frozen=True)
class PoolEstimate:
    level: str
    n_independent: int
    p_upper_before_stop: float
    expected_r: float
    expected_mae_r: float | None
    median_hours_to_resolution: float | None
    expected_r_ci_low: float | None
    expected_r_ci_high: float | None
    interval_confidence: float


@dataclass(frozen=True)
class NoEstimate:
    """No level reached the floor. ``best_level`` / ``best_n`` is the level with the most independent outcomes (diagnostic)."""

    reason: str
    best_level: str | None
    best_n: int


def distinct_levels() -> list[tuple[str, tuple[str, ...]]]:
    """The edge line without repeated cells: a level with the same dimensions as the previous one is skipped."""
    out: list[tuple[str, tuple[str, ...]]] = []
    previous: tuple[str, ...] | None = None
    for level, dims in C.POOL_WALK_UP_LEVELS:
        if previous is not None and set(dims) == set(previous):
            continue
        out.append((level, tuple(dims)))
        previous = tuple(dims)
    return out


def _poolable(row: Observation, target: PoolTarget) -> bool:
    return (
        row.horizon == target.horizon
        and row.source == target.source
        and row.options_path == target.options_path
        and classify_lookback(row.bar_close) == LOOKBACK_FULL
        and row.decision_time < target.decision_time
    )


def _median(values: Sequence[float]) -> float | None:
    return float(np.median(values)) if len(values) else None


def estimate_pool(
    history: Sequence[Observation],
    target: PoolTarget,
    *,
    knowable_lag: timedelta,
    window: timedelta | None = None,
    floor: int | None = None,
) -> PoolEstimate | NoEstimate:
    """The pooled estimate for ``target``, or why there is none. See the module docstring."""
    window = window if window is not None else timedelta(days=C.POOL_WINDOW_DAYS)
    floor = floor if floor is not None else C.POOL_MIN_INDEPENDENT_OUTCOMES
    universe = [row for row in history if _poolable(row, target)]
    oldest = target.decision_time - window

    best_level: str | None = None
    best_n = 0
    for level, dims in distinct_levels():
        wanted = {dim: target.dimension(dim) for dim in dims}
        members = [row for row in universe if all(row.pool_dimension(dim) == value for dim, value in wanted.items())]
        # representative first (outcome-blind), then knowability and the window, then drop no_entry
        reps = collapse_independent(members)
        usable = [
            row
            for row in reps
            if row.resolved_at + knowable_lag <= target.decision_time and row.resolved_at > oldest
        ]
        filled = [row for row in usable if row.first_barrier != E.BARRIER_NO_ENTRY and row.stored_r is not None]
        if len(filled) > best_n:
            best_level, best_n = level, len(filled)
        if len(filled) < floor:
            continue
        returns = [float(row.stored_r) for row in filled if row.stored_r is not None]
        interval = bootstrap_interval(returns)
        maes = [row.mae_r for row in filled if row.mae_r is not None]
        hours = [row.hours_to_resolution for row in filled if row.hours_to_resolution is not None]
        return PoolEstimate(
            level=level,
            n_independent=len(filled),
            p_upper_before_stop=sum(1 for row in filled if row.first_barrier == E.BARRIER_UPPER) / len(filled),
            expected_r=float(np.mean(returns)),
            expected_mae_r=float(np.mean(maes)) if maes else None,
            median_hours_to_resolution=_median(hours),
            expected_r_ci_low=None if interval is None else interval[0],
            expected_r_ci_high=None if interval is None else interval[1],
            interval_confidence=P.INTERVAL_CONFIDENCE,
        )
    return NoEstimate(
        reason=f'no level reached {floor} independent outcomes in {window.days} days',
        best_level=best_level,
        best_n=best_n,
    )
