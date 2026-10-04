"""Assemble the M8 report from observations. Pure: no I/O, no clock.

Structure (per cohort; cohorts are never merged, see `observations.py`):

* **alerts** and **shadow** (cancelled / delayed / watch / abstained) are separate populations. Shadow rows are also
  itemized by status and `status_reason`, so a filter (CVD, a stand-down, a vote conflict) can be judged by what it
  blocked.
* each population: overall, per horizon, and the pool walk-up (full slice -> drop event flag -> drop session -> trend x
  vol -> setup family; a level whose dimension set equals the previous one's is skipped, and a cell that is identical to
  its parent cell at the previous level is marked and not repeated in the text).
* `no_entry` counts and rates per status and horizon.
* reliability diagram dataset and Brier (alerts).
* comparison with the always-flat baseline (0 R) and the swing-break baseline, per scope (all horizons and each horizon).
* kill-rule check (alerts, all horizons).

Swing-break baseline (what exists and what does not)
----------------------------------------------------
The baseline is every candidate with `trigger = 'swing_break'`, whatever its status, whose barriers the options layer left
alone (`stop_price = pre_options_stop_price` and no target cap). Those rows were resolved on the chandelier stop, the same
12 bps costs, the 15m barrier labeler and the horizon's vertical barrier: the published rule's inputs. Rows the options
layer moved (amplify widened the stop, headwind / dampen / expiry_pin capped the target) are NOT the published rule's
outcome and are excluded and counted (`n_excluded_options_modified`); re-resolving them on the pre-options barriers is
not built. The set is restricted to the 4h breaks that became candidates, so it excludes breaks the app never wrote (the
trend-family gate, an open signal on the coin, the alt residual gate). See the M8 BUILD-LOG entry for the open decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core import constants as C
from app.core.stats import params as P
from app.core.stats.observations import CohortKey, Observation, split_by_cohort
from app.core.stats.reliability import ReliabilityDataset, reliability_dataset
from app.core.stats.summary import CellSummary, summarize
from app.models import enums as E

POPULATION_ALERT = 'alert'
POPULATION_SHADOW = 'shadow'
POPULATIONS = (POPULATION_ALERT, POPULATION_SHADOW)

KILL_NOT_APPLICABLE = 'not_yet_applicable'
KILL_TRIGGERED = 'triggered'
KILL_NOT_TRIGGERED = 'not_triggered'
KILL_NO_BASELINE = 'baseline_unavailable'


def in_population(observation: Observation, population: str) -> bool:
    return (observation.status == E.STATUS_ALERT) == (population == POPULATION_ALERT)


# ---------------------------------------------------------------------------
# Walk-up
# ---------------------------------------------------------------------------
def distinct_walk_up_levels() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """`constants.POOL_WALK_UP_LEVELS` without any level whose dimension set equals the previous one's."""
    kept: list[tuple[str, tuple[str, ...]]] = []
    for name, dims in C.POOL_WALK_UP_LEVELS:
        if kept and set(kept[-1][1]) == set(dims):
            continue
        kept.append((name, tuple(dims)))
    return tuple(kept)


@dataclass(frozen=True)
class WalkUpCell:
    level: str
    key: dict[str, str]
    same_as_previous_level: bool
    summary: CellSummary


@dataclass(frozen=True)
class WalkUpLevel:
    level: str
    dimensions: tuple[str, ...]
    cells: tuple[WalkUpCell, ...]


def walk_up(observations: Sequence[Observation], cohort: CohortKey) -> tuple[tuple[str, ...], tuple[WalkUpLevel, ...]]:
    """Cells at each distinct walk-up level. Returns `(skipped_level_names, levels)`."""
    kept = distinct_walk_up_levels()
    skipped = tuple(name for name, _ in C.POOL_WALK_UP_LEVELS if name not in {k for k, _ in kept})
    levels: list[WalkUpLevel] = []
    previous: tuple[tuple[str, ...], dict[tuple[str, ...], frozenset[int]]] | None = None
    for name, dims in kept:
        groups: dict[tuple[str, ...], list[Observation]] = {}
        for o in observations:
            groups.setdefault(tuple(o.pool_dimension(d) for d in dims), []).append(o)
        cells: list[WalkUpCell] = []
        ids: dict[tuple[str, ...], frozenset[int]] = {}
        for key in sorted(groups):
            rows = groups[key]
            members = frozenset(o.candidate_id for o in rows)
            ids[key] = members
            same = False
            if previous is not None:
                # The previous level is finer (more dimensions): this cell is "the previous one" when it contains
                # exactly one finer cell and that cell has the same members.
                prev_dims, prev_ids = previous
                inside = [m for fine, m in prev_ids.items() if tuple(fine[prev_dims.index(d)] for d in dims) == key]
                same = len(inside) == 1 and inside[0] == members
            cells.append(WalkUpCell(name, dict(zip(dims, key)), same, summarize(rows, cohort)))
        levels.append(WalkUpLevel(name, dims, tuple(cells)))
        previous = (dims, ids)
    return skipped, tuple(levels)


