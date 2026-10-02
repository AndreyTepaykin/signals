"""Lagged beta of an alt on BTC, and the residual on the signal bar.

Spec: "Beta is a 90-day rolling OLS of the alt on BTC, fit on returns that end
before the signal bar. The live gate uses that lagged beta. Residual return over
the hold is stored on the label and is not a feature at decision time."

What this module computes (decision-time only)
----------------------------------------------
* ``beta`` / ``alpha`` from OLS of the alt's 15m log returns on BTC's, over the
  trailing 90 days, using ONLY returns whose bar CLOSED at or before the signal
  bar's OPEN. The signal bar's own return (the pair that closes at ``bar_close``)
  is therefore not in the fit - including it would let the bar being judged
  pull the line toward itself.
* ``beta_fit_end`` is the close time of the last return used. It is at most
  ``bar_close - 15m``, and the database has a CHECK that it is <= ``bar_close``.
* ``alt_residual_z_signal_bar = (r_alt - alpha - beta * r_btc) / sigma_resid``
  where ``r`` are the signal bar's returns and ``sigma_resid`` is the unbiased
  residual standard deviation of the fit. Positive: the alt outperformed what
  its BTC beta explains.

What it does not compute: the residual over the HOLD. That needs prices after
the decision and is ``label.cmi_alt_residual_return`` (Milestone 6).

Returns are log returns of perp closes, paired by bar close time; a return
exists only when the previous bar is the immediately preceding one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.features.params import HistoryPolicy
from app.core.features.reading import Reading, have, missing
from app.core.features.series import Bar, visible_bars
from app.core.features.stats import OlsFit, ols


def log_returns(bars: Sequence[Bar], step: timedelta, as_of: datetime) -> dict[datetime, float]:
    """``{close_time: ln(C_i / C_{i-1})}`` for visible bars whose predecessor is the previous bar."""
    seen = visible_bars(bars, as_of)
    out: dict[datetime, float] = {}
    for previous, bar in zip(seen, seen[1:]):
        if bar.open_time - previous.open_time != step:
            continue
        if previous.close <= 0.0 or bar.close <= 0.0:
            continue
        out[bar.close_time] = math.log(bar.close / previous.close)
    return out


@dataclass(frozen=True)
class BetaFit:
    fit: OlsFit
    fit_end: datetime  # close time of the last return used
    fit_start: datetime


def lagged_beta_fit(
    alt_returns: dict[datetime, float],
    btc_returns: dict[datetime, float],
    bar_close: datetime,
    step: timedelta,
    policy: HistoryPolicy,
) -> tuple[BetaFit | None, str | None]:
    """OLS of alt on BTC over returns that closed at or before the signal bar's open."""
    signal_open = bar_close - step
    earliest = signal_open - policy.window
    times = sorted(t for t in alt_returns if t in btc_returns and earliest < t <= signal_open)
    if len(times) < policy.min_samples:
        return None, f'only {len(times)} paired returns before the signal bar, need {policy.min_samples}'
    span = times[-1] - times[0]
    if span < policy.min_span:
        return None, f'paired returns span {span.total_seconds() / 86400.0:.1f} days, need {policy.min_span_days:g}'
    fit = ols([btc_returns[t] for t in times], [alt_returns[t] for t in times])
    if fit is None:
        return None, 'BTC returns have no variance in the window'
    return BetaFit(fit, times[-1], times[0]), None


def alt_residual_z(
    alt_returns: dict[datetime, float],
    btc_returns: dict[datetime, float],
    bar_close: datetime,
    step: timedelta,
    policy: HistoryPolicy,
) -> tuple[Reading, Reading, datetime | None]:
    """``(beta, residual z on the signal bar, beta_fit_end)``; each missing with a reason."""
    fit, why = lagged_beta_fit(alt_returns, btc_returns, bar_close, step, policy)
    if fit is None:
        gone = missing(why or 'no beta fit')
        return gone, gone, None
    beta = have(fit.fit.beta)
    alt_signal = alt_returns.get(bar_close)
    btc_signal = btc_returns.get(bar_close)
    if alt_signal is None or btc_signal is None:
        return beta, missing('the signal bar has no return for the alt or for BTC'), fit.fit_end
    if fit.fit.residual_std == 0.0:
        return beta, missing('the fit has zero residual variance'), fit.fit_end
    residual = alt_signal - fit.fit.alpha - fit.fit.beta * btc_signal
    return beta, have(residual / fit.fit.residual_std), fit.fit_end
