"""Deterministic statistics and the causal 90-day window.

**How causality is guaranteed here.** A rolling statistic at time ``t`` may use
only samples that (a) were available at ``t`` and (b) happened strictly before
``t``. :func:`prior_window` is the only place that rule is written and every
z-score and percentile in the engine goes through it:

* ``sample.available_at <= as_of``  - nothing published later is visible;
* ``current_time - window <= sample.time < current_time`` - the window is
  trailing, one-sided and EXCLUDES the observation being scored, so the current
  value cannot pull its own mean or its own rank toward itself;
* no function here ever receives a full-sample mean or a centred window: the
  mean and standard deviation are recomputed from the window each call.

The tests append future data to a series and assert an earlier value does not
move; they would fail if any of these three lines were removed.

**Determinism.** Sums use :func:`math.fsum` (exactly rounded, independent of
order), means and variances are two-pass, windows are sorted by time before use.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.core.features.params import HistoryPolicy
from app.core.features.reading import Reading, have, missing
from app.core.features.series import Sample
from app.core.time import ensure_utc


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError('mean of an empty sequence')
    return math.fsum(values) / len(values)


def sample_variance(values: Sequence[float]) -> float:
    """Unbiased variance (divisor n-1), two-pass."""
    n = len(values)
    if n < 2:
        raise ValueError('variance needs at least two values')
    centre = mean(values)
    return math.fsum((v - centre) ** 2 for v in values) / (n - 1)


def sample_std(values: Sequence[float]) -> float:
    return math.sqrt(sample_variance(values))


def population_std(values: Sequence[float]) -> float:
    """Divisor n. Bollinger bands use this convention."""
    n = len(values)
    if n < 1:
        raise ValueError('std of an empty sequence')
    centre = mean(values)
    return math.sqrt(math.fsum((v - centre) ** 2 for v in values) / n)


@dataclass(frozen=True)
class Window:
    """The prior samples a statistic is measured against."""

    values: list[float]
    first_time: datetime
    last_time: datetime


def prior_window(
    samples: Sequence[Sample],
    current_time: datetime,
    as_of: datetime,
    policy: HistoryPolicy,
) -> tuple[Window | None, str | None]:
    """The samples strictly before ``current_time`` inside the trailing window.

    Returns ``(window, None)`` or ``(None, reason)`` when the history is too
    thin for the policy.
    """
    now = ensure_utc(current_time)
    cutoff = ensure_utc(as_of)
    start = now - policy.window
    chosen = [s for s in samples if s.available_at <= cutoff and start <= s.time < now]
    chosen.sort(key=lambda s: s.time)
    if len(chosen) < policy.min_samples:
        return None, f'only {len(chosen)} prior samples, need {policy.min_samples}'
    span = chosen[-1].time - chosen[0].time
    if span < policy.min_span:
        days = span.total_seconds() / 86400.0
        return None, f'prior samples span {days:.1f} days, need {policy.min_span_days:g}'
    return Window([s.value for s in chosen], chosen[0].time, chosen[-1].time), None


def trailing_zscore(
    samples: Sequence[Sample],
    current_time: datetime,
    current_value: float,
    as_of: datetime,
    policy: HistoryPolicy,
) -> Reading:
    """``(x - mean) / std`` of the current value against the prior window.

    The standard deviation is the unbiased sample std. A window with zero
    spread has no z-score (missing, not 0.0 and not infinity).
    """
    window, why = prior_window(samples, current_time, as_of, policy)
    if window is None:
        return missing(why or 'no history')
    spread = sample_std(window.values)
    if spread == 0.0:
        return missing('prior window has zero variance')
    return have((current_value - mean(window.values)) / spread)


def trailing_percentile(
    samples: Sequence[Sample],
    current_time: datetime,
    current_value: float,
    as_of: datetime,
    policy: HistoryPolicy,
) -> Reading:
    """Mid-rank percentile of the current value among the prior window, in 0-1.

    ``(count(prior < x) + 0.5 * count(prior == x)) / n``. Ties split evenly, so
    a value equal to every prior sample is the 50th percentile rather than the
    0th or the 100th.
    """
    window, why = prior_window(samples, current_time, as_of, policy)
    if window is None:
        return missing(why or 'no history')
    below = sum(1 for v in window.values if v < current_value)
    equal = sum(1 for v in window.values if v == current_value)
    return have((below + 0.5 * equal) / len(window.values))


@dataclass(frozen=True)
class OlsFit:
    alpha: float
    beta: float
    residual_std: float  # unbiased: sqrt(SSR / (n - 2))
    n: int


def ols(x: Sequence[float], y: Sequence[float]) -> OlsFit | None:
    """Ordinary least squares of ``y`` on ``x`` with an intercept.

    ``beta = Sxy / Sxx``, ``alpha = mean(y) - beta * mean(x)``. Returns None when
    there are fewer than three pairs or ``x`` has no variance.
    """
    if len(x) != len(y):
        raise ValueError('x and y must have the same length')
    n = len(x)
    if n < 3:
        return None
    x_mean, y_mean = mean(x), mean(y)
    sxx = math.fsum((xi - x_mean) ** 2 for xi in x)
    if sxx == 0.0:
        return None
    sxy = math.fsum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
    beta = sxy / sxx
    alpha = y_mean - beta * x_mean
    ssr = math.fsum((yi - alpha - beta * xi) ** 2 for xi, yi in zip(x, y))
    return OlsFit(alpha, beta, math.sqrt(ssr / (n - 2)), n)
