import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / '.env'

# ---------------------------------------------------------------------------
# Watchlist and feed configuration (Milestone 2)
#
# DELIBERATELY NOT in ``app.core.constants``. That module feeds ``rule_hash()``;
# the spec lists the watchlist under "Preferences" as an explicit setting, not a
# frozen rule, so editing it must not invalidate the rule hash of every
# historical label. The same goes for polling intervals and staleness grace:
# they are operating parameters, not decision rules.
# ---------------------------------------------------------------------------

#: Liquid USDT perpetuals that also have a USDT spot pair (both are needed: the
#: perp-vs-spot stand-down and the perp basis read both series). BTC and ETH
#: first because the options modifier and the one-BTC-view gate key on them.
#: Every symbol was confirmed TRADING on both Binance markets on 2026-10-02.
#: 1000-multiplier perps (1000PEPE...) are avoided: their spot symbol differs.
DEFAULT_WATCHLIST: tuple[str, ...] = (
    'BTCUSDT',
    'ETHUSDT',
    'SOLUSDT',
    'BNBUSDT',
    'XRPUSDT',
    'DOGEUSDT',
    'ADAUSDT',
    'AVAXUSDT',
    'LINKUSDT',
    'TRXUSDT',
    'DOTUSDT',
    'LTCUSDT',
    'BCHUSDT',
    'NEARUSDT',
    'ATOMUSDT',
)

#: Spec: "Phase 1 - 10-20 coins".
WATCHLIST_MAX_COINS = 20

_SYMBOL = re.compile(r'^[A-Z0-9]{2,20}USDT$')


