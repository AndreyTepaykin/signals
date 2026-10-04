"""The replay harness: walk 15m bar closes and run the same pipeline as live, through ``as_of``.

For each bar close from ``start`` through ``end`` (inclusive, on the 15m grid):

1. **Features** — :func:`app.worker.feature_job.assemble_feature_rows` with ``source='replay'`` and every feed read
   gated at ``as_of(bar_close)``. No predicted-funding read (live-only).
2. **Rules** — :func:`app.worker.rules_job.run_rules_for_bar` with ``source='replay'``, ``decision_time=bar_close``,
   and no feed-health breaches (replay has no honest breach history).
3. **Options** — :func:`app.worker.options_job.run_options_job` for ``source='replay'`` after each bar.

After the walk, **labels** — :func:`app.worker.label_job.run_label_job` with ``as_of_time=label_at`` (default
``end``), never the wall clock.

Dry run
-------
``dry_run=True`` executes the full walk with real writes to every job (same code path as production) inside a
single database transaction: no intermediate commits. The session is rolled back at the end, so nothing persists,
but the :class:`ReplayReport` reflects exactly what a committed run would have produced (including cross-bar priors
for sample groups and open-signal checks).

Real runs commit after each bar so a long walk can be resumed.

Live vs replay
--------------
The only intentional difference in the options layer for a missing Deribit book on BTC/ETH is the ignore code:
live ``no_snapshot``, replay ``no_options_history``. Everything else matches when the feeds and books match.

No wall clock
-------------
Nothing in this module calls ``utc_now`` or reads the clock for decisions. Callers pass ``start``, ``end`` and
``label_at``; feature assembly uses ``now=bar_close`` so readiness grace never consults the real time.

CLI
---
``python -m app.worker.replay --coins A,B --start ISO --end ISO [--label-at ISO] [--dry-run]``
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings, parse_symbols
from app.core import constants as C
from app.core.db_sync import get_sync_session_factory
from app.core.features import params as P
from app.core.features.params import HistoryPolicy, PRODUCTION_POLICY
from app.core.features.params import timeframe_delta
from app.core.features.series import floor_to
from app.core.hashing import code_version as current_code_version
from app.core.hashing import rule_hash as current_rule_hash
from app.core.time import ensure_utc
from app.models import enums as E
from app.worker import feature_job as FJ
from app.worker import label_job as LJ
from app.worker import options_job as OJ
from app.worker import rules_job as RJ

log = logging.getLogger(__name__)

JOB_NAME = 'replay'


@dataclass
class ReplayReport:
    """Summary of one replay run (also returned by :func:`run_replay`)."""

    start: datetime
    end: datetime
    label_at: datetime
    source: str = E.SOURCE_REPLAY
    dry_run: bool = False
    rule_hash: str = ''
    bars_walked: int = 0
    features_written: int = 0
    features_already_present: int = 0
    features_skipped_not_ready: int = 0
    candidates_by_status: dict[str, int] = field(default_factory=dict)
    candidates_by_status_reason: dict[str, int] = field(default_factory=dict)
    options_verdicts: dict[str, int] = field(default_factory=dict)
    options_ignore_reasons: dict[str, int] = field(default_factory=dict)
    options_checked: int = 0
    labeled: int = 0
    no_entry: int = 0
    funding_partial: int = 0
    pending: int = 0
    pending_by_detail: dict[str, int] = field(default_factory=dict)
    stale_rule_hash: int = 0
    label_errors: int = 0
    elapsed_seconds: float = 0.0

    @property
    def bars_per_second(self) -> float:
        if self.elapsed_seconds <= 0.0 or self.bars_walked == 0:
            return 0.0
        return self.bars_walked / self.elapsed_seconds

    def format_summary(self) -> str:
        parts = [
            f'start={self.start.isoformat()}',
            f'end={self.end.isoformat()}',
            f'label_at={self.label_at.isoformat()}',
            f'dry_run={self.dry_run}',
            f'rule_hash={self.rule_hash}',
            f'bars={self.bars_walked}',
            f'features written={self.features_written} present={self.features_already_present} not_ready={self.features_skipped_not_ready}',
            f'candidates status={dict(self.candidates_by_status)} reason={dict(self.candidates_by_status_reason)}',
            f'options verdicts={dict(self.options_verdicts)} ignore={dict(self.options_ignore_reasons)} checked={self.options_checked}',
            (
                f'labels labeled={self.labeled} no_entry={self.no_entry} funding_partial={self.funding_partial} '
                f'pending={self.pending} pending_detail={dict(self.pending_by_detail)} stale_rule_hash={self.stale_rule_hash} errors={self.label_errors}'
            ),
            f'elapsed={self.elapsed_seconds:.2f}s bars_per_s={self.bars_per_second:.3f}',
        ]
        return ' '.join(parts)


def iter_bar_closes(start: datetime, end: datetime) -> list[datetime]:
    """Every 15m signal bar close from ``start`` through ``end``, inclusive, UTC."""
    step = timeframe_delta(P.SIGNAL_TIMEFRAME)
    first = floor_to(ensure_utc(start), step)
    last = floor_to(ensure_utc(end), step)
    if last < first:
        return []
    out: list[datetime] = []
    t = first
    while t <= last:
        out.append(t)
        t += step
    return out


def resolve_coins(coins: Sequence[str] | None) -> list[str]:
    if coins:
        return RJ.order_coins(coins)
    return RJ.order_coins(get_settings().watchlist_symbols)


def validate_replay_args(
    *,
    start: datetime,
    end: datetime,
    label_at: datetime | None,
    coins: Sequence[str] | None,
) -> tuple[datetime, datetime, datetime, list[str]]:
    start = ensure_utc(start)
    end = ensure_utc(end)
    if end < start:
        raise ValueError('end must not precede start')
    label = ensure_utc(label_at) if label_at is not None else end
    if label < end:
        raise ValueError('label_at must not precede end')
    resolved = resolve_coins(coins)
    allowed = set(get_settings().watchlist_symbols)
    unknown = [c for c in resolved if c not in allowed]
    if unknown:
        raise ValueError(f'unknown coin(s) not on the watchlist: {", ".join(unknown)}')
    return start, end, label, resolved


def _accumulate_rules(report: ReplayReport, rules: RJ.RulesReport) -> None:
    for outcome in rules.outcomes:
        if outcome.result not in (RJ.RESULT_CREATED, RJ.RESULT_PRESENT, RJ.RESULT_DRY_RUN):
            continue
        if outcome.status:
            report.candidates_by_status[outcome.status] = report.candidates_by_status.get(outcome.status, 0) + 1
        if outcome.status_reason:
            report.candidates_by_status_reason[outcome.status_reason] = (
                report.candidates_by_status_reason.get(outcome.status_reason, 0) + 1
            )


def _accumulate_options(report: ReplayReport, options: OJ.OptionsReport) -> None:
    report.options_checked += options.checked
    for outcome in options.outcomes:
        if outcome.verdict:
            report.options_verdicts[outcome.verdict] = report.options_verdicts.get(outcome.verdict, 0) + 1
        if outcome.detail:
            report.options_ignore_reasons[outcome.detail] = report.options_ignore_reasons.get(outcome.detail, 0) + 1


def _accumulate_labels(report: ReplayReport, label_report: LJ.LabelReport) -> None:
    report.labeled += label_report.labeled
    report.no_entry += label_report.no_entry
    report.funding_partial += sum(1 for o in label_report.outcomes if o.result == LJ.RESULT_LABELED and o.funding_partial)
    report.pending += label_report.count(LJ.RESULT_PENDING)
    report.stale_rule_hash += label_report.stale_rule_hash
    report.label_errors += label_report.count(LJ.RESULT_ERROR)
    for outcome in label_report.outcomes:
        if outcome.result == LJ.RESULT_PENDING and outcome.detail:
            report.pending_by_detail[outcome.detail] = report.pending_by_detail.get(outcome.detail, 0) + 1


def _one_bar(
    session: Session,
    report: ReplayReport,
    *,
    bar_close: datetime,
    coin_list: Sequence[str],
    version: str,
    policy: HistoryPolicy,
) -> None:
    for coin in coin_list:
        feat = FJ.assemble_feature_rows(
            session,
            coin,
            bar_close,
            source=E.SOURCE_REPLAY,
            now=bar_close,
            code_version=version,
            policy=policy,
        )
        report.features_written += feat.written
        report.features_already_present += feat.already_present
        if feat.skipped_not_ready:
            report.features_skipped_not_ready += 1
    rules = RJ.run_rules_for_bar(
        session,
        coin_list,
        bar_close,
        source=E.SOURCE_REPLAY,
        decision_time=bar_close,
        feed_breaches={},
        code_version=version,
        dry_run=False,
    )
    _accumulate_rules(report, rules)
    options = OJ.run_options_job(session, source=E.SOURCE_REPLAY, coins=coin_list, dry_run=False)
    _accumulate_options(report, options)


def _run_label_pass(
    session: Session,
    report: ReplayReport,
    *,
    label: datetime,
    coin_list: Sequence[str],
    version: str,
) -> None:
    label_report = LJ.run_label_job(
        session,
        label,
        source=E.SOURCE_REPLAY,
        coins=coin_list,
        dry_run=False,
        code_version=version,
    )
    _accumulate_labels(report, label_report)


def _walk_full(
    session: Session,
    report: ReplayReport,
    *,
    bars: Sequence[datetime],
    coin_list: Sequence[str],
    label: datetime,
    version: str,
    commit_each_bar: bool,
    policy: HistoryPolicy,
) -> None:
    """Features, rules and options for every bar, then one label pass. Jobs always write."""
    for bar_close in bars:
        _one_bar(session, report, bar_close=bar_close, coin_list=coin_list, version=version, policy=policy)
        report.bars_walked += 1
        if commit_each_bar:
            session.commit()
    _run_label_pass(session, report, label=label, coin_list=coin_list, version=version)
    if commit_each_bar:
        session.commit()


def run_replay(
    session_or_factory: Session | sessionmaker[Session] | Callable[[], Session],
    *,
    coins: Sequence[str],
    start: datetime,
    end: datetime,
    label_at: datetime | None = None,
    dry_run: bool = False,
    code_version: str | None = None,
    history_policy: HistoryPolicy | None = None,
) -> ReplayReport:
    """Walk ``start``..``end`` and run features, rules, options, then label at ``label_at``.

    Real runs commit after each bar. ``dry_run`` uses one transaction and rolls back at the end.
    """
    start, end, label, coin_list = validate_replay_args(start=start, end=end, label_at=label_at, coins=coins)
    bars = iter_bar_closes(start, end)
    version = code_version if code_version is not None else current_code_version()
    policy = history_policy if history_policy is not None else PRODUCTION_POLICY
    report = ReplayReport(start=start, end=end, label_at=label, dry_run=dry_run, rule_hash=current_rule_hash())
    t0 = time.perf_counter()

    passed_session = session_or_factory if isinstance(session_or_factory, Session) else None

    if dry_run and passed_session is not None:
        nested = passed_session.begin_nested()
        try:
            _walk_full(
                passed_session,
                report,
                bars=bars,
                coin_list=coin_list,
                label=label,
                version=version,
                commit_each_bar=False,
                policy=policy,
            )
        finally:
            nested.rollback()
    elif dry_run:
        factory = session_or_factory if isinstance(session_or_factory, sessionmaker) else session_or_factory
        with factory() as session:
            _walk_full(session, report, bars=bars, coin_list=coin_list, label=label, version=version, commit_each_bar=False, policy=policy)
            session.rollback()
    elif passed_session is not None:
        _walk_full(passed_session, report, bars=bars, coin_list=coin_list, label=label, version=version, commit_each_bar=True, policy=policy)
    else:
        factory = session_or_factory if isinstance(session_or_factory, sessionmaker) else session_or_factory
        for bar_close in bars:
            with factory() as session:
                _one_bar(session, report, bar_close=bar_close, coin_list=coin_list, version=version, policy=policy)
                session.commit()
            report.bars_walked += 1
        if bars:
            with factory() as session:
                _run_label_pass(session, report, label=label, coin_list=coin_list, version=version)
                session.commit()

    report.elapsed_seconds = time.perf_counter() - t0
    return report


def _parse_instant(text: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(text.replace('Z', '+00:00')))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see module docstring."""
    parser = argparse.ArgumentParser(description='Replay features, rules, options and labels over a 15m bar range.')
    parser.add_argument('--coins', help='comma-separated symbols (default: watchlist)')
    parser.add_argument('--start', required=True, help='first bar close, ISO-8601 UTC')
    parser.add_argument('--end', required=True, help='last bar close, ISO-8601 UTC')
    parser.add_argument('--label-at', help='label as_of instant (default: end)')
    parser.add_argument('--dry-run', action='store_true', help='run the full pipeline in one transaction, then roll back')
    args = parser.parse_args(argv)
    coins = parse_symbols(args.coins) if args.coins else None
    try:
        start, end, label_at, _coin_list = validate_replay_args(
            start=_parse_instant(args.start),
            end=_parse_instant(args.end),
            label_at=_parse_instant(args.label_at) if args.label_at else None,
            coins=coins,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    report = run_replay(
        get_sync_session_factory(),
        coins=coins or get_settings().watchlist_symbols,
        start=start,
        end=end,
        label_at=label_at,
        dry_run=args.dry_run,
    )
    print(report.format_summary())
    return 0


if __name__ == '__main__':
    sys.exit(main())
