"""The label job: freeze the fill values of every options-checked candidate and resolve it on its barriers.

Everything that decides lives in ``app.core.labeler.barriers`` (conventions in ``constants.LABEL_*``, hashed) and is pure. This
module does the I/O:

1. **Select** candidates that have been through the options check (``options_verdict IS NOT NULL``, so the barriers are the
   ones frozen after it), have no label yet, and carry the CURRENT ``rule_hash``. A candidate decided under another rule hash is
   skipped and counted (``stale_rule_hash``): it was decided by rules that are no longer the frozen ones. Every status is
   labeled: ``alert``, ``watch`` (including the BTC/ETH ``ignore`` rows), ``cancelled``, ``delayed`` and ``abstained``, live and
   replay alike. Shadow rows resolve on the barriers they would have had. A ``delayed`` row is labeled from its original fill;
   Phase 1 never re-enters it after expiry.
2. **Freeze the fill** once: the next 15m perp bar's open is the entry; with the final ``stop_price`` and ``target_cap_price``
   it gives 1R, both barrier prices and the deadline. One guarded UPDATE (``WHERE entry_price IS NULL``) writes them, and a
   database trigger refuses any later change. A later run reads them back and never recomputes them. A next open at or through
   the stop, or through the strike cap, invalidated the setup before the fill: the observed open and the deadline are frozen,
   1R and the barriers stay NULL, and the label is ``no_entry`` with no R at all.
3. **Resolve** over perp 15m bars read through ``as_of(as_of_time)``: only bars whose ``available_at`` has passed, contiguous
   from the fill, up to the deadline. Realized funding comes through the same gate.
4. **Write** the label with ``ON CONFLICT DO NOTHING`` on its primary key (the candidate).

A candidate that cannot be resolved yet stays unlabeled and is picked up by the next run: the entry bar or a later bar is not
visible, a bar is missing, or the settled funding prints do not prove that none was missed. Funding waits at most
``LABEL_FUNDING_STALL_CAP_HOURS`` after the exit became visible; then the label is written with the prints seen so far and
``funding_partial = true``.

No wall clock here: ``as_of_time`` is the caller's instant (now, live; the replay clock, in replay).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select as sa_select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.hashing import code_version as current_code_version
from app.core.hashing import rule_hash as current_rule_hash
from app.core.labeler import barriers as B
from app.core.labeler import outcome as O
from app.core.time import ensure_utc, utc_now, vertical_barrier_deadline
from app.models import enums as E
from app.models.as_of import as_of
from app.models.decisions import Candidate
from app.models.feeds import Candle, Funding
from app.models.features import FeatureRow
from app.models.labels import Label

log = logging.getLogger(__name__)

RESULT_LABELED = 'labeled'
RESULT_NO_ENTRY = 'no_entry'
RESULT_PENDING = 'pending'
RESULT_RACE = 'already_labeled'
RESULT_DRY = 'dry_run'
RESULT_ERROR = 'error'

PENDING_ENTRY_BAR = O.PENDING_ENTRY_BAR
PENDING_AWAITING_BARS = O.PENDING_AWAITING_BARS
PENDING_MISSING_BAR = O.PENDING_MISSING_BAR
PENDING_FUNDING = O.PENDING_FUNDING

#: A bar this far past its own close and still absent is a hole in the feed, not a delay (reporting only).
MISSING_BAR_AFTER = 2 * B.BAR_LENGTH

#: The fill is frozen but carries no barriers: the open was at or through the stop or the cap.
NoEntry = O.NoEntry


@dataclass(frozen=True)
class LabelOutcome:
    candidate_id: int
    coin: str
    horizon: str
    result: str
    detail: str = ''
    first_barrier: str | None = None
    stored_r: Decimal | None = None
    funding_partial: bool = False


@dataclass
class LabelReport:
    as_of_time: datetime
    rule_hash: str = ''
    stale_rule_hash: int = 0
    outcomes: list[LabelOutcome] = field(default_factory=list)

    def count(self, result: str) -> int:
        return sum(1 for o in self.outcomes if o.result == result)

    @property
    def labeled(self) -> int:
        return self.count(RESULT_LABELED)

    @property
    def no_entry(self) -> int:
        return self.count(RESULT_NO_ENTRY)


def default_funding_max_gap() -> timedelta:
    return B.FUNDING_MAX_GAP


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _unlabeled(as_of_time: datetime, source: str | None, coins: Sequence[str] | None) -> list[Any]:
    conditions: list[Any] = [
        Candidate.options_verdict.is_not(None),
        ~exists().where(Label.candidate_id == Candidate.id),
        Candidate.bar_close <= ensure_utc(as_of_time) - B.BAR_LENGTH,
    ]
    if source is not None:
        conditions.append(Candidate.source == source)
    if coins:
        conditions.append(Candidate.coin.in_(list(coins)))
    return conditions


def pending_candidates(
    session: Session,
    as_of_time: datetime,
    *,
    rule_hash: str | None = None,
    source: str | None = None,
    coins: Sequence[str] | None = None,
) -> list[Candidate]:
    """Options-checked candidates of the current rule hash without a label and whose entry bar has closed, oldest decision first."""
    statement = sa_select(Candidate).where(Candidate.rule_hash == (rule_hash or current_rule_hash()), *_unlabeled(as_of_time, source, coins))
    return list(session.execute(statement.order_by(Candidate.decision_time, Candidate.id)).scalars())


def count_stale_rule_hash(
    session: Session,
    as_of_time: datetime,
    *,
    rule_hash: str | None = None,
    source: str | None = None,
    coins: Sequence[str] | None = None,
) -> int:
    """Candidates that would be labeled but were decided under a different rule hash."""
    statement = sa_select(func.count()).select_from(Candidate).where(
        Candidate.rule_hash != (rule_hash or current_rule_hash()), *_unlabeled(as_of_time, source, coins)
    )
    return int(session.execute(statement).scalar_one())


def _bar(row: Candle) -> B.Bar:
    return B.Bar(
        open_time=ensure_utc(row.open_time),
        close_time=ensure_utc(row.close_time),
        open=row.open_price,
        high=row.high_price,
        low=row.low_price,
        close=row.close_price,
        available_at=ensure_utc(row.available_at),
    )


def load_bars(session: Session, as_of_time: datetime, coin: str, start: datetime, end: datetime) -> list[B.Bar]:
    """Visible perp 15m bars with ``start <= open_time < end``, oldest first."""
    statement = (
        as_of(as_of_time)
        .select(Candle)
        .where(
            Candle.coin == coin,
            Candle.market == E.MARKET_PERP,
            Candle.timeframe == B.BAR_TIMEFRAME,
            Candle.open_time >= start,
            Candle.open_time < end,
        )
        .order_by(Candle.open_time)
    )
    return [_bar(row) for row in session.execute(statement).scalars()]


def load_funding(session: Session, as_of_time: datetime, coin: str, start: datetime, end: datetime) -> list[B.FundingPrint]:
    """Visible settled prints with ``start <= funding_time <= end``, oldest first."""
    statement = (
        as_of(as_of_time)
        .select(Funding)
        .where(Funding.coin == coin, Funding.kind == E.FUNDING_SETTLED, Funding.funding_time >= start, Funding.funding_time <= end)
        .order_by(Funding.funding_time)
    )
    return [B.FundingPrint(ensure_utc(r.funding_time), r.funding_rate, r.mark_price) for r in session.execute(statement).scalars()]


# ---------------------------------------------------------------------------
# One candidate
# ---------------------------------------------------------------------------
def stored_fill(candidate: Candidate) -> B.Barriers | NoEntry:
    if candidate.one_r is None:
        return NoEntry(candidate.entry_price, 'stored')
    return B.Barriers(
        entry=candidate.entry_price,
        one_r=candidate.one_r,
        upper=candidate.upper_barrier_price,
        lower=candidate.lower_barrier_price,
        deadline=ensure_utc(candidate.vertical_deadline),
    )


def freeze_fill(session: Session, candidate: Candidate, as_of_time: datetime, *, dry_run: bool) -> B.Barriers | NoEntry | str:
    """The candidate's fill: the stored one, or newly frozen from the entry bar. A string is the reason there is none yet."""
    if candidate.entry_price is not None:
        return stored_fill(candidate)
    fill_time = ensure_utc(candidate.bar_close)
    entry_bars = load_bars(session, as_of_time, candidate.coin, fill_time, fill_time + B.BAR_LENGTH)
    fill = O.plan_fill(
        direction=candidate.direction,
        stop=candidate.stop_price,
        cap=candidate.target_cap_price,
        fill_time=fill_time,
        horizon=candidate.horizon,
        entry_bars=entry_bars,
    )
    if isinstance(fill, str):
        return fill
    values: dict[str, Any]
    if isinstance(fill, B.Barriers):
        values = dict(
            entry_price=fill.entry,
            one_r=fill.one_r,
            upper_barrier_price=fill.upper,
            lower_barrier_price=fill.lower,
            vertical_deadline=fill.deadline,
        )
    else:
        values = dict(
            entry_price=fill.entry,
            one_r=None,
            upper_barrier_price=None,
            lower_barrier_price=None,
            vertical_deadline=vertical_barrier_deadline(fill_time, candidate.horizon),
        )
    if dry_run:
        return fill
    updated = session.execute(
        update(Candidate).where(Candidate.id == candidate.id, Candidate.entry_price.is_(None)).values(**values).returning(Candidate.id)
    ).first()
    if updated is None:  # another writer froze it first: read theirs, never ours
        session.refresh(candidate)
        return stored_fill(candidate)
    return fill


