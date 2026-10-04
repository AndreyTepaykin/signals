"""The naive swing-break baseline: the published rule traded as written, with none of the app's machinery.

What it is
----------
Every ``swing_break_event`` on a 4h break (the feature rows, both sources, every horizon) is traded in the direction of
``swing_break_direction`` with the chandelier stop, and nothing else: no direction vote, no gates (trend family, CVD,
stand-downs, the alt residual gate), no options layer, no watch / shadow statuses. It is resolved by the labeler's own pure
core (:mod:`app.core.labeler.outcome`): fill at the next 15m perp open, ``no_entry`` when that open is at or through the
stop, +1R upper barrier (no cap), the stop, the horizon's vertical barrier, ambiguous bar -> stop, 12 bps round trip and
realized funding, the 8h+5min completeness rule. Parity with an app candidate that kept the unmodified barriers is
asserted by a test (same label, field for field).

Independence (so the baseline is held to the same standard as the app)
----------------------------------------------------------------------
One decision per coin per horizon: while a taken trade's frozen hold ``[bar_close, bar_close + horizon)`` is open, a later
event on that coin and horizon is *skipped* (not traded, not counted as an outcome). The hold is the frozen window, never
the actual resolution, as in the app. Taken trades get ``sample_group_id`` from the app's own ``assign_sample_group``
(overlap / alt-follows-BTC-impulse), so the independent sample is the same first-of-group rule. BTC is processed first on
every bar, as the rules job does.

An event whose chandelier stop is unusable (``stop_plan`` is ``None``: stop breached, ATR or price unusable) has no rule to
trade and is skipped and counted (``no_stop``).

Cohorts
-------
``BaselineKey(source, coin_set, lookback)``: ``alts`` (coins with no Deribit book) or ``covered`` (BTC / ETH). The baseline
never touches the options layer, so it has no options path; an app cohort is compared with the baseline of its coin set
(``no_options_book`` -> alts; every other path -> covered). The thin / full lookback split uses the same cutoff.

Caveats stated, not hidden: feature rows are not rule-hash tagged (the baseline reads every row the engine wrote); events
whose entry bar, bars or funding are not yet visible are *pending* and excluded (counted).
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from app.core import constants as C
from app.core.labeler import barriers as B
from app.core.labeler import outcome as O
from app.core.rules.inputs import RuleInputs
from app.core.rules.sample_group import PriorCandidate, assign_sample_group, has_open_signal
from app.core.rules.stops import stop_plan
from app.core.stats.observations import (
    BaselineKey,
    BaselineObservation,
    classify_lookback,
    coin_set_for_coin,
)
from app.core.time import ensure_utc
from app.models import enums as E

SKIP_NO_STOP = 'no_stop'
SKIP_HOLD_OPEN = 'hold_open'


@dataclass(frozen=True)
class NaiveEvent:
    """One 4h swing break the published rule would trade."""

    feature_row_id: int
    coin: str
    horizon: str
    source: str
    bar_close: datetime
    direction: str
    stop_price: Decimal | None  # None when stop_plan() cannot read a safe stop
    trend_family: str | None = None
    vol_pct: float | None = None
    session: str | None = None
    event_flag: bool | None = None


@dataclass(frozen=True)
class NaiveTrade:
    event: NaiveEvent
    trade_id: int
    sample_group_id: uuid.UUID


class MarketReader(Protocol):
    """Where the bars and the settled funding come from; the stats job reads them through the ``as_of`` gate."""

    def bars(self, coin: str, start: datetime, end: datetime) -> Sequence[B.Bar]: ...

    def prints(self, coin: str, start: datetime, end: datetime) -> Sequence[B.FundingPrint]: ...


@dataclass
class BaselineStats:
    """What happened to the events of one cohort. ``events`` = every event; the rest partition it (plus ``pending``)."""

    events: int = 0
    no_stop: int = 0
    skipped_hold_open: int = 0
    taken: int = 0
    pending: int = 0
    no_entry: int = 0
    filled: int = 0


@dataclass(frozen=True)
class BaselineData:
    observations: tuple[BaselineObservation, ...] = ()
    stats: dict[BaselineKey, BaselineStats] = field(default_factory=dict)

    def for_key(self, key: BaselineKey, horizon: str | None = None) -> list[BaselineObservation]:
        return [o for o in self.observations if o.cohort == key and (horizon is None or o.horizon == horizon)]


def key_of(event: NaiveEvent) -> BaselineKey:
    return BaselineKey(event.source, coin_set_for_coin(event.coin), classify_lookback(event.bar_close))


def event_from_feature_row(row: Any, *, reference_coin: str) -> NaiveEvent | None:
    """A naive event for a feature row, or ``None`` when the row is not a swing-break event with a direction."""
    if row.swing_break_event is not True or row.swing_break_direction is None:
        return None
    inputs = RuleInputs.from_feature_row(row, reference_coin=reference_coin)
    plan = stop_plan(inputs, row.swing_break_direction)
    vol = row.realized_vol_pct
    return NaiveEvent(
        feature_row_id=row.id,
        coin=row.coin,
        horizon=row.horizon,
        source=row.source,
        bar_close=ensure_utc(row.bar_close),
        direction=row.swing_break_direction,
        stop_price=None if plan is None else plan.stop_price,
        trend_family=row.trend_family,
        vol_pct=None if vol is None else float(vol),
        session=row.session,
        event_flag=row.event_flag,
    )


def select_trades(
    events: Sequence[NaiveEvent], *, reference_coin: str
) -> tuple[list[NaiveTrade], list[tuple[NaiveEvent, str]]]:
    """Apply "one decision per coin per horizon" on the frozen hold and assign sample groups. Pure and deterministic.

    Returns ``(taken_trades, skipped)`` where ``skipped`` is ``(event, SKIP_*)``.
    """
    ordered = sorted(events, key=lambda e: (e.source, e.horizon, e.bar_close, e.coin != reference_coin, e.coin))
    priors: dict[tuple[str, str], list[PriorCandidate]] = {}
    trades: list[NaiveTrade] = []
    skipped: list[tuple[NaiveEvent, str]] = []
    for event in ordered:
        if event.stop_price is None:
            skipped.append((event, SKIP_NO_STOP))
            continue
        book = priors.setdefault((event.source, event.horizon), [])
        if has_open_signal(book, event.coin, event.horizon, event.bar_close):
            skipped.append((event, SKIP_HOLD_OPEN))
            continue
        assignment = assign_sample_group(
            coin=event.coin,
            horizon=event.horizon,
            source=event.source,
            bar_close=event.bar_close,
            direction=event.direction,
            priors=book,
            reference_coin=reference_coin,
        )
        trade_id = len(trades) + 1
        trades.append(NaiveTrade(event, trade_id, assignment.sample_group_id))
        book.append(
            PriorCandidate(
                id=trade_id,
                coin=event.coin,
                horizon=event.horizon,
                bar_close=event.bar_close,
                decision_time=event.bar_close,
                direction=event.direction,
                status=E.STATUS_ALERT,
                status_reason=None,
                sample_group_id=assignment.sample_group_id,
            )
        )
    return trades, skipped


def resolve_trade(
    trade: NaiveTrade,
    reader: MarketReader,
    *,
    as_of_time: datetime,
    funding_max_gap: timedelta,
    missing_bar_after: timedelta,
) -> BaselineObservation | O.NoEntry | O.Waiting:
    """Fill and resolve one naive trade with the labeler's pure core."""
    event = trade.event
    fill_time = event.bar_close
    assert event.stop_price is not None
    entry_bars = reader.bars(event.coin, fill_time, fill_time + B.BAR_LENGTH)
    planned = O.plan_fill(
        direction=event.direction, stop=event.stop_price, cap=None, fill_time=fill_time, horizon=event.horizon, entry_bars=entry_bars
    )
    if isinstance(planned, str):
        return O.Waiting(planned)
    if isinstance(planned, O.NoEntry):
        return planned
    bars = reader.bars(event.coin, fill_time, planned.deadline)
    outcome = O.resolve_outcome(
        direction=event.direction,
        barriers=planned,
        bars=bars,
        load_prints=lambda start, end: reader.prints(event.coin, start, end),
        fill_time=fill_time,
        as_of_time=as_of_time,
        funding_max_gap=funding_max_gap,
        missing_bar_after=missing_bar_after,
    )
    if isinstance(outcome, O.Waiting):
        return outcome
    return _observation(trade, outcome, planned)


