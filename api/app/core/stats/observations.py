"""What the stats engine sees: one `Observation` per labeled candidate, and the cohort it belongs to.

Cohorts
-------
Outcomes are never pooled across cohorts. A `CohortKey` is `(source, options_path, lookback)` and **every** statistic in
this package is computed under exactly one key (`summarize` refuses a mixed list with `MixedCohortError`). The three
parts, and why each is a key and not a filter someone has to remember:

``source``
    `live` or `replay`. A replay row is a backtest on backfilled data; a live row is what the loop produced.
``options_path``
    How the options layer treated the row. `no_options_history` (replay, BTC/ETH, no Deribit book existed at the
    decision instant) never shares a cohort with a row that went through the options layer. The classification reads the
    candidate's structure, not only its `status_reason`, because shadow rows (vote conflict, CVD, stand-down) keep their
    own reason and lose the `options_ignored:*` one: 171 of the 423 BTC/ETH replay rows are such rows.
``lookback``
    `thin` for signal bars before `params.THIN_LOOKBACK_CUTOFF`, `full` afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core import constants as C
from app.core.stats import params as P
from app.models import enums as E

PATH_NO_HISTORY = 'no_options_history'  # replay, BTC/ETH, no book visible at the decision instant
PATH_NO_BOOK = 'no_options_book'  # the coin has no Deribit book (alts): the layer does not apply, live and replay alike
PATH_LAYER = 'options_layer'  # went through the options layer and got a real verdict
PATH_LAYER_IGNORED = 'options_layer_ignored'  # went through it on BTC/ETH and was ignored (book stale/thin, or live with no snapshot)
OPTIONS_PATHS = (PATH_NO_HISTORY, PATH_NO_BOOK, PATH_LAYER, PATH_LAYER_IGNORED)

LOOKBACK_THIN = 'thin'
LOOKBACK_FULL = 'full'


class MixedCohortError(ValueError):
    """Raised when a statistic is asked to pool observations from more than one cohort."""


@dataclass(frozen=True, order=True)
class CohortKey:
    source: str
    options_path: str
    lookback: str

    def __post_init__(self) -> None:
        if self.source not in E.SOURCES:
            raise ValueError(f'unknown source {self.source!r}')
        if self.options_path not in OPTIONS_PATHS:
            raise ValueError(f'unknown options path {self.options_path!r}')
        if self.lookback not in (LOOKBACK_THIN, LOOKBACK_FULL):
            raise ValueError(f'unknown lookback {self.lookback!r}')

    @property
    def label(self) -> str:
        return f'{self.source}/{self.options_path}/{self.lookback}'


def classify_options_path(
    *,
    source: str,
    coin: str,
    options_verdict: str | None,
    status_reason: str | None,
    options_snapshot_time: datetime | None,
) -> str:
    """Which options path a candidate took. Pure; reads only fields the options job wrote."""
    if options_verdict is None:
        raise ValueError('a candidate that has not been through the options check has no options path')
    if coin not in C.OPTIONS_CURRENCY_BY_COIN:
        return PATH_NO_BOOK
    if options_verdict != 'ignore':
        return PATH_LAYER
    marker = C.STATUS_REASON_OPTIONS_IGNORED_PREFIX + C.OPTIONS_IGNORE_NO_HISTORY
    if status_reason == marker or (source == E.SOURCE_REPLAY and options_snapshot_time is None):
        return PATH_NO_HISTORY
    return PATH_LAYER_IGNORED


def classify_lookback(bar_close: datetime) -> str:
    return LOOKBACK_THIN if bar_close < P.THIN_LOOKBACK_CUTOFF else LOOKBACK_FULL


def vol_bucket(vol_pct: float | None) -> str:
    if vol_pct is None:
        return P.UNKNOWN
    low, high = P.VOL_BUCKET_EDGES
    if vol_pct < low:
        return P.VOL_BUCKET_NAMES[0]
    if vol_pct < high:
        return P.VOL_BUCKET_NAMES[1]
    return P.VOL_BUCKET_NAMES[2]


def _text(value: Any) -> str:
    return P.UNKNOWN if value is None else str(value)


@dataclass(frozen=True)
class Observation:
    """One labeled candidate. R values are floats (the report is not a ledger)."""

    candidate_id: int
    sample_group_id: str
    decision_time: datetime
    bar_close: datetime
    resolved_at: datetime
    coin: str
    horizon: str
    source: str
    status: str
    status_reason: str | None
    trigger: str
    setup_family: str
    # regime slice, as copied onto the label
    trend_family: str | None
    vol_pct: float | None
    session: str | None
    event_flag: bool | None
    # outcome
    first_barrier: str
    stored_r: float | None
    funding_r: float | None
    barrier_ambiguous: bool
    funding_partial: bool
    gapped_stop: bool
    # decision-time probability (candidate.p_upper_before_stop); NULL on every row today
    p_upper: float | None
    # options-path inputs
    options_verdict: str | None
    options_snapshot_time: datetime | None
    # True when the options layer left the stop and the target alone, so the label IS the baseline outcome
    barriers_unmodified: bool
    # M8.1: pool inputs (label) and the stored estimate / cost (candidate); all default to "unknown"
    mae_r: float | None = None
    hours_to_resolution: float | None = None
    fees_r: float | None = None  # round-trip fees + slippage in R (label.fees_r); NULL on a no_entry label
    pool_n: int | None = None
    expected_r_ci_low: float | None = None

    @property
    def options_path(self) -> str:
        return classify_options_path(
            source=self.source,
            coin=self.coin,
            options_verdict=self.options_verdict,
            status_reason=self.status_reason,
            options_snapshot_time=self.options_snapshot_time,
        )

    @property
    def lookback(self) -> str:
        return classify_lookback(self.bar_close)

    @property
    def cohort(self) -> CohortKey:
        return CohortKey(self.source, self.options_path, self.lookback)

    @property
    def is_no_entry(self) -> bool:
        return self.first_barrier == E.BARRIER_NO_ENTRY

    @property
    def vol_bucket(self) -> str:
        return vol_bucket(self.vol_pct)

    def pool_dimension(self, name: str) -> str:
        """The value of one pool-walk-up dimension (`constants.POOL_DIM_*`) as a plain string."""
        if name == C.POOL_DIM_SETUP_FAMILY:
            return self.setup_family
        if name == C.POOL_DIM_TREND_FAMILY:
            return _text(self.trend_family)
        if name == C.POOL_DIM_VOL_PERCENTILE:
            return self.vol_bucket
        if name == C.POOL_DIM_SESSION:
            return _text(self.session)
        if name == C.POOL_DIM_EVENT_FLAG:
            return _text(self.event_flag)
        raise KeyError(name)


def split_by_cohort(observations: list[Observation]) -> dict[CohortKey, list[Observation]]:
    """The only place observations are grouped across cohorts, and it groups them apart."""
    out: dict[CohortKey, list[Observation]] = {}
    for observation in observations:
        out.setdefault(observation.cohort, []).append(observation)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# The naive swing-break baseline lives in its own cohort space
# ---------------------------------------------------------------------------
COIN_SET_ALTS = 'alts'  # coins with no Deribit book: the app cohort `no_options_book`
COIN_SET_COVERED = 'covered'  # BTC / ETH: the app cohorts that have an options path
COIN_SETS = (COIN_SET_ALTS, COIN_SET_COVERED)


def coin_set_for_path(options_path: str) -> str:
    """Which baseline coin set an app cohort is compared with."""
    return COIN_SET_ALTS if options_path == PATH_NO_BOOK else COIN_SET_COVERED


def coin_set_for_coin(coin: str) -> str:
    return COIN_SET_COVERED if coin in C.OPTIONS_CURRENCY_BY_COIN else COIN_SET_ALTS


@dataclass(frozen=True, order=True)
class BaselineKey:
    """`(source, coin_set, lookback)`: the naive baseline has no options layer, so it has no options path."""

    source: str
    coin_set: str
    lookback: str

    def __post_init__(self) -> None:
        if self.source not in E.SOURCES:
            raise ValueError(f'unknown source {self.source!r}')
        if self.coin_set not in COIN_SETS:
            raise ValueError(f'unknown coin set {self.coin_set!r}')
        if self.lookback not in (LOOKBACK_THIN, LOOKBACK_FULL):
            raise ValueError(f'unknown lookback {self.lookback!r}')

    @property
    def label(self) -> str:
        return f'{self.source}/{self.coin_set}/{self.lookback}'


class BaselineObservation(Observation):
    """A naive-baseline trade. Same fields and statistics as an app observation; a different cohort space.

    ``options_verdict`` is ``None`` on purpose: the baseline never consulted the options layer.
    """

    @property
    def options_path(self) -> str:  # pragma: no cover - a baseline trade has no options path
        raise ValueError('a naive baseline trade has no options path')

    @property
    def coin_set(self) -> str:
        return coin_set_for_coin(self.coin)

    @property
    def cohort(self) -> BaselineKey:  # type: ignore[override]
        return BaselineKey(self.source, self.coin_set, self.lookback)