def _common_values(candidate: Candidate, fill_time: datetime, feature_row: FeatureRow | None, code_version: str) -> dict[str, Any]:
    return {
        'candidate_id': candidate.id,
        'fill_time': fill_time,
        'regime_trend_family': candidate.trend_family,
        'regime_vol_pct': None if feature_row is None else feature_row.realized_vol_pct,
        'regime_session': None if feature_row is None else feature_row.session,
        'regime_event_flag': None if feature_row is None else feature_row.event_flag,
        'regime_is_weekend': None if feature_row is None else feature_row.is_weekend,
        'cost_assumption_bps': C.ROUND_TRIP_COST_BPS,
        'taker_fee_bps_per_side': C.TAKER_FEE_BPS_PER_SIDE,
        'slippage_bps_per_side': C.SLIPPAGE_BPS_PER_SIDE,
        'feature_hash': candidate.feature_hash,
        'rule_hash': candidate.rule_hash,
        'code_version': code_version,
    }


def label_values(
    candidate: Candidate,
    barriers: B.Barriers,
    resolution: B.Resolution,
    funding_r: Decimal,
    funding_bps: Decimal,
    funding_partial: bool,
    feature_row: FeatureRow | None,
    code_version: str,
) -> dict[str, Any]:
    fill_time = ensure_utc(candidate.bar_close)
    fees = B.fees_r(barriers)
    return {
        **_common_values(candidate, fill_time, feature_row, code_version),
        'fill_price': barriers.entry,
        'resolved_at': resolution.resolved_at,
        'first_barrier': resolution.first_barrier,
        'barrier_ambiguous': resolution.ambiguous,
        'gapped_stop': resolution.gapped_stop,
        'funding_partial': funding_partial,
        'hours_to_resolution': B.hours_between(fill_time, resolution.resolved_at),
        'price_r': resolution.price_r,
        'fees_r': fees,
        'funding_r': funding_r,
        'stored_r': B.stored_r(resolution.price_r, fees, funding_r),
        'mae_r': resolution.mae_r,
        'mfe_r': resolution.mfe_r,
        'hours_to_mfe': None if resolution.mfe_at is None else B.hours_between(fill_time, resolution.mfe_at),
        'realized_funding_bps': funding_bps,
    }


