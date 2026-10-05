"""Assemble the M8 report from observations. Pure: no I/O, no clock.

Structure (per cohort; cohorts are never merged, see `observations.py`):

* **alerts** and **shadow** (cancelled / delayed / watch / abstained) are separate populations. Shadow rows are also
  itemized by status and `status_reason`, so a filter (CVD, a stand-down, a vote conflict) can be judged by what it
  blocked.
* each population: overall, per horizon, and the pool walk-up (full slice -> drop event flag -> drop session -> trend x
  vol -> setup family; a level whose dimension set equals the previous one's is skipped, and a cell that is identical to
  its parent cell at the previous level is marked and not repeated in the text).
* `no_entry` counts and rates per status and horizon.
* the walk-up is keyed by horizon at every level (a scalp and a swing outcome are never pooled).
* reliability diagram dataset and Brier read the STORED probability (`candidate.p_upper_before_stop`), for alerts and,
  separately, for every status. Bins under 20 independent outcomes are hidden.
* comparison with the always-flat baseline (0 R) and the NAIVE swing-break baseline (`baseline.py`: the published rule
  traded as written on every 4h break, no votes / gates / options), per scope (all horizons and each horizon). The old
  subset of app candidates is kept next to it as `baseline_candidates_subset` (rows whose barriers the options layer left
  alone; contaminated by the app's gates, so it is not the comparison).
* kill rule: BINDS ON LIVE ONLY (alert independent labels vs the live naive baseline, floor 300). Replay full-lookback
  cohorts carry an advisory `kill_rule_preview` whose status can never be `triggered`; thin cohorts get neither.
* `would_abstain` (alerts): alerts that carry a stored pool of >= 30 independent outcomes whose interval's lower bound is
  below 0 R. The abstain rule (M10) already turns those into `abstained`, so under the current rule hash this must be 0;
  abstained rows themselves are a shadow status.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core import constants as C
from app.core.stats import params as P
from app.core.stats.baseline import BaselineData, BaselineStats
from app.core.stats.observations import (
    LOOKBACK_FULL,
    BaselineKey,
    CohortKey,
    Observation,
    coin_set_for_path,
    split_by_cohort,
)
from app.core.stats.pool import distinct_levels
from app.core.stats.reliability import ReliabilityDataset, reliability_dataset
from app.core.stats.summary import CellSummary, collapse_independent, summarize
from app.models import enums as E

POPULATION_ALERT = 'alert'
POPULATION_SHADOW = 'shadow'
POPULATIONS = (POPULATION_ALERT, POPULATION_SHADOW)

KILL_NOT_APPLICABLE = 'not_yet_applicable'
KILL_TRIGGERED = 'triggered'
KILL_NOT_TRIGGERED = 'not_triggered'
KILL_NO_BASELINE = 'baseline_unavailable'
#: The advisory reading of the same rule on a replay cohort: none of these is ever `triggered`.
PREVIEW_PREFIX = 'preview_'
PREVIEW_STATUS = {
    KILL_NOT_APPLICABLE: PREVIEW_PREFIX + 'not_yet_applicable',
    KILL_TRIGGERED: PREVIEW_PREFIX + 'would_trigger',
    KILL_NOT_TRIGGERED: PREVIEW_PREFIX + 'would_not_trigger',
    KILL_NO_BASELINE: PREVIEW_PREFIX + 'baseline_unavailable',
}


def in_population(observation: Observation, population: str) -> bool:
    return (observation.status == E.STATUS_ALERT) == (population == POPULATION_ALERT)


# ---------------------------------------------------------------------------
# Walk-up
# ---------------------------------------------------------------------------
def distinct_walk_up_levels() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """`constants.POOL_WALK_UP_LEVELS` without any level whose dimension set equals the previous one's (as the estimator)."""
    return tuple(distinct_levels())


