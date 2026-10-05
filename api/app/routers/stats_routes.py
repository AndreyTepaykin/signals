"""Stats report endpoints (cached)."""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.stats import params as P
from app.core.stats.reliability import ReliabilityBin
from app.core.stats.report import CohortReport, StatsReport
from app.core.stats.summary import CellSummary
from app.routers.deps import ReadonlySession, SourceQuery
from app.routers.stats_cache import get_cached_stats, stats_envelope

router = APIRouter(prefix='/stats', tags=['stats'])


def _cell_dict(s: CellSummary) -> dict[str, Any]:
    return dataclasses.asdict(s)


def _cohort_expectancy(c: CohortReport) -> dict[str, Any]:
    return {
        'cohort': c.cohort.label,
        'flags': list(c.flags),
        'alert': {
            'overall': _cell_dict(c.populations['alert'].overall),
            'by_horizon': {h: _cell_dict(v) for h, v in c.populations['alert'].by_horizon.items()},
        },
        'shadow': {
            'overall': _cell_dict(c.populations['shadow'].overall),
            'by_horizon': {h: _cell_dict(v) for h, v in c.populations['shadow'].by_horizon.items()},
        },
    }


def _reliability_payload(c: CohortReport) -> dict[str, Any]:
    def pack(ds: Any) -> dict[str, Any]:
        bins = []
        for b in ds.bins:
            if b.hidden:
                continue
            bins.append(dataclasses.asdict(b))
        out = dataclasses.asdict(ds)
        out['bins'] = bins
        return out

    brier_alert = c.populations['alert'].overall.brier
    return {
        'cohort': c.cohort.label,
        'reliability': pack(c.reliability),
        'reliability_all_statuses': pack(c.reliability_all_statuses),
        'brier': brier_alert,
        'brier_note': c.populations['alert'].overall.brier_note,
    }


def _baseline_payload(report: StatsReport) -> dict[str, Any]:
    cohorts = []
    for c in report.cohorts:
        kill = c.kill_rule
        preview = c.kill_rule_preview
        cohorts.append(
            {
                'cohort': c.cohort.label,
                'comparisons': [dataclasses.asdict(x) for x in c.comparisons],
                'kill_rule': dataclasses.asdict(kill) if kill else None,
                'kill_rule_preview': dataclasses.asdict(preview) if preview else None,
                'would_abstain': dataclasses.asdict(c.would_abstain),
                'flags': list(c.flags),
            }
        )
    baseline_cohorts = [dataclasses.asdict(b) for b in report.baseline_cohorts]
    return {'cohorts': cohorts, 'baseline_cohorts': baseline_cohorts, 'baseline_available': report.baseline_available}


@router.get('')
def stats_full(session: ReadonlySession, source: SourceQuery) -> dict[str, Any]:
    cached = get_cached_stats(session, source)
    out = dict(cached.jsonable)
    out['built_at'] = cached.built_at.isoformat()
    return out


@router.get('/expectancy')
def stats_expectancy(session: ReadonlySession, source: SourceQuery) -> dict[str, Any]:
    cached = get_cached_stats(session, source)
    return {
        **stats_envelope(cached),
        'cohorts': [_cohort_expectancy(c) for c in cached.report.cohorts],
    }


@router.get('/calibration')
def stats_calibration(session: ReadonlySession, source: SourceQuery) -> dict[str, Any]:
    cached = get_cached_stats(session, source)
    return {
        **stats_envelope(cached),
        'min_bin_outcomes': P.MIN_BIN_OUTCOMES,
        'cohorts': [_reliability_payload(c) for c in cached.report.cohorts],
    }


@router.get('/baselines')
def stats_baselines(session: ReadonlySession, source: SourceQuery) -> dict[str, Any]:
    cached = get_cached_stats(session, source)
    payload = _baseline_payload(cached.report)
    return {**stats_envelope(cached), **payload}