def no_entry_values(candidate: Candidate, no_entry: NoEntry, feature_row: FeatureRow | None, code_version: str) -> dict[str, Any]:
    """A label with no trade behind it: the observed open and the hashes, and no R of any kind."""
    fill_time = ensure_utc(candidate.bar_close)
    return {
        **_common_values(candidate, fill_time, feature_row, code_version),
        'fill_price': no_entry.entry,
        'resolved_at': fill_time,
        'first_barrier': E.BARRIER_NO_ENTRY,
    }


def _insert(session: Session, values: dict[str, Any]) -> bool:
    inserted = session.execute(
        pg_insert(Label).values(**values).on_conflict_do_nothing(index_elements=[Label.candidate_id]).returning(Label.candidate_id)
    ).first()
    return inserted is not None


def label_candidate(
    session: Session,
    candidate: Candidate,
    as_of_time: datetime,
    *,
    code_version: str,
    funding_max_gap: timedelta | None = None,
    dry_run: bool = False,
) -> LabelOutcome:
    """Freeze (once) and resolve one candidate. Does not commit; wraps its own writes in a SAVEPOINT."""
    base = dict(candidate_id=candidate.id, coin=candidate.coin, horizon=candidate.horizon)
    gap = funding_max_gap if funding_max_gap is not None else default_funding_max_gap()
    fill_time = ensure_utc(candidate.bar_close)
    try:
        with session.begin_nested():
            frozen = freeze_fill(session, candidate, as_of_time, dry_run=dry_run)
            if isinstance(frozen, str):
                return LabelOutcome(**base, result=RESULT_PENDING, detail=frozen)
            feature_row = session.get(FeatureRow, candidate.feature_row_id)
            if isinstance(frozen, NoEntry):
                if dry_run:
                    return LabelOutcome(**base, result=RESULT_DRY, first_barrier=E.BARRIER_NO_ENTRY, detail=frozen.reason)
                written = _insert(session, no_entry_values(candidate, frozen, feature_row, code_version))
                return LabelOutcome(
                    **base, result=RESULT_NO_ENTRY if written else RESULT_RACE, first_barrier=E.BARRIER_NO_ENTRY, detail=frozen.reason
                )
            barriers = frozen

            bars = load_bars(session, as_of_time, candidate.coin, fill_time, barriers.deadline)
            outcome = O.resolve_outcome(
                direction=candidate.direction,
                barriers=barriers,
                bars=bars,
                load_prints=lambda start, end: load_funding(session, as_of_time, candidate.coin, start, end),
                fill_time=fill_time,
                as_of_time=ensure_utc(as_of_time),
                funding_max_gap=gap,
                missing_bar_after=MISSING_BAR_AFTER,
            )
            if isinstance(outcome, O.Waiting):
                return LabelOutcome(**base, result=RESULT_PENDING, detail=outcome.detail)
            resolution, partial = outcome.resolution, outcome.funding_partial
            values = label_values(
                candidate, barriers, resolution, outcome.funding_r, outcome.funding_bps, partial, feature_row, code_version
            )
            summary = dict(first_barrier=resolution.first_barrier, stored_r=values['stored_r'], funding_partial=partial)
            if dry_run:
                return LabelOutcome(**base, result=RESULT_DRY, **summary)
            return LabelOutcome(**base, result=RESULT_LABELED if _insert(session, values) else RESULT_RACE, **summary)
    except SQLAlchemyError as exc:
        log.exception('labeling candidate %s failed', candidate.id)
        return LabelOutcome(**base, result=RESULT_ERROR, detail=f'{type(exc).__name__}: {str(exc)[:200]}')


