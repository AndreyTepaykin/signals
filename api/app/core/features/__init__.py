"""The deterministic feature engine.

Pure functions, no database, no clock, no randomness. Everything is computed
from lists of records and an ``as_of`` instant; the worker's assembly job reads
those records through ``app.models.as_of`` and a replay can load them from
memory. The modules, in the order a reader should audit them:

``params``         choices the spec leaves open (timeframes, warm-ups, minimum history)
``series``         record types and the visibility filter
``stats``          causal z-score / percentile, OLS, deterministic sums
``volatility``     true range, Wilder ATR, Yang-Zhang, annualisation, vol percentile
``trend``          efficiency ratio and the stateless-by-construction family
``positioning``    aggressor imbalance, OI-price quadrant, funding, perp basis
``indicators``     CVD divergence, VWAP, volume profile, squeeze, RSI, swings, chandelier
``cross_asset``    lagged beta against BTC and the signal-bar residual
``liquidation``    shadow-only liquidation-distance estimate
``columns``        the feature_row column registry (kind and rounding per column)
``assemble``       inputs -> the feature_row mapping and its feature_hash
"""
