"""Text and JSON output of a `StatsReport`. Presentation only: no number is computed here."""

from __future__ import annotations

import dataclasses
from typing import Any

from app.core import constants as C
from app.core.stats.reliability import ReliabilityDataset
from app.core.stats.report import CohortReport, PopulationReport, StatsReport
from app.core.stats.summary import CellSummary


def to_jsonable(report: StatsReport) -> dict[str, Any]:
    return dataclasses.asdict(report)


def _num(value: float | None, spec: str = '+.3f') -> str:
    return 'n/a' if value is None else format(value, spec)


def _pct(value: float | None) -> str:
    return 'n/a' if value is None else f'{value:.1%}'


def cell_line(s: CellSummary, label: str = '', *, short_flag: bool = False) -> str:
    """One table row: raw next to independent, expectancy with interval, companions, and the plain-words verdict.

    `short_flag` shortens the too-small text for the walk-up tables, where nearly every cell carries it."""
    interval = 'n/a' if s.interval_low is None else f'[{s.interval_low:+.3f}, {s.interval_high:+.3f}]'
    if not s.insufficient_sample:
        flag = ''
    elif short_flag:
        flag = f'  ** TOO SMALL (n={s.n_filled_independent} independent)'
    else:
        flag = '  ** ' + s.sample_note
    pf = _num(s.profit_factor, '.2f') if s.profit_factor is not None else f'n/a ({s.profit_factor_note})'
    return (
        f'{label}labels raw/ind {s.n_labels_raw}/{s.n_labels_independent}'
        f' | filled raw/ind {s.n_filled_raw}/{s.n_filled_independent}'
        f' | no_entry raw/ind {s.n_no_entry_raw}/{s.n_no_entry_independent}'
        f' | E[R] {_num(s.expectancy_r)} {int(s.interval_confidence * 100)}% {interval}'
        f' | PF {pf} | DD {s.max_drawdown_r:.2f}R | win {_pct(s.win_rate)} | Brier {_num(s.brier, ".4f")}'
        f'{flag}'
    )


def _population_block(p: PopulationReport) -> list[str]:
    out = [f'  [{p.population.upper()}] overall', '    ' + cell_line(p.overall)]
    o = p.overall
    if not o.n_labels_raw:
        return out
    out.append(
        f'    ambiguous {o.n_ambiguous_raw}/{o.n_filled_raw} filled raw ({_pct(o.ambiguous_share_raw)})'
        f' | funding_partial {o.n_funding_partial_raw}/{o.n_filled_raw} ({_pct(o.funding_partial_share_raw)})'
        f' | E[R] ex funding_partial {_num(o.expectancy_r_ex_funding_partial)} over {o.n_ex_funding_partial}'
        f' | mean funding_r (funding-complete) {_num(o.funding_r_mean_complete, "+.4f")}'
    )
    if p.by_horizon:
        out.append('    per horizon:')
        for horizon, s in p.by_horizon.items():
            out.append('      ' + cell_line(s, f'{horizon:<8} '))
    if p.walk_up and p.overall.n_labels_raw:
        out.append(f'    pool walk-up (skipped identical levels: {", ".join(p.walk_up_skipped_levels) or "none"}):')
        for level in p.walk_up:
            shown = [c for c in level.cells if not c.same_as_previous_level]
            out.append(f'      {level.level} [{", ".join(level.dimensions)}]: {len(level.cells)} cells, {len(level.cells) - len(shown)} identical to the previous level')
            for cell in shown:
                key = '/'.join(cell.key.values())
                out.append('        ' + cell_line(cell.summary, f'{key}: ', short_flag=True))
    return out


