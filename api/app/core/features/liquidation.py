"""Liquidation estimate (rank 4) and print rate. SHADOW ONLY, always estimated.

The force-order stream is partial (Binance serves the latest liquidation per
symbol per second) and cannot be backfilled, so this is an estimate by
construction and is stamped so: ``liquidation_distance_estimated`` is True on
every row that carries a distance. The values go into ``shadow_metrics``, which
has no typed column, so a rule cannot reach them by attribute access.

Estimate
--------
Over the last ``LIQUIDATION_LOOKBACK_HOURS`` of prints, bucket price into bins
of ``LIQUIDATION_BIN_ATR`` x ATR, sum notional (price x quantity) per bin, take
the densest bin (ties: the lowest price) and place the pool at the
notional-weighted mean price inside it. The distance is ``(pool - price) / ATR``:
positive when the pool sits ABOVE price.

Nothing is reported unless at least ``LIQUIDATION_MIN_PRINTS`` prints are
visible: with no prints you cannot tell a quiet market from a dead stream, so
the answer is "unknown" (the keys are omitted), never a distance of zero and
never a print rate of zero. When prints exist, a rate of 0.0 over the last 15
minutes is a real observation (the stream is demonstrably alive).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

from app.core.features import params as P
from app.core.features.series import ForcePrint
from app.core.time import ensure_utc

KEY_DISTANCE = 'liquidation_distance_atr'
KEY_ESTIMATED = 'liquidation_distance_estimated'
KEY_RATE = 'liquidation_print_rate_per_min'


def liquidation_shadow_metrics(
    prints: Sequence[ForcePrint],
    price: float,
    atr: float | None,
    as_of: datetime,
) -> tuple[dict[str, object], str | None]:
    """The shadow-bag entries this feed can support, and why they are absent if empty."""
    now = ensure_utc(as_of)
    lookback = now - timedelta(hours=P.LIQUIDATION_LOOKBACK_HOURS)
    recent = sorted(
        (p for p in prints if p.available_at <= now and lookback < p.traded_at <= now and p.quantity > 0.0 and p.price > 0.0),
        key=lambda p: (p.traded_at, p.price, p.quantity, p.side),
    )
    if len(recent) < P.LIQUIDATION_MIN_PRINTS:
        return {}, f'{len(recent)} force-order prints in {P.LIQUIDATION_LOOKBACK_HOURS}h, need {P.LIQUIDATION_MIN_PRINTS}'

    out: dict[str, object] = {}
    rate_window = now - timedelta(minutes=P.LIQUIDATION_RATE_WINDOW_MINUTES)
    quantity = math.fsum(p.quantity for p in recent if p.traded_at > rate_window)
    out[KEY_RATE] = quantity / P.LIQUIDATION_RATE_WINDOW_MINUTES

    if atr is None or atr <= 0.0 or price <= 0.0:
        return out, 'ATR or price unavailable, so no distance'
    width = P.LIQUIDATION_BIN_ATR * atr
    bins: dict[int, list[ForcePrint]] = {}
    for p in recent:
        bins.setdefault(math.floor(p.price / width), []).append(p)
    best = min(bins, key=lambda index: (-math.fsum(p.price * p.quantity for p in bins[index]), index))
    members = bins[best]
    notional = math.fsum(p.price * p.quantity for p in members)
    pool = math.fsum(p.price * (p.price * p.quantity) for p in members) / notional
    out[KEY_DISTANCE] = (pool - price) / atr
    out[KEY_ESTIMATED] = True
    return out, None
