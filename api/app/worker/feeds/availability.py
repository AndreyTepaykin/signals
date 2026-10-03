"""The available-at rule for every feed, in one place.

The spec's "Feeds" table: "Join on available-at, the time the print could have
been known, not the economic time it describes."

=====================  ============================  ===============================
Feed                   Spec ("Available at")         Rule implemented here
=====================  ============================  ===============================
Binance kline          Bar close                     ``open_time + timeframe``
Binance open interest  Close of the poll bar         ``poll_time + lag`` (MEASURED)
Binance funding        Last **settled** print        ``funding_time + lag`` (MEASURED)
(settled)
Binance funding        (live carry band reads the    ``observed_at``
(predicted)            current rate)
Binance force orders   Print time. Stream is partial ``traded_at``
Deribit public book    Snapshot time                 ``max(snapshot_time + lag, fetched_at)`` (MEASURED lag)
=====================  ============================  ===============================

(The Deribit rule is ``options_snapshot_available_at``; the book cannot be backfilled, so the
moment we received it is also a floor.)

The two ``lag`` values are measured infrastructure facts (``app/config.py``), deliberately conservative.
Using the bare boundary for open interest was lookahead bias: Binance serves a 15m/1h/4h sample up to ~3 minutes
after its boundary, so a row stamped at the boundary claimed knowledge the live system did not yet have.

Every row the worker writes gets its ``available_at`` from one of these five
functions and from nowhere else; ``tests/test_ingest_records.py`` pins each one.

Notes on the individual rules
-----------------------------
* **Kline.** ``open_time + timeframe`` is the bar's closing boundary (the
  exclusive end). Binance's own ``closeTime`` is that boundary minus 1 ms; the
  boundary is what ``bar_close`` means everywhere else in the system
  (``feature_row.bar_close``), so it is what is stored in ``candle.close_time``.
  A bar can only be written once that boundary has passed AND its values have stopped
  changing (``Settings.feed_kline_settle_seconds``: the closing bar is served provisionally
  for up to ~8 s) - see ``BinanceClient.klines``. ``available_at`` itself stays at the boundary.
* **Open interest.** Binance's ``openInterestHist`` timestamps are the
  period-aligned instant the sample describes (a 1h series observed at 16:45
  ends at ``16:00``), so ``poll_time`` is the close of the poll bar. The sample is
  PUBLISHED later (3 to 179 s after the boundary across 5 measured boundaries, varying by
  symbol), so ``available_at = poll_time + publication_lag``
  (``Settings.feed_open_interest_publication_lag_seconds``, chosen above the worst sample,
  not the mean). The writer refuses a row whose ``available_at`` is still in the future,
  so live and replay see the same rows at the same instants. The measurements are in
  ``config.py`` and ``BUILD-LOG.md``.
* **Funding, settled.** The print is known when it settles PLUS the serving delay
  (``Settings.feed_funding_publication_lag_seconds``: measured ~5 s once, allowance 30 s).
  Binance reports the settlement time with a few milliseconds of jitter (``...899200002``);
  that value is stored unchanged in ``funding_time`` so a re-fetch produces the identical key.
* **Funding, predicted.** The rate is a live estimate for a *future*
  settlement. It was known when the exchange stamped it, not at the settlement
  it predicts, so it is available at ``observed_at``.
* **Force order.** Available at the print (trade) time. The stream is partial,
  so anything derived from it is an estimate, flagged as such and kept in
  shadow.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.time import ensure_utc


def kline_available_at(open_time: datetime, timeframe_length: timedelta) -> datetime:
    """A kline is knowable at bar close, never earlier."""
    return ensure_utc(open_time) + timeframe_length


def open_interest_available_at(poll_time: datetime, publication_lag: timedelta) -> datetime:
    """Open interest is knowable at the close of the poll bar PLUS the time Binance takes to publish it.

    ``publication_lag`` is required (no default) so no caller can quietly fall back to the optimistic
    boundary; it comes from ``Settings.feed_open_interest_publication_lag_seconds``.
    """
    if publication_lag < timedelta(0):
        raise ValueError('publication_lag must not be negative')
    return ensure_utc(poll_time) + publication_lag


def funding_settled_available_at(funding_time: datetime, publication_lag: timedelta) -> datetime:
    """A settled funding print is knowable when it settles PLUS the time Binance takes to serve it.

    ``publication_lag`` is required (no default): see ``Settings.feed_funding_publication_lag_seconds``.
    """
    if publication_lag < timedelta(0):
        raise ValueError('publication_lag must not be negative')
    return ensure_utc(funding_time) + publication_lag


def funding_predicted_available_at(observed_at: datetime) -> datetime:
    """A predicted funding rate is knowable when the exchange stamped it."""
    return ensure_utc(observed_at)


def force_order_available_at(traded_at: datetime) -> datetime:
    """A liquidation print is knowable at its print time."""
    return ensure_utc(traded_at)


def options_snapshot_available_at(snapshot_time: datetime, fetched_at: datetime, publication_lag: timedelta) -> datetime:
    """A Deribit book snapshot is knowable at the LATER of its (lagged) snapshot instant and the moment we received it.

    ``snapshot_time`` is the venue's ``creation_timestamp``; ``fetched_at`` is when the response reached
    us (the book is not backfillable, so this is the only moment it could have been known).
    ``publication_lag`` is required (no default): ``Settings.feed_deribit_publication_lag_seconds``.
    The result is never earlier than ``fetched_at``, so a stored snapshot cannot claim knowledge the worker did not have.
    """
    if publication_lag < timedelta(0):
        raise ValueError('publication_lag must not be negative')
    return max(ensure_utc(snapshot_time) + publication_lag, ensure_utc(fetched_at))
