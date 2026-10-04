"""One-shot feed loads for replay feature assembly.

Replay walks thousands of bars per coin. Calling :func:`app.worker.feature_job.load_inputs`
each time re-queries the same candles, OI and funding through ``as_of(bar_close)``. This
module loads each coin's feeds once through ``as_of(range_end)`` (the scan test still
requires gated SQL here; nothing reads feeds without ``as_of``), then builds each bar's
:class:`~app.core.features.assemble.FeatureInputs` in memory with the same window cuts and
``available_at <= bar_close`` rule that ``read_bars`` / ``visible_*`` apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.features import params as P
from app.core.features.assemble import FeatureInputs
from app.core.time import ensure_utc
from app.models import enums as E
from app.models.as_of import as_of
from app.core.features.series import Bar, CalendarEvent, ForcePrint, FundingPrint, OIPoint
from app.models.feeds import Candle, EventCalendar, ForceOrder, Funding, OpenInterest
from app.worker import feature_job as FJ

SPAN = timedelta(days=P.HISTORY_DAYS) + FJ.READ_PAD


@dataclass
class PreloadedCoinFeeds:
    """All feed series for one coin, loaded through ``as_of(gate_end)``."""

    coin: str
    gate_end: datetime
    range_start: datetime
    is_reference_coin: bool
    perp_15m: list[Bar]
    spot_15m: list[Bar]
    perp_1h: list[Bar]
    spot_1h: list[Bar]
    perp_4h: list[Bar]
    btc_perp_15m: list[Bar] | None
    oi_1h: list[OIPoint]
    funding_settled: list[FundingPrint]
    force_orders: list[ForcePrint]
    calendar_events: list[CalendarEvent]
    calendar_available_at: list[datetime]


def _bars_for_bar(all_bars: list[Bar], bar_close: datetime) -> list[Bar]:
    lo = bar_close - SPAN
    return [b for b in all_bars if b.available_at <= bar_close and b.open_time >= lo]


def _oi_for_bar(all_points: list[OIPoint], bar_close: datetime) -> list[OIPoint]:
    lo = bar_close - SPAN
    return [p for p in all_points if p.available_at <= bar_close and p.poll_time >= lo]


def _funding_for_bar(all_prints: list[FundingPrint], bar_close: datetime) -> list[FundingPrint]:
    lo = bar_close - SPAN
    return [p for p in all_prints if p.available_at <= bar_close and p.funding_time >= lo]


def _force_for_bar(all_prints: list[ForcePrint], bar_close: datetime) -> list[ForcePrint]:
    lo = bar_close - timedelta(hours=P.LIQUIDATION_LOOKBACK_HOURS)
    return [p for p in all_prints if p.available_at <= bar_close and p.traded_at > lo]


def _calendar_loaded(preloaded: PreloadedCoinFeeds, bar_close: datetime) -> bool:
    return any(a <= bar_close for a in preloaded.calendar_available_at)


def _calendar_for_bar(preloaded: PreloadedCoinFeeds, bar_close: datetime) -> tuple[list[CalendarEvent], bool]:
    if not _calendar_loaded(preloaded, bar_close):
        return [], False
    lo = bar_close - timedelta(days=1)
    hi = bar_close + timedelta(days=1)
    events = [
        e
        for e in preloaded.calendar_events
        if e.available_at <= bar_close and lo <= e.event_time <= hi
    ]
    return events, True


def preload_replay_feeds(
    session: Session,
    coin: str,
    *,
    range_start: datetime,
    range_end: datetime,
    include_btc_reference: bool | None = None,
) -> PreloadedCoinFeeds:
    """Load feeds for ``coin`` visible at ``range_end``, covering every bar from ``range_start`` through ``range_end``."""
    range_start = ensure_utc(range_start)
    gate_end = ensure_utc(range_end)
    is_reference = coin == FJ.REFERENCE_COIN
    if include_btc_reference is None:
        include_btc_reference = not is_reference
    open_min = range_start - SPAN
    force_min = range_start - timedelta(hours=P.LIQUIDATION_LOOKBACK_HOURS)
    cal_lo = range_start - timedelta(days=1)
    cal_hi = gate_end + timedelta(days=1)

    perp_15m = _read_bars_bulk(session, gate_end, coin, E.MARKET_PERP, '15m', open_min)
    spot_15m = _read_bars_bulk(session, gate_end, coin, E.MARKET_SPOT, '15m', open_min)
    perp_1h = _read_bars_bulk(session, gate_end, coin, E.MARKET_PERP, '1h', open_min)
    spot_1h = _read_bars_bulk(session, gate_end, coin, E.MARKET_SPOT, '1h', open_min)
    perp_4h = _read_bars_bulk(session, gate_end, coin, E.MARKET_PERP, '4h', open_min)
    btc_perp_15m = (
        None
        if not include_btc_reference
        else _read_bars_bulk(session, gate_end, FJ.REFERENCE_COIN, E.MARKET_PERP, '15m', open_min)
    )
    oi_1h = _read_oi_bulk(session, gate_end, coin, open_min)
    funding = _read_funding_bulk(session, gate_end, coin, open_min)
    force_orders = _read_force_bulk(session, gate_end, coin, force_min)
    calendar_events, calendar_available_at = _read_calendar_bulk(session, gate_end, cal_lo, cal_hi)

    return PreloadedCoinFeeds(
        coin=coin,
        gate_end=gate_end,
        range_start=range_start,
        is_reference_coin=is_reference,
        perp_15m=perp_15m,
        spot_15m=spot_15m,
        perp_1h=perp_1h,
        spot_1h=spot_1h,
        perp_4h=perp_4h,
        btc_perp_15m=btc_perp_15m,
        oi_1h=oi_1h,
        funding_settled=funding,
        force_orders=force_orders,
        calendar_events=calendar_events,
        calendar_available_at=calendar_available_at,
    )


def feature_inputs_from_preloaded(
    preloaded: PreloadedCoinFeeds,
    bar_close: datetime,
    horizon: str,
    source: str,
) -> FeatureInputs:
    """Same shape as :func:`app.worker.feature_job.load_inputs` for replay (no predicted funding)."""
    bar_close = ensure_utc(bar_close)
    events, loaded = _calendar_for_bar(preloaded, bar_close)
    return FeatureInputs(
        coin=preloaded.coin,
        bar_close=bar_close,
        horizon=horizon,
        source=source,
        perp_15m=_bars_for_bar(preloaded.perp_15m, bar_close),
        spot_15m=_bars_for_bar(preloaded.spot_15m, bar_close),
        perp_1h=_bars_for_bar(preloaded.perp_1h, bar_close),
        spot_1h=_bars_for_bar(preloaded.spot_1h, bar_close),
        perp_4h=_bars_for_bar(preloaded.perp_4h, bar_close),
        btc_perp_15m=None if preloaded.btc_perp_15m is None else _bars_for_bar(preloaded.btc_perp_15m, bar_close),
        is_reference_coin=preloaded.is_reference_coin,
        oi_1h=_oi_for_bar(preloaded.oi_1h, bar_close),
        funding_settled=_funding_for_bar(preloaded.funding_settled, bar_close),
        funding_predicted=None,
        force_orders=_force_for_bar(preloaded.force_orders, bar_close),
        events=events,
        calendar_loaded=loaded,
    )


def inputs_equal(a: FeatureInputs, b: FeatureInputs) -> bool:
    """Every ``FeatureInputs`` field equal, in order; a field added later is compared automatically."""
    return a == b


def _read_bars_bulk(session, gate_end: datetime, coin: str, market: str, timeframe: str, open_min: datetime) -> list[Bar]:
    statement = (
        as_of(gate_end)
        .select(Candle)
        .where(
            Candle.coin == coin,
            Candle.market == market,
            Candle.timeframe == timeframe,
            Candle.open_time >= open_min,
        )
        .order_by(Candle.open_time)
    )
    return [
        Bar(
            row.open_time,
            row.close_time,
            FJ._f(row.open_price),
            FJ._f(row.high_price),
            FJ._f(row.low_price),
            FJ._f(row.close_price),
            FJ._f(row.base_volume),
            FJ._f(row.taker_buy_base_volume),
            row.available_at,
        )
        for row in session.execute(statement).scalars().all()
    ]


def _read_oi_bulk(session, gate_end: datetime, coin: str, poll_min: datetime) -> list[OIPoint]:
    statement = (
        as_of(gate_end)
        .select(OpenInterest)
        .where(OpenInterest.coin == coin, OpenInterest.timeframe == P.OI_SERIES_TIMEFRAME, OpenInterest.poll_time >= poll_min)
        .order_by(OpenInterest.poll_time)
    )
    return [OIPoint(r.poll_time, r.available_at, FJ._f(r.open_interest_base)) for r in session.execute(statement).scalars().all()]


def _read_funding_bulk(session, gate_end: datetime, coin: str, funding_min: datetime) -> list[FundingPrint]:
    statement = (
        as_of(gate_end)
        .select(Funding)
        .where(Funding.coin == coin, Funding.kind == E.FUNDING_SETTLED, Funding.funding_time >= funding_min)
        .order_by(Funding.funding_time)
    )
    return [
        FundingPrint(r.funding_time, r.available_at, FJ._f(r.funding_rate), r.interval_hours)
        for r in session.execute(statement).scalars().all()
    ]


def _read_force_bulk(session, gate_end: datetime, coin: str, traded_min: datetime) -> list[ForcePrint]:
    statement = (
        as_of(gate_end)
        .select(ForceOrder)
        .where(ForceOrder.coin == coin, ForceOrder.traded_at > traded_min)
        .order_by(ForceOrder.traded_at, ForceOrder.id)
    )
    return [
        ForcePrint(r.traded_at, r.available_at, r.side, FJ._f(r.average_price if r.average_price is not None else r.price), FJ._f(r.original_quantity))
        for r in session.execute(statement).scalars().all()
    ]


def _read_calendar_bulk(session, gate_end: datetime, lo: datetime, hi: datetime) -> tuple[list[CalendarEvent], list[datetime]]:
    reader = as_of(gate_end)
    all_visible = session.execute(reader.select(EventCalendar.available_at)).scalars().all()
    if not all_visible:
        return [], []
    statement = reader.select(EventCalendar).where(EventCalendar.event_time >= lo, EventCalendar.event_time <= hi)
    rows = session.execute(statement.order_by(EventCalendar.event_time, EventCalendar.id)).scalars().all()
    return [CalendarEvent(r.kind, r.event_time, r.available_at, r.is_large) for r in rows], list(all_visible)
