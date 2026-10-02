"""The feature assembly job: read feeds through ``as_of``, compute, write ``feature_row``.

All the arithmetic is in ``app.core.features`` and is pure. This module does the
I/O and nothing else:

1. **Read** every input through ``app.models.as_of.as_of(bar_close)``. There is
   no other read path for feeds (a source scan test enforces it), so a print
   published after the signal bar cannot reach a feature. The one input read at a
   different instant is the live carry band's predicted funding rate, explained
   below.
2. **Compute** with :func:`app.core.features.assemble.compute_features`.
3. **Write** one ``feature_row`` per horizon, idempotently.

Live carry band
---------------
The predicted-funding feed stores only its latest estimate, refreshed every
minute, and a bar is written to the database ~30 s after its close. A job that
runs just after the close therefore usually finds the estimate stamped AFTER
``bar_close``, and reading it ``as_of(bar_close)`` would leave the carry band NULL
more often than not. The live job passes ``observed_at`` (the wall clock) for
that single read. Nothing else uses it, and a ``source='replay'`` row never reads
predicted funding at all (it is not point-in-time).

Readiness
---------
A kline can be missing from the database for ~30 s after its close. Computing
then would key a row on the new ``bar_close`` while every statistic sat on the
PREVIOUS bar, which is a plausible-looking wrong row. So a live run first checks
that the bars which must exist at ``bar_close`` are present, and if not it raises
:class:`InputsNotReady` and writes nothing; the caller retries. Past
``READINESS_GRACE`` it writes anyway, with NULLs where the input is genuinely
absent, so a permanent hole does not block the series forever.

Rows are immutable. If a row for the same key already exists the job does not
touch it; if its ``feature_hash`` differs from what would be computed now it is
counted and logged (the data under it changed, or the engine is not
deterministic - either is a finding).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import null as sa_null
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core import constants as C
from app.core.features import params as P
from app.core.features.assemble import FeatureComputation, FeatureInputs, compute_features
from app.core.features.columns import FEATURE_COLUMNS
from app.core.features.params import PRODUCTION_POLICY, HistoryPolicy, timeframe_delta
from app.core.features.series import (
    Bar,
    CalendarEvent,
    ForcePrint,
    FundingPrint,
    OIPoint,
    floor_to,
    visible_bars,
)
from app.core.hashing import code_version as current_code_version
from app.core.time import ensure_utc, utc_now
from app.models import enums as E
from app.models.as_of import as_of
from app.models.feeds import Candle, EventCalendar, ForceOrder, Funding, OpenInterest
from app.models.features import FeatureRow

log = logging.getLogger(__name__)

REFERENCE_COIN = 'BTCUSDT'
#: How much extra history to read beyond the 90-day window (rolling windows
#: need their own warm-up behind the oldest sample).
READ_PAD = timedelta(days=3)
#: After this long past the bar close a live run proceeds with NULLs rather than waiting.
READINESS_GRACE = timedelta(minutes=10)
#: A bar is written to the database this long after its close (ingest settle).
LIVE_SETTLE = timedelta(seconds=30)

UNIQUE_KEY = 'uq_feature_row_series_bar_close'


class InputsNotReady(RuntimeError):
    """Bars that must exist at ``bar_close`` are not in the database yet."""

    def __init__(self, missing: Sequence[str]) -> None:
        super().__init__('inputs not ready: ' + ', '.join(missing))
        self.missing = list(missing)


# ---------------------------------------------------------------------------
# Reading (every feed read goes through as_of)
# ---------------------------------------------------------------------------
def _f(value: Decimal | float | int | None) -> float:
    return float(value)  # type: ignore[arg-type]


def read_bars(session: Session, coin: str, market: str, timeframe: str, bar_close: datetime, span: timedelta) -> list[Bar]:
    reader = as_of(bar_close)
    statement = (
        reader.select(Candle)
        .where(
            Candle.coin == coin,
            Candle.market == market,
            Candle.timeframe == timeframe,
            Candle.open_time >= bar_close - span,
        )
        .order_by(Candle.open_time)
    )
    return [
        Bar(
            row.open_time,
            row.close_time,
            _f(row.open_price),
            _f(row.high_price),
            _f(row.low_price),
            _f(row.close_price),
            _f(row.base_volume),
            _f(row.taker_buy_base_volume),
            row.available_at,
        )
        for row in session.execute(statement).scalars().all()
    ]


def read_open_interest(session: Session, coin: str, timeframe: str, bar_close: datetime, span: timedelta) -> list[OIPoint]:
    statement = (
        as_of(bar_close)
        .select(OpenInterest)
        .where(OpenInterest.coin == coin, OpenInterest.timeframe == timeframe, OpenInterest.poll_time >= bar_close - span)
        .order_by(OpenInterest.poll_time)
    )
    return [OIPoint(r.poll_time, r.available_at, _f(r.open_interest_base)) for r in session.execute(statement).scalars().all()]


def read_settled_funding(session: Session, coin: str, bar_close: datetime, span: timedelta) -> list[FundingPrint]:
    statement = (
        as_of(bar_close)
        .select(Funding)
        .where(Funding.coin == coin, Funding.kind == E.FUNDING_SETTLED, Funding.funding_time >= bar_close - span)
        .order_by(Funding.funding_time)
    )
    return [
        FundingPrint(r.funding_time, r.available_at, _f(r.funding_rate), r.interval_hours)
        for r in session.execute(statement).scalars().all()
    ]


def read_predicted_funding(session: Session, coin: str, observed_at: datetime) -> FundingPrint | None:
    """The newest predicted estimate visible at ``observed_at`` (live carry band only)."""
    row = as_of(observed_at).latest(session, Funding, coin=coin, kind=E.FUNDING_PREDICTED)
    if row is None:
        return None
    return FundingPrint(row.funding_time, row.available_at, _f(row.funding_rate), row.interval_hours)


def read_force_orders(session: Session, coin: str, bar_close: datetime) -> list[ForcePrint]:
    statement = (
        as_of(bar_close)
        .select(ForceOrder)
        .where(ForceOrder.coin == coin, ForceOrder.traded_at > bar_close - timedelta(hours=P.LIQUIDATION_LOOKBACK_HOURS))
        .order_by(ForceOrder.traded_at, ForceOrder.id)
    )
    return [
        ForcePrint(r.traded_at, r.available_at, r.side, _f(r.average_price if r.average_price is not None else r.price), _f(r.original_quantity))
        for r in session.execute(statement).scalars().all()
    ]


def read_calendar(session: Session, bar_close: datetime) -> tuple[list[CalendarEvent], bool]:
    """Events within a day of the bar, and whether the calendar holds any visible row at all."""
    reader = as_of(bar_close)
    loaded = session.execute(reader.select(EventCalendar.id).limit(1)).first() is not None
    if not loaded:
        return [], False
    statement = reader.select(EventCalendar).where(
        EventCalendar.event_time >= bar_close - timedelta(days=1),
        EventCalendar.event_time <= bar_close + timedelta(days=1),
    )
    rows = session.execute(statement.order_by(EventCalendar.event_time, EventCalendar.id)).scalars().all()
    return [CalendarEvent(r.kind, r.event_time, r.available_at, r.is_large) for r in rows], True


def load_inputs(
    session: Session,
    coin: str,
    bar_close: datetime,
    horizon: str,
    source: str,
    *,
    observed_at: datetime | None = None,
) -> FeatureInputs:
    """Read every input for one coin at one signal bar, through ``as_of(bar_close)``."""
    bar_close = ensure_utc(bar_close)
    span = timedelta(days=P.HISTORY_DAYS) + READ_PAD
    is_reference = coin == REFERENCE_COIN
    events, loaded = read_calendar(session, bar_close)
    predicted = None
    if source == E.SOURCE_LIVE:
        predicted = read_predicted_funding(session, coin, ensure_utc(observed_at) if observed_at is not None else bar_close)
    return FeatureInputs(
        coin=coin,
        bar_close=bar_close,
        horizon=horizon,
        source=source,
        perp_15m=read_bars(session, coin, E.MARKET_PERP, '15m', bar_close, span),
        spot_15m=read_bars(session, coin, E.MARKET_SPOT, '15m', bar_close, span),
        perp_1h=read_bars(session, coin, E.MARKET_PERP, '1h', bar_close, span),
        spot_1h=read_bars(session, coin, E.MARKET_SPOT, '1h', bar_close, span),
        perp_4h=read_bars(session, coin, E.MARKET_PERP, '4h', bar_close, span),
        btc_perp_15m=None if is_reference else read_bars(session, REFERENCE_COIN, E.MARKET_PERP, '15m', bar_close, span),
        is_reference_coin=is_reference,
        oi_1h=read_open_interest(session, coin, P.OI_SERIES_TIMEFRAME, bar_close, span),
        funding_settled=read_settled_funding(session, coin, bar_close, span),
        funding_predicted=predicted,
        force_orders=read_force_orders(session, coin, bar_close),
        events=events,
        calendar_loaded=loaded,
    )


def missing_required_bars(inputs: FeatureInputs) -> list[str]:
    """Bars that should exist at ``bar_close`` but are not visible."""
    t = inputs.bar_close
    wanted = (
        ('perp 15m', inputs.perp_15m, P.SIGNAL_TIMEFRAME),
        ('spot 15m', inputs.spot_15m, P.SIGNAL_TIMEFRAME),
        ('perp 1h', inputs.perp_1h, '1h'),
        ('spot 1h', inputs.spot_1h, '1h'),
        ('perp 4h', inputs.perp_4h, P.ATR_TIMEFRAME),
    )
    absent = []
    for label, bars, timeframe in wanted:
        expected = floor_to(t, timeframe_delta(timeframe))
        if not any(b.close_time == expected for b in visible_bars(bars, t)):
            absent.append(f'{label} bar closing {expected.isoformat()}')
    return absent


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def to_db_value(name: str, value: Any) -> Any:
    """Python value -> the value handed to SQLAlchemy. Floats are already rounded to the column scale."""
    kind = FEATURE_COLUMNS[name].kind
    if value is None:
        # A JSONB column given Python None stores the JSON value ``null``, which is NOT SQL NULL
        # (and violates active_event_kinds_is_array). Missing must be SQL NULL.
        return sa_null() if kind in ('str_list', 'json_object') else None
    if kind == 'float':
        return Decimal(repr(value))
    return value


def row_values(
    coin: str,
    horizon: str,
    bar_close: datetime,
    source: str,
    computation: FeatureComputation,
    code_version: str,
) -> dict[str, Any]:
    values = {name: to_db_value(name, value) for name, value in computation.values.items()}
    values.update(
        coin=coin,
        horizon=horizon,
        signal_timeframe=P.SIGNAL_TIMEFRAME,
        bar_close=bar_close,
        source=source,
        feature_hash=computation.feature_hash,
        code_version=code_version,
    )
    return values


@dataclass
class AssemblyReport:
    coin: str
    bar_close: datetime
    written: int = 0
    already_present: int = 0
    hash_mismatches: int = 0
    skipped_not_ready: list[str] = field(default_factory=list)
    null_reasons: dict[str, str] = field(default_factory=dict)


def assemble_feature_rows(
    session: Session,
    coin: str,
    bar_close: datetime,
    *,
    source: str = E.SOURCE_LIVE,
    horizons: Iterable[str] = C.HORIZONS,
    observed_at: datetime | None = None,
    now: datetime | None = None,
    code_version: str | None = None,
    policy: HistoryPolicy = PRODUCTION_POLICY,
    require_ready: bool | None = None,
) -> AssemblyReport:
    """Compute and store the feature rows (one per horizon) for ``coin`` at ``bar_close``.

    Raises :class:`InputsNotReady` (before writing anything) when running live,
    the bars that must exist are absent, and ``READINESS_GRACE`` has not passed.
    ``require_ready`` defaults to True for ``live`` and False for ``replay``.
    """
    bar_close = ensure_utc(bar_close)
    clock = ensure_utc(now) if now is not None else utc_now()
    if source not in E.SOURCES:
        raise ValueError(f'source must be one of {E.SOURCES}')
    version = code_version if code_version is not None else current_code_version()
    report = AssemblyReport(coin, bar_close)

    inputs_by_horizon: dict[str, FeatureInputs] = {}
    horizon_list = list(horizons)
    for horizon in horizon_list:
        if not inputs_by_horizon:
            inputs = load_inputs(session, coin, bar_close, horizon, source, observed_at=observed_at)
        else:
            inputs = replace(next(iter(inputs_by_horizon.values())), horizon=horizon)
        inputs_by_horizon[horizon] = inputs

    first_inputs = next(iter(inputs_by_horizon.values()))
    strict = (source == E.SOURCE_LIVE) if require_ready is None else require_ready
    absent = missing_required_bars(first_inputs)
    if absent:
        report.skipped_not_ready = absent
        if strict and clock - bar_close < READINESS_GRACE:
            raise InputsNotReady(absent)
        log.warning('%s %s: writing with absent inputs: %s', coin, bar_close.isoformat(), '; '.join(absent))

    for horizon, inputs in inputs_by_horizon.items():
        computation = compute_features(inputs, policy)
        report.null_reasons = {**computation.reasons}
        values = row_values(coin, horizon, bar_close, source, computation, version)
        inserted = session.execute(
            pg_insert(FeatureRow).values(**values).on_conflict_do_nothing(constraint=UNIQUE_KEY).returning(FeatureRow.id)
        ).first()
        if inserted is not None:
            report.written += 1
            continue
        report.already_present += 1
        existing = session.execute(
            sa_select(FeatureRow.feature_hash).where(
                FeatureRow.coin == coin,
                FeatureRow.horizon == horizon,
                FeatureRow.signal_timeframe == P.SIGNAL_TIMEFRAME,
                FeatureRow.bar_close == bar_close,
                FeatureRow.source == source,
            )
        ).scalar_one()
        if existing != computation.feature_hash:
            report.hash_mismatches += 1
            log.error(
                'feature_row %s %s %s %s already exists with hash %s but the data now give %s',
                coin,
                horizon,
                bar_close.isoformat(),
                source,
                existing,
                computation.feature_hash,
            )
    return report


# ---------------------------------------------------------------------------
# Scheduled entry point and CLI
# ---------------------------------------------------------------------------
def latest_closed_bar(now: datetime) -> datetime:
    """Close of the most recent 15m bar that has been closed for at least the ingest settle time."""
    return floor_to(ensure_utc(now) - LIVE_SETTLE, timeframe_delta(P.SIGNAL_TIMEFRAME))


def run_feature_job(session: Session, coins: Sequence[str], *, now: datetime | None = None) -> dict[str, Any]:
    """One scheduled tick: the newest closed 15m bar for every coin. Returns a summary."""
    clock = ensure_utc(now) if now is not None else utc_now()
    bar_close = latest_closed_bar(clock)
    version = current_code_version()
    summary: dict[str, Any] = {'bar_close': bar_close.isoformat(), 'written': 0, 'not_ready': {}, 'hash_mismatches': 0}
    for coin in coins:
        try:
            report = assemble_feature_rows(session, coin, bar_close, now=clock, observed_at=clock, code_version=version)
        except InputsNotReady as exc:
            summary['not_ready'][coin] = exc.missing
            continue
        summary['written'] += report.written
        summary['hash_mismatches'] += report.hash_mismatches
    return summary


def _parse_instant(text: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(text.replace('Z', '+00:00')))


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.worker.feature_job --coins BTCUSDT [--at ISO] [--source live|replay] [--dry-run]``."""
    from app.core.db_sync import get_sync_session_factory

    parser = argparse.ArgumentParser(description='Compute feature_row for the newest (or a given) 15m bar close.')
    parser.add_argument('--coins', default=REFERENCE_COIN, help='comma-separated symbols')
    parser.add_argument('--at', help='bar close as an ISO-8601 UTC instant (default: newest closed bar)')
    parser.add_argument('--source', choices=E.SOURCES, default=E.SOURCE_LIVE)
    parser.add_argument('--dry-run', action='store_true', help='compute and print, write nothing')
    args = parser.parse_args(argv)
    coins = [c.strip().upper() for c in args.coins.split(',') if c.strip()]
    bar_close = _parse_instant(args.at) if args.at else latest_closed_bar(utc_now())
    factory = get_sync_session_factory()
    with factory() as session:
        for coin in coins:
            if args.dry_run:
                inputs = load_inputs(session, coin, bar_close, C.HORIZON_SWING, args.source, observed_at=utc_now())
                result = compute_features(inputs)
                print(json.dumps({'coin': coin, 'bar_close': bar_close.isoformat(), 'hash': result.feature_hash,
                                  'values': result.values, 'null_reasons': result.reasons}, default=str, indent=2))
                continue
            report = assemble_feature_rows(session, coin, bar_close, source=args.source, observed_at=utc_now())
            session.commit()
            print(f'{coin} {bar_close.isoformat()}: written={report.written} present={report.already_present} '
                  f'mismatches={report.hash_mismatches}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
