"""The rules job: read ``feature_row``, apply the frozen rules, write ``candidate``.

Everything that decides lives in ``app.core.rules`` and is pure. This module does
the I/O only:

1. **Read** the stored ``feature_row`` for (coin, horizon, bar_close, source). The row
   was built through the ``as_of`` gate by ``feature_job``; the rules read no raw feed.
   The one other input is the feed-staleness stand-down, taken from
   ``stand_down_breaches`` for ``source='live'`` only (``feed_health`` is a present-tense
   table, so a replay has no honest history of it; a replay passes none, and its
   protection against stale inputs is that ``feature_job`` writes NULL for a stale
   series). Earlier candidates are read from ``candidate`` for the one-open-signal test
   and the sample group; those are our own decisions, not a feed.
2. **Evaluate** with :func:`app.core.rules.evaluate.evaluate`.
3. **Write** the candidate idempotently (``ON CONFLICT DO NOTHING`` on
   ``uq_candidate_series_bar_close``). Rows are immutable at this milestone: a re-run
   never changes an existing candidate.

Live and replay
---------------
Nothing here reads the wall clock except the live entry point, which passes ``now``
down. ``decision_time`` is the caller's ``now`` for ``source='live'`` and the bar close
for ``source='replay'``; ``source`` is taken from the feature row being decided, so a
replay candidate is always built from a replay feature row. A replay must walk bars in
time order (earlier candidates are what the one-open-signal and sample-group rules look
back at), with the reference coin (BTCUSDT) evaluated before the alts on every bar; this
function sorts the coins that way.

What M4 writes, and what it leaves for M5/M6 (see ``docs/BUILD-LOG.md``)
-------------------------------------------------------------------------
Written: identity, ``trigger``, ``setup_family``, ``trend_family``, the three hashes,
``feature_row_id``, ``pre_options_stop_price`` (the chandelier stop), ``pre_options_target_price``
(+1R estimate), ``stop_price`` (= the pre-options stop until M5 widens it; the column is NOT
NULL), ``status`` / ``status_reason``, ``locked_book`` and the sample group.
Left NULL: ``entry_price``, ``one_r``, both barrier prices, ``vertical_deadline`` and every
``options_*`` / estimate column.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.features import params as P
from app.core.hashing import code_version as current_code_version
from app.core.hashing import rule_hash as current_rule_hash
from app.core.rules.evaluate import Evaluation, RuleContext, evaluate
from app.core.rules.inputs import RuleInputs
from app.core.rules.locked_book import is_locked_book
from app.core.rules.sample_group import GroupAssignment, PriorCandidate, assign_sample_group, has_open_signal
from app.core.time import ensure_utc, horizon_hours, utc_now
from app.models import enums as E
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.worker.feature_job import REFERENCE_COIN, latest_closed_bar, run_feature_job
from app.worker.feeds.health import stand_down_breaches

log = logging.getLogger(__name__)

UNIQUE_KEY = 'uq_candidate_series_bar_close'

RESULT_CREATED = 'created'
RESULT_PRESENT = 'already_present'
RESULT_SKIPPED = 'skipped'
RESULT_NO_FEATURE_ROW = 'no_feature_row'
RESULT_DRY_RUN = 'dry_run'


@dataclass
class CandidateOutcome:
    coin: str
    horizon: str
    bar_close: datetime
    result: str
    skipped: str | None = None
    candidate_id: int | None = None
    direction: str | None = None
    trigger: str | None = None
    status: str | None = None
    status_reason: str | None = None
    sample_group_reason: str | None = None
    locked_book: bool | None = None


@dataclass
class RulesReport:
    bar_close: datetime
    source: str
    decision_time: datetime
    rule_hash: str
    outcomes: list[CandidateOutcome] = field(default_factory=list)

    def count(self, result: str) -> int:
        return sum(1 for o in self.outcomes if o.result == result)

    @property
    def created(self) -> int:
        return self.count(RESULT_CREATED)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def load_feature_row(session: Session, coin: str, horizon: str, bar_close: datetime, source: str) -> FeatureRow | None:
    return session.execute(
        sa_select(FeatureRow).where(
            FeatureRow.coin == coin,
            FeatureRow.horizon == horizon,
            FeatureRow.signal_timeframe == P.SIGNAL_TIMEFRAME,
            FeatureRow.bar_close == bar_close,
            FeatureRow.source == source,
        )
    ).scalar_one_or_none()


def load_priors(session: Session, coin: str, horizon: str, source: str, bar_close: datetime) -> list[PriorCandidate]:
    """Earlier candidates of this horizon and source on ``coin`` and the reference coin whose hold may still be open."""
    earliest = bar_close - timedelta(hours=horizon_hours(horizon))
    rows = session.execute(
        sa_select(Candidate).where(
            Candidate.source == source,
            Candidate.horizon == horizon,
            Candidate.coin.in_({coin, REFERENCE_COIN}),
            Candidate.bar_close > earliest,
            Candidate.bar_close <= bar_close,  # the pure layer ignores same-bar rows except BTC's (btc_impulse)
        )
    ).scalars()
    return [
        PriorCandidate(
            id=r.id,
            coin=r.coin,
            horizon=r.horizon,
            bar_close=ensure_utc(r.bar_close),
            decision_time=ensure_utc(r.decision_time),
            direction=r.direction,
            status=r.status,
            status_reason=r.status_reason,
            sample_group_id=r.sample_group_id,
        )
        for r in rows
    ]


def live_feed_breaches(session: Session, decision_time: datetime, coin: str) -> tuple[str, ...]:
    """Gating feeds past their available-at rule at ``decision_time`` for ``coin`` (and BTC, which an alt reads)."""
    coins = {coin, REFERENCE_COIN}
    breaches = stand_down_breaches(session, decision_time, coins=sorted(coins))
    return tuple(sorted(f'{b.feed}:{b.coin}' for b in breaches))


# ---------------------------------------------------------------------------
# One decision
# ---------------------------------------------------------------------------
def decide(
    session: Session,
    row: FeatureRow,
    *,
    feed_breaches: Sequence[str] = (),
) -> tuple[Evaluation, GroupAssignment | None, list[PriorCandidate]]:
    """Evaluate one stored feature row. Reads earlier candidates; writes nothing."""
    inputs = RuleInputs.from_feature_row(row, reference_coin=REFERENCE_COIN)
    bar_close = ensure_utc(row.bar_close)
    priors = load_priors(session, row.coin, row.horizon, row.source, bar_close)
    context = RuleContext(
        has_open_signal=has_open_signal(priors, row.coin, row.horizon, bar_close),
        feed_breaches=tuple(feed_breaches),
    )
    evaluation = evaluate(inputs, context)
    if evaluation.proposal is None:
        return evaluation, None, priors
    group = assign_sample_group(
        coin=row.coin,
        horizon=row.horizon,
        source=row.source,
        bar_close=bar_close,
        direction=evaluation.proposal.direction,
        priors=priors,
        reference_coin=REFERENCE_COIN,
    )
    return evaluation, group, priors


def candidate_values(
    row: FeatureRow,
    evaluation: Evaluation,
    group: GroupAssignment,
    *,
    decision_time: datetime,
    rule_hash_value: str,
    code_version: str,
) -> dict[str, Any]:
    proposal = evaluation.proposal
    assert proposal is not None
    bar_close = ensure_utc(row.bar_close)
    return {
        'decision_time': decision_time,
        'bar_close': bar_close,
        'coin': row.coin,
        'horizon': row.horizon,
        'direction': proposal.direction,
        'source': row.source,
        'trigger': proposal.trigger,
        'setup_family': proposal.setup_family,
        'trend_family': proposal.trend_family,
        'feature_hash': row.feature_hash,
        'rule_hash': rule_hash_value,
        'code_version': code_version,
        'feature_row_id': row.id,
        'pre_options_stop_price': proposal.plan.stop_price,
        'pre_options_target_price': proposal.plan.target_price,
        # NOT NULL column owned by M5: equal to the pre-options stop until the options check widens it.
        'stop_price': proposal.plan.stop_price,
        'status': proposal.status,
        'status_reason': proposal.status_reason,
        'locked_book': is_locked_book(row.coin, bar_close),
        'sample_group_id': group.sample_group_id,
        'sample_group_leader_id': group.leader_id,
        'sample_group_reason': group.reason,
    }


# ---------------------------------------------------------------------------
# One bar, many coins
# ---------------------------------------------------------------------------
def order_coins(coins: Iterable[str]) -> list[str]:
    """Unique coins, the reference coin first (alts look for its candidate on the same bar)."""
    unique = sorted({c.strip().upper() for c in coins if c.strip()})
    return sorted(unique, key=lambda coin: (coin != REFERENCE_COIN, coin))


def run_rules_for_bar(
    session: Session,
    coins: Iterable[str],
    bar_close: datetime,
    *,
    source: str = E.SOURCE_LIVE,
    horizons: Iterable[str] = C.HORIZONS,
    now: datetime | None = None,
    decision_time: datetime | None = None,
    code_version: str | None = None,
    feed_breaches: Mapping[str, Sequence[str]] | None = None,
    dry_run: bool = False,
) -> RulesReport:
    """Apply the frozen rules to the feature rows of ``coins`` at ``bar_close``. Does not commit.

    ``decision_time`` defaults to ``now`` (or the clock) for ``live`` and to ``bar_close`` for
    ``replay``; it may never precede ``bar_close``. ``feed_breaches`` maps a coin to the names of
    its feeds past their rule: if omitted, a live run asks ``stand_down_breaches`` and a replay
    uses none.
    """
    if source not in E.SOURCES:
        raise ValueError(f'source must be one of {E.SOURCES}')
    bar_close = ensure_utc(bar_close)
    if decision_time is None:
        decision_time = bar_close if source == E.SOURCE_REPLAY else (ensure_utc(now) if now is not None else utc_now())
    decision_time = ensure_utc(decision_time)
    if decision_time < bar_close:
        raise ValueError('decision_time cannot precede the signal bar close')
    version = code_version if code_version is not None else current_code_version()
    rule_hash_value = current_rule_hash()
    report = RulesReport(bar_close, source, decision_time, rule_hash_value)
    horizon_list = list(horizons)

    for coin in order_coins(coins):
        if feed_breaches is not None:
            breaches: Sequence[str] = tuple(feed_breaches.get(coin, ()))
        elif source == E.SOURCE_LIVE:
            breaches = live_feed_breaches(session, decision_time, coin)
        else:
            breaches = ()
        for horizon in horizon_list:
            row = load_feature_row(session, coin, horizon, bar_close, source)
            if row is None:
                report.outcomes.append(CandidateOutcome(coin, horizon, bar_close, RESULT_NO_FEATURE_ROW))
                continue
            evaluation, group, _priors = decide(session, row, feed_breaches=breaches)
            proposal = evaluation.proposal
            if proposal is None or group is None:
                report.outcomes.append(CandidateOutcome(coin, horizon, bar_close, RESULT_SKIPPED, skipped=evaluation.skipped))
                continue
            values = candidate_values(
                row, evaluation, group, decision_time=decision_time, rule_hash_value=rule_hash_value, code_version=version
            )
            outcome = CandidateOutcome(
                coin,
                horizon,
                bar_close,
                RESULT_DRY_RUN,
                direction=proposal.direction,
                trigger=proposal.trigger,
                status=proposal.status,
                status_reason=proposal.status_reason,
                sample_group_reason=group.reason,
                locked_book=values['locked_book'],
            )
            if not dry_run:
                inserted = session.execute(
                    pg_insert(Candidate).values(**values).on_conflict_do_nothing(constraint=UNIQUE_KEY).returning(Candidate.id)
                ).first()
                outcome.result = RESULT_CREATED if inserted is not None else RESULT_PRESENT
                outcome.candidate_id = inserted[0] if inserted is not None else None
            report.outcomes.append(outcome)
    return report


# ---------------------------------------------------------------------------
# Scheduled entry points and CLI
# ---------------------------------------------------------------------------
def run_rules_job(session: Session, coins: Sequence[str], *, now: datetime | None = None) -> dict[str, Any]:
    """One live tick: the rules for the newest closed 15m bar. Run after :func:`run_feature_job`."""
    clock = ensure_utc(now) if now is not None else utc_now()
    bar_close = latest_closed_bar(clock)
    report = run_rules_for_bar(session, coins, bar_close, source=E.SOURCE_LIVE, now=clock)
    return {
        'bar_close': bar_close.isoformat(),
        'created': report.created,
        'already_present': report.count(RESULT_PRESENT),
        'no_feature_row': report.count(RESULT_NO_FEATURE_ROW),
        'rule_hash': report.rule_hash,
    }


def run_feature_and_rules_job(session: Session, coins: Sequence[str], *, now: datetime | None = None) -> dict[str, Any]:
    """Features, then rules, for the newest closed bar. The scheduled path is ``app.worker.pipeline``."""
    clock = ensure_utc(now) if now is not None else utc_now()
    features = run_feature_job(session, coins, now=clock)
    rules = run_rules_job(session, coins, now=clock)
    return {'features': features, 'rules': rules}


def _parse_instant(text: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(text.replace('Z', '+00:00')))


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.worker.rules_job --coins BTCUSDT[,..] [--at ISO] [--source live|replay] [--dry-run]``."""
    from app.core.db_sync import get_sync_session_factory

    parser = argparse.ArgumentParser(description='Apply the frozen rules to the feature rows of one 15m bar close.')
    parser.add_argument('--coins', default=REFERENCE_COIN, help='comma-separated symbols')
    parser.add_argument('--at', help='bar close as an ISO-8601 UTC instant (default: newest closed bar)')
    parser.add_argument('--source', choices=E.SOURCES, default=E.SOURCE_LIVE)
    parser.add_argument('--dry-run', action='store_true', help='decide and print, write nothing')
    args = parser.parse_args(argv)
    bar_close = _parse_instant(args.at) if args.at else latest_closed_bar(utc_now())
    factory = get_sync_session_factory()
    with factory() as session:
        report = run_rules_for_bar(
            session, args.coins.split(','), bar_close, source=args.source, now=utc_now(), dry_run=args.dry_run
        )
        if not args.dry_run:
            session.commit()
    print(f'bar_close={bar_close.isoformat()} source={args.source} rule_hash={report.rule_hash}')
    for o in report.outcomes:
        detail = o.skipped or f'{o.direction} {o.trigger} {o.status}/{o.status_reason} group={o.sample_group_reason} locked={o.locked_book}'
        print(f'  {o.coin} {o.horizon}: {o.result} {detail}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
