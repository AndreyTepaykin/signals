"""Black-76 delta and gamma with a zero rate.

Deribit prices every option off the forward (``underlying_price``) of its own
expiry, with a zero interest rate (``interest_rate: 0.0`` in the book summary), so
the Black-76 formulas apply with the discount factor equal to 1:

    d1    = (ln(F / K) + sigma^2 T / 2) / (sigma sqrt(T))
    delta = N(d1)            call,   N(d1) - 1 put
    gamma = phi(d1) / (F sigma sqrt(T))          per unit move of the forward

``sigma`` is the annualised implied vol as a fraction, ``T`` is years on a 365-day
year. ``N`` is built from ``math.erf`` so there is no SciPy dependency and the
result is bit-for-bit reproducible.
"""

from __future__ import annotations

import math

_SQRT_2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def norm_pdf(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _validate(forward: float, strike: float, iv: float, years: float) -> None:
    if forward <= 0.0 or strike <= 0.0 or iv <= 0.0 or years <= 0.0:
        raise ValueError('forward, strike, iv and years must all be positive')


def d1(forward: float, strike: float, iv: float, years: float) -> float:
    _validate(forward, strike, iv, years)
    return (math.log(forward / strike) + 0.5 * iv * iv * years) / (iv * math.sqrt(years))


def call_delta(forward: float, strike: float, iv: float, years: float) -> float:
    return norm_cdf(d1(forward, strike, iv, years))


def put_delta(forward: float, strike: float, iv: float, years: float) -> float:
    return norm_cdf(d1(forward, strike, iv, years)) - 1.0


def gamma(forward: float, strike: float, iv: float, years: float) -> float:
    """Second derivative of the option price with respect to the forward (same for a call and a put)."""
    return norm_pdf(d1(forward, strike, iv, years)) / (forward * iv * math.sqrt(years))
