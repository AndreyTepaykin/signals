"""The options job: apply the options check to every candidate that has not been through it.

Everything that decides lives in ``app.core.options.check`` and is pure. This module does the I/O:

1. **Select** candidates with ``options_verdict IS NULL``, oldest decision first. That is every M4 row: the
   pending ones (``status_reason = 'pending_options_check'``) and the shadow ones (stand-down, CVD divergence,
   vote conflict), live and replay alike.
2. **Re-derive the price layer** from the candidate's own feature row with the same pure function M4 used
   (:func:`app.core.rules.stops.stop_plan`) and require it to equal the stored ``pre_options_*`` prices. The reference
   price (signal-bar close) and 1R are not stored on the candidate until M6, and this is the only honest way to get
   them without a second copy of the arithmetic. A mismatch is reported and the row is left untouched.
3. **Read the book** through ``as_of(candidate.decision_time)``: the newest ``options_metric`` row for (deribit, coin,
   horizon) visible at the decision, and the ``options_snapshot`` it came from. A snapshot that arrived after the
   decision does not exist for this candidate, in a live run or a replay.
4. **Decide** with :func:`~app.core.options.check.check_options` and write the result with one guarded UPDATE
   (``WHERE id = :id AND options_verdict IS NULL``): the options columns are written exactly once and a re-run changes
   nothing.

No wall clock
-------------
There is no ``now`` here. The decision instant is ``candidate.decision_time`` (the caller's ``now`` for a live
candidate, the bar close for a replay one), so running this job a minute or a month later gives the same decision
for the same stored rows. That is what lets the M7 replay reuse it.

Shadow rows (M4) and rows with no book
--------------------------------------
A shadow row is evaluated exactly like a pending one and gets every options column and both price layers (so it
resolves on the barriers it would have had), but its ``status`` and ``status_reason`` are never changed: a shadow row
is never promoted to an alert and never cancelled by the options check. A candidate whose coin Deribit does not list
gets ``options_verdict = 'ignore'`` with reason ``options_ignored:no_options_book`` in the log line; its status moves
pending -> alert (the spec's ``ignore`` "leaves size, stop, and targets unchanged").

Replay
------
There is no historical Deribit book (the endpoint is point-in-time only), so a replay over a period before the
first stored snapshot has no book for any covered coin. Those rows come out ``ignore`` / ``no_snapshot`` and, for BTC and
ETH, stay ``watch``. M7 must report that gap rather than read it as "no options effect".
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select as sa_select
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.options import metrics as M
from app.core.options.book import book_from_payload
from app.core.options.check import BookView, CheckInputs, OptionsDecision, check_options
from app.core.rules.inputs import RuleInputs
from app.core.rules.stops import stop_plan
from app.core.time import ensure_utc
from app.models import enums as E
from app.models.as_of import as_of
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.models.options import OptionsMetric, OptionsSnapshot
from app.worker.feature_job import REFERENCE_COIN

log = logging.getLogger(__name__)

RESULT_CHECKED = 'checked'
RESULT_PRICE_MISMATCH = 'price_mismatch'
RESULT_NO_FEATURE_ROW = 'no_feature_row'
RESULT_RACE = 'already_checked'
RESULT_DRY = 'dry_run'


@dataclass(frozen=True)
class OptionsOutcome:
    candidate_id: int
    coin: str
    horizon: str
    result: str
    verdict: str | None = None
    status: str | None = None
    status_reason: str | None = None
    detail: str = ''


@dataclass
class OptionsReport:
    outcomes: list[OptionsOutcome] = field(default_factory=list)

    def count(self, result: str) -> int:
        return sum(1 for o in self.outcomes if o.result == result)

    @property
    def checked(self) -> int:
        return self.count(RESULT_CHECKED)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def pending_candidates(session: Session, *, source: str | None = None, coins: Sequence[str] | None = None) -> list[Candidate]:
    """Candidates the options check has not touched, oldest decision first (ties by id)."""
    statement = sa_select(Candidate).where(Candidate.options_verdict.is_(None))
    if source is not None:
        statement = statement.where(Candidate.source == source)
    if coins:
        statement = statement.where(Candidate.coin.in_(list(coins)))
    return list(session.execute(statement.order_by(Candidate.decision_time, Candidate.id)).scalars())


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def load_book_view(session: Session, coin: str, horizon: str, decision_time: datetime) -> BookView | None:
    """The newest derived row for (deribit, coin, horizon) visible at ``decision_time``, with its near-dated strike profile."""
    if coin not in C.OPTIONS_CURRENCY_BY_COIN:
        return None
    reader = as_of(decision_time)
    metric = session.execute(
        reader.select(OptionsMetric)
        .where(OptionsMetric.venue == E.OPTIONS_VENUE_DERIBIT, OptionsMetric.coin == coin, OptionsMetric.horizon == horizon)
        .order_by(OptionsMetric.snapshot_time.desc(), OptionsMetric.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if metric is None:
        return None
    snapshot = session.execute(reader.select(OptionsSnapshot).where(OptionsSnapshot.id == metric.snapshot_id)).scalar_one_or_none()
    strikes: tuple[M.StrikeOI, ...] = ()
    if snapshot is not None:
        book = book_from_payload(snapshot.payload, ensure_utc(snapshot.snapshot_time))
        strikes = tuple(M.strike_profile(M.near_dated_quotes(book)))
    return BookView(
        venue=metric.venue,
        metric_id=metric.id,
        snapshot_time=ensure_utc(metric.snapshot_time),
        total_open_interest=_f(metric.total_open_interest),
        near_dated_open_interest=_f(metric.near_dated_open_interest),
        expiry_time=None if metric.expiry_time is None else ensure_utc(metric.expiry_time),
        dealer_gamma_sign=metric.dealer_gamma_sign,
        gamma_flip_price=_f(metric.gamma_flip_price),
        max_pain_strike=_f(metric.max_pain_strike),
        top_strike_oi_share=_f(metric.top_strike_oi_share),
        iv_minus_rv_pct=_f(metric.iv_minus_rv_pct),
        strikes=strikes,
    )


def load_check_inputs(session: Session, candidate: Candidate) -> tuple[CheckInputs | None, str]:
    """Build the pure check's input, or ``(None, why)`` if the candidate's price layer cannot be re-derived."""
    row = session.get(FeatureRow, candidate.feature_row_id)
    if row is None:
        return None, RESULT_NO_FEATURE_ROW
    rule_inputs = RuleInputs.from_feature_row(row, reference_coin=REFERENCE_COIN)
    plan = stop_plan(rule_inputs, candidate.direction)
    if (
        plan is None
        or rule_inputs.atr_price is None
        or Decimal(plan.stop_price) != Decimal(candidate.pre_options_stop_price)
        or (plan.target_price is None) != (candidate.pre_options_target_price is None)
        or (plan.target_price is not None and Decimal(plan.target_price) != Decimal(candidate.pre_options_target_price))
    ):
        return None, RESULT_PRICE_MISMATCH
    decision_time = ensure_utc(candidate.decision_time)
    return (
        CheckInputs(
            coin=candidate.coin,
            direction=candidate.direction,
            decision_time=decision_time,
            status=candidate.status,
            status_reason=candidate.status_reason,
            reference_price=plan.reference_price,
            one_r=plan.one_r,
            atr_price=Decimal(rule_inputs.atr_price),
            pre_options_stop_price=Decimal(candidate.pre_options_stop_price),
            pre_options_target_price=None if candidate.pre_options_target_price is None else Decimal(candidate.pre_options_target_price),
            with_break=candidate.direction == rule_inputs.swing_break_direction,
            book=load_book_view(session, candidate.coin, candidate.horizon, decision_time),
        ),
        RESULT_CHECKED,
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def decision_values(decision: OptionsDecision) -> dict[str, Any]:
    return {
        'stop_price': decision.stop_price,
        'target_cap_price': decision.target_cap_price,
        'status': decision.status,
        'status_reason': decision.status_reason,
        'options_verdict': decision.verdict,
        'options_venue': decision.venue,
        'options_vol_tag': decision.vol_tag,
        'options_dealer_gamma_sign': decision.dealer_gamma_sign,
        'options_nearest_path_strike': decision.nearest_path_strike,
        'options_hours_to_expiry': None if decision.hours_to_expiry is None else Decimal(repr(decision.hours_to_expiry)),
        'options_expression': decision.expression,
        'size_multiplier': Decimal(repr(decision.size_multiplier)),
        'options_influence': Decimal(repr(decision.influence)),
        'options_confidence': None if decision.confidence is None else Decimal(repr(decision.confidence)),
        'options_metric_id': decision.metric_id,
        'options_snapshot_time': decision.snapshot_time,
    }


def check_candidate(session: Session, candidate: Candidate, *, dry_run: bool = False) -> OptionsOutcome:
    """Check one candidate and write the outcome once. Does not commit."""
    inputs, why = load_check_inputs(session, candidate)
    base = dict(candidate_id=candidate.id, coin=candidate.coin, horizon=candidate.horizon)
    if inputs is None:
        return OptionsOutcome(**base, result=why)
    decision = check_options(inputs)
    detail = decision.ignore_reason or ''
    if dry_run:
        return OptionsOutcome(**base, result=RESULT_DRY, verdict=decision.verdict, status=decision.status, status_reason=decision.status_reason, detail=detail)
    updated = session.execute(
        update(Candidate)
        .where(Candidate.id == candidate.id, Candidate.options_verdict.is_(None))
        .values(**decision_values(decision))
        .returning(Candidate.id)
    ).first()
    result = RESULT_CHECKED if updated is not None else RESULT_RACE
    return OptionsOutcome(**base, result=result, verdict=decision.verdict, status=decision.status, status_reason=decision.status_reason, detail=detail)


def run_options_job(
    session: Session,
    *,
    source: str | None = None,
    coins: Sequence[str] | None = None,
    dry_run: bool = False,
) -> OptionsReport:
    """Check every unchecked candidate (optionally one source / some coins). Does not commit.

    Candidates are processed in decision order. The order cannot change a result (the check reads only the book and
    the candidate's own rows), it only fixes the order of the log.
    """
    report = OptionsReport()
    for candidate in pending_candidates(session, source=source, coins=coins):
        report.outcomes.append(check_candidate(session, candidate, dry_run=dry_run))
    session.expire_all()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.worker.options_job [--source live|replay] [--coins A,B] [--dry-run]``."""
    from app.core.db_sync import get_sync_session_factory

    parser = argparse.ArgumentParser(description='Apply the options check to candidates that have not been through it.')
    parser.add_argument('--source', choices=E.SOURCES)
    parser.add_argument('--coins', help='comma-separated symbols')
    parser.add_argument('--dry-run', action='store_true', help='decide and print, write nothing')
    args = parser.parse_args(argv)
    with get_sync_session_factory()() as session:
        report = run_options_job(session, source=args.source, coins=args.coins.split(',') if args.coins else None, dry_run=args.dry_run)
        if not args.dry_run:
            session.commit()
    print(f'checked={report.checked} of {len(report.outcomes)}')
    for o in report.outcomes:
        print(f'  #{o.candidate_id} {o.coin} {o.horizon}: {o.result} {o.verdict or ""} {o.status or ""}/{o.status_reason or ""} {o.detail}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
