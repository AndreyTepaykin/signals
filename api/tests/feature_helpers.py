"""Builders shared by the Milestone 3 tests.

The golden-fixture tests build tiny series BY HAND and compare with numbers worked
out from the definitions; they use :func:`bars_from_rows`. The causality,
determinism and NULL tests need a realistic 100-day market; :func:`synthetic_inputs`
builds one from a seeded ``random.Random`` (no wall clock, no global state), with
the 1h and 4h bars aggregated from the 15m bars so the three timeframes agree.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.core.features.assemble import FeatureInputs
from app.core.features.series import Bar, FundingPrint, OIPoint

UTC = timezone.utc
M15 = timedelta(minutes=15)
H1 = timedelta(hours=1)
H4 = timedelta(hours=4)


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def bars_from_rows(start: datetime, step: timedelta, rows: Sequence[tuple]) -> list[Bar]:
    """``rows`` are ``(open, high, low, close[, volume[, taker_buy]])`` per consecutive bar."""
    out = []
    for i, row in enumerate(rows):
        o, h, l, c = row[:4]
        v = row[4] if len(row) > 4 else 0.0
        tb = row[5] if len(row) > 5 else 0.0
        out.append(Bar.make(start + step * i, step, o, h, l, c, v, tb))
    return out


def flat_bars(start: datetime, step: timedelta, closes: Sequence[float], spread: float = 0.0) -> list[Bar]:
    """Bars with ``open = previous close`` and the given closes; ``spread`` pads high/low."""
    out = []
    previous = closes[0]
    for i, c in enumerate(closes):
        o = previous
        out.append(Bar.make(start + step * i, step, o, max(o, c) + spread, min(o, c) - spread, c, 1.0, 0.5))
        previous = c
    return out


# ---------------------------------------------------------------------------
# A realistic synthetic market
# ---------------------------------------------------------------------------
def _aggregate(bars15: Sequence[Bar], step: timedelta) -> list[Bar]:
    per = step // M15
    out: list[Bar] = []
    for i in range(0, len(bars15) - per + 1, per):
        group = bars15[i : i + per]
        if group[0].open_time.timestamp() % step.total_seconds() != 0:
            continue
        out.append(
            Bar(
                group[0].open_time,
                group[-1].close_time,
                group[0].open,
                max(b.high for b in group),
                min(b.low for b in group),
                group[-1].close,
                sum(b.volume for b in group),
                sum(b.taker_buy_volume for b in group),
                group[-1].close_time,
            )
        )
    return out


def random_walk_15m(
    start: datetime,
    count: int,
    seed: int,
    *,
    price: float = 80_000.0,
    step_vol: float = 0.0015,
    market_returns: Sequence[float] | None = None,
    beta: float = 1.0,
) -> tuple[list[Bar], list[float]]:
    """``count`` consecutive 15m bars; returns also the per-bar log-ish returns used."""
    rng = random.Random(seed)
    bars: list[Bar] = []
    used: list[float] = []
    previous = price
    for i in range(count):
        if market_returns is None:
            ret = rng.gauss(0.0, step_vol)
        else:
            ret = beta * market_returns[i] + rng.gauss(0.0, step_vol * 0.6)
        used.append(ret)
        open_ = previous
        close = open_ * (1.0 + ret)
        wiggle = abs(rng.gauss(0.0, step_vol * 0.5)) * open_
        high = max(open_, close) + wiggle
        low = min(open_, close) - wiggle
        volume = 50.0 + rng.random() * 100.0
        taker = volume * (0.5 + rng.gauss(0.0, 0.05))
        taker = min(max(taker, 0.0), volume)
        bars.append(Bar.make(start + M15 * i, M15, open_, high, low, close, volume, taker))
        previous = close
    return bars, used


def synthetic_inputs(
    *,
    coin: str = 'BTCUSDT',
    days: int = 100,
    seed: int = 7,
    source: str = 'replay',
    horizon: str = 'swing',
    with_alt: bool = False,
    end: datetime | None = None,
) -> FeatureInputs:
    """A 100-day market ending at ``end`` (default 2026-06-01 00:00 UTC)."""
    end = end or dt(2026, 6, 1)
    count = days * 96
    start = end - M15 * count
    btc15, btc_returns = random_walk_15m(start, count, seed)
    if with_alt:
        perp15, _ = random_walk_15m(start, count, seed + 1, price=100.0, market_returns=btc_returns, beta=1.4)
    else:
        perp15 = btc15
    rng = random.Random(seed + 2)
    spot15 = [
        Bar(b.open_time, b.close_time, b.open, b.high, b.low, b.close * (1.0 + rng.gauss(0.0004, 0.0002)),
            b.volume, b.taker_buy_volume, b.available_at)
        for b in perp15
    ]
    # keep high >= close on the shifted spot bars
    spot15 = [replace(b, high=max(b.high, b.close), low=min(b.low, b.close)) for b in spot15]

    perp1h, spot1h, perp4h = _aggregate(perp15, H1), _aggregate(spot15, H1), _aggregate(perp15, H4)

    oi: list[OIPoint] = []
    level = 90_000.0
    first_hour = perp1h[0].close_time
    for i in range(len(perp1h)):
        level *= 1.0 + rng.gauss(0.0, 0.004)
        poll = first_hour + H1 * i
        oi.append(OIPoint(poll, poll + timedelta(minutes=5), level))

    funding: list[FundingPrint] = []
    t = dt(2026, 6, 1) - timedelta(days=days)
    while t < end:
        funding.append(FundingPrint(t, t + timedelta(seconds=30), 0.0001 + rng.gauss(0.0, 0.00005), None))
        t += timedelta(hours=8)

    return FeatureInputs(
        coin=coin,
        bar_close=end,
        horizon=horizon,
        source=source,
        perp_15m=perp15,
        spot_15m=spot15,
        perp_1h=perp1h,
        spot_1h=spot1h,
        perp_4h=perp4h,
        btc_perp_15m=btc15 if with_alt else None,
        is_reference_coin=not with_alt,
        oi_1h=oi,
        funding_settled=funding,
    )


def truncate_inputs(inputs: FeatureInputs, bar_close: datetime) -> FeatureInputs:
    """The same market with everything that closes after ``bar_close`` physically removed."""

    def bars(seq):
        return [b for b in seq if b.close_time <= bar_close]

    return replace(
        inputs,
        bar_close=bar_close,
        perp_15m=bars(inputs.perp_15m),
        spot_15m=bars(inputs.spot_15m),
        perp_1h=bars(inputs.perp_1h),
        spot_1h=bars(inputs.spot_1h),
        perp_4h=bars(inputs.perp_4h),
        btc_perp_15m=None if inputs.btc_perp_15m is None else bars(inputs.btc_perp_15m),
        oi_1h=[p for p in inputs.oi_1h if p.available_at <= bar_close],
        funding_settled=[p for p in inputs.funding_settled if p.available_at <= bar_close],
    )


def at_bar(inputs: FeatureInputs, bar_close: datetime) -> FeatureInputs:
    """Same data (future included), evaluated at an earlier ``bar_close``."""
    return replace(inputs, bar_close=bar_close)


# ---------------------------------------------------------------------------
# Future poisoning and recent-data mutation
# ---------------------------------------------------------------------------
def _scale_bar(b: Bar, factor: float) -> Bar:
    return replace(
        b,
        open=b.open * factor,
        high=b.high * factor,
        low=b.low * factor,
        close=b.close * factor,
        volume=b.volume * 3.0,
        taker_buy_volume=b.taker_buy_volume * 3.0,
    )


def poison_future(inputs: FeatureInputs, bar_close: datetime) -> FeatureInputs:
    """Evaluate at ``bar_close`` but make everything that closes later absurd.

    Bars after ``bar_close`` are scaled x7 (and their volume x3), later OI and funding
    prints are replaced with wild values. If any feature leaked a future print it would
    move by an order of magnitude.
    """

    def bars(seq):
        return [_scale_bar(b, 7.0) if b.close_time > bar_close else b for b in seq]

    return replace(
        inputs,
        bar_close=bar_close,
        perp_15m=bars(inputs.perp_15m),
        spot_15m=bars(inputs.spot_15m),
        perp_1h=bars(inputs.perp_1h),
        spot_1h=bars(inputs.spot_1h),
        perp_4h=bars(inputs.perp_4h),
        btc_perp_15m=None if inputs.btc_perp_15m is None else bars(inputs.btc_perp_15m),
        oi_1h=[replace(p, open_interest=p.open_interest * 9.0) if p.available_at > bar_close else p for p in inputs.oi_1h],
        funding_settled=[replace(p, rate=p.rate * -50.0) if p.available_at > bar_close else p for p in inputs.funding_settled],
    )


def nudge_latest(inputs: FeatureInputs, bar_close: datetime, factor: float = 1.03) -> FeatureInputs:
    """Positive control: change the NEWEST visible print of every series at ``bar_close``."""

    def bars(seq):
        seq = list(seq)
        last = max(i for i, b in enumerate(seq) if b.available_at <= bar_close)
        b = seq[last]
        seq[last] = replace(
            b,
            close=b.close * factor,
            high=max(b.high, b.close * factor),
            low=min(b.low, b.close * factor),
            taker_buy_volume=b.volume * 0.9,  # flow, not just price, moves on the newest bar
        )
        return seq

    oi = list(inputs.oi_1h)
    k = max(i for i, p in enumerate(oi) if p.available_at <= bar_close)
    oi[k] = replace(oi[k], open_interest=oi[k].open_interest * 1.2)
    fund = list(inputs.funding_settled)
    k = max(i for i, p in enumerate(fund) if p.available_at <= bar_close)
    fund[k] = replace(fund[k], rate=fund[k].rate * 4.0 + 0.0003)
    return replace(
        inputs,
        bar_close=bar_close,
        perp_15m=bars(inputs.perp_15m),
        spot_15m=bars(inputs.spot_15m),
        perp_1h=bars(inputs.perp_1h),
        spot_1h=bars(inputs.spot_1h),
        perp_4h=bars(inputs.perp_4h),
        btc_perp_15m=None if inputs.btc_perp_15m is None else bars(inputs.btc_perp_15m),
        oi_1h=oi,
        funding_settled=fund,
    )
