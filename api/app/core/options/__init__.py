"""Pure options math and the options influence check (Milestone 5).

No database, no clock, no environment, no network. The worker reads the snapshot
and the candidate through the ``as_of`` gate and hands plain data to these
modules; the same data gives the same verdict live and in a replay.

``book``     instruments parsed from their names; one snapshot as a tuple of quotes
``greeks``   Black-76 delta and gamma (zero rate, the forward of each expiry)
``metrics``  expiries, max pain, strike walls, dealer gamma and its flip, skew, ATM IV, same-tenor RV
``check``    the precedence ladder: verdict, vol tag, expression, stop / target layers, status
"""