def run_label_job(
    session: Session,
    as_of_time: datetime,
    *,
    source: str | None = None,
    coins: Sequence[str] | None = None,
    dry_run: bool = False,
    code_version: str | None = None,
    funding_max_gap: timedelta | None = None,
    rule_hash: str | None = None,
) -> LabelReport:
    """Label every options-checked candidate of the current rule hash that can be labeled at ``as_of_time``. Does not commit."""
    as_of_time = ensure_utc(as_of_time)
    version = code_version if code_version is not None else current_code_version()
    current = rule_hash if rule_hash is not None else current_rule_hash()
    report = LabelReport(as_of_time, rule_hash=current)
    report.stale_rule_hash = count_stale_rule_hash(session, as_of_time, rule_hash=current, source=source, coins=coins)
    for candidate in pending_candidates(session, as_of_time, rule_hash=current, source=source, coins=coins):
        report.outcomes.append(label_candidate(session, candidate, as_of_time, code_version=version, funding_max_gap=funding_max_gap, dry_run=dry_run))
    session.expire_all()
    return report


def summarize(report: LabelReport) -> dict[str, Any]:
    return {
        'as_of_time': report.as_of_time.isoformat(),
        'rule_hash': report.rule_hash,
        'labeled': report.labeled,
        'no_entry': report.no_entry,
        'funding_partial': sum(1 for o in report.outcomes if o.result == RESULT_LABELED and o.funding_partial),
        'pending': report.count(RESULT_PENDING),
        'stale_rule_hash': report.stale_rule_hash,
        'errors': report.count(RESULT_ERROR),
    }


def run_live_label_job(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """One live tick over live candidates. NOT registered with the scheduler (see BUILD-LOG)."""
    clock = ensure_utc(now) if now is not None else utc_now()
    return summarize(run_label_job(session, clock, source=E.SOURCE_LIVE))


def _parse_instant(text: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(text.replace('Z', '+00:00')))


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.worker.label_job [--source live|replay] [--coins A,B] [--at ISO] [--dry-run]``."""
    from app.core.db_sync import get_sync_session_factory

    parser = argparse.ArgumentParser(description='Freeze fill values and resolve barriers for options-checked candidates.')
    parser.add_argument('--source', choices=E.SOURCES)
    parser.add_argument('--coins', help='comma-separated symbols')
    parser.add_argument('--at', help='as_of instant, ISO-8601 UTC (default: now)')
    parser.add_argument('--dry-run', action='store_true', help='resolve and print, write nothing')
    args = parser.parse_args(argv)
    instant = _parse_instant(args.at) if args.at else utc_now()
    with get_sync_session_factory()() as session:
        report = run_label_job(
            session, instant, source=args.source, coins=args.coins.split(',') if args.coins else None, dry_run=args.dry_run
        )
        if not args.dry_run:
            session.commit()
    summary = summarize(report)
    print(' '.join(f'{k}={v}' for k, v in summary.items()))
    for o in report.outcomes:
        print(f'  #{o.candidate_id} {o.coin} {o.horizon}: {o.result} {o.first_barrier or ""} {o.stored_r if o.stored_r is not None else ""} {o.detail}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
