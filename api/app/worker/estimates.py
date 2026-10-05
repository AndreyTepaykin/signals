"""Write the pooled estimate onto a candidate (M8.1): in the options job, and by one-off enrichment.

Both paths call the same pure estimator (:func:`app.core.stats.pool.estimate_pool`) on the same point-in-time history,
and both write through :func:`write_estimate`, a guarded ``UPDATE ... WHERE <estimate columns> IS NULL`` that fills
NULL -> value once and never overwrites a stored estimate. The estimate columns are not covered by the write-once fill
trigger (``candidate_freeze_fill_values`` guards the entry price, 1R, barriers and deadline only).

Since M10 the estimate is a decision input: the options job computes it with the verdict and the abstain rule may turn an
``alert`` into ``abstained`` (:mod:`app.worker.options_job`). Enrichment never changes a status; it only fills estimates
on rows checked before a pool existed for them.

The options path of a candidate decides its pool, so it must be known: an alt has no Deribit book and is on
``no_options_book`` from the start; BTC / ETH are known only after the options check has written the verdict. A BTC / ETH
candidate whose check has not run is left alone (``no_options_path``) until the options job reaches it.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.hashing import rule_hash as current_rule_hash
from app.core.stats import pool as PL
from app.core.stats.observations import PATH_NO_BOOK, Observation, classify_options_path
from app.core.time import ensure_utc
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.worker.stats_job import fetch_observations

log = logging.getLogger(__name__)

ESTIMATE_COLUMNS = (
    'p_upper_before_stop',
    'expected_r',
    'expected_mae_r',
    'median_hours_to_resolution',
    'pool_level',
    'pool_n',
    'expected_r_ci_low',
    'expected_r_ci_high',
    'pool_interval_confidence',
)

FILLED = 'filled'
BELOW_FLOOR = 'below_floor'
NO_OPTIONS_PATH = 'no_options_path'
ALREADY_FILLED = 'already_filled'
RACE = 'not_written'  # another writer filled it first, or the row is gone

_Q6 = Decimal('0.000001')
_Q4 = Decimal('0.0001')


def knowable_lag() -> timedelta:
    """The kline settle lag the ``as_of`` gate applies to a closing bar (``Settings.feed_kline_settle_seconds``)."""
    from app.config import get_settings

    return timedelta(seconds=get_settings().feed_kline_settle_seconds)


def options_path_of(
    *, source: str, coin: str, options_verdict: str | None, status_reason: str | None, options_snapshot_time: datetime | None
) -> str | None:
    """The candidate's options path, or ``None`` while it cannot be known (BTC / ETH before the options check)."""
    if coin not in C.OPTIONS_CURRENCY_BY_COIN:
        return PATH_NO_BOOK
    if options_verdict is None:
        return None
    return classify_options_path(
        source=source,
        coin=coin,
        options_verdict=options_verdict,
        status_reason=status_reason,
        options_snapshot_time=options_snapshot_time,
    )


def _decimal(value: float, quantum: Decimal) -> Decimal:
    return Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_EVEN)


def estimate_values(estimate: PL.PoolEstimate) -> dict[str, Any]:
    """The estimate as column values (quantized to the column scale; the interval stays ordered)."""
    low = None if estimate.expected_r_ci_low is None else _decimal(estimate.expected_r_ci_low, _Q6)
    high = None if estimate.expected_r_ci_high is None else _decimal(estimate.expected_r_ci_high, _Q6)
    return {
        'p_upper_before_stop': _decimal(estimate.p_upper_before_stop, _Q6),
        'expected_r': _decimal(estimate.expected_r, _Q6),
        'expected_mae_r': None if estimate.expected_mae_r is None else _decimal(estimate.expected_mae_r, _Q6),
        'median_hours_to_resolution': (
            None if estimate.median_hours_to_resolution is None else _decimal(estimate.median_hours_to_resolution, _Q4)
        ),
        'pool_level': estimate.level,
        'pool_n': estimate.n_independent,
        'expected_r_ci_low': low,
        'expected_r_ci_high': high,
        'pool_interval_confidence': _decimal(estimate.interval_confidence, _Q6),
    }


def write_estimate(session: Session, candidate_id: int, values: dict[str, Any]) -> bool:
    """Fill the estimate columns of one candidate if (and only if) none is stored yet. True when a row changed."""
    still_empty = [getattr(Candidate, name).is_(None) for name in ESTIMATE_COLUMNS]
    updated = session.execute(
        update(Candidate).where(Candidate.id == candidate_id, *still_empty).values(**values).returning(Candidate.id)
    ).first()
    return updated is not None