def _observation(trade: NaiveTrade, outcome: O.Outcome, barriers: B.Barriers) -> BaselineObservation:
    event, resolution = trade.event, outcome.resolution
    fees = B.fees_r(barriers)
    stored = B.stored_r(resolution.price_r, fees, outcome.funding_r)
    return _make(
        trade,
        resolved_at=resolution.resolved_at,
        first_barrier=resolution.first_barrier,
        stored_r=float(stored),
        funding_r=float(outcome.funding_r),
        barrier_ambiguous=resolution.ambiguous,
        funding_partial=outcome.funding_partial,
        gapped_stop=resolution.gapped_stop,
        mae_r=float(resolution.mae_r),
        hours_to_resolution=float(B.hours_between(event.bar_close, resolution.resolved_at)),
        fees_r=float(fees),
    )


def _make(trade: NaiveTrade, **outcome: Any) -> BaselineObservation:
    event = trade.event
    values: dict[str, Any] = dict(
        candidate_id=trade.trade_id,
        sample_group_id=str(trade.sample_group_id),
        decision_time=event.bar_close,
        bar_close=event.bar_close,
        coin=event.coin,
        horizon=event.horizon,
        source=event.source,
        status=E.STATUS_ALERT,  # the baseline takes every trade: there is no watch / shadow
        status_reason=None,
        trigger=C.TRIGGER_SWING_BREAK,
        setup_family=C.SETUP_FAMILY_BREAKOUT,
        trend_family=event.trend_family,
        vol_pct=event.vol_pct,
        session=event.session,
        event_flag=event.event_flag,
        p_upper=None,
        options_verdict=None,
        options_snapshot_time=None,
        barriers_unmodified=True,
    )
    values.update(outcome)
    return BaselineObservation(**values)


