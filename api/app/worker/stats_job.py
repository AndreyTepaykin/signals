"""The stats job: read labels and candidates (read-only) and print the M8 report.

``python -m app.worker.stats_job [--source live|replay] [--json] [--out PATH]``
``python -m app.worker.stats_job --enrich-estimates [--source ...] [--dry-run]``

Reads `candidate` joined to `label`, keeps only rows whose candidate AND label carry the CURRENT `rule_hash` (the same gate as
the labeler), turns each into an `Observation` and hands the list to the pure engine in `app.core.stats`. The report writes
nothing: the CLI opens its session read-only at the database level (`SET TRANSACTION READ ONLY`). The naive swing-break
baseline is computed here from the stored feature rows, candles and funding (read-only, deterministic, no table).

`--enrich-estimates` is the single write path (M8.1): it fills the NULL pooled-estimate columns of candidates, point in time,
once, through `app.worker.estimates`, and never overwrites a stored estimate. The label-only context columns
(`cmi_*`, `lead_venue*`) are still not needed by these statistics and stay NULL.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.core.hashing import rule_hash as current_rule_hash
from app.core.features import params as FP
from app.core.stats.baseline import BaselineData, NaiveEvent, event_from_feature_row, run_naive_baseline
from app.core.stats.observations import Observation
from app.core.time import ensure_utc, utc_now
from app.core.stats.render import format_text, to_jsonable
from app.core.stats.report import StatsReport, build_report
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.models.labels import Label
from app.worker.feature_job import REFERENCE_COIN
from app.worker.label_job import MISSING_BAR_AFTER, default_funding_max_gap, load_bars, load_funding


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


@dataclass(frozen=True)
class LoadResult:
    observations: list[Observation]
    context: dict[str, Any]


def fetch_observations(
    session: Session,
    *,
    rule_hash: str,
    source: str | None = None,
    horizon: str | None = None,
    decision_before: datetime | None = None,
) -> list[Observation]:
    """Every labeled candidate on `rule_hash` (candidate AND label), oldest decision first. Optional narrowing for the estimator."""
    wanted = rule_hash
    stmt = (
        select(Candidate, Label)
        .join(Label, Label.candidate_id == Candidate.id)
        .where(Candidate.rule_hash == wanted, Label.rule_hash == wanted)
        .order_by(Candidate.decision_time, Candidate.id)
    )
    if source is not None:
        stmt = stmt.where(Candidate.source == source)
    if horizon is not None:
        stmt = stmt.where(Candidate.horizon == horizon)
    if decision_before is not None:
        stmt = stmt.where(Candidate.decision_time < decision_before)
    observations: list[Observation] = []
    for c, l in session.execute(stmt).all():
        observations.append(
            Observation(
                candidate_id=c.id,
                sample_group_id=str(c.sample_group_id),
                decision_time=c.decision_time,
                bar_close=c.bar_close,
                resolved_at=l.resolved_at,
                coin=c.coin,
                horizon=c.horizon,
                source=c.source,
                status=c.status,
                status_reason=c.status_reason,
                trigger=c.trigger,
                setup_family=c.setup_family,
                trend_family=l.regime_trend_family,
                vol_pct=_f(l.regime_vol_pct),
                session=l.regime_session,
                event_flag=l.regime_event_flag,
                first_barrier=l.first_barrier,
                stored_r=_f(l.stored_r),
                funding_r=_f(l.funding_r),
                barrier_ambiguous=l.barrier_ambiguous,
                funding_partial=l.funding_partial,
                gapped_stop=l.gapped_stop,
                p_upper=_f(c.p_upper_before_stop),
                options_verdict=c.options_verdict,
                options_snapshot_time=c.options_snapshot_time,
                barriers_unmodified=(c.stop_price == c.pre_options_stop_price and c.target_cap_price is None),
                mae_r=_f(l.mae_r),
                hours_to_resolution=_f(l.hours_to_resolution),
                fees_r=_f(l.fees_r),
                pool_n=c.pool_n,
                expected_r_ci_low=_f(c.expected_r_ci_low),
            )
        )
    return observations


def load_observations(session: Session, *, rule_hash: str | None = None, source: str | None = None) -> LoadResult:
    """Every labeled candidate on `rule_hash`, plus the counts the report prints about what it left out."""
    wanted = rule_hash or current_rule_hash()
    observations = fetch_observations(session, rule_hash=wanted, source=source)

    def count(*conditions: Any) -> int:
        q = select(func.count()).select_from(Candidate)
        if source is not None:
            q = q.where(Candidate.source == source)
        return int(session.execute(q.where(*conditions)).scalar_one())

    labeled_ids = select(Label.candidate_id)
    context = {
        'candidates_total': count(),
        'candidates_on_current_rule_hash': count(Candidate.rule_hash == wanted),
        'candidates_other_rule_hash': count(Candidate.rule_hash != wanted),
        'candidates_current_hash_unlabeled': count(Candidate.rule_hash == wanted, Candidate.id.not_in(labeled_ids)),
        'observations_used': len(observations),
        'sources': sorted({o.source for o in observations}),
        'decision_time_range': (
            [observations[0].decision_time.isoformat(), observations[-1].decision_time.isoformat()] if observations else None
        ),
    }
    return LoadResult(observations, context)


class DbMarketReader:
    """Bars and settled funding for the naive baseline, read through the same `as_of` gate the labeler uses."""

    def __init__(self, session: Session, as_of_time: datetime) -> None:
        self.session = session
        self.as_of_time = as_of_time

    def bars(self, coin: str, start: datetime, end: datetime) -> list[Any]:
        return load_bars(self.session, self.as_of_time, coin, start, end)

    def prints(self, coin: str, start: datetime, end: datetime) -> list[Any]:
        return load_funding(self.session, self.as_of_time, coin, start, end)


def load_naive_events(session: Session, *, source: str | None = None) -> list[NaiveEvent]:
    """Every 4h swing-break event in the stored feature rows (both sources, every horizon)."""
    stmt = (
        select(FeatureRow)
        .where(FeatureRow.swing_break_event.is_(True), FeatureRow.signal_timeframe == FP.SIGNAL_TIMEFRAME)
        .order_by(FeatureRow.bar_close, FeatureRow.id)
    )
    if source is not None:
        stmt = stmt.where(FeatureRow.source == source)
    events = (event_from_feature_row(row, reference_coin=REFERENCE_COIN) for row in session.execute(stmt).scalars())
    return [event for event in events if event is not None]


def compute_baseline(session: Session, *, as_of_time: datetime | None = None, source: str | None = None) -> BaselineData:
    """The naive swing-break baseline (read-only, deterministic for the stored data as of ``as_of_time``)."""
    when = ensure_utc(as_of_time) if as_of_time is not None else utc_now()
    return run_naive_baseline(
        load_naive_events(session, source=source),
        DbMarketReader(session, when),
        reference_coin=REFERENCE_COIN,
        as_of_time=when,
        funding_max_gap=default_funding_max_gap(),
        missing_bar_after=MISSING_BAR_AFTER,
    )


def run_stats(
    session: Session,
    *,
    rule_hash: str | None = None,
    source: str | None = None,
    as_of_time: datetime | None = None,
    with_baseline: bool = True,
) -> StatsReport:
    wanted = rule_hash or current_rule_hash()
    loaded = load_observations(session, rule_hash=wanted, source=source)
    baseline = compute_baseline(session, as_of_time=as_of_time, source=source) if with_baseline else None
    return build_report(loaded.observations, rule_hash=wanted, context=loaded.context, baseline=baseline)


@contextmanager
def readonly_session(engine: Engine, *, search_path: str | None = None) -> Iterator[Session]:
    """A session whose transaction is READ ONLY at the database level: any write raises. Always rolled back."""
    with engine.connect() as connection:
        connection = connection.execution_options(postgresql_readonly=True)
        if search_path is not None:  # tests point this at a throwaway schema
            connection.execute(text(f'SET search_path TO "{search_path}"'))
        with Session(bind=connection) as session:
            try:
                yield session
            finally:
                session.rollback()


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.db_sync import get_sync_engine

    parser = argparse.ArgumentParser(description='M8 stats report over labeled candidates (read-only).')
    parser.add_argument('--source', choices=E.SOURCES)
    parser.add_argument('--json', action='store_true', help='print JSON instead of text')
    parser.add_argument('--out', help='also write the output to this file (UTF-8)')
    parser.add_argument(
        '--enrich-estimates',
        action='store_true',
        help='the ONE write path of this job: fill NULL pooled-estimate columns on candidates, point in time, once (idempotent)',
    )
    parser.add_argument('--dry-run', action='store_true', help='with --enrich-estimates: compute and count, write nothing')
    args = parser.parse_args(argv)

    if args.enrich_estimates:
        from app.core.db_sync import get_sync_session_factory
        from app.worker import estimates

        with get_sync_session_factory()() as session:
            enriched = estimates.enrich(session, source=args.source, dry_run=args.dry_run)
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
        sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
        print(json.dumps(enriched.as_dict(), indent=2))
        return 0

    with readonly_session(get_sync_engine()) as session:
        report = run_stats(session, source=args.source)
    text = json.dumps(to_jsonable(report), indent=2, default=str) if args.json else format_text(report)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            handle.write(text)
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