HORIZON_DIMENSION = 'horizon'


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
    """Cells at each distinct walk-up level, keyed by horizon first (no cross-horizon cell). Returns `(skipped, levels)`."""
    kept = distinct_walk_up_levels()
    skipped = tuple(name for name, _ in C.POOL_WALK_UP_LEVELS if name not in {k for k, _ in kept})
    levels: list[WalkUpLevel] = []
    previous: tuple[tuple[str, ...], dict[tuple[str, ...], frozenset[int]]] | None = None
    for name, level_dims in kept:
        dims = (HORIZON_DIMENSION, *level_dims)
        groups: dict[tuple[str, ...], list[Observation]] = {}
        for o in observations:
            groups.setdefault((o.horizon, *(o.pool_dimension(d) for d in level_dims)), []).append(o)
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
class NaiveBaseline:
    """The naive swing-break baseline for one scope of one app cohort (see `baseline.py`)."""

    key: str  # the baseline cohort label, e.g. `replay/alts/full`
    summary: CellSummary | None  # None when the baseline was not computed
    stats: BaselineStats | None  # event accounting for the baseline cohort (all horizons)

    @property
    def expectancy_r(self) -> float | None:
        return None if self.summary is None else self.summary.expectancy_r


@dataclass(frozen=True)
class Comparison:
    scope: str  # 'all' or a horizon
    app: CellSummary
    flat_expectancy_r: float
    vs_flat: str
    baseline: NaiveBaseline
    vs_swing_break: str
    baseline_candidates_subset: SwingBreakBaseline  # the M8 baseline, kept for reference only
    vs_candidates_subset: str


@dataclass(frozen=True)
class KillRuleResult:
    status: str
    n_independent: int
    floor: int
    app_interval: tuple[float, float] | None
    baseline_expectancy_r: float | None
    note: str
    binding: bool = True  # False on a replay preview: advisory, can never be `triggered`


def kill_rule_check(app: CellSummary, baseline: CellSummary | NaiveBaseline | None) -> KillRuleResult:
    """"At 300 independent labels, if the app's expectancy interval lies entirely below the swing-break baseline."

    The baseline is the NAIVE swing-break baseline of the same source / coin set / lookback. Counted in independent FILLED
    labels (`no_entry` has no R to put in an interval). Below the floor the answer is "not yet applicable" whatever the
    interval says. "Below the baseline" means the interval's upper end is under the baseline's expectancy.
    """
    expectancy = None if baseline is None else baseline.expectancy_r
    n = app.n_filled_independent
    floor = P.KILL_RULE_MIN_INDEPENDENT
    interval = None if app.interval_low is None else (app.interval_low, app.interval_high)
    if n < floor:
        return KillRuleResult(KILL_NOT_APPLICABLE, n, floor, interval, expectancy, f'not yet applicable: n={n} independent of {floor}')
    if expectancy is None or interval is None:
        return KillRuleResult(KILL_NO_BASELINE, n, floor, interval, expectancy, 'no naive swing-break baseline outcomes to compare with')
    if interval[1] < expectancy:
        return KillRuleResult(KILL_TRIGGERED, n, floor, interval, expectancy, 'app interval entirely below the naive swing-break baseline: follow the kill rule')
    return KillRuleResult(KILL_NOT_TRIGGERED, n, floor, interval, expectancy, 'app interval is not entirely below the naive swing-break baseline')


def kill_rule_for(
    cohort: CohortKey, app: CellSummary, baseline: NaiveBaseline | None
) -> tuple[KillRuleResult | None, KillRuleResult | None]:
    """`(kill_rule, kill_rule_preview)` for a cohort. The rule BINDS ON LIVE ONLY.

    * live, full lookback: the binding rule (status may be `triggered`);
    * replay, full lookback: an advisory preview of the same comparison, `binding=False`, status prefixed `preview_` and
      never `triggered`;
    * thin lookback (either source): neither.
    """
    if cohort.lookback != LOOKBACK_FULL:
        return None, None
    result = kill_rule_check(app, baseline)
    if cohort.source == E.SOURCE_LIVE:
        return result, None
    return None, KillRuleResult(
        PREVIEW_STATUS[result.status],
        result.n_independent,
        result.floor,
        result.app_interval,
        result.baseline_expectancy_r,
        'advisory preview (replay is a backtest; the kill rule binds on live only): ' + result.note,
        binding=False,
    )