def parse_symbols(raw: str) -> tuple[str, ...]:
    """``'btcusdt, ETHUSDT'`` -> ``('BTCUSDT', 'ETHUSDT')``: upper-cased, de-duplicated, order kept.

    ``coin`` in every feed table is the Binance USDT symbol (``BTCUSDT``), the
    same string on the spot and the perpetual market.
    """
    symbols: list[str] = []
    for token in raw.split(','):
        symbol = token.strip().upper()
        if not symbol:
            continue
        if not _SYMBOL.match(symbol):
            raise ValueError(f'{symbol!r} is not a Binance USDT symbol such as BTCUSDT')
        if symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding='utf-8',
        extra='ignore',
    )

    app_name: str = 'Signals API'
    database_url: str = 'postgresql+asyncpg://signals:password@127.0.0.1:5432/signals'
    cors_origins: str = 'http://localhost:4200'

    # -- M9 dashboard / notifications ---------------------------------------
    ws_poll_seconds: int = Field(15, ge=1, le=300)
    ntfy_url: str = ''
    ntfy_token: str = ''
    notify_poll_seconds: int = Field(60, ge=15, le=3600)
    # Live signal pipeline (labels, features, rules, options for the newest closed 15m bar).
    signal_pipeline_poll_seconds: int = Field(60, ge=15, le=300)

    # -- watchlist (a Preference, not a frozen rule) ------------------------
    watchlist: str = ','.join(DEFAULT_WATCHLIST)

    # -- Binance public market data. No API key, no signed endpoint, ever. --
    binance_futures_url: str = 'https://fapi.binance.com'
    binance_spot_url: str = 'https://api.binance.com'
    binance_futures_ws_url: str = 'wss://fstream.binance.com'

    # -- Deribit public market data (Milestone 5). No API key, no private endpoint, ever. --
    deribit_url: str = 'https://www.deribit.com'
    #: Poll the Deribit book from the live worker. The Binance feeds do not depend on it.
    deribit_enabled: bool = True
    #: Spec section 12: "Deribit book (BTC, ETH) | a few minutes". The options check calls a snapshot
    #: stale after 15 minutes, so 5 minutes leaves two missed polls of slack.
    feed_deribit_poll_seconds: int = Field(300, ge=30, le=300)
    #: MEASURED, not a rule. A book summary row carries ``creation_timestamp`` (the snapshot instant).
    #: Measured 2026-10-03 (public ``get_book_summary_by_currency``, 7 requests, BTC and ETH): the
    #: summary was 0.9 to 2.4 s old when the response arrived (server cache up to ~0.9 s plus transfer
    #: and our own clock), and every row of one response carried a creation_timestamp within 10-16 ms
    #: of the others. ``available_at = max(snapshot_time + this, time we received it)``. Chosen 10 s
    #: (4x the worst sample); errs late, like the Binance lags, so replay never knows a book earlier than live did.
    feed_deribit_publication_lag_seconds: int = Field(10, ge=0, le=300)
    #: Also write an ``event_calendar`` row (kind ``expiry``, ``is_large`` decided by
    #: ``LARGE_EXPIRY_MIN_OI_SHARE``) for each expiry that enters the 48h window. OFF by default:
    #: any calendar row makes ``feature_row.event_flag`` read FALSE ("no event") instead of NULL
    #: ("unknown") for CPI and FOMC too, and the owner has not loaded those yet. Turn it on once the
    #: macro calendar is loaded.
    deribit_write_expiry_calendar: bool = False

    # -- polling intervals, seconds. Spec section 12: "1-5 min". ------------
    feed_kline_poll_seconds: int = Field(60, ge=30, le=300)
    feed_open_interest_poll_seconds: int = Field(120, ge=30, le=300)
    feed_funding_settled_poll_seconds: int = Field(300, ge=30, le=300)
    feed_funding_predicted_poll_seconds: int = Field(60, ge=30, le=300)
    feed_health_poll_seconds: int = Field(60, ge=15, le=300)

    # -- staleness (see app.worker.feeds.health for the derivation) ----------
    #: Slack added on top of "one full period + one poll interval" before a feed
    #: is called stale. Covers a missed poll and a publication that is slower than the allowance below
    #: (the allowances themselves are already inside ``available_at``, which staleness is measured from).
    feed_staleness_grace_seconds: int = Field(120, ge=0, le=3600)
    #: The longest settlement interval the funding feed is expected to have.
    feed_funding_interval_hours: int = Field(8, ge=1, le=24)

    #: Open-interest history periods to poll (comma separated).
    feed_open_interest_periods: str = '15m,1h,4h'

    # -- publication lags: MEASURED infrastructure facts, not trading rules --------------------------
    # These live here (not in app/core/constants.py, which feeds rule_hash()) because they describe how
    # Binance behaves, not what the strategy does. They decide ``available_at`` and therefore what replay
    # (M7) may know. ERR LATE: a too-large value costs a little feature freshness; a too-small one silently
    # lets live and replay trade on data that was not yet served, inflating every statistic.
    #: Open-interest history is served well AFTER its 15m/1h/4h boundary, and the delay varies strongly by
    #: symbol. ``available_at = poll_time + this``. Measured 2026-10-02 (live, public ``openInterestHist``;
    #: seconds from the boundary until a probe FIRST saw the row; see docs/BUILD-LOG.md for the table):
    #:   17:00 ~90 (BTC only) | 17:30 22-166 (4 symbols) | 17:45 2.8-164.1 | 18:00 9.6-175.1 (15m and 1h)
    #:   18:15 18.0-179.2 | 18:30 10.6-180.1 | 18:45 5.4-164.5 | 19:00 12.5-172.9 (15m and 1h)
    #: i.e. 8 boundaries, 125 symbol-samples (15 symbols x 15m/1h), every sample inside 2.8-180.1 s, none
    #: missing: mean 90.5 s, median 93.8 s, p90 164.5 s, max 180.1 s (over the 120 threaded samples). The delay is per SYMBOL and spread
    #: evenly across the range (AVAX/NEAR/ADA/LTC/TRX often 150-180 s, BTC/ETH anywhere from 30 to 165 s),
    #: and the ceiling is ~180 s (looks like a ~3 min publication cycle). The original single 90 s sample
    #: happened to equal the mean: a mean-based allowance would still be early for about half of all rows.
    #: Chosen 300 s = 1.67x the worst sample (180.1 s), NOT the mean.
    #: Re-measure if Binance changes the endpoint: a lag above this value would make live optimistic again.
    feed_open_interest_publication_lag_seconds: int = Field(300, ge=0, le=900)
    #: Settled funding prints appear shortly after settlement. ``available_at = funding_time + this``.
    #: Measured: one 1h-interval symbol (ARKUSDT, 2026-10-02) at two settlements, 4.9 s (18:00) and 1.4 s
    #: (19:00) after settlement (n=2, thin; 8h prints not sampled). Chosen 30 s, ~6x the worst sample,
    #: deliberately generous because the evidence is two points.
    feed_funding_publication_lag_seconds: int = Field(30, ge=0, le=600)
    #: A just-closed kline is served with PROVISIONAL values for a few seconds, then changes. Candle writes
    #: wait this long after the boundary so the frozen row (inserts are ON CONFLICT DO NOTHING) is final.
    #: Measured 2026-10-02 over 6 boundaries: perp values stable 4.3-7.5 s after the boundary, spot
    #: 0.5-3.7 s. Chosen 30 s (4x the worst).
    #: ``available_at`` stays at bar close (spec): the margin is a WRITE delay, not a feature delay.
    feed_kline_settle_seconds: int = Field(30, ge=1, le=120)

    @property
    def watchlist_symbols(self) -> tuple[str, ...]:
        symbols = parse_symbols(self.watchlist)
        if not symbols:
            raise ValueError('WATCHLIST is empty; Phase 1 needs at least one coin')
        if len(symbols) > WATCHLIST_MAX_COINS:
            raise ValueError(f'WATCHLIST has {len(symbols)} coins; Phase 1 covers at most {WATCHLIST_MAX_COINS}')
        return symbols

    @property
    def open_interest_periods(self) -> tuple[str, ...]:
        periods = tuple(dict.fromkeys(p.strip() for p in self.feed_open_interest_periods.split(',') if p.strip()))
        if not periods:
            raise ValueError('FEED_OPEN_INTEREST_PERIODS is empty')
        return periods


@lru_cache
def get_settings() -> Settings:
    return Settings()
