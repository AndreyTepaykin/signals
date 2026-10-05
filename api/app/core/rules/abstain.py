"""The abstain rule (Milestone 10): a candidate whose pooled edge does not clear cost does not become an alert.

Pure. The caller supplies the candidate's would-be status, its pooled estimate (pool size and the lower bound of the
expected-R interval, both NULL below the floor) and the price inputs for the funding band. Every convention is a
hashed ``ABSTAIN_*`` value in :mod:`app.core.constants`; :func:`check_conventions` refuses one this code does not
implement, so a constant cannot drift away from the arithmetic unnoticed.

* Only a candidate about to be an ``alert`` can abstain; shadow, ``watch``, ``delayed`` and ``cancelled`` rows are not
  touched.
* Below the pool floor (cold start) nothing abstains: the card hides the edge and the alert stands.
* At the floor: abstain when ``expected_r_ci_low < cost_edge_r``. Stored R is already net of fees, slippage and realized
  funding, so ``cost_edge_r`` is the funding band's adverse edge alone: ``|carry_bps| / 1e4 * reference / one_r``, with
  ``one_r = |reference - stop|`` on the post-options stop. An unknown carry counts as
  ``ABSTAIN_UNKNOWN_FUNDING_EDGE_BPS``. No interval at the floor abstains (``ABSTAIN_WITHOUT_INTERVAL``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core import constants as C

BPS = Decimal('10000')


def check_conventions() -> None:
    """Raise if a hashed abstain convention names behaviour this module does not implement."""
    expected = {
        'ABSTAIN_APPLIES_TO_STATUS': 'alert',
        'ABSTAIN_TESTED_BOUND': 'expected_r_ci_low',
        'ABSTAIN_COST_EDGE': 'funding_band_adverse_edge',
        'ABSTAIN_FUNDING_EDGE_NOTIONAL': 'signal_bar_close',
    }
    for name, value in expected.items():
        if getattr(C, name) != value:
            raise ValueError(f'{name}={getattr(C, name)!r} is not implemented (expected {value!r})')
    if not isinstance(C.ABSTAIN_WITHOUT_INTERVAL, bool):
        raise ValueError('ABSTAIN_WITHOUT_INTERVAL must be a bool')
    if C.ABSTAIN_UNKNOWN_FUNDING_EDGE_BPS < 0:
        raise ValueError('ABSTAIN_UNKNOWN_FUNDING_EDGE_BPS cannot be negative')


check_conventions()


@dataclass(frozen=True)
class AbstainCheck:
    """The test and its inputs, for the log and the tests. ``abstain`` is the only field that changes a decision."""

    abstain: bool
    tested: bool  # the candidate was an alert with a pool at the floor
    pool_n: int | None
    expected_r_ci_low: float | None
    funding_edge_bps: Decimal | None
    cost_edge_r: float | None


def funding_edge_bps(funding_carry_bps: Decimal | float | None) -> Decimal:
    """Adverse edge of the funding band {carry, -carry}, in bps of notional."""
    if funding_carry_bps is None:
        return C.ABSTAIN_UNKNOWN_FUNDING_EDGE_BPS
    value = funding_carry_bps if isinstance(funding_carry_bps, Decimal) else Decimal(repr(float(funding_carry_bps)))
    return abs(value)


def cost_edge_r(edge_bps: Decimal, reference_price: Decimal, stop_price: Decimal) -> float:
    one_r = abs(Decimal(reference_price) - Decimal(stop_price))
    if one_r <= 0:
        raise ValueError('1R must be positive')
    return float(edge_bps / BPS * Decimal(reference_price) / one_r)


def check_abstain(
    *,
    status: str,
    pool_n: int | None,
    expected_r_ci_low: float | None,
    funding_carry_bps: Decimal | float | None,
    reference_price: Decimal,
    stop_price: Decimal,
) -> AbstainCheck:
    if status != C.ABSTAIN_APPLIES_TO_STATUS or pool_n is None or pool_n < C.ABSTAIN_MIN_POOL_INDEPENDENT:
        return AbstainCheck(False, False, pool_n, expected_r_ci_low, None, None)
    edge_bps = funding_edge_bps(funding_carry_bps)
    edge_r = cost_edge_r(edge_bps, reference_price, stop_price)
    if expected_r_ci_low is None:
        abstain = C.ABSTAIN_WITHOUT_INTERVAL
    else:
        abstain = float(expected_r_ci_low) < edge_r
    return AbstainCheck(abstain, True, pool_n, expected_r_ci_low, edge_bps, edge_r)