# ---------------------------------------------------------------------------
# Baselines and kill rule
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SwingBreakBaseline:
    summary: CellSummary
    n_swing_break_rows: int
    n_excluded_options_modified: int


def swing_break_baseline(cohort_rows: Sequence[Observation], cohort: CohortKey, horizon: str | None = None) -> SwingBreakBaseline:
    rows = [o for o in cohort_rows if o.trigger == 'swing_break' and (horizon is None or o.horizon == horizon)]
    usable = [o for o in rows if o.barriers_unmodified]
    return SwingBreakBaseline(summarize(usable, cohort), len(rows), len(rows) - len(usable))


def verdict_against(app: CellSummary, reference: float) -> str:
    """Where the app's interval sits against a reference expectancy."""
    if app.interval_low is None or app.interval_high is None:
        return 'no_interval'
    if app.interval_low > reference:
        return 'interval_above'
    if app.interval_high < reference:
        return 'interval_below'
    return 'interval_includes'


@dataclass(frozen=True)
class Comparison:
    scope: str  # 'all' or a horizon
    app: CellSummary
    flat_expectancy_r: float
    vs_flat: str
    baseline: SwingBreakBaseline
    vs_swing_break: str


@dataclass(frozen=True)
class KillRuleResult:
    status: str
    n_independent: int
    floor: int
    app_interval: tuple[float, float] | None
    baseline_expectancy_r: float | None
    note: str


def kill_rule_check(app: CellSummary, baseline: CellSummary) -> KillRuleResult:
    """"At 300 independent labels, if the app's expectancy interval lies entirely below the swing-break baseline."

    Counted in independent FILLED labels (`no_entry` has no R to put in an interval). Below the floor the answer is "not yet
    applicable" whatever the interval says. "Below the baseline" means the interval's upper end is under the baseline's
    expectancy.
    """
    n = app.n_filled_independent
    floor = P.KILL_RULE_MIN_INDEPENDENT
    interval = None if app.interval_low is None else (app.interval_low, app.interval_high)
    if n < floor:
        return KillRuleResult(KILL_NOT_APPLICABLE, n, floor, interval, baseline.expectancy_r, f'not yet applicable: n={n} independent of {floor}')
    if baseline.expectancy_r is None or interval is None:
        return KillRuleResult(KILL_NO_BASELINE, n, floor, interval, baseline.expectancy_r, 'no swing-break baseline outcomes to compare with')
    if interval[1] < baseline.expectancy_r:
        return KillRuleResult(KILL_TRIGGERED, n, floor, interval, baseline.expectancy_r, 'app interval entirely below the swing-break baseline: follow the kill rule')
    return KillRuleResult(KILL_NOT_TRIGGERED, n, floor, interval, baseline.expectancy_r, 'app interval is not entirely below the swing-break baseline')


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NoEntryRow:
    status: str
    horizon: str
    n_labels_raw: int
    n_no_entry_raw: int
    rate_raw: float | None
    n_labels_independent: int
    n_no_entry_independent: int
    rate_independent: float | None


@dataclass(frozen=True)
class ShadowRow:
    status: str
    status_reason: str
    summary: CellSummary


@dataclass(frozen=True)
class PopulationReport:
    population: str
    overall: CellSummary
    by_horizon: dict[str, CellSummary]
    walk_up_skipped_levels: tuple[str, ...]
    walk_up: tuple[WalkUpLevel, ...]


@dataclass(frozen=True)
class CohortReport:
    cohort: CohortKey
    populations: dict[str, PopulationReport]
    shadow_by_reason: tuple[ShadowRow, ...]
    no_entry: tuple[NoEntryRow, ...]
    reliability: ReliabilityDataset
    comparisons: tuple[Comparison, ...]
    kill_rule: KillRuleResult
    flags: tuple[str, ...]


@dataclass(frozen=True)
class StatsReport:
    rule_hash: str | None
    context: dict[str, Any]
    cohorts: tuple[CohortReport, ...]
    parameters: dict[str, Any]


def _no_entry_rows(rows: Sequence[Observation], cohort: CohortKey) -> tuple[NoEntryRow, ...]:
    keys = sorted({(o.status, o.horizon) for o in rows})
    out: list[NoEntryRow] = []
    for status, horizon in [('all', 'all')] + [(s, 'all') for s in sorted({k[0] for k in keys})] + keys:
        subset = [o for o in rows if (status == 'all' or o.status == status) and (horizon == 'all' or o.horizon == horizon)]
        s = summarize(subset, cohort)
        out.append(
            NoEntryRow(
                status, horizon, s.n_labels_raw, s.n_no_entry_raw, s.no_entry_rate_raw,
                s.n_labels_independent, s.n_no_entry_independent, s.no_entry_rate_independent,
            )
        )
    return tuple(out)