def format_cohort(c: CohortReport) -> list[str]:
    out = [f'=== COHORT {c.cohort.label} ===']
    for flag in c.flags:
        out.append(f'  ! {flag}')
    for name in ('alert', 'shadow'):
        out.extend(_population_block(c.populations[name]))
    if c.shadow_by_reason:
        out.append('  [SHADOW] by status / reason')
        for row in c.shadow_by_reason:
            out.append('    ' + cell_line(row.summary, f'{row.status}/{row.status_reason or "-"}: '))
    out.append('  no_entry (raw labels / no_entry, independent labels / no_entry):')
    for r in c.no_entry:
        out.append(
            f'    {r.status:<10} {r.horizon:<9} raw {r.n_no_entry_raw}/{r.n_labels_raw} ({_pct(r.rate_raw)})'
            f' | ind {r.n_no_entry_independent}/{r.n_labels_independent} ({_pct(r.rate_independent)})'
        )
    out.extend(_reliability_block('alerts', c.reliability))
    out.extend(_reliability_block('all statuses', c.reliability_all_statuses))
    out.append('  vs baselines (alerts vs always-flat 0R and vs the NAIVE swing-break rule, same source / coin set / lookback):')
    for cmp in c.comparisons:
        b = cmp.baseline
        s = b.summary
        interval = 'n/a' if s is None or s.interval_low is None else f'[{s.interval_low:+.3f}, {s.interval_high:+.3f}]'
        counts = '' if s is None else f' raw/ind {s.n_filled_raw}/{s.n_filled_independent}'
        out.append(
            f'    {cmp.scope:<9} app E[R] {_num(cmp.app.expectancy_r)} ind {cmp.app.n_filled_independent}'
            f' | vs flat: {cmp.vs_flat}'
            f' | naive[{b.key}] E[R] {_num(b.expectancy_r)} {interval}{counts}'
            f' | vs naive: {cmp.vs_swing_break}'
            + (f'  ** baseline {s.sample_note}' if s is not None and s.insufficient_sample else '')
            + (f'  ** app {cmp.app.sample_note}' if cmp.app.insufficient_sample else '')
        )
        r = cmp.baseline_candidates_subset
        out.append(
            f'        baseline_candidates_subset E[R] {_num(r.summary.expectancy_r)} ind {r.summary.n_filled_independent}'
            f' (rows {r.n_swing_break_rows}, excluded options-modified {r.n_excluded_options_modified}) | {cmp.vs_candidates_subset}'
        )
    for label, k in (('kill rule (binding, live)', c.kill_rule), ('kill_rule_preview (advisory, replay)', c.kill_rule_preview)):
        if k is not None:
            out.append(f'  {label}: {k.status}: {k.note}')
    if c.kill_rule is None and c.kill_rule_preview is None:
        out.append('  kill rule: not evaluated (thin lookback cohorts get neither the rule nor a preview)')
    w = c.would_abstain
    out.append(
        f'  would_abstain (alerts; no rule applied): {w.would_abstain} of {w.alerts_with_estimate} with a pool >= {C.POOL_MIN_INDEPENDENT_OUTCOMES}'
        f' ({w.alerts} filled alerts) | independent {w.would_abstain_independent} of {w.alerts_with_estimate_independent} ({w.alerts_independent})'
        f' | cost: {w.cost_basis}'
    )
    return out


def _reliability_block(label: str, rel: ReliabilityDataset) -> list[str]:
    out = [f'  reliability ({label}): {rel.note}']
    for b in rel.bins:
        if b.n_raw or b.n_independent:
            shown = 'hidden' if b.hidden else f'p {b.mean_probability:.3f} obs {b.observed_rate:.3f} R {b.mean_stored_r:+.3f}'
            out.append(f'    [{b.low:.1f}, {b.high:.1f}) raw {b.n_raw} ind {b.n_independent}: {shown}')
    if rel.available:
        monotone = 'cannot be checked (fewer than two visible bins)' if rel.r_non_decreasing is None else (
            'yes' if rel.r_non_decreasing else 'NO'
        )
        gap = 'n/a' if rel.max_abs_gap is None else f'{rel.max_abs_gap:.3f}'
        out.append(f'    visible bins {rel.visible_bins} | max |p - observed| {gap} | higher bins show higher average R: {monotone}')
    return out


def _baseline_block(report: StatsReport) -> list[str]:
    out = ['NAIVE SWING-BREAK BASELINE (the published rule traded on every 4h break; no votes / gates / options; cohorts never pooled)']
    if not report.baseline_available:
        return out + ['  not computed']
    for b in report.baseline_cohorts:
        st = b.stats
        out.append(
            f'  {b.key.label:<28} events {st.events} | no_stop {st.no_stop} | skipped while hold open {st.skipped_hold_open}'
            f' | taken {st.taken} (pending {st.pending}, no_entry {st.no_entry}, filled {st.filled})'
        )
        out.append('    ' + cell_line(b.overall, 'all: ', short_flag=True))
        for horizon, s in b.by_horizon.items():
            out.append('      ' + cell_line(s, f'{horizon:<8} ', short_flag=True))
    return out

def _kill_summary(c: CohortReport) -> str:
    if c.kill_rule is not None:
        return c.kill_rule.status
    if c.kill_rule_preview is not None:
        return c.kill_rule_preview.status
    return 'none (thin)'


def format_text(report: StatsReport) -> str:
    lines = ['M8 STATS REPORT', f'rule_hash: {report.rule_hash}']
    for key, value in report.context.items():
        lines.append(f'{key}: {value}')
    lines.append('parameters: ' + '; '.join(f'{k}={v}' for k, v in report.parameters.items()))
    lines.append('R figures are stored R (price - fees + funding) of independent samples; `no_entry` labels are excluded from every R statistic.')
    lines.append('')
    lines.append('COHORT OVERVIEW (labels raw/independent; cohorts are never pooled)')
    for c in report.cohorts:
        a, s = c.populations['alert'].overall, c.populations['shadow'].overall
        lines.append(
            f'  {c.cohort.label:<46} alerts {a.n_labels_raw}/{a.n_labels_independent}'
            f' (filled ind {a.n_filled_independent}) | shadow {s.n_labels_raw}/{s.n_labels_independent}'
            f' (filled ind {s.n_filled_independent}) | kill rule: {_kill_summary(c)}'
        )
    lines.append('')
    lines.extend(_baseline_block(report))
    lines.append('')
    for cohort in report.cohorts:
        lines.extend(format_cohort(cohort))
        lines.append('')
    return '\n'.join(lines)
