"""The live signal pipeline (Milestone 10): label, then features, rules and options for the newest closed 15m bar.

One scheduled tick (``signal_pipeline`` in :mod:`app.worker.jobs`, every ``signal_pipeline_poll_seconds``):

1. **Labels** for live candidates at ``now``, first, so a decision taken in this tick sees every outcome that has
   resolved (the replay labels at each bar close before deciding, for the same reason).
2. **Features** for the newest closed bar, only for coins that do not have every horizon's row yet. A coin whose bars
   are not in the database is skipped (``InputsNotReady``) and retried next tick; past the feature job's readiness
   grace it is written with NULLs.
3. **Rules** for the coins that have their rows. Alts read BTC's same-bar candidate (one BTC view, sample groups), so
   nothing is decided until BTC's rows exist. A coin already decided for the bar hits the unique key and is unchanged.
4. **Options** for every unchecked live candidate: verdict, pooled estimate, abstain rule.

Each step commits, so a failure later in the tick never discards labels or feature rows already written. A bar the
worker was down for is not decided afterwards: a live decision cannot be taken in the past (the replay covers history).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.features import params as P
from app.core.time import ensure_utc
from app.models import enums as E
from app.models.features import FeatureRow
from app.worker import feature_job as FJ
from app.worker import label_job as LJ
from app.worker import options_job as OJ
from app.worker import rules_job as RJ


def coins_with_rows(session: Session, coins: Sequence[str], bar_close: datetime) -> set[str]:
    """Coins that have a live feature row for every horizon at ``bar_close``."""
    rows = session.execute(
        select(FeatureRow.coin)
        .where(
            FeatureRow.coin.in_(list(coins)),
            FeatureRow.bar_close == bar_close,
            FeatureRow.source == E.SOURCE_LIVE,
            FeatureRow.signal_timeframe == P.SIGNAL_TIMEFRAME,
            FeatureRow.horizon.in_(C.HORIZONS),
        )
        .group_by(FeatureRow.coin)
        .having(func.count(func.distinct(FeatureRow.horizon)) == len(C.HORIZONS))
    ).scalars()
    return set(rows)


def run_signal_pipeline(session: Session, coins: Sequence[str], *, now: datetime, code_version: str) -> dict[str, Any]:
    """One live tick. Commits after each step. Returns a summary for ``job_run.detail``."""
    clock = ensure_utc(now)
    ordered = RJ.order_coins(coins)

    labels = LJ.summarize(LJ.run_label_job(session, clock, source=E.SOURCE_LIVE, code_version=code_version))
    session.commit()

    bar_close = FJ.latest_closed_bar(clock)
    present = coins_with_rows(session, ordered, bar_close)
    written = mismatches = 0
    not_ready: dict[str, list[str]] = {}
    for coin in ordered:
        if coin in present:
            continue
        try:
            report = FJ.assemble_feature_rows(
                session, coin, bar_close, now=clock, observed_at=clock, code_version=code_version
            )
        except FJ.InputsNotReady as exc:
            not_ready[coin] = exc.missing
            continue
        written += report.written
        mismatches += report.hash_mismatches
        session.commit()
    ready = [coin for coin in ordered if coin in coins_with_rows(session, ordered, bar_close)]

    rules: dict[str, Any] = {'deferred': None, 'created': 0, 'already_present': 0}
    if FJ.REFERENCE_COIN in ordered and FJ.REFERENCE_COIN not in ready:
        rules['deferred'] = f'{FJ.REFERENCE_COIN} has no feature rows for this bar yet'
    elif ready:
        decided = RJ.run_rules_for_bar(
            session, ready, bar_close, source=E.SOURCE_LIVE, now=clock, code_version=code_version
        )
        rules.update(created=decided.created, already_present=decided.count(RJ.RESULT_PRESENT))
        session.commit()

    options = OJ.run_options_job(session, source=E.SOURCE_LIVE)
    session.commit()
    statuses: dict[str, int] = {}
    for outcome in options.outcomes:
        if outcome.result == OJ.RESULT_CHECKED and outcome.status:
            statuses[outcome.status] = statuses.get(outcome.status, 0) + 1

    return {
        'bar_close': bar_close.isoformat(),
        'labels': labels,
        'features': {'written': written, 'not_ready': not_ready, 'hash_mismatches': mismatches, 'ready': len(ready)},
        'rules': rules,
        'options': {
            'checked': options.checked,
            'statuses': statuses,
            'estimate_errors': options.count(OJ.RESULT_ESTIMATE_ERROR),
            'price_mismatches': options.count(OJ.RESULT_PRICE_MISMATCH),
        },
    }