def target_for(
    *,
    decision_time: datetime,
    horizon: str,
    source: str,
    options_path: str,
    setup_family: str,
    trend_family: str | None,
    feature_row: FeatureRow | None,
) -> PL.PoolTarget:
    """A pool target; the regime slice comes from the feature row exactly as the label copies it."""
    vol = None if feature_row is None or feature_row.realized_vol_pct is None else float(feature_row.realized_vol_pct)
    return PL.PoolTarget(
        decision_time=ensure_utc(decision_time),
        horizon=horizon,
        source=source,
        options_path=options_path,
        setup_family=setup_family,
        trend_family=trend_family,
        vol_pct=vol,
        session=None if feature_row is None else feature_row.session,
        event_flag=None if feature_row is None else feature_row.event_flag,
    )


def fill_candidate(
    session: Session,
    candidate_id: int,
    target: PL.PoolTarget,
    *,
    history: Sequence[Observation] | None = None,
    rule_hash: str | None = None,
    lag: timedelta | None = None,
    dry_run: bool = False,
) -> tuple[str, PL.PoolEstimate | PL.NoEstimate]:
    """Estimate for one candidate and write it (guarded). Returns ``(result, estimate)``."""
    wanted = rule_hash or current_rule_hash()
    if history is None:
        history = fetch_observations(
            session, rule_hash=wanted, source=target.source, horizon=target.horizon, decision_before=target.decision_time
        )
    estimate = PL.estimate_pool(history, target, knowable_lag=lag if lag is not None else knowable_lag())
    if isinstance(estimate, PL.NoEstimate):
        return BELOW_FLOOR, estimate
    if dry_run:
        return FILLED, estimate
    return (FILLED if write_estimate(session, candidate_id, estimate_values(estimate)) else RACE), estimate


# ---------------------------------------------------------------------------
# One-off enrichment
# ---------------------------------------------------------------------------
@dataclass
class EnrichReport:
    candidates_considered: int = 0
    results: Counter = field(default_factory=Counter)
    filled_by_column: Counter = field(default_factory=Counter)
    filled_by_level: Counter = field(default_factory=Counter)
    below_floor_best_n: Counter = field(default_factory=Counter)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            'candidates_considered': self.candidates_considered,
            'results': dict(self.results),
            'filled_by_column': dict(self.filled_by_column),
            'filled_by_level': dict(self.filled_by_level),
            'below_floor_best_n_histogram': dict(sorted(self.below_floor_best_n.items())),
            'dry_run': self.dry_run,
        }


def enrich(session: Session, *, rule_hash: str | None = None, source: str | None = None, dry_run: bool = False) -> EnrichReport:
    """Fill the estimate columns of every current-hash candidate that has none, in decision-time order. Idempotent.

    The history is loaded once (every labeled current-hash outcome); ``estimate_pool`` then applies the point-in-time rules
    per target, so the result for a candidate does not depend on the order or on which other candidates were enriched.
    Does not commit.
    """
    wanted = rule_hash or current_rule_hash()
    lag = knowable_lag()
    history = fetch_observations(session, rule_hash=wanted, source=source)
    stmt = (
        select(Candidate, FeatureRow)
        .outerjoin(FeatureRow, FeatureRow.id == Candidate.feature_row_id)
        .where(Candidate.rule_hash == wanted)
        .order_by(Candidate.decision_time, Candidate.id)
    )
    if source is not None:
        stmt = stmt.where(Candidate.source == source)
    report = EnrichReport(dry_run=dry_run)
    for candidate, feature_row in session.execute(stmt).all():
        report.candidates_considered += 1
        if any(getattr(candidate, name) is not None for name in ESTIMATE_COLUMNS):
            report.results[ALREADY_FILLED] += 1
            continue
        path = options_path_of(
            source=candidate.source,
            coin=candidate.coin,
            options_verdict=candidate.options_verdict,
            status_reason=candidate.status_reason,
            options_snapshot_time=candidate.options_snapshot_time,
        )
        if path is None:
            report.results[NO_OPTIONS_PATH] += 1
            continue
        target = target_for(
            decision_time=candidate.decision_time,
            horizon=candidate.horizon,
            source=candidate.source,
            options_path=path,
            setup_family=candidate.setup_family,
            trend_family=candidate.trend_family,
            feature_row=feature_row,
        )
        result, estimate = fill_candidate(
            session, candidate.id, target, history=history, rule_hash=wanted, lag=lag, dry_run=dry_run
        )
        report.results[result] += 1
        if result == BELOW_FLOOR:
            report.below_floor_best_n[estimate.best_n] += 1  # type: ignore[union-attr]
        elif result == FILLED:
            values = estimate_values(estimate)  # type: ignore[arg-type]
            report.filled_by_level[values['pool_level']] += 1
            for name, value in values.items():
                if value is not None:
                    report.filled_by_column[name] += 1
    return report
