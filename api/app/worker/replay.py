"""The replay harness: walk 15m bar closes and run the same pipeline as live, through ``as_of``.

For each bar close from ``start`` through ``end`` (inclusive, on the 15m grid):

1. **Features** — :func:`app.worker.feature_job.assemble_feature_rows` with ``source='replay'`` and every feed read
   gated at ``as_of(bar_close)``. Replay loads feeds once per coin via
   :func:`app.worker.replay_feeds.preload_replay_feeds` (SQL still uses ``as_of(range_end)``) and builds each bar's
   inputs in memory with the same visibility windows as :func:`app.worker.feature_job.load_inputs`.
2. **Rules** — :func:`app.worker.rules_job.run_rules_for_bar` with ``source='replay'``, ``decision_time=bar_close``,
   and no feed-health breaches (replay has no honest breach history).
3. **Options** — :func:`app.worker.options_job.run_options_job` for ``source='replay'`` after each bar.

After the walk, **labels** — :func:`app.worker.label_job.run_label_job` with ``as_of_time=label_at`` (default
``end``), never the wall clock.

Real runs (non-dry) use three phases:

* **Phase A** — feature rows for every (coin, bar), parallel by coin (``--workers N``, default ``min(7, len(coins))``).
  Each worker owns a DB session, preloads once, commits every ``FEATURE_COMMIT_CHUNK`` bars. Rows are idempotent
  (existing keys are skipped).
* **Phase B** — walk bars in time order; rules and options per bar (reference coin first), single process, commit
  after each bar.
* **Phase C** — one label pass.

Dry run
-------
``dry_run=True`` runs phases A–C in a **single process** with preloading, inside one database transaction: real
writes, no intermediate commits, rollback at the end. The :class:`ReplayReport` matches what a committed run would
produce.

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
``python -m app.worker.replay --coins A,B --start ISO --end ISO [--label-at ISO] [--workers N] [--dry-run]``
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings, parse_symbols
from app.core import constants as C
from app.core.db_sync import create_utc_engine, get_sync_database_url, get_sync_session_factory
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
from app.worker import replay_feeds as RF
from app.worker import rules_job as RJ

log = logging.getLogger(__name__)

JOB_NAME = 'replay'
FEATURE_COMMIT_CHUNK = 96


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


@dataclass
class _CoinFeatureTotals:
    written: int = 0
    already_present: int = 0
    skipped_not_ready: int = 0


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


def default_workers(coin_count: int) -> int:
    return min(7, max(1, coin_count))


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


def _accumulate_feature_report(report: ReplayReport, feat: FJ.AssemblyReport) -> None:
    report.features_written += feat.written
    report.features_already_present += feat.already_present
    if feat.skipped_not_ready:
        report.features_skipped_not_ready += 1


def _build_preload_map(session: Session, coin_list: Sequence[str], bars: Sequence[datetime]) -> dict[str, RF.PreloadedCoinFeeds]:
    if not bars:
        return {}
    range_start, range_end = bars[0], bars[-1]
    return {coin: RF.preload_replay_feeds(session, coin, range_start=range_start, range_end=range_end) for coin in coin_list}


def _features_one_bar(
    session: Session,
    report: ReplayReport,
    *,
    bar_close: datetime,
    coin_list: Sequence[str],
    version: str,
    policy: HistoryPolicy,
    preload_map: Mapping[str, RF.PreloadedCoinFeeds],
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
            preloaded=preload_map.get(coin),
        )
        _accumulate_feature_report(report, feat)


def _rules_options_one_bar(
    session: Session,
    report: ReplayReport,
    *,
    bar_close: datetime,
    coin_list: Sequence[str],
    version: str,
) -> None:
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


def _phase_a_features_session(
    session: Session,
    report: ReplayReport,
    *,
    bars: Sequence[datetime],
    coin_list: Sequence[str],
    version: str,
    policy: HistoryPolicy,
    preload_map: Mapping[str, RF.PreloadedCoinFeeds],
    commit: bool,
) -> None:
    for index, bar_close in enumerate(bars):
        _features_one_bar(
            session,
            report,
            bar_close=bar_close,
            coin_list=coin_list,
            version=version,
            policy=policy,
            preload_map=preload_map,
        )
        if commit and bars and (index + 1) % FEATURE_COMMIT_CHUNK == 0:
            session.commit()
    if commit and bars:
        session.commit()


def _phase_a_features_for_coin(
    session: Session,
    coin: str,
    bars: Sequence[datetime],
    *,
    version: str,
    policy: HistoryPolicy,
    commit: bool,
) -> _CoinFeatureTotals:
    totals = _CoinFeatureTotals()
    if not bars:
        return totals
    preloaded = RF.preload_replay_feeds(session, coin, range_start=bars[0], range_end=bars[-1])
    for index, bar_close in enumerate(bars):
        feat = FJ.assemble_feature_rows(
            session,
            coin,
            bar_close,
            source=E.SOURCE_REPLAY,
            now=bar_close,
            code_version=version,
            policy=policy,
            preloaded=preloaded,
        )
        totals.written += feat.written
        totals.already_present += feat.already_present
        if feat.skipped_not_ready:
            totals.skipped_not_ready += 1
        if commit and (index + 1) % FEATURE_COMMIT_CHUNK == 0:
            session.commit()
    if commit:
        session.commit()
    return totals


def schema_session_factory(search_path: str) -> sessionmaker[Session]:
    """Sessions on the configured database whose every connection uses ``search_path`` (survives commits)."""
    engine = create_utc_engine(get_sync_database_url(), poolclass=NullPool, future=True)

    @event.listens_for(engine, 'connect')
    def _pin_schema(dbapi_connection: Any, _record: Any) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{search_path}"')
        dbapi_connection.commit()

    return sessionmaker(bind=engine, expire_on_commit=False)


def _replay_features_worker(payload: tuple[Any, ...]) -> _CoinFeatureTotals:
    """Process-pool entry: one coin, all bar closes (ISO strings), code version, history policy, search_path.

    A spawned process cannot inherit the caller's session factory, so it opens the configured database and, when
    ``search_path`` is set, pins that schema (tests use a throwaway schema; production passes None).
    """
    coin, bars_iso, version, policy_fields = payload[:4]
    search_path = payload[4] if len(payload) > 4 else None
    bars = [ensure_utc(datetime.fromisoformat(t)) for t in bars_iso]
    policy = HistoryPolicy(*policy_fields)
    factory = get_sync_session_factory() if search_path is None else schema_session_factory(search_path)
    with factory() as session:
        return _phase_a_features_for_coin(
            session,
            coin,
            bars,
            version=version,
            policy=policy,
            commit=True,
        )


def _policy_fields(policy: HistoryPolicy) -> tuple[int, float, int]:
    return (policy.window_days, policy.min_span_days, policy.min_samples)


def _parallel_phase_a(
    report: ReplayReport,
    *,
    bars: Sequence[datetime],
    coin_list: Sequence[str],
    version: str,
    policy: HistoryPolicy,
    workers: int,
    factory: Callable[[], Session],
    search_path: str | None,
) -> None:
    if not bars or not coin_list:
        return
    bars_iso = tuple(b.isoformat() for b in bars)
    pf = _policy_fields(policy)
    payloads = [(coin, bars_iso, version, pf, search_path) for coin in coin_list]
    if workers <= 1:
        with factory() as session:
            for coin in coin_list:
                totals = _phase_a_features_for_coin(session, coin, bars, version=version, policy=policy, commit=True)
                report.features_written += totals.written
                report.features_already_present += totals.already_present
                report.features_skipped_not_ready += totals.skipped_not_ready
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_replay_features_worker, p): p[0] for p in payloads}
        for future in as_completed(futures):
            totals = future.result()
            report.features_written += totals.written
            report.features_already_present += totals.already_present
            report.features_skipped_not_ready += totals.skipped_not_ready


def _walk_dry_run(
    session: Session,
    report: ReplayReport,
    *,
    bars: Sequence[datetime],
    coin_list: Sequence[str],
    label: datetime,
    version: str,
    policy: HistoryPolicy,
) -> None:
    preload_map = _build_preload_map(session, coin_list, bars)
    for bar_close in bars:
        _features_one_bar(
            session,
            report,
            bar_close=bar_close,
            coin_list=coin_list,
            version=version,
            policy=policy,
            preload_map=preload_map,
        )
        _rules_options_one_bar(session, report, bar_close=bar_close, coin_list=coin_list, version=version)
        report.bars_walked += 1
    _run_label_pass(session, report, label=label, coin_list=coin_list, version=version)


def _phase_b_rules_options(
    session_or_factory: Session | sessionmaker[Session] | Callable[[], Session],
    report: ReplayReport,
    *,
    bars: Sequence[datetime],
    coin_list: Sequence[str],
    version: str,
    passed_session: Session | None,
) -> None:
    if passed_session is not None:
        for bar_close in bars:
            _rules_options_one_bar(passed_session, report, bar_close=bar_close, coin_list=coin_list, version=version)
            report.bars_walked += 1
            passed_session.commit()
        return
    factory = session_or_factory if isinstance(session_or_factory, sessionmaker) else session_or_factory
    for bar_close in bars:
        with factory() as session:
            _rules_options_one_bar(session, report, bar_close=bar_close, coin_list=coin_list, version=version)
            session.commit()
        report.bars_walked += 1


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
    workers: int | None = None,
    search_path: str | None = None,
) -> ReplayReport:
    """Walk ``start``..``end`` and run features, rules, options, then label at ``label_at``.

    Real runs use phased features (optional process pool), then rules/options per bar, then labels.
    ``dry_run`` uses one transaction and rolls back at the end (single process, preloaded feeds).
    Every session comes from ``session_or_factory`` except the pool workers', which open the configured
    database; ``search_path`` must then name the schema the factory's sessions use (None = the default).
    """
    start, end, label, coin_list = validate_replay_args(start=start, end=end, label_at=label_at, coins=coins)
    bars = iter_bar_closes(start, end)
    version = code_version if code_version is not None else current_code_version()
    policy = history_policy if history_policy is not None else PRODUCTION_POLICY
    worker_count = workers if workers is not None else default_workers(len(coin_list))
    if worker_count < 1:
        raise ValueError('workers must be at least 1')
    report = ReplayReport(start=start, end=end, label_at=label, dry_run=dry_run, rule_hash=current_rule_hash())
    t0 = time.perf_counter()

    passed_session = session_or_factory if isinstance(session_or_factory, Session) else None

    if dry_run and passed_session is not None:
        nested = passed_session.begin_nested()
        try:
            _walk_dry_run(
                passed_session,
                report,
                bars=bars,
                coin_list=coin_list,
                label=label,
                version=version,
                policy=policy,
            )
        finally:
            nested.rollback()
    elif dry_run:
        factory = session_or_factory if isinstance(session_or_factory, sessionmaker) else session_or_factory
        with factory() as session:
            _walk_dry_run(session, report, bars=bars, coin_list=coin_list, label=label, version=version, policy=policy)
            session.rollback()
    elif passed_session is not None:
        preload_map = _build_preload_map(passed_session, coin_list, bars)
        _phase_a_features_session(
            passed_session,
            report,
            bars=bars,
            coin_list=coin_list,
            version=version,
            policy=policy,
            preload_map=preload_map,
            commit=True,
        )
        for bar_close in bars:
            _rules_options_one_bar(passed_session, report, bar_close=bar_close, coin_list=coin_list, version=version)
            report.bars_walked += 1
            passed_session.commit()
        _run_label_pass(passed_session, report, label=label, coin_list=coin_list, version=version)
        passed_session.commit()
    else:
        _parallel_phase_a(
            report,
            bars=bars,
            coin_list=coin_list,
            version=version,
            policy=policy,
            workers=worker_count,
            factory=session_or_factory,
            search_path=search_path,
        )
        _phase_b_rules_options(
            session_or_factory,
            report,
            bars=bars,
            coin_list=coin_list,
            version=version,
            passed_session=None,
        )
        factory = session_or_factory if isinstance(session_or_factory, sessionmaker) else session_or_factory
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
    parser.add_argument(
        '--workers',
        type=int,
        default=None,
        help='feature phase process pool size (default min(7, coins); ignored for --dry-run)',
    )
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
        workers=args.workers,
    )
    print(report.format_summary())
    return 0


if __name__ == '__main__':
    sys.exit(main())