def run_naive_baseline(
    events: Sequence[NaiveEvent],
    reader: MarketReader,
    *,
    reference_coin: str,
    as_of_time: datetime,
    funding_max_gap: timedelta,
    missing_bar_after: timedelta,
) -> BaselineData:
    """Select, fill and resolve every naive trade. Deterministic for a given set of events and visible market data."""
    as_of_time = ensure_utc(as_of_time)
    trades, skipped = select_trades(events, reference_coin=reference_coin)
    stats: dict[BaselineKey, BaselineStats] = {}
    counts = Counter(key_of(e) for e in events)
    for key, n in counts.items():
        stats.setdefault(key, BaselineStats()).events = n
    for event, why in skipped:
        s = stats.setdefault(key_of(event), BaselineStats())
        if why == SKIP_NO_STOP:
            s.no_stop += 1
        else:
            s.skipped_hold_open += 1
    observations: list[BaselineObservation] = []
    for trade in trades:
        s = stats.setdefault(key_of(trade.event), BaselineStats())
        s.taken += 1
        result = resolve_trade(
            trade, reader, as_of_time=as_of_time, funding_max_gap=funding_max_gap, missing_bar_after=missing_bar_after
        )
        if isinstance(result, O.Waiting):
            s.pending += 1
        elif isinstance(result, O.NoEntry):
            s.no_entry += 1
            observations.append(
                _make(
                    trade,
                    resolved_at=trade.event.bar_close,
                    first_barrier=E.BARRIER_NO_ENTRY,
                    stored_r=None,
                    funding_r=None,
                    barrier_ambiguous=False,
                    funding_partial=False,
                    gapped_stop=False,
                )
            )
        else:
            s.filled += 1
            observations.append(result)
    return BaselineData(tuple(observations), stats)