def _flags(cohort: CohortKey, populations: dict[str, PopulationReport], kill: KillRuleResult) -> tuple[str, ...]:
    flags: list[str] = []
    for name, report in populations.items():
        s = report.overall
        if s.ambiguous_flag:
            flags.append(
                f'{name}: barrier_ambiguous is {s.ambiguous_share_raw:.1%} of filled labels (above {P.AMBIGUOUS_SHARE_FLAG:.0%}): '
                'revisit with on-demand Binance 1m klines for those bars only (not built)'
            )
        if s.n_funding_partial_raw:
            flags.append(f'{name}: {s.n_funding_partial_raw} funding_partial labels; funding-sensitive figures drop them')
    if cohort.lookback == 'thin':
        flags.append(f'thin lookback: signal bars before {P.THIN_LOOKBACK_CUTOFF:%Y-%m-%d} ran on less history than the engine windows')
    if cohort.options_path == 'no_options_history':
        flags.append('no_options_history: BTC/ETH replay rows with no Deribit book; never comparable with live options-layer rows')
    return tuple(flags)


def build_cohort_report(rows: Sequence[Observation], cohort: CohortKey) -> CohortReport:
    horizons = sorted({o.horizon for o in rows}, key=lambda h: C.HORIZONS.index(h))
    populations: dict[str, PopulationReport] = {}
    for population in POPULATIONS:
        subset = [o for o in rows if in_population(o, population)]
        skipped, levels = walk_up(subset, cohort)
        populations[population] = PopulationReport(
            population=population,
            overall=summarize(subset, cohort),
            by_horizon={h: summarize([o for o in subset if o.horizon == h], cohort) for h in horizons},
            walk_up_skipped_levels=skipped,
            walk_up=levels,
        )

    shadow_rows = [o for o in rows if in_population(o, POPULATION_SHADOW)]
    pairs = sorted({(o.status, o.status_reason or '') for o in shadow_rows})
    shadow_by_reason = tuple(
        ShadowRow(s, r, summarize([o for o in shadow_rows if o.status == s and (o.status_reason or '') == r], cohort))
        for s, r in pairs
    )

    alerts = [o for o in rows if in_population(o, POPULATION_ALERT)]
    comparisons: list[Comparison] = []
    for scope in ['all', *horizons]:
        app = populations[POPULATION_ALERT].overall if scope == 'all' else populations[POPULATION_ALERT].by_horizon[scope]
        baseline = swing_break_baseline(rows, cohort, None if scope == 'all' else scope)
        comparisons.append(
            Comparison(
                scope=scope,
                app=app,
                flat_expectancy_r=0.0,
                vs_flat=verdict_against(app, 0.0),
                baseline=baseline,
                vs_swing_break='baseline_unavailable' if baseline.summary.expectancy_r is None else verdict_against(app, baseline.summary.expectancy_r),
            )
        )
    kill = kill_rule_check(populations[POPULATION_ALERT].overall, comparisons[0].baseline.summary)
    return CohortReport(
        cohort=cohort,
        populations=populations,
        shadow_by_reason=shadow_by_reason,
        no_entry=_no_entry_rows(rows, cohort),
        reliability=reliability_dataset(alerts, cohort),
        comparisons=tuple(comparisons),
        kill_rule=kill,
        flags=_flags(cohort, populations, kill),
    )


def report_parameters() -> dict[str, Any]:
    return {
        'min_independent_to_conclude': P.MIN_INDEPENDENT_TO_CONCLUDE,
        'min_bin_outcomes': P.MIN_BIN_OUTCOMES,
        'kill_rule_min_independent': P.KILL_RULE_MIN_INDEPENDENT,
        'interval_confidence': P.INTERVAL_CONFIDENCE,
        'interval_method': f'percentile bootstrap, {P.BOOTSTRAP_RESAMPLES} resamples, seed {P.BOOTSTRAP_SEED}',
        'independent_sample': 'first row of each sample_group_id by (decision_time, candidate_id)',
        'ambiguous_share_flag': P.AMBIGUOUS_SHARE_FLAG,
        'thin_lookback_cutoff': P.THIN_LOOKBACK_CUTOFF.isoformat(),
        'vol_bucket_edges': list(P.VOL_BUCKET_EDGES),
        'reliability_bin_edges': list(P.RELIABILITY_BIN_EDGES),
    }


def build_report(
    observations: Sequence[Observation],
    *,
    rule_hash: str | None = None,
    context: dict[str, Any] | None = None,
) -> StatsReport:
    cohorts = tuple(build_cohort_report(rows, key) for key, rows in split_by_cohort(list(observations)).items())
    return StatsReport(rule_hash=rule_hash, context=dict(context or {}), cohorts=cohorts, parameters=report_parameters())