@dataclass(frozen=True)
class WouldAbstain:
    """Alerts whose stored pool fails the abstain test even at a zero funding edge. A consistency check, not a rule.

    The abstain rule (M10, ``app.core.rules.abstain``) turns an alert into ``abstained`` when its pool has >= 30
    independent outcomes and the interval's lower bound is below the funding band's adverse edge in R. That edge is never
    negative, so an alert with such a pool and ``expected_r_ci_low < 0`` (or no interval) should not exist under the
    current rule hash; a non-zero count here is a bug. The funding edge itself is not stored, so it is not re-tested.
    """

    alerts: int
    alerts_with_estimate: int
    would_abstain: int
    alerts_independent: int
    alerts_with_estimate_independent: int
    would_abstain_independent: int
    cost_basis: str


COST_BASIS = 'interval lower bound below 0 R (stored R is net of fees and funding; the funding-band edge is not stored)'


def would_abstain(alerts: Sequence[Observation]) -> WouldAbstain:
    def counted(rows: Sequence[Observation]) -> tuple[int, int, int]:
        filled = [o for o in rows if not o.is_no_entry]
        with_estimate = [o for o in filled if o.pool_n is not None and o.pool_n >= C.ABSTAIN_MIN_POOL_INDEPENDENT]
        abstain = [o for o in with_estimate if o.expected_r_ci_low is None or o.expected_r_ci_low < 0.0]
        return len(filled), len(with_estimate), len(abstain)

    raw = counted(alerts)
    independent = counted(collapse_independent(alerts))
    return WouldAbstain(raw[0], raw[1], raw[2], independent[0], independent[1], independent[2], COST_BASIS)


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
    reliability: ReliabilityDataset  # alerts, stored probability
    reliability_all_statuses: ReliabilityDataset  # every status, stored probability
    comparisons: tuple[Comparison, ...]
    kill_rule: KillRuleResult | None  # binding, live full-lookback only
    kill_rule_preview: KillRuleResult | None  # advisory, replay full-lookback only
    would_abstain: WouldAbstain
    flags: tuple[str, ...]


@dataclass(frozen=True)
class BaselineCohortReport:
    """One naive-baseline cohort, whether or not an app cohort of the same coin set exists."""

    key: BaselineKey
    stats: BaselineStats
    overall: CellSummary
    by_horizon: dict[str, CellSummary]


@dataclass(frozen=True)
class StatsReport:
    rule_hash: str | None
    context: dict[str, Any]
    cohorts: tuple[CohortReport, ...]
    parameters: dict[str, Any]
    baseline_cohorts: tuple[BaselineCohortReport, ...] = ()
    baseline_available: bool = False


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


def _flags(cohort: CohortKey, populations: dict[str, PopulationReport]) -> tuple[str, ...]:
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
    if cohort.source == E.SOURCE_REPLAY:
        flags.append('replay: the kill rule does not bind here; a preview is shown for full-lookback cohorts only')
    return tuple(flags)


def baseline_key_for(cohort: CohortKey) -> BaselineKey:
    return BaselineKey(cohort.source, coin_set_for_path(cohort.options_path), cohort.lookback)


def _naive_baseline(cohort: CohortKey, baseline: BaselineData | None, horizon: str | None) -> NaiveBaseline:
    key = baseline_key_for(cohort)
    if baseline is None:
        return NaiveBaseline(key.label, None, None)
    return NaiveBaseline(key.label, summarize(baseline.for_key(key, horizon), key), baseline.stats.get(key, BaselineStats()))


