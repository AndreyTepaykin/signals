"""Trend efficiency (rank 5) and the trend/chop family.

Efficiency ratio (Kaufman) over ``n`` bars of closes::

    ER = |C_t - C_{t-n}| / sum_{i=t-n+1..t} |C_i - C_{i-1}|

``n + 1`` closes. A window whose closes never move has a zero denominator and NO
efficiency (missing, not 0.0 and not 1.0).

Family
------
Spec: "Trend at or above 0.40. Chop at or below 0.25. Between those, keep the
previous family. Only that family may fire."

``keep the previous family`` makes the family a state machine. A state machine
that lives in process memory is a replay hazard: a live worker that was
restarted, or a replay that starts at a different bar, would disagree about the
family and quietly invalidate every replayed label. So the state is NOT kept. It
is DERIVED, every time, from the data:

    family(t) = the family of the most recent bar, among the last
                ``FAMILY_MEMORY_BARS`` hourly readings up to and including t,
                whose efficiency was decisive (>= 0.40 or <= 0.25).

That is exactly what the machine would hold, provided it was started at least
``FAMILY_MEMORY_BARS`` bars ago, and it is a pure function of the visible bars:
live and replay give the same answer for the same ``as_of`` by construction.

**Cold start.** If none of the readings inside the memory window is decisive
(including when there is too little history to compute any), the family is
NULL. It is never defaulted to trend or to chop: "no family yet" must not look
like a family, and a rule that requires a family simply does not fire.
If the latest efficiency itself cannot be computed (a hole in the last
``n + 1`` hourly bars, or a window that never moves) the family is NULL too,
because the reading it would have to carry from is not adjacent to now.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core import constants as C
from app.core.features import params as P
from app.core.features.series import Bar, is_contiguous, visible_bars


def efficiency_of_closes(closes: Sequence[float]) -> float | None:
    """Efficiency ratio of ``n + 1`` closes, or None if they never move."""
    if len(closes) < 2:
        raise ValueError('need at least two closes')
    path = math.fsum(abs(b - a) for a, b in zip(closes, closes[1:]))
    if path == 0.0:
        return None
    return abs(closes[-1] - closes[0]) / path


def classify_efficiency(ratio: float) -> str | None:
    """``trend`` at or above the trend minimum, ``chop`` at or below the chop maximum, else None."""
    if ratio >= C.EFFICIENCY_TREND_MIN:
        return C.FAMILY_TREND
    if ratio <= C.EFFICIENCY_CHOP_MAX:
        return C.FAMILY_CHOP
    return None


@dataclass(frozen=True)
class EfficiencyPoint:
    time: datetime  # close of the window's last bar
    ratio: float
    net_change: float  # C_t - C_{t-n}, signed


def efficiency_points(
    bars: Sequence[Bar],
    step: timedelta,
    as_of: datetime,
    *,
    n: int = C.EFFICIENCY_BARS,
) -> list[EfficiencyPoint]:
    """Efficiency at every visible bar that has ``n`` consecutive bars before it."""
    seen = visible_bars(bars, as_of)
    points: list[EfficiencyPoint] = []
    for end in range(n, len(seen)):
        window = seen[end - n : end + 1]
        if not is_contiguous(window, step):
            continue
        closes = [b.close for b in window]
        ratio = efficiency_of_closes(closes)
        if ratio is None:
            continue
        points.append(EfficiencyPoint(window[-1].close_time, ratio, closes[-1] - closes[0]))
    return points


@dataclass(frozen=True)
class TrendState:
    ratio: float | None
    net_change: float | None
    family: str | None
    #: 'decisive' (latest reading decided it), 'carried' (an earlier reading did), or None (cold start / unknown)
    family_source: str | None
    reason: str | None = None


def derive_family(points: Sequence[EfficiencyPoint], latest_time: datetime, step: timedelta) -> tuple[str | None, str | None]:
    """Family at the last point from the memory window, and where it came from."""
    if not points or points[-1].time != latest_time:
        return None, None
    memory_start = latest_time - step * P.FAMILY_MEMORY_BARS
    for point in reversed(points):
        if point.time <= memory_start:
            break
        family = classify_efficiency(point.ratio)
        if family is not None:
            return family, ('decisive' if point.time == latest_time else 'carried')
    return None, None


def trend_state(bars: Sequence[Bar], step: timedelta, as_of: datetime, *, n: int = C.EFFICIENCY_BARS) -> TrendState:
    """Efficiency and family at the last bar visible at ``as_of``."""
    seen = visible_bars(bars, as_of)
    if not seen:
        return TrendState(None, None, None, None, 'no bars')
    latest_time = seen[-1].close_time
    points = efficiency_points(seen, step, as_of, n=n)
    if not points or points[-1].time != latest_time:
        return TrendState(None, None, None, None, f'efficiency needs {n + 1} consecutive moving bars ending at the latest bar')
    latest = points[-1]
    family, source = derive_family(points, latest_time, step)
    reason = None if family is not None else 'cold start: no decisive efficiency reading in the memory window'
    return TrendState(latest.ratio, latest.net_change, family, source, reason)


def incremental_family(ratios: Sequence[float]) -> list[str | None]:
    """The textbook state machine, one reading at a time. Used by tests to prove
    that :func:`derive_family` equals it whenever the memory window covers the run."""
    state: str | None = None
    out: list[str | None] = []
    for ratio in ratios:
        decided = classify_efficiency(ratio)
        if decided is not None:
            state = decided
        out.append(state)
    return out