def build_cohort_report(rows: Sequence[Observation], cohort: CohortKey, baseline: BaselineData | None = None) -> CohortReport:
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
        horizon = None if scope == 'all' else scope
        naive = _naive_baseline(cohort, baseline, horizon)
        subset = swing_break_baseline(rows, cohort, horizon)
        comparisons.append(
            Comparison(
                scope=scope,
                app=app,
                flat_expectancy_r=0.0,
                vs_flat=verdict_against(app, 0.0),
                baseline=naive,
                vs_swing_break='baseline_unavailable' if naive.expectancy_r is None else verdict_against(app, naive.expectancy_r),
                baseline_candidates_subset=subset,
                vs_candidates_subset=(
                    'baseline_unavailable'
                    if subset.summary.expectancy_r is None
                    else verdict_against(app, subset.summary.expectancy_r)
                ),
            )
        )
    kill, preview = kill_rule_for(cohort, populations[POPULATION_ALERT].overall, comparisons[0].baseline)
    return CohortReport(
        cohort=cohort,
        populations=populations,
        shadow_by_reason=shadow_by_reason,
        no_entry=_no_entry_rows(rows, cohort),
        reliability=reliability_dataset(alerts, cohort),
        reliability_all_statuses=reliability_dataset(rows, cohort),
        comparisons=tuple(comparisons),
        kill_rule=kill,
        kill_rule_preview=preview,
        would_abstain=would_abstain(alerts),
        flags=_flags(cohort, populations),
    )


def report_parameters() -> dict[str, Any]:
    return {
        'min_independent_to_conclude': P.MIN_INDEPENDENT_TO_CONCLUDE,
        'min_bin_outcomes': P.MIN_BIN_OUTCOMES,
        'kill_rule_min_independent': P.KILL_RULE_MIN_INDEPENDENT,
        'kill_rule_binds_on': 'live only (replay: advisory preview, never triggered)',
        'interval_confidence': P.INTERVAL_CONFIDENCE,
        'interval_method': f'percentile bootstrap, {P.BOOTSTRAP_RESAMPLES} resamples, seed {P.BOOTSTRAP_SEED}',
        'independent_sample': 'first row of each sample_group_id by (decision_time, candidate_id)',
        'ambiguous_share_flag': P.AMBIGUOUS_SHARE_FLAG,
        'thin_lookback_cutoff': P.THIN_LOOKBACK_CUTOFF.isoformat(),
        'vol_bucket_edges': list(P.VOL_BUCKET_EDGES),
        'reliability_bin_edges': list(P.RELIABILITY_BIN_EDGES),
        'pool_window_days': C.POOL_WINDOW_DAYS,
        'pool_floor': C.POOL_MIN_INDEPENDENT_OUTCOMES,
        'round_trip_cost_bps': str(C.ROUND_TRIP_COST_BPS),
    }


def _baseline_cohorts(baseline: BaselineData | None) -> tuple[BaselineCohortReport, ...]:
    if baseline is None:
        return ()
    keys = sorted(set(baseline.stats) | {o.cohort for o in baseline.observations})
    out: list[BaselineCohortReport] = []
    for key in keys:
        rows = baseline.for_key(key)
        horizons = sorted({o.horizon for o in rows}, key=lambda h: C.HORIZONS.index(h))
        out.append(
            BaselineCohortReport(
                key=key,
                stats=baseline.stats.get(key, BaselineStats()),
                overall=summarize(rows, key),
                by_horizon={h: summarize(baseline.for_key(key, h), key) for h in horizons},
            )
        )
    return tuple(out)


def build_report(
    observations: Sequence[Observation],
    *,
    rule_hash: str | None = None,
    context: dict[str, Any] | None = None,
    baseline: BaselineData | None = None,
) -> StatsReport:
    cohorts = tuple(build_cohort_report(rows, key, baseline) for key, rows in split_by_cohort(list(observations)).items())
    return StatsReport(
        rule_hash=rule_hash,
        context=dict(context or {}),
        cohorts=cohorts,
        parameters=report_parameters(),
        baseline_cohorts=_baseline_cohorts(baseline),
        baseline_available=baseline is not None,
    )