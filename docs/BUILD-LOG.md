# BUILD-LOG

Handoff log for the Signals build. Each milestone agent **appends exactly one
entry on completion** (never edits earlier entries; corrections go in your own
entry's Deviations). Every agent **reads this file and the approved plan
(`phase_1_signals_loop_*.plan.md`) before starting**, and treats the
"Contracts frozen" sections below as verbatim API: table names, module paths,
function signatures and constant names are used exactly as written, not
re-derived. The spec (`self-learning-crypto-signals.md`) stays the source of
truth for numbers; thresholds live only in `repo/api/app/core/constants.py`.

Paper only. There is no order endpoint in any phase.

## Entry format

Copy this template to the end of the file and fill every heading (write
"None" rather than deleting one).

```
## M<id> - <name>

- Date: <YYYY-MM-DD>
- Model: <model used>

### Files created or modified
- `path` - one-line purpose

### Contracts frozen
Exact names later milestones must use verbatim: table names (and key
columns), module paths, function/class signatures, constant names, CLI
invocations, enum/string values.

### Deferred to a later milestone
- <item> -> M<n>

### Deviations from the plan
- <what changed from the plan or the brief, and why> (or "None")
```

Optional, when relevant: **Ambiguities** - spec lines that were unclear or
self-contradictory, quoted, with the choice made.

---

## M0 - Foundations

- Date: 2026-10-02
- Model: Claude Sonnet 5.5

### Files created or modified
- `repo/docs/BUILD-LOG.md` - this handoff convention and the M0 entry.
- `repo/api/requirements.txt` - added alembic, psycopg[binary], httpx, pandas, numpy, apscheduler, websockets, pytest, pytest-asyncio, tzdata (all `>=`).
- `repo/api/app/core/__init__.py` - package marker (empty).
- `repo/api/app/core/constants.py` - every frozen threshold, grouped by spec section.
- `repo/api/app/core/time.py` - UTC sessions, weekend, US cash session with DST, vertical-barrier deadline, event-window arithmetic.
- `repo/api/app/core/hashing.py` - canonical serializer, `feature_hash`, `rule_hash`, `code_version`.
- `repo/api/app/core/db_sync.py` - sync SQLAlchemy engine/session on psycopg 3.
- `repo/api/alembic.ini`, `repo/api/alembic/env.py`, `repo/api/alembic/script.py.mako`, `repo/api/alembic/versions/` - Alembic wiring; NO revisions, NO models.
- `repo/api/pytest.ini` - `testpaths = tests`, `pythonpath = .`, asyncio auto mode.
- `repo/api/tests/test_constants.py`, `test_time.py`, `test_hashing.py`, `test_db_sync.py` - 74 tests; `tests/conftest.py` left empty.

### Contracts frozen

**Modules:** `app.core.constants`, `app.core.time`, `app.core.hashing`, `app.core.db_sync`. Run Alembic from `repo/api` as `python -m alembic ...`; `alembic/env.py` reads the URL via `app.core.db_sync.get_sync_database_url()`. M1 must set `target_metadata` in `alembic/env.py` (marked with a comment) and add `app/models/base.py` content.

**Numeric types in `constants.py`:** ratios, z-scores, quantiles, ATR/R multiples and size multipliers are `float`. Basis-point costs are `Decimal` (`TAKER_FEE_BPS_PER_SIDE`, `SLIPPAGE_BPS_PER_SIDE`, `ROUND_TRIP_COST_BPS`). Counts/hours are `int`. Containers are `tuple` or `MappingProxyType`. Horizons are the strings `'scalp'`, `'swing'`, `'position'`; families `'trend'`, `'chop'`; sessions `'asia'`, `'europe'`, `'us'`, `'late'`; event kinds `'cpi'`, `'fomc'`, `'expiry'`.

**Constant groups in `constants.py`** (names are the contract): `RULE_VERSION`; horizons/barriers (`HORIZONS`, `VERTICAL_BARRIER_HOURS`, `LONGEST_HORIZON_HOURS`); costs (`TAKER_FEE_BPS_PER_SIDE`, `SLIPPAGE_BPS_PER_SIDE`, `SIDES_PER_ROUND_TRIP`, `ROUND_TRIP_COST_BPS`); `LOOKBACK_DAYS`; efficiency (`EFFICIENCY_BARS`, `EFFICIENCY_TIMEFRAME`, `EFFICIENCY_TREND_MIN`, `EFFICIENCY_CHOP_MAX`, `FAMILY_TREND`, `FAMILY_CHOP`, `FAMILIES`); `OI_CHANGE_DEAD_ZONE_SIGMA`; beta/residual (`BETA_LOOKBACK_DAYS`, `BETA_FIT_ENDS_BEFORE_SIGNAL_BAR`, `ALT_RESIDUAL_Z_GATE`); CVD (`CVD_BARS`, `CVD_TIMEFRAME`, `CVD_PRICE_EXTREME_BARS`); volume profile (`VOLUME_PROFILE_BINS`, `VOLUME_PROFILE_VALUE_AREA`); squeeze (`SQUEEZE_TIMEFRAME`, `BOLLINGER_PERIOD`, `BOLLINGER_SIGMA`, `KELTNER_PERIOD`, `KELTNER_ATR_MULT`); swing/chandelier (`SWING_TIMEFRAME`, `SWING_FRACTAL_BARS`, `CHANDELIER_ATR_MULT`, `ATR_PERIOD`); pullback (`PULLBACK_RSI_PERIOD`, `PULLBACK_RSI_LONG_BAND`, `PULLBACK_RSI_SHORT_BAND`); options (`OPTIONS_SNAPSHOT_MAX_AGE_MINUTES`, `EXPIRY_PIN_*`, `HEADWIND_*`, `AMPLIFY_*`, `DAMPEN_*`, `ALIGN_SIZE_MULTIPLIER`, `VOL_RICH_QUANTILE`, `VOL_CHEAP_QUANTILE`, `VOL_SPREAD_LOOKBACK_DAYS`, `OPTIONS_VERDICT_PRECEDENCE`, `OPTIONS_VOL_TAGS`, `OPTIONS_EXPRESSIONS`, `OPTIONS_OI_FLOOR_BY_COIN`); pools (`POOL_MIN_INDEPENDENT_OUTCOMES`, `POOL_WINDOW_DAYS`, `CALIBRATION_BIN_MIN_OUTCOMES`, `CONFIDENCE_TRUST_MIN_BIN_OUTCOMES`, `INTERVAL_CONFIDENCE`, `POOL_DIM_*`, `POOL_WALK_UP_LEVELS`); model floors (`LIGHTGBM_MIN_INDEPENDENT_LABELS`, `SEQUENCE_MODEL_MIN_INDEPENDENT_LABELS`, `KILL_RULE_INDEPENDENT_LABELS`); events (`EVENT_KIND_*`, `EVENT_WINDOW_MINUTES`); stand-down (`PERP_SPOT_MAX_DISAGREEMENT_ATR`); locked book/promotion (`LOCKED_BOOK_ONE_IN`, `PROMOTION_*`); sessions (`SESSION_*`, `SESSION_BOUNDARIES_UTC_HOURS`, `WEEKEND_*`, `US_CASH_TIMEZONE`, `US_CASH_HOURS_UTC_DST`, `US_CASH_HOURS_UTC_STANDARD`); `HASH_ALGORITHM`.

**`POOL_WALK_UP_LEVELS`** is `((level_name, (dimension, ...)), ...)` in walk order: `full_slice`, `drop_event_flag`, `drop_session`, `trend_x_vol`, `setup_family`. Dimension names: `POOL_DIM_SETUP_FAMILY='setup_family'`, `'trend_family'`, `'vol_percentile'`, `'session'`, `'event_flag'`.

**`app.core.time`** (all instants must be tz-aware; naive raises `ValueError`):
```python
UTC: timezone
def utc_now() -> datetime
def ensure_utc(instant: datetime) -> datetime
@dataclass(frozen=True) class SessionInfo: bucket: str; session: str; session_date: date; folded: bool
def raw_session_bucket(instant: datetime) -> str            # asia|europe|us|late, no folding
def classify_session(instant: datetime) -> SessionInfo
def is_weekend(instant: datetime) -> bool
def is_us_dst(instant: datetime) -> bool
def us_cash_window_utc(day: date) -> tuple[datetime, datetime]
def is_us_cash_open(instant: datetime) -> bool
def horizon_hours(horizon: str) -> int
def vertical_barrier_deadline(entry_time: datetime, horizon: str) -> datetime
class EventTime(NamedTuple): kind: str; at: datetime
def event_window(kind: str, at: datetime) -> tuple[datetime, datetime]
def active_event_windows(instant: datetime, events: Iterable[tuple[str, datetime]]) -> list[EventTime]
def in_event_window(instant: datetime, events: Iterable[tuple[str, datetime]]) -> bool
```
Session value used in the regime slice is `SessionInfo.session` (`asia|europe|us`; a folded late instant reports `asia` with `session_date` = next UTC date). Intervals: sessions, weekend and US cash are half-open `[start, end)`; weekend is Fri 21:00 inclusive to Sun 21:00 exclusive; event windows are CLOSED `[event - before, event + after]`. `vertical_barrier_deadline` takes the FILL time (next bar's open), not the signal bar close.

**`app.core.hashing`:**
```python
CODE_VERSION_UNKNOWN = 'unknown'; DIRTY_SUFFIX = '-dirty'; REPO_ROOT: Path
def canonical_serialize(value: Any) -> str                 # TypeError on undefined types
def feature_hash(features: Mapping[str, Any]) -> str       # sha256 hex, 64 chars
def frozen_constants() -> dict[str, Any]
def rule_hash_input(constants: Mapping[str, Any] | None = None, rule_version: str | None = None) -> str
def rule_hash(constants: Mapping[str, Any] | None = None, rule_version: str | None = None) -> str
def code_version() -> str                                  # '<short sha>' | '<short sha>-dirty' | 'unknown'
```
Feature value types with a defined rule: `None`, `bool`, `int`, `float` (shortest round-trip `repr`, `-0.0` folded to `0.0`, `nan`/`inf` literal), `Decimal` (normalized, finite only), `str`, aware `datetime` (to UTC), `date`, mappings with `str` keys, `list`/`tuple`, and numpy bool/int/float scalars. `int 1`, `float 1.0` and `Decimal 1` hash differently on purpose. Everything else, including `set`, raises `TypeError`. **Callers must pass the same Python types every time** (do not let a feature flip between `int` and `float`).

**Rule hash derivation (choice and reason):** `sha256(canonical_serialize({"rule_version": RULE_VERSION, "constants": {NAME: value for public UPPERCASE names in constants.py except RULE_VERSION}}))`. It hashes VALUES, not file bytes, so editing a comment, docstring or formatting does not change it, while changing, adding or removing any threshold, or bumping `RULE_VERSION`, does. Consequence: any new UPPERCASE name added to `constants.py` changes the rule hash, and its value type must be serializable (a test enforces this). Use lowercase or `_`-prefixed names for non-frozen helpers.

**Code version:** `git rev-parse --short HEAD` in the repo root, plus `-dirty` if `git status --porcelain` is non-empty (untracked files count). Returns `'unknown'` when git is unavailable. It is NOT cached; callers that stamp many rows should call it once per run.

**`app.core.db_sync`:**
```python
SYNC_DRIVERNAME = 'postgresql+psycopg'
def get_sync_database_url(database_url: str | None = None) -> str   # from Settings.database_url
def get_sync_engine() -> Engine
def get_sync_session_factory() -> sessionmaker[Session]
def get_sync_db() -> Generator[Session, None, None]
def check_sync_database() -> dict[str, str]
def dispose_sync_engine() -> None
```
`app/db.py` (asyncpg) is untouched.

**Alembic:** `repo/api/alembic.ini`, `script_location = %(here)s/alembic`, sync psycopg URL from settings, `target_metadata = None` until M1. Revision file names: `YYYYMMDD_HHMM_<rev>_<slug>`, timezone UTC.

### Deferred to a later milestone
- All tables, models and the first revision, and setting `target_metadata` -> M1.
- Event calendar table; the definition of a "large" expiry (feeds `in_event_window`) -> M1/M5.
- Per-feed staleness limits for the "feed past its available-at rule" stand-down (the spec gives a number only for Deribit: 15 minutes) -> M2 (`feed_health`).
- The locked-book bucket function (`hash(coin, bar_time)`); only `LOCKED_BOOK_ONE_IN = 5` is frozen here -> M4. It must hash with `HASH_ALGORITHM` over a canonical text of coin and bar time and be computed before any outcome exists.
- The per-coin OI floor for `ignore`: `OPTIONS_OI_FLOOR_BY_COIN` is an EMPTY mapping because the spec says "a floor you set by hand" and gives no number -> owner/M5. Empty means no floor is configured; M5 must not treat that as "floor satisfied" silently.
- A definition of "near-dated open interest" (headwind denominator) -> M5.
- Feature math, ATR implementation, trend-efficiency state machine and its cold-start rule -> M3.

### Deviations from the plan
- Added `tzdata` to `requirements.txt` (not in the brief's list): Windows has no system timezone database, so `zoneinfo.ZoneInfo('America/New_York')` needs it. It was already present transitively via pandas; pinning it explicitly keeps the DST logic from breaking if that changes. No `pytz` or `python-dateutil` was added.
- `ATR_PERIOD = 14` is an ASSUMPTION (see Ambiguities); it is the only constant not taken from the spec text.
- `PULLBACK_RSI_*`, `DAMPEN_TARGET_CAP_R`, `OPTIONS_*` tuples and the `POOL_DIM_*` names are structural encodings of spec rules, not new thresholds.
- The working tree already held ten zero-byte placeholder files from an earlier attempt (`app/core/*`, `app/models/*`, `app/db_sync.py`, `alembic/*`, `alembic.ini`, `pytest.ini`, `tests/*`). I overwrote the ones I own. `app/models/__init__.py`, `app/models/base.py` (M1's) and a stray empty `app/db_sync.py` (wrong path; the real module is `app/core/db_sync.py`) were left untouched. M1 should delete `app/db_sync.py`. `api/.env.example` also shows an uncommitted edit that is not mine.

### Ambiguities (spec quotes and choices)
1. **Late session vs US cash.** Spec: "late 21:00-24:00 folded into the next Asia date unless the US cash session is open". With cash hours of 13:30-20:00 (DST) or 14:30-21:00 (standard), cash is never open between 21:00 and 24:00, so the "unless" can never fire on a real clock time. Implemented literally (the branch exists and is tested with a monkeypatch); the practical result is that late always folds. Cash windows are half-open, so exactly 21:00 standard time is closed.
2. **Pool walk-up has a redundant step.** Spec: "full slice, then drop the event flag, then session, then trend x vol, then the setup family". The regime slice is trend x vol x session x event, so "drop session" already yields trend x vol. Kept both as separate named levels with identical dimensions (setup family included at every level); M8 may skip a level whose cell equals the previous one.
3. **ATR period is never stated.** Used by the chandelier stop, Keltner, and every "in ATR" distance. Assumed Wilder 14 (`ATR_PERIOD`). The owner should confirm before M3 relies on it.
4. **Event window endpoints.** Spec: "from 30 minutes before CPI or FOMC until 30 minutes after". Treated as closed on both ends (conservative for a stand-down).
5. **Weekend end.** "Friday 21:00 UTC through Sunday 21:00 UTC": start inclusive, end exclusive, so Sunday 21:00 sharp is not weekend.
6. **US cash ignores weekends and holidays.** The spec gives clock hours only, so `is_us_cash_open` is a pure time-of-day test.
7. **"Large expiry"** is undefined in the spec (size or OI threshold); `in_event_window` takes whichever expiries the caller passes.
8. **Pool floor wording.** Abstain uses "30 independent outcomes", the edge line "30 independent outcomes in 90 days". One constant (`POOL_MIN_INDEPENDENT_OUTCOMES`) plus `POOL_WINDOW_DAYS` serves both.
9. **Percentile direction names.** "80th percentile" stored as the quantile `0.80`, "20th" as `0.20`.
10. **Vertical-barrier origin.** The spec measures horizons "on the horizon you actually hold" from the entry, which is the next bar's open; the deadline function takes that fill time.

### Verification
- `cd repo/api; & '.\.venv\Scripts\python.exe' -m pytest` -> `74 passed in 3.39s`.
- `python -m alembic current` from `repo/api` against live `signals` (Postgres 18.6): connects over psycopg, logs `Context impl PostgresqlImpl.` / `Will assume transactional DDL.`, prints no revision. `python -m alembic heads` prints nothing (no revisions yet). `public` schema table count remains 0.
- Installed versions: alembic 1.20.0, psycopg 3.3.6 (+binary), httpx 0.28.1, pandas 3.0.6, numpy 2.5.3, APScheduler 3.11.3, websockets 17.1, pytest 9.1.1, pytest-asyncio 1.4.0, tzdata 2026.4.

---

## M1 - Schema and available-at discipline

- Date: 2026-10-02
- Model: Claude Opus 5

### Files created or modified
- `repo/api/app/models/base.py` - declarative `Base`, the exact-numeric column aliases, the `SurrogateKey` / `CreatedAt` / `FeedRow` mixins, constraint naming convention (was a 0-byte placeholder).
- `repo/api/app/models/enums.py` - closed vocabularies (re-exported from `constants` where they exist) and the `one_of` / `in_range` / `non_negative` CHECK helpers.
- `repo/api/app/models/feeds.py` - `candle`, `open_interest`, `funding`, `force_order`, `event_calendar`, `feed_health`.
- `repo/api/app/models/options.py` - `options_snapshot`, `options_metric`, both keyed by venue.
- `repo/api/app/models/features.py` - `feature_row` plus `SHADOW_METRIC_KEYS`.
- `repo/api/app/models/decisions.py` - `candidate` and the `sample_group_id` design (the module docstring is the design record).
- `repo/api/app/models/labels.py` - `label`, `LABEL_ONLY_COLUMNS`, `MFE_DIAGNOSTIC_COLUMNS`.
- `repo/api/app/models/observability.py` - `job_run`, `promotion_log`.
- `repo/api/app/models/as_of.py` - the available-at read gate: `as_of()`, `AsOf`, `AsOfError`, `FEED_MODELS`.
- `repo/api/app/models/__init__.py` - imports every model module so `Base.metadata` is complete (was a 0-byte placeholder).
- `repo/api/alembic/env.py` - `target_metadata = Base.metadata` (the only change; the M0 TODO comment is gone).
- `repo/api/alembic/versions/20261002_1620_7cd7d7f2e800_initial_schema.py` - the first revision; its docstring lists what the hand review changed.
- `repo/api/tests/conftest.py` - DB fixtures (throwaway schema + per-test rollback) and the `make_feature_row` / `make_candidate` factories (was M0's empty file).
- `repo/api/tests/test_as_of.py` - 28 tests: the available-at guard rail.
- `repo/api/tests/test_sample_group.py` - 14 tests: independence grouping.
- `repo/api/tests/test_schema_constraints.py` - 44 tests: everything the schema refuses, including the duplicate candle.
- `repo/api/tests/test_models_schema.py` - 60 tests: schema-wide invariants off the metadata, plus the source-tree feature-path scan.
- `repo/api/tests/test_migration_matches_models.py` - 3 tests: the revision and the models describe the same database.
- DELETED `repo/api/app/db_sync.py` - 0-byte stray at the wrong path (the real module is `app/core/db_sync.py`), and `app/models/__pycache__/` stale entries.
- `repo/api/requirements.txt` - NOT touched. No dependency was added.

### Contracts frozen

**Modules:** `app.models` (importing it is what completes `Base.metadata`), `app.models.base`, `.enums`, `.feeds`, `.options`, `.features`, `.decisions`, `.labels`, `.observability`, `.as_of`. Alembic revision `7cd7d7f2e800`.

**Tables, primary key, uniqueness.** Every table has a `bigint` identity `id` unless stated. `label`'s primary key IS `candidate_id`.

| Table | PK | UNIQUE |
|---|---|---|
| `candle` | `id` | `(coin, market, timeframe, open_time)` |
| `open_interest` | `id` | `(coin, timeframe, poll_time)` |
| `funding` | `id` | `(coin, funding_time, kind)` |
| `force_order` | `id` | `(dedup_key)` only - see Ambiguities |
| `event_calendar` | `id` | `(kind, coin, event_time)` NULLS NOT DISTINCT |
| `feed_health` | `id` | `(feed, coin)` NULLS NOT DISTINCT |
| `options_snapshot` | `id` | `(venue, coin, snapshot_time)` |
| `options_metric` | `id` | `(venue, coin, snapshot_time, horizon)` |
| `feature_row` | `id` | `(coin, horizon, signal_timeframe, bar_close, source)`; plus `(id, coin, horizon, bar_close, source)` as the target of `candidate`'s composite FK |
| `candidate` | `id` | `(coin, horizon, bar_close, source)`; plus `(id, sample_group_id, horizon, source)` as the target of the sample-group FK |
| `label` | `candidate_id` | - (the PK is the uniqueness) |
| `job_run` | `id` | - |
| `promotion_log` | `id` | `(layer, decided_at)` |

**Column naming is the unit contract:** `*_price` price in quote terms, `*_atr` distance in ATR units, `*_z` 90-day z-score, `*_pct` 90-day quantile in 0-1, `*_bps` basis points, `*_r` a multiple of 1R, `*_time`/`*_at` an aware UTC instant. Numeric aliases in `app.models.base`: `Price` 24,10 · `Size` 32,10 · `Rate` 18,12 · `RValue` 14,6 · `AtrUnits` 14,6 · `ZScore` 14,6 · `Quantile` 7,6 · `Bps` 14,6 · `Ratio` 12,8 · `Multiplier` 8,4 · `Hours` 12,4 · `Influence` 6,5 · `Sign` smallint. No `float` column exists anywhere and a test enforces that. Every timestamp is `timestamptz`; the only `date` column in the schema is `feature_row.session_date`.

**The `as_of` gate - how M3 must call it.** `app.models.as_of` exports exactly `as_of`, `AsOf`, `AsOfError`, `FEED_MODELS`.

```python
from app.core import constants as C
from app.models.as_of import as_of
from app.models.feeds import Candle, Funding

reader = as_of(bar_close)                      # bar_close = the signal bar's close, tz-aware

# the normal read: select() has already added `available_at <= bar_close`
bars = session.execute(
    reader.select(Candle)
    .where(Candle.coin == coin, Candle.market == 'perp', Candle.timeframe == C.CVD_TIMEFRAME)
    .order_by(Candle.open_time.desc())
    .limit(C.CVD_BARS)
).scalars().all()

# scalar columns work the same way
closes = session.execute(
    reader.select(Candle.close_price).where(Candle.coin == coin).order_by(Candle.open_time)
).scalars().all()

# the canonical "last settled funding print I was allowed to see"
print_ = reader.latest(session, Funding, coin=coin, kind='settled', order_by=Funding.funding_time)

# a statement you had to build yourself (join, aggregate, window)
stmt = reader.gate(select(func.sum(Candle.taker_buy_base_volume)).where(Candle.coin == coin))
```

Rules M3 inherits: `select(*entities)` accepts ONLY feed entities and raises `AsOfError` for `FeatureRow`, `Candidate`, `Label`, `FeedHealth` or a raw `Table`. `gate(statement)` raises if the statement contains no feed table, and raises if a feed table is hidden inside a subquery or CTE (gate the inner select and call `.subquery()` on the result). The comparison is `available_at <= bar_close`, INCLUSIVE, so the signal bar itself is visible - the entry is the next bar's open. **The resolver and ingest are not exceptions:** they call `as_of(utc_now())` live or `as_of(replay_clock)` in replay. There is one read path for feeds, and `tests/test_models_schema.py::test_no_module_outside_app_models_reads_a_feed_without_the_gate` scans `app/**` and fails on a bare `select(Candle)`.

`FEED_MODELS` = `Candle, OpenInterest, Funding, ForceOrder, EventCalendar, OptionsSnapshot, OptionsMetric`. Enrolment is by subclassing `FeedRow` AND being listed in `FEED_MODELS`; a test fails if a mapped `FeedRow` subclass is missing from the tuple, so a feed added in M2+ cannot silently escape the gate. `feed_health` is deliberately NOT a feed row.

**Vocabularies.** Re-exported from `constants` (never restated): `HORIZONS`, `TREND_FAMILIES`, `OPTIONS_VERDICTS`, `OPTIONS_VOL_TAGS`, `OPTIONS_EXPRESSIONS`, `POOL_LEVELS`, `REGIME_SESSIONS`, `SESSION_BUCKETS`, `TIMEFRAMES` (`15m`,`1h`,`4h`, the union of the timeframe constants), `STAND_DOWN_EVENT_KINDS`. Defined in `app.models.enums` (structural, not thresholds - see Ambiguities): `DIRECTIONS` (`long`,`short`), `CANDIDATE_STATUSES` (`alert`,`watch`,`cancelled`,`delayed`,`abstained`), `STATUSES_REQUIRING_REASON`, `SOURCES` (`live`,`replay`), `TRIGGERS` (`swing_break`,`squeeze`,`vwap_tag`,`pullback_reset`), `FIRST_BARRIERS` (`upper`,`lower`,`vertical`), `OI_PRICE_QUADRANTS` (four states plus `price_up_oi_flat`/`price_down_oi_flat` for the dead zone), `OPTIONS_VENUES` (`deribit`,`ibit`,`cme`), `OPTION_SIDES`, `LEAD_VENUES` (`spot`,`perp`,`cme`), `CANDLE_MARKETS` (`perp`,`spot`), `FUNDING_KINDS` (`settled`,`predicted`), `FORCE_ORDER_SIDES`, `SAMPLE_GROUP_REASONS` (`new`,`overlap`,`btc_impulse`), `EVENT_KINDS` (stand-down kinds plus `unlock`, `funding_settlement`), `JOB_STATUSES`, `FEED_STATUSES`, `PROMOTION_DECISIONS`, `DEALER_GAMMA_SIGNS`.

**Postgres native ENUM vs text + CHECK: text + CHECK, deliberately.** A native enum puts the vocabulary in the database catalog, a second source of truth beside `constants.py`. Widening one is `ALTER TYPE ... ADD VALUE`, which Alembic's autogenerate does not detect, so the migration has to be hand-written anyway; narrowing or renaming a value means creating a new type, rewriting every column that uses it, and dropping the old one. `text` plus a named CHECK is a two-statement migration (`DROP CONSTRAINT` / `ADD CONSTRAINT`), shows up in autogenerate as a constraint diff, and keeps the authoritative list in Python where `rule_hash` sees the values that came from `constants.py`. Cost: one byte per value over the enum's four, and no cross-table type reuse. Note for later milestones: `CHECK (col IN (...))` passes when `col` is NULL, so nullability is controlled by `nullable=` alone. Autogenerate does NOT compare CHECK constraints at all - they are rendered in full for new tables only, so a revision that CHANGES a CHECK must be written by hand.

**`sample_group_id`: assigned at candidate creation, by M4, never rewritten.** Type `uuid`, NOT NULL, indexed. Columns: `sample_group_id`, `sample_group_leader_id` (nullable self-reference), `sample_group_reason` in `new` / `overlap` / `btc_impulse`.

Reasoning, because M8's statistics are only valid if this is right:

1. **It cannot depend on an outcome.** If the group were resolved later from how the trades turned out, independence would become a function of results and every sample floor would be contaminated by hindsight. So it is assigned when the candidate is written, from decision-time data only.
2. **It must be known while the card is on screen,** because "Card counts use that sample, not the raw alert count" - the n on the card has to be the number the stats engine will later count.
3. **The grouping key does not need to encode the group's shape as data.** Two shapes have to collapse: overlapping holds on one coin and horizon, and a cluster of alts riding one BTC impulse. A single composite key cannot express both (one is coin-scoped, the other spans coins). Inheritance from a named leader expresses both, and a cluster that is *both* an overlap and a BTC impulse collapses into one group for free, because the group is the transitive closure of the leader pointers.

How M4 assigns it:
- **`overlap`** - a candidate already exists on the same `(coin, horizon, source)` whose `vertical_deadline` is later than this candidate's `decision_time`, i.e. the holds can overlap. Inherit its `sample_group_id`. The test is the FROZEN deadline, not the actual resolution, so a prior trade that resolved early still swallows the new one. That is deliberately conservative: under-counting independent samples delays a promotion, over-counting invalidates it.
- **`btc_impulse`** - this is an alt candidate that only exists because the one-BTC-view gate let it through on a BTC impulse. Inherit the BTC candidate's group, so a fifth long in the same BTC move is not a fifth sample.
- **`new`** - neither applies. Mint `uuid4()` and leave the leader NULL.

Three invariants are enforced by the database, not by convention, through one composite self-FK (`fk_candidate_sample_group_leader`: `(sample_group_leader_id, sample_group_id, horizon, source)` -> `uq_candidate_sample_group_identity`): a leader must carry the SAME `sample_group_id`, the SAME `horizon` and the SAME `source`. So a replay row cannot join a live group, a scalp cannot join a swing group ("one decision per coin per horizon" makes horizons independent), and nobody can point at a leader while minting a different group id. When the leader is NULL the FK is satisfied automatically (Postgres MATCH SIMPLE), which is exactly the `new` case. A CHECK pins `(leader IS NULL) = (reason = 'new')`.

**How M8 counts.** Independent outcomes:
```sql
SELECT count(DISTINCT c.sample_group_id)
FROM label l JOIN candidate c ON c.id = l.candidate_id
WHERE c.source = 'live' AND ...
```
To collapse a group to one observation, rank within the group and take the first:
```sql
ROW_NUMBER() OVER (PARTITION BY c.sample_group_id ORDER BY c.decision_time, c.id) = 1
```
M8 owns the choice between "first of group" and "mean of group"; the schema supports both and neither needs a schema change. **M8 must confirm which it uses and state it.**

**Label-only columns: how they are made unreachable, not merely forbidden.** `LABEL_ONLY_COLUMNS` = `cmi_btc_return`, `cmi_eth_btc_return`, `cmi_dxy_return`, `cmi_nasdaq_return`, `cmi_alt_residual_return`.
1. They exist only on `label`, which is written at resolution. A test asserts that none of those names appears on ANY other table.
2. `as_of` is the only sanctioned decision-time read path and it accepts only `FeedRow` subclasses. `Label` is not one, so `as_of(bar_close).select(Label)` and `as_of(bar_close).select(Label.cmi_alt_residual_return)` raise `AsOfError`. There is no argument that makes them work, and `gate(select(Label))` raises too (no feed table to gate). Tests cover all three.
3. The decision-time counterpart of the residual is on `feature_row` under a deliberately different, self-describing name - `alt_residual_z_signal_bar` ("residual strength ON the signal bar") versus `cmi_alt_residual_return` ("residual over the HOLD") - so nobody reaching for "the residual" lands on the label column by accident.
4. `MFE_DIAGNOSTIC_COLUMNS` (`mfe_r`, `hours_to_mfe`) are the weaker category: legitimate label columns that may be shown on the tracker but must not redefine a miss. A test keeps them off `feature_row`.

**Shadow metrics.** `feature_row.shadow_metrics` is NOT NULL `jsonb` defaulting to `'{}'`, with a CHECK that it is an object. `SHADOW_METRIC_KEYS` (in `app.models.features`) is its expected vocabulary: `liquidation_distance_atr`, `liquidation_distance_estimated`, `liquidation_print_rate_per_min`, `iv_rank`, `iv_term_slope`, `top_of_book_imbalance`, `exchange_netflow_z`, `stablecoin_net_issuance_z`. A test asserts none of them has a typed column, which is what keeps a card renderer or a rule from reaching them by attribute access.

**Columns for feeds that do not exist yet** (present, nullable, no default, never zero-filled): `feature_row.cme_basis_z`, `feature_row.etf_net_flow_z`, `feature_row.ibit_dealer_gamma_sign`, `feature_row.cme_dealer_gamma_sign`, `label.cmi_dxy_return`, `label.cmi_nasdaq_return`, `event_calendar.is_large`/`notional_usd`. The richer IBIT and CME option figures do not need columns: `options_metric` is keyed by venue, so those rows simply do not exist yet. Tests assert the nullability and the absence of a server default.

**`candidate` price layers, and who writes what.** `pre_options_stop_price` (NOT NULL, the chandelier stop from the frozen rules) and `pre_options_target_price` are M4's. `stop_price` (NOT NULL, after an amplify widening) and `target_cap_price` (the headwind / expiry_pin cap) are M5's. `entry_price`, `one_r`, `upper_barrier_price`, `lower_barrier_price` and `vertical_deadline` are NULL at creation and written exactly once, by whoever observes the next bar's open, because the entry IS that open and is not knowable when the decision is taken. `lower_barrier_price` always equals `stop_price` and a CHECK says so. A CHECK forbids `status = 'alert'` with a NULL `options_verdict`, which is the schema-level form of "freeze the barriers only after the options check".

**`stored_r = price_r - fees_r + funding_r`**, with `fees_r` a non-negative cost magnitude and `funding_r` signed (positive = received). A CHECK enforces that to within `1e-6`, one unit in the last place of `numeric(14, 6)`, purely to absorb per-component rounding. **M6 must make its parts add up**; it cannot store a stored R that does not reconcile. `mae_r` is a non-negative magnitude.

**Other enforced invariants worth knowing before you write a row:** `candle.available_at >= close_time`; `open_interest.available_at >= poll_time`; `options_*.available_at >= snapshot_time`; `funding.available_at >= funding_time` for `kind = 'settled'` only (a `predicted` row legitimately points at a future settlement); `taker_buy_base_volume <= base_volume`; `candidate.bar_close <= decision_time`; a non-`alert` status must carry a `status_reason`; `expected_r` may not be stored without `pool_level` AND `pool_n`; `options_influence` in [-1, 1]; `options_confidence`, `p_upper_before_stop`, every `*_pct` and every `*_share` in [0, 1]; `feature_row.beta_fit_end <= bar_close`; `promotion_log.bin_edges` NOT NULL and a json array or object. A composite FK (`fk_candidate_feature_row`) makes the database refuse a candidate whose feature row belongs to a different coin, horizon, signal bar or source.

**Test fixtures M2+ can reuse:** `tests/conftest.py` exposes `db_engine`, `db_schema` (a throwaway Postgres schema holding the whole metadata, dropped at the end), `session` (per-test transaction, always rolled back; `join_transaction_mode='create_savepoint'`, so a test may `commit()`), `BAR_CLOSE`, and the factories `make_feature_row(**overrides)` and `make_candidate(session, **overrides)` (which creates the matching feature row and accepts `leader=` to inherit a sample group). The DB fixtures SKIP rather than fail when Postgres is unreachable, so the metadata tests still run.

### Deferred to a later milestone
- Per-feed staleness limits (`feed_health.max_staleness_seconds` is nullable; NULL means "no rule configured" and must NOT be read as "fresh") -> M2.
- Which open-interest history periods to poll: `open_interest.timeframe` deliberately has NO CHECK because Binance's period vocabulary is wider than our kline timeframes -> M2 owns it.
- `force_order.dedup_key`: optional ingest-side hash so an at-least-once reinsert is idempotent -> M2.
- The `setup_family` vocabulary: free text, because the spec names it as a pool dimension but never enumerates it. **M4 must freeze the list and say so in its entry**; M8's pool walk-up keys on it.
- The locked-book bucket function (`locked_book` is a plain NOT NULL bool here) -> M4.
- Whether an independent sample is the group's first row or the group's mean -> M8 (both supported; M8 must state its choice).
- A raw storage table for ETF flow, CME basis, DXY and Nasdaq. None exists: the brief's table list is closed and those feeds do not exist in Phase 1, so only the feature/label columns are provided. **When a feed arrives, whoever adds it needs a new table and a migration** - the column alone has no available-at of its own.
- Table-level `COMMENT`s. Column comments are in the schema; table comments are not.

### Deviations from the plan or the brief
- **`bar_time` -> `bar_close`.** The plan's schema sketch says `feature_row (coin, horizon, bar_time, ...)`. I named the column `bar_close` on both `feature_row` and `candidate`, because "bar time" does not say open or close and storing `open_time` there would silently shift every feature by one bar. `as_of(bar_close)` compares against this instant. **M4's locked-book hash of "coin and bar time" must use `candidate.bar_close`.**
- **`candle.market` added** (`perp` / `spot`), and it is part of the natural key, so the required uniqueness is `(coin, market, timeframe, open_time)` rather than `(coin, timeframe, open_time)`. It is unavoidable: the "last completed perp and spot bars disagree by more than 1 ATR" stand-down, the perp-basis z-score and the lead-venue column all need both series, and without `market` they would collide on the same key.
- **`funding.kind` added** (`settled` / `predicted`) and it is part of the natural key. The spec reads the settled print for the z-score and "the current rate" for the live carry band; without a kind those are the same row.
- **`feature_row.signal_timeframe` added** and included in its natural key. The brief's key is coin, horizon and bar time; a horizon does not imply a bar cadence, so without it two timeframes computed for one horizon would collide.
- **`candidate` carries both `decision_time` and `bar_close`.** The brief asks for the decision time; the gate instant and the signal bar are a separate fact (they differ by however long the rules took), and the locked book hashes the bar.
- **Two redundant UNIQUE constraints exist on purpose:** `uq_feature_row_identity` and `uq_candidate_sample_group_identity`. Both are redundant as keys (`id` is already unique) and exist solely as targets of composite foreign keys, which is what lets the database enforce the feature-row/candidate match and the sample-group invariants.
- **An EXCLUDE constraint for non-overlapping live alerts was considered and rejected.** `EXCLUDE USING gist (coin WITH =, horizon WITH =, tstzrange(decision_time, vertical_deadline) WITH &&) WHERE (status = 'alert' AND source = 'live')` would hard-enforce "one open signal per coin per horizon", but it needs the `btree_gist` extension (a privileged `CREATE EXTENSION` on every box including the CX23), and cancelled, delayed and abstained candidates legitimately overlap because they are resolved as shadow rows. The rule stays M4's, and independence is counted through `sample_group_id`.
- **I filled M0's empty `tests/conftest.py`** rather than adding a separate fixture module. It is the pytest-idiomatic location and M0's entry only says it was left empty, not that it was reserved.
- **No dependency was added.** `requirements.txt` is untouched; its uncommitted diff is still M0's. `api/.env.example` was left alone as instructed.
- Housekeeping done as asked: `app/models/__init__.py` and `app/models/base.py` replaced, `app/models/__pycache__/` cleared, the 0-byte `app/db_sync.py` stray deleted.
- One constant I would challenge but did NOT change: nothing. `constants.py`, `time.py`, `hashing.py` and `core/db_sync.py` are byte-identical to M0's. `ATR_PERIOD = 14` is still the open assumption M0 flagged, and `OPTIONS_OI_FLOOR_BY_COIN` is still empty; both matter to M3/M5, not to the schema.

### Ambiguities (spec quotes and choices)
1. **"Upper barrier: +1R in the trade direction" - upper in R space, not in price space.** For a short the "upper" barrier sits at a LOWER price than the entry. I kept the spec's names (`upper_barrier_price`, `lower_barrier_price`, and `first_barrier` in `upper`/`lower`/`vertical`) for contract fidelity, and added a CHECK that makes storing them the wrong way round impossible: long requires `upper > entry > lower`, short requires `upper < entry < lower`. This is the single most likely silent sign error in the project and it is now a constraint, not a comment.
2. **Status is one column, not several flags.** The spec gives "Action: Long / Short / Watch" on the card, "Every cancelled or delayed candidate", and the abstain rule, without ever saying whether those are one state machine. I made `status` a single column over `alert|watch|cancelled|delayed|abstained` with a `status_reason` that is required for all four non-alert states, and `direction` a separate NOT NULL column (`watch` is a status, not a direction). All five statuses are resolved on the same barriers and all five count in the promotion pair.
3. **"The suppression or abstain reason" (singular).** One column, `status_reason`, documented as the gate that suppressed it or the abstain rule that fired. If M4/M8 need a machine-readable code separate from the prose, that is an additive migration.
4. **"Four states, plus the OI-change z-score. A dead zone ignores OI changes inside 0.25 sigma."** Four quadrants plus a dead zone is five or six values. I used six: `price_{up,down}_oi_{up,down}` plus `price_{up,down}_oi_flat`, so a dead-zone reading still records the price side. The z-score is stored either way. **M3 should confirm it wants the price side preserved in the dead zone.**
5. **"Unlocks are slice features."** `unlock` and `funding_settlement` are calendar kinds with no stand-down window, so `EVENT_WINDOW_MINUTES` (which has only `cpi`, `fomc`, `expiry`) correctly does not cover them. `EVENT_KINDS` is the union. The brief asked for "kinds matching the constants" and the constants are a subset; I added the two slice-only kinds in `app.models.enums` rather than editing `constants.py`, so the rule hash is unchanged.
6. **"A large expiry" is still undefined** (M0 flagged it too). `event_calendar.is_large` and `notional_usd` are nullable, and NULL means undecided - explicitly not "not large". M5 or the owner has to set a rule.
7. **`force_order` has no honest natural key.** "Print time. The stream is partial." Binance's force-order payload carries no venue-side identifier and two genuine liquidations can share coin, side, price and size in the same millisecond, so a uniqueness constraint over those columns would silently drop real prints. The brief asks for natural uniqueness on every table; this is the one table where I could not provide it without losing data. `dedup_key` is nullable and unique when present, for M2 to use if it wants idempotent reinserts.
8. **"Options snapshot at decision time: venue, dealer gamma sign, flip distance, path-strike distance, hours to expiry, skew, same-tenor IV-RV, verdict, expression"** on the outcome tracker, versus the shorter "Fields on the alert" list. I denormalised only the alert list onto `candidate` (verdict, venue, vol tag, dealer gamma sign, nearest path strike, hours to expiry, expression, size multiplier, influence, confidence) and left flip distance, path-strike distance, skew and IV-RV to be read through `candidate.options_metric_id`. M9's tracker query is a join, not a wide select.
9. **`options_metric` is keyed by horizon.** Ranks 8-10 are all defined against "the expiry nearest the hold", which differs for a 4h scalp and a 72h position, so one snapshot yields up to three derived rows. The spec never says this explicitly.
10. **Which instant is `available_at` for a derived options row.** I set `options_metric.available_at` equal to its snapshot's, on the grounds that derivation is instantaneous and adds no information. If M5's derivation ever waits on a second feed, it must set a later `available_at` itself.
11. **The ATR behind every `*_atr` column.** The spec never states the ATR period (M0's assumption 3). Rather than re-derive it, `feature_row.atr_price` / `atr_timeframe` and `options_metric.atr_price_used` record the divisor actually used, so a stored distance can always be audited back to a price even if the period later changes.
12. **Percentile convention.** Following M0: an "80th percentile" is stored as the quantile `0.80`. `numeric(7, 6)` leaves exactly one integer digit, so writing `80` does not merely trip the CHECK, it overflows the column. A test asserts both behaviours.

### Verification
- `cd repo/api; & '.\.venv\Scripts\python.exe' -m pytest` -> **`223 passed in 14.01s`** (M0's 74 all still pass; 149 new: 28 `test_as_of`, 14 `test_sample_group`, 44 `test_schema_constraints`, 60 `test_models_schema`, 3 `test_migration_matches_models`).
- `python -m alembic upgrade head` -> `Running upgrade -> 7cd7d7f2e800, initial schema`, no errors.
- `public` afterwards: **14 base tables** (the 13 model tables plus `alembic_version`), 55 indexes, 13 UNIQUE constraints, 94 CHECK constraints, 5 foreign keys, **0 float/double columns, 0 naive timestamp columns**.
- Round trip: `alembic downgrade base` -> `public` drops to 1 table (`alembic_version` only), 0 indexes beyond its own, 0 checks, 0 uniques, 0 FKs; `alembic upgrade head` -> back to exactly 14 tables / 55 indexes / 94 checks / 13 uniques / 5 FKs. No orphan objects either way.
- `alembic current` -> `7cd7d7f2e800 (head)`. **The database is left at head**, with only the `public` schema present (the tests' throwaway schema is dropped).
- `tests/test_migration_matches_models.py` runs Alembic's `compare_metadata` against the live `public` schema and finds an empty diff, so the revision and the models agree. It does not and cannot check CHECK constraints - those have behavioural tests instead.
- No linter errors in `app/models/**`, `alembic/**` or `tests/**`.


---

## M2 - Binance ingest and backfill

- Date: 2026-10-02
- Model: Claude Sonnet 5.5

### Files created or modified
- `repo/api/app/config.py` - MODIFIED (additive): `DEFAULT_WATCHLIST`, `WATCHLIST_MAX_COINS`, `parse_symbols()`, and the `Settings` fields for the watchlist, the three Binance base URLs, the five polling intervals, the staleness grace, the funding interval, the OI periods, and the three MEASURED publication allowances (`feed_open_interest_publication_lag_seconds` 300, `feed_funding_publication_lag_seconds` 30, `feed_kline_settle_seconds` 30; each with the measurement it came from in its comment). Nothing that existed was changed.
- `repo/api/app/worker/__init__.py`, `app/worker/feeds/__init__.py` - docstring-only packages.
- `repo/api/app/worker/feeds/availability.py` - one function per feed kind, the available-at rule table in its docstring. The only place a rule is written.
- `repo/api/app/worker/feeds/records.py` - ms to aware UTC (`from_ms`/`to_ms`, integer arithmetic), frozen record dataclasses, the row parsers and their validation, `force_order_dedup_key`.
- `repo/api/app/worker/feeds/http.py` - `BinanceHttp`: weight-aware sliding windows, 429/418/451/403/5xx/timeout handling, retries with backoff and jitter. The single egress point of the worker (a `GET`).
- `repo/api/app/worker/feeds/binance.py` - `BinanceClient`: perp and spot klines, settled funding, predicted funding, OI history, server-clock offset, startTime/endTime pagination.
- `repo/api/app/worker/feeds/store.py` - idempotent writers (`ON CONFLICT DO NOTHING`; predicted funding is update-if-newer). They never commit.
- `repo/api/app/worker/feeds/sync.py` - gap-aware, resumable range sync shared by the live jobs and the backfill; `SeriesReport`.
- `repo/api/app/worker/feeds/health.py` - the staleness rule, `expected_feeds`, `refresh_feed_health`, `write_force_order_health`, `stand_down_breaches`.
- `repo/api/app/worker/feeds/forceorder.py` - the `!forceOrder@arr` collector (reconnect, backoff, gap recording, 23.5h planned reconnect). Knows nothing about SQL.
- `repo/api/app/worker/jobs.py` - the five scheduled jobs, `run_job` (the `job_run` wrapper), `reap_orphaned_runs`, `job_intervals`.
- `repo/api/app/worker/stream.py` - the thread that runs the collector, its `job_run`-per-connection and `feed_health` hooks.
- `repo/api/app/worker/scheduler.py`, `app/worker/__main__.py` - APScheduler wiring and `python -m app.worker`.
- `repo/api/app/worker/backfill.py` - `python -m app.worker.backfill`.
- `repo/api/tests/ingest_helpers.py` - `FakeExchange` (an `httpx.MockTransport` that reproduces the in-progress bar, the OI last-N quirk and the -1130 limit), fixtures `session_factory`, `exchange`, `worker_context`.
- `repo/api/tests/test_ingest_records.py` (39), `_http.py` (18), `_binance.py` (36), `_store.py` (19), `_sync.py` (23), `_health.py` (36), `_forceorder.py` (18), `_jobs.py` (37), `_config.py` (36) - 262 tests, no network.
- `repo/api/tests/test_utc_session.py` (13 tests) - the UTC session guarantee (see Contracts).
- `repo/api/app/core/db_sync.py` - MODIFIED, one change and no more, with the owner's explicit permission: `create_utc_engine()` (plus `SYNC_CONNECT_ARGS` and a connect listener) now builds every sync engine with `timezone=UTC`; `get_sync_engine()` calls it. `repo/api/app/db.py` (the async API engine) - MODIFIED, one line: `connect_args={'server_settings': {'timezone': 'UTC'}}`. `repo/api/tests/conftest.py` (an M1 file) - MODIFIED, one line: the `db_engine` fixture builds its engine with `create_utc_engine` so the test session is UTC too.
- NOT touched: `app/core/constants.py`, `time.py`, `hashing.py`, `app/models/**`, `alembic/**` (`alembic/env.py` still builds a bare `create_engine`: it only runs DDL and no data is read through it), `requirements.txt` (httpx, websockets, apscheduler were already there). No table or column was added or altered.

### Contracts frozen
- **`coin` is the Binance USDT symbol** (`BTCUSDT`), identical on the spot and perp markets and in every feed table.
- **available_at, per feed** (`availability.py`):

  | Feed | `available_at` | Stored in |
  |------|----------------|-----------|
  | kline | `open_time + timeframe` = the bar close | `candle.available_at`; `candle.close_time` is that same closing boundary (not Binance's `...59.999`) |
  | open interest | `poll_time + feed_open_interest_publication_lag_seconds` (300 s). `poll_time` is the close of the poll bar (the bar start + period); the allowance is MEASURED, not from the spec (see Measurements) | `open_interest.available_at` |
  | funding, settled | `funding_time + feed_funding_publication_lag_seconds` (30 s, measured). `funding_time` is Binance's own print timestamp (it carries a few ms of jitter, e.g. `00:00:00.002`) and stays the row key | `funding` kind `settled` |
  | funding, predicted | the observation time | `funding` kind `predicted`; `funding_time` is the NEXT settlement (a future instant) |
  | force order | `traded_at` (`T`), the print time, not the event time `E` | `force_order.available_at` |

  Everything is aware UTC. `from_ms` rejects floats, bools and non-positive values. A naive datetime in a record raises `MalformedRow`.
- **A row that is not yet knowable can never be written.** Two independent layers per feed. Klines: the client drops any bar with `close_time > server_now - settle` (and counts it in `dropped_unclosed`), where `settle` is `feed_kline_settle_seconds` (30 s); `store.write_candles(session, records, now=)` raises `UnclosedBarError` for the WHOLE batch if any `available_at > now`, and the sync passes it `now - settle`, so the writer enforces the settle margin as well as the close. Open interest: the client drops a row whose `poll_time + lag` has not passed, and `store.write_open_interest(session, records, *, now)` raises `UnclosedBarError` for the batch likewise. Settled funding: the client drops a print whose `funding_time + lag` has not passed, and `store.write_funding(session, records, *, now)` raises for a settled row with `available_at > now` (predicted rows are exempt: they describe a future settlement). The database CHECKs (`available_at >= close_time` / `>= poll_time` / `>= funding_time`) cannot catch this, which is why the writers do. Consequence for M7: a stored row's `available_at` is a time at which the live system COULD have read it, never earlier, so replay and live agree.
- **Feed names** (`feed_health.feed`): `binance_kline_{perp|spot}_{15m|1h|4h}`, `binance_open_interest_{15m|1h|4h}`, `binance_funding_settled`, `binance_funding_predicted`, `binance_force_order` (`coin` NULL). The backfill summary uses its own labels: `kline:perp:15m`, `open_interest:15m`, `funding:settled`.
- **Idempotency.** Klines, OI, settled funding and force orders: `INSERT ... ON CONFLICT DO NOTHING` on the natural key. A stored row is never changed by a later fetch.
- **Predicted funding is updated in place**, by the key `(coin, kind='predicted', funding_time)`: it is overwritten only when the new observation's `available_at` is NEWER (a stale write never regresses it). See Deviations.
- **`force_order.dedup_key`** = `{coin}:{connection_id}:{sequence}` where `sequence` counts frames on one connection. A retried flush is idempotent; two genuine liquidations with identical price, size and millisecond are both kept. (Resolves M1's Ambiguity 7.)
- **`stand_down_breaches(session, now, settings=None, coins=None) -> list[FeedBreach]`** (`app.worker.feeds.health`). Empty list = clear. M4/M5 call this; it judges by the CLOCK against `last_available_at`, not by the `status` column (a dead worker can no longer update the status). A feed with no `feed_health` row, no data, or a NULL limit is a breach. `binance_force_order` is exempt. `FeedBreach` has `feed, coin, reason, staleness_seconds, max_staleness_seconds`.
- **Module paths and commands.** `python -m app.worker` (`--once`, `--no-stream`, `--log-level`), `python -m app.worker.backfill`. Run both from `repo/api`.
- **Job names** (`job_run.job_name`): `binance_klines`, `binance_open_interest`, `binance_funding_settled`, `binance_funding_predicted`, `feed_health`, `binance_force_order_stream` (one row per websocket CONNECTION), and `backfill` (`source` NULL, `detail` has per-feed totals).
- **Live bootstrap** of an empty database: 200 bars per kline series, 30 days of settled funding, 29 days of OI. Anything deeper is the backfill's job.
- **Watchlist.** `app/config.py`: `DEFAULT_WATCHLIST` (15 symbols, BTCUSDT and ETHUSDT first), overridden by the `WATCHLIST` env var, validated to 1..20 symbols by `Settings.watchlist_symbols`. It is NOT in `constants.py`; a test proves `rule_hash()` is identical under a different `WATCHLIST`.
- **Staleness limits** (`max = period + poll interval + grace`, grace 120 s; all in config, none in `constants.py`):

  | Feed | period | poll | limit |
  |------|--------|------|-------|
  | kline 15m (perp, spot) | 900 | 60 | **1080 s** |
  | kline 1h | 3600 | 60 | **3780 s** |
  | kline 4h | 14400 | 60 | **14580 s** |
  | open interest 15m / 1h / 4h | 900 / 3600 / 14400 | 120 | **1140 / 3840 / 14640 s** |
  | funding, settled | 28800 | 300 | **29220 s** |
  | funding, predicted | 0 (a rolling estimate) | 60 | **180 s** |
  | force orders | - | - | NULL, exempt |

  Reasoning. A series is legitimately as old as its period right up to the moment the next row is due, so the limit is "one period plus how late the next row may arrive". That is the same as saying a series is stale about `poll + grace` after a missing close: a 1h series is declared stale about 3 minutes after a missed close, not an hour after, and the limit does not loosen as the timeframe grows. `poll` is OUR cadence (the row can sit on the exchange for one poll), `grace` is one missed poll plus any publication slower than the allowance (the allowances themselves are already inside `available_at`, from which age is measured). The OI poll is slower (120 s) so its limit is 60 s looser than a kline's; its 300 s publication lag is NOT added (it is already inside `available_at`). Settled funding is limited by the funding interval (8 h), not by the poll. The predicted rate has no period, so only `poll + grace`. Force orders are exempt because they are partial, an estimate and shadow-only (they cannot move a live alert), and a quiet market prints nothing, which cannot be told from a dead stream by the data alone; liveness is the connection (`status`, `last_success_at`) instead. The spec gives ONE number for any feed (Deribit, 15 minutes); the rest are mine and are arguable. Each is one env var away (`FEED_STALENESS_GRACE_SECONDS`, `FEED_*_POLL_SECONDS`).
- **Poll bounds.** Every data poll interval is validated 30..300 s (health 15..300); the spec says 1-5 min, and the 30 s floor stops a typo hammering the exchange. Defaults sit inside 60..300.
- **The database session time zone is UTC, structurally.** Every sync engine is built with `app.core.db_sync.create_utc_engine`: a libpq startup option (`-c timezone=UTC`) AND a `SET TIME ZONE 'UTC'` run on every new connection (the second is needed because libpq's `PGTZ` environment variable overrides the startup option; a test sets `PGTZ=Asia/Tokyo` to prove it). The async API engine (`app/db.py`) sets `server_settings={'timezone': 'UTC'}`. asyncpg already returned UTC-aware datetimes, so the async change did not alter any value it returns, but session-zone-dependent SQL (`date_trunc`, `::date`, `extract`, `to_char`) follows the server zone and would otherwise have bucketed by Moscow time; it is changed for that reason. `tests/test_utc_session.py` fails if either engine, the worker's own engine, or the test `session` fixture is not UTC; proves a round-tripped instant (written with +0, +3, -8 and +14 offsets) comes back with a zero UTC offset and the same instant regardless of the machine's zone; proves a 22:00 UTC bar has hour 22 and day 2 in SQL; and an AST scan fails on any bare `create_engine` in `app/**` outside `db_sync.py` (or a `create_async_engine` that does not mention `timezone`). **`ensure_utc` is still required**: M3 should keep wrapping instants it reads before extracting a calendar field, as belt and braces (naive datetimes, other drivers, a future engine built without the helper).
- **Open interest and replay.** Binance serves OI history for the last ~30 days only (`-1130` beyond that). The client clamps the start to `now - 30d + 1h` and says so; the live job bootstraps 29 days. On 2026-10-02 that means nothing before about **2026-09-02 18:00 UTC** can be fetched, and the earliest OI a database built today can hold is about 2026-09-03. Every day the worker does not run loses a day of OI for good. Any replay of the OI-price quadrant can only start where OI rows exist; replay before that has to leave the feature NULL, not zero.
- **Force orders cannot be backfilled** (no historical endpoint). The stream is partial (the latest liquidation per symbol per second). Replay rows must leave liquidation distance NULL (as M1 already allows).
- **Publication lags and the settle margin are MEASURED infrastructure facts, in `config.py`, not in `constants.py`** (they do not feed `rule_hash()`). See Measurements. Open interest: `available_at = poll_time + 300 s`. Settled funding: `funding_time + 30 s`. Klines: `available_at` stays the bar close (the spec, and what the M1 inclusive gate assumes), but a bar is only WRITTEN 30 s after its boundary, because the closing bar is served with provisional values for up to ~8 s and `ON CONFLICT DO NOTHING` would freeze them. The live feature builder therefore reads a bar at its close in the sense of `as_of(bar_close)`, but the bar can be missing from the database for the first ~30 s after the close.
- **Staleness limits are unchanged, on purpose** (see the table and the reasoning): they are measured from `available_at`, which already includes the allowance. Consecutive OI rows are exactly one period apart in `available_at` and are fetched within one poll interval, so the lag is already inside the limit; adding it again would double-count it. What the lag changes is the failure mode when Binance is SLOWER than the allowance: the excess (not the whole lag) must fit inside the 120 s grace, which is why the allowance is 300 s against a worst sample of 180 s.
- **Reading the feeds.** Every ingest read goes through `as_of(...)` (the M1 source scan covers `app/worker`). The stored-row readers in `sync.py` (what is already stored, the newest row per series) are all `as_of(client.now()).select/gate`, so a row is only ever "stored" if it was already available.
- **Public market data only.** No API key, no signature, no order endpoint; the config has no credential field. A test walks the AST of `app/worker/**` and fails on any path outside the seven listed read endpoints, any signing/key identifier, any non-GET verb, or any `_client` call that is not the single `get` in `http.py`.

### Measurements (live Binance, 2026-10-02, public endpoints)
Open-interest publication lag: seconds from the poll boundary until a probe FIRST saw the row (probe polled every ~3 s from a few seconds before the boundary; the 15 watchlist symbols at 15m, and at 1h on the hour; threaded, so the polls did not delay one another).

| Boundary (UTC) | Series | Symbols | Min | Max | Notes |
|---|---|---|---|---|---|
| 17:00 | 15m/1h | 1 (BTC) | | ~90 | the single sample the first version was built on |
| 17:30 | 15m | 4 | 22 | 166 | ETH 22, SOL 66, DOGE 126, BTC 166 |
| 17:45 | 15m | 15 | 2.8 | 164.1 | |
| 18:00 | 15m + 1h | 15 + 15 | 9.6 | 175.1 | |
| 18:15 | 15m | 15 | 18.0 | 179.2 | |
| 18:30 | 15m | 15 | 10.6 | 180.1 | worst of all |
| 18:45 | 15m | 15 | 5.4 | 164.5 | |
| 19:00 | 15m + 1h | 15 + 15 | 12.5 | 172.9 | |

The 120 symbol-samples of the six threaded boundaries: mean 90.5 s, median 93.8 s, p90 164.5 s, max 180.1 s; none missing. The lag is per SYMBOL and spread across the whole 3-180 s range (AVAX, NEAR, ADA, LTC and TRX are often 150-180 s, BTC and ETH anywhere from 30 to 165 s) and the ceiling looks like a ~3 minute publication cycle. **Chosen: 300 s** (1.67x the worst sample), deliberately not the mean: the mean (and the original single 90 s sample, which happened to equal it) would still be early for about half of all rows. Erring late costs up to ~2 minutes of OI freshness on a 15m series; erring early silently inflates every statistic.

Other feeds, each checked:
- **Klines: `available_at` = bar close is right, but the closing bar is provisional for a few seconds.** The probe saw the just-closed bar listed at the very first poll after the boundary (0.1-0.7 s), but its close and volume kept changing for a few seconds (the final values were reached when the next bar appeared): perp values stable 4.3-7.5 s after the boundary (4.5, 4.3, 7.5, 7.5, 5.6, 6.5), spot 0.5-3.7 s. The old 1 s settle margin could therefore freeze provisional values under `ON CONFLICT DO NOTHING`. The margin is now 30 s (4x the worst). All 990 stored candles were re-fetched from Binance and compared on all nine value columns: **0 mismatches** (the backfill had run long after the bars closed), so nothing needed correcting.
- **Settled funding: a small lag exists.** ARKUSDT (a 1h-interval symbol, the only one on that cadence of the 804 in `fundingInfo`) was served 4.9 s (18:00) and 1.4 s (19:00) after settlement. Allowance 30 s. Thin evidence (two points; the 4h and 8h symbols were not sampled), so deliberately generous.
- **Predicted funding** is stamped when observed (the `premiumIndex` `time` field), so there is nothing to lag. **Force orders**: `available_at` is the print time `T`; the 600 s probe (and the earlier 25 s one) saw 0 frames, so what delay the stream has relative to `T` is NOT measured; the rows are shadow-only and exempt from staleness.

Existing rows were corrected in place (a re-fetch cannot change a stored timestamp, and the data values were right; only the claimed `available_at` was wrong): `UPDATE open_interest SET available_at = poll_time + 300 s` (968 rows) and the same for the 3 settled funding rows (+30 s). Afterwards 0 of 968 OI rows and 0 of 3 settled funding rows have `available_at = poll_time` / `funding_time`, 0 have a future `available_at`, and every row sits at exactly boundary + 300 s / print + 30 s.

### Deferred to a later milestone
- Deribit (M5), the feature math (M3), rules and the options check (M4/M5), the labeler (M6), the stats engine (M8) and API routes: none are here.
- **The consumer of `stand_down_breaches`** (M4/M5). It is only the query.
- Replay's handling of NULL OI before ~2026-09-02 and of NULL liquidation distance (M7).
- `feed_health` for the Deribit book (15-minute rule) - M5.
- **`job_run` retention policy: DEFERRED TO M10 (the operational checklist).** The table grows by about 5,300 rows a day (five jobs at 60/120/300/60/60 s plus one row per websocket connection) with no pruning. Harmless at this stage; M10 must decide retention.
- A watch on the OI publication lag in production (the allowance is a measured constant; nothing in the worker re-measures it): M10 may want a periodic re-measurement.
- A repair job for interior holes: see Deviations.

### Deviations from the plan or the brief
- **Predicted funding is not `DO NOTHING`.** The brief says idempotent `ON CONFLICT DO NOTHING`. A predicted row's key is the next settlement, so under DO NOTHING the first estimate of each 8-hour window would freeze for 8 hours and the live carry band would read a stale rate. It is an upsert that updates only if the new `available_at` is newer (and within one batch only the newest observation per key is sent). Settled rows stay strictly DO NOTHING. Consequence for replay: a `predicted` row is the LATEST estimate for its settlement, not the history of estimates, so replay must not use it as if it were point-in-time.
- **OI and settled funding are not available at the boundary or the print, and klines are written late.** The spec's "close of the poll bar" is a lower bound on knowledge, and for OI it is ~90 s optimistic on average and up to 180 s (see Measurements); M2's first delivery used it and recorded the lag only as a note. That was lookahead bias that replay (M7) would have carried undetected, and it was corrected: `available_at` is the boundary plus a measured, conservative allowance, and a row is not written before then. Settled funding got the same treatment (a small lag). Klines keep `available_at` = close (the spec), but are written 30 s after it because the closing bar is provisional for up to ~8 s.
- **The backfill includes OI by default** (`--feeds klines,funding,oi`), because the OI history disappears if it is not collected. `--feeds klines` restores the minimal behaviour. The OI periods requested are the `--timeframes` values.
- **`funding.interval_hours` is NULL on settled rows.** The history endpoint does not return the interval, and guessing 8 h would be wrong for the symbols Binance lists as 4 h or 1 h. Predicted rows carry the interval from `fundingInfo`, defaulting to 8.
- **`job_run` grows by about 5,300 rows a day** (five jobs at 60/120/300/60/60 s, plus one row per websocket connection). One row per execution was the brief. There is no retention policy; that is deferred to M10 (see Deferred).
- **One worker per database is assumed.** `reap_orphaned_runs` closes every `running` row of a live job name at start-up. A second worker would have its in-flight rows marked as errors. Only live job names are reaped, never `backfill`.
- **Interior holes older than a series' newest row are repaired by the backfill, not by the live jobs.** A live job asks only for what comes after the newest stored row (one cheap check per series per poll). Downtime at the tail heals itself on the next poll; a hole in the middle (a rejected page, a partial failure) does not, until `python -m app.worker.backfill --days N` is run. The backfill is gap-aware and fetches only the hole.
- **Empty answers are counted as requests** in the summary (found by the live smoke run: a funding re-run that asked once and got nothing printed "data requests 0"). Fixed in `iter_funding_history`/`iter_klines`, with a test.
- **`websockets` reconnect is not "resumption".** There is nothing to resume from: Binance replays nothing. The collector reconnects and records the gap (`CollectorStats.gaps`, written into `job_run.detail.gaps_seconds`); it never fills it.

### Ambiguities (spec quotes and choices)
1. **"Binance open interest | Close of the poll bar".** Read as: the row's `poll_time` is the aligned boundary of the poll bar (the endpoint's `timestamp`, validated to sit on a period boundary), and the sample is KNOWABLE only once Binance publishes it, so `available_at = poll_time + 300 s` (a measured allowance, see Measurements). The spec's literal rule (`available_at = poll_time`) was implemented first and rejected as lookahead bias.
2. **"Binance funding | Last settled print. The live carry band also reads the current rate".** Two kinds, one table: `settled` (`available_at` = the print + a 30 s measured serving lag) and `predicted` (the current estimate, `available_at` = the observation). The band reads `kind = 'predicted'` live; the z-score reads `settled` only.
3. **"Binance force orders | Print time. The stream is partial".** `available_at = traded_at` (`T`), not the event time `E` (which is a few hundred ms later); the stream's partiality is recorded in `feed_health.detail` and in `job_run.detail.partial_stream`.
4. **"Stand down when a feed is past its available-at rule".** The only number the spec gives is Deribit's 15 minutes. Every Binance limit is derived (see the table) and configurable. "Past its available-at rule" is implemented as "older than its limit by the clock", never "the last status said ok".
5. **"Klines, OI, funding | 1-5 min, stored at available-at".** Interpreted as a polling cadence of 1-5 minutes for all three, stored with their available-at, not as the interval of the data. Defaults: klines 60 s, OI 120 s, settled funding 300 s, predicted 60 s, health 60 s.
6. **"10-20 coins" / "Watchlist" (a Preference).** Default 15 liquid perps that also have a USDT spot pair (the perp-vs-spot stand-down needs both): BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, TRX, DOT, LTC, BCH, NEAR, ATOM. 1000x-multiplier perps are avoided because their spot symbol differs. All 15 were confirmed TRADING on both markets on 2026-10-02.
7. **Binance returns HTTP 451 to US IPs.** A deployment note, not a code path: the clients raise `GeoBlocked`, a job stops instead of failing every series, and the backfill exits 3. Run the worker from a non-US host.

### Verification
- `cd repo/api; & '.\.venv\Scripts\python.exe' -m pytest` -> **`498 passed in 55.87s`** (M0+M1's 223 all still pass; 262 in the nine `test_ingest_*` files and 13 in `test_utc_session.py`).
- **Live smoke** (real Binance, 2026-10-02 ~17:11 UTC, `python -m app.worker.backfill --coins BTCUSDT --timeframes 15m --days 1`): perp 15m 95 rows, spot 15m 95, settled funding 3 (00:00, 08:00, 16:00 UTC), OI 15m 96; exit 0, every gap count 0. SQL afterwards: 0 candles with `close_time > now()`, 0 with `available_at <> open_time + 15 minutes`, 0 misaligned `open_time`, (OI and settled-funding `available_at` were then still the boundary and the print: that first rule was replaced, and those rows corrected in place, see Measurements), all columns `timestamp with time zone`. Newest perp row: `open 2026-10-02T16:45:00Z`, `close 2026-10-02T17:00:00Z`, `available_at 2026-10-02T17:00:00Z`, open 85159.2, close 85130.3, base volume 1654.248, taker buy base 936.426, 39,435 trades. The 17:00 bar, forming at the time of the run, is absent.
- **Re-run of the same command: 0 new rows and 0 kline/OI requests**, `missing before this run 0` on every feed.
- `WATCHLIST=BTCUSDT python -m app.worker --once --no-stream` (a second, small live check of the scheduler path, BTC only): `binance_klines ok`, `binance_open_interest ok`, `binance_funding_settled skipped`, `binance_funding_predicted ok`, `feed_health ok`; 11 `feed_health` rows, all `ok`.
- **Force-order WebSocket**: the real URL `wss://fstream.binance.com/ws/!forceOrder@arr` connected and stayed up 25 s through the production collector with 0 frames received (liquidations are sparse; this proves the URL and the handshake, NOT that a live frame parses; parsing is covered by tests built from the documented payload).
- **Left behind in `public`** (kept as evidence): 1,002 candles (BTCUSDT: perp and spot at 15m/1h/4h; 990 from the backfill, 12 from the later `--once` run), 974 OI rows (968 corrected in place, 6 written by the later `--once` run), 3 settled and 1 predicted funding row, 11 `feed_health` rows, 13 `job_run` rows (3 `backfill`, 10 from two `--once` runs). 0 force orders.
- **After the publication-lag and UTC corrections** (live, 2026-10-02 18:24 UTC, `WATCHLIST=BTCUSDT python -m app.worker --once --no-stream`): every job `ok`; the newest OI rows were boundary 18:15 (15m) and 18:00 (1h), the newest knowable at 18:24:33 (18:15 + 300 s = 18:20); all 11 BTC `feed_health` rows `ok` with the limits unchanged. SQL over `public`: `open_interest` 968+ rows, 0 with `available_at = poll_time`, 0 with `available_at <> poll_time + 300 s`, 0 in the future; settled `funding` 3 rows, 0 optimistic, all at +30 s; `candle` 990 rows, all `available_at = close_time`; session `SHOW timezone` = `UTC`.
- The test suite runs in a throwaway schema, so none of it touched `public`.

---

## M3 - Feature engine

- Date: 2026-10-02
- Model: Claude Sonnet 5.5

### Files created or modified
Source (`repo/api/app/core/features/`, all pure: no database, no clock, no environment):
- `__init__.py` - package index.
- `params.py` - engine choices the spec leaves open (timeframes, history lengths, `HistoryPolicy`), deliberately NOT in `constants.py` (see Deviations).
- `series.py` - frozen carriers (`Bar`, `OIPoint`, `FundingPrint`, `ForcePrint`, `CalendarEvent`, `Sample`) and the `available_at` visibility filter every pure function applies.
- `reading.py` - `Reading(value, reason)`: a missing value must carry a reason; a non-finite float cannot be built.
- `stats.py` - mean/std, `prior_window` (the one causal-window rule), `trailing_zscore`, `trailing_percentile`, `ols`.
- `volatility.py` - true range, Wilder ATR, Yang-Zhang (annualised), hourly realized-vol samples.
- `trend.py` - Kaufman efficiency and the derived (stateless) trend/chop family.
- `positioning.py` - aggressor imbalance, OI-price quadrant, funding z and carry, perp basis, perp/spot disagreement.
- `indicators.py` - CVD divergence, Bollinger/Keltner squeeze, Wilder RSI, pullback reset.
- `levels.py` - session and anchored VWAP, volume profile (POC, value area), 4h fractal swings, chandelier.
- `cross_asset.py` - lagged rolling OLS beta of an alt on BTC and the signal-bar residual z.
- `liquidation.py` - the shadow liquidation estimate and print rate (into `shadow_metrics` only).
- `columns.py` - the 53 feature columns with kind/precision/scale, and the normaliser (rounds to the column scale BEFORE hashing).
- `assemble.py` - `FeatureInputs` -> `compute_features` -> values, NULL reasons and `feature_hash`.

Worker and tests:
- `repo/api/app/worker/feature_job.py` - the assembly job: reads every feed through `as_of`, computes, writes one `feature_row` per horizon idempotently; CLI.
- `repo/api/app/core/constants.py` - comment only on `ATR_PERIOD` ("owner-confirmed rather than assumption"); no value changed, `rule_hash()` unchanged.
- `repo/api/tests/feature_helpers.py` - hand-built bars, a seeded 100-day synthetic market, future-poisoning and newest-print-nudge helpers.
- `repo/api/tests/test_features_golden.py` (118) - hand-computed fixtures with the working in comments.
- `repo/api/tests/test_features_causality.py` (39) - per-statistic early / late / poisoned-future equality, plus a positive control.
- `repo/api/tests/test_features_determinism.py` (14) - hash repeats, key order, input order, other process/hash-seed/TZ, no clock, sensitivity.
- `repo/api/tests/test_features_null.py` (26) - unavailable input is NULL with a reason; a real zero stays zero.
- `repo/api/tests/test_features_columns.py` (68) - registry equals the model (names, order, precision/scale), ATR-unit naming, normalisation.
- `repo/api/tests/test_feature_job.py` (17) - as-of source scan and wire-level check, idempotence, hash round trip through Postgres, readiness, live carry band.
- `repo/docs/BUILD-LOG.md` - this entry.

### Contracts frozen
Module paths, names and signatures M4 and later use verbatim:
- `app.core.features.assemble.compute_features(inputs: FeatureInputs, policy: HistoryPolicy = PRODUCTION_POLICY) -> FeatureComputation`. `FeatureComputation.values` has EXACTLY the keys of `columns.FEATURE_COLUMNS` (53, `None` = NULL), `.reasons` maps each NULL column to a sentence (diagnostic, not stored, not hashed), `.feature_hash = app.core.hashing.feature_hash(values)`.
- `FeatureInputs(coin, bar_close, horizon, source, perp_15m, spot_15m, perp_1h, spot_1h, perp_4h, btc_perp_15m=None, is_reference_coin=False, oi_1h=(), funding_settled=(), funding_predicted=None, force_orders=(), events=(), calendar_loaded=False)`. Lists need not be sorted or filtered.
- `app.worker.feature_job.assemble_feature_rows(session, coin, bar_close, *, source='live', horizons=C.HORIZONS, observed_at=None, now=None, code_version=None, policy=PRODUCTION_POLICY, require_ready=None) -> AssemblyReport`; `load_inputs(...)`; `run_feature_job(session, coins, *, now=None)`; `latest_closed_bar(now)`; `InputsNotReady`; CLI `python -m app.worker.feature_job --coins BTCUSDT[,..] [--at ISO] [--source live|replay] [--dry-run]`.
- `feature_row` is keyed `(coin, horizon, signal_timeframe='15m', bar_close, source)` (`uq_feature_row_series_bar_close`); the job inserts `ON CONFLICT DO NOTHING`. Rows are immutable; a re-run that computes a different hash for an existing row is counted in `AssemblyReport.hash_mismatches` and logged, never overwritten.
- **The stored row reproduces its own `feature_hash`**: every float is rounded to its column scale before hashing, and `float(Decimal)` from the database is the same double. Reading a row back as `{column: float(Decimal) | value}` and calling `feature_hash` returns `row.feature_hash` (tested through Postgres).
- Column semantics a rule may rely on:
  - Every `*_atr` column is `(price - level) / atr_price`, POSITIVE when price is ABOVE the level, in units of the 4h Wilder ATR(14) stored in `atr_price` (`atr_timeframe = '4h'`). `chandelier_stop_distance_atr` is positive on the protective side (<= 0 means the stop is breached). `perp_spot_disagreement_atr` is an absolute value.
  - `trend_family` is NULL on a cold start (see Cold start) and a rule that needs a family does not fire on NULL.
  - `event_flag` / `active_event_kinds` are NULL when no calendar row is loaded ("unknown"), `false` / `[]` when a calendar is loaded and no window is active.
  - `funding_rate_current` and `funding_carry_bps_horizon` are filled ONLY for `source='live'` (and only with a stated settlement interval); a `replay` row has them NULL by design.
  - `swing_break` is false and the direction NULL when no swing is broken; `chandelier_*` are NULL then (the stop is defined for a break direction).
  - `cme_basis_z`, `etf_net_flow_z`, `ibit_dealer_gamma_sign`, `cme_dealer_gamma_sign` are always NULL.
  - `shadow_metrics` is `{}` when unknown; keys `liquidation_distance_atr`, `liquidation_distance_estimated`, `liquidation_print_rate_per_min` only.
- Public pure functions the later milestones reuse: `volatility.yang_zhang_vol(window, step)` (any window length: M5's same-tenor realized vol), `volatility.wilder_atr`, `levels.fractal_swings`, `levels.chandelier_stop`, `stats.trailing_zscore`, `stats.trailing_percentile`, `stats.prior_window`, `params.HistoryPolicy`.
- How each feature is computed (formula, column) is in the module docstrings and the report below.

### Deferred to a later milestone
- Scheduling `run_feature_job` in `app.worker` (the scheduler module is M2's ingest code and was not touched) -> M4 or M10; until then it runs by CLI or by a caller.
- Frozen rules, direction vote, options check -> M4/M5. Labeler and the label-only features (hold-period residual, cross-market impulse) -> M6. Replay driver that fills `feature_row` for history -> M7.
- A tenor-matched realized vol for rank 10 -> M5 (use `yang_zhang_vol` on the tenor's bars).
- Feeds that fill the four always-NULL columns and `iv_*`/book shadow keys -> after Phase 1.
- Calendar rows (CPI, FOMC, expiry): the table is empty on the real database, so `event_flag` is NULL there -> whichever milestone loads the calendar (the spec lists CPI, FOMC and expiry); not decided here.

### Deviations from the plan
- **Engine choices are in `app/core/features/params.py`, not `constants.py`.** Adding an UPPERCASE name to `constants.py` moves `rule_hash()`; the brief said no constant moves in M3. Consequence: changing a `params.py` value changes feature VALUES (and `feature_hash`) without changing `rule_hash`. `code_version` is what distinguishes those builds, and it only means something once the tree is committed (`25aec17-dirty` while it is not). Decision for the owner at M4: promote `params.py` into `constants.py` and accept a one-time `rule_hash` change.
- **The assembly job is not registered with the scheduler** (M2 code is out of bounds). See Deferred.
- **`cvd_divergence_against` is NULL when there is no divergence**, and says why (found by the NULL tests: a NULL with no reason). It is the true value, not a gap.
- **A real bug found by the DB tests and fixed in this milestone:** SQLAlchemy sends Python `None` for a JSONB column as the JSON value `null`, not SQL NULL, which violates `ck_feature_row_active_event_kinds_is_array` (and would have been a second spelling of "missing"). `to_db_value` now sends `sqlalchemy.null()` for the JSONB columns. A test asserts `active_event_kinds IS NULL` in SQL.
- **Staleness is NULL, not carry-forward.** A series whose newest visible bar is not the bar that should be newest at `bar_close` (a stopped 1h feed, a late 4h bar, OI older than two periods, funding older than 16 h) gives NULL for the features built on it. The brief did not ask; the alternative is a current-looking number built on the previous bar. Together with the live readiness check (`InputsNotReady` for ten minutes, then write with NULLs) this is why a row is never keyed on bar T over statistics from bar T-1.
- **A minimum history for every 90-day statistic** (`HistoryPolicy`: prior samples must span 7 days and number 30, else NULL). The spec gives no minimum; without one a z-score over 25 hours would be quoted as a 90-day z-score.

### Ambiguities (spec quotes and choices)
Each: the quoted line, the choice, and the milestone that confirms it.
1. **Session VWAP reset.** "Session VWAP in UTC". Reset at 00:00 UTC (the UTC day of the bar's OPEN, so the bar closing at exactly 00:00 belongs to the day that ended); NOT at the Asia/Europe/US bucket edges (that is the regime slice's `session`). Typical price (H+L+C)/3 times base volume. Fewer than 8 bars into the day -> NULL. Confirms: M4 (the VWAP tag trigger).
2. **The "2-bar fractal".** "A 4h close beyond the last 2-bar fractal swing". Two bars on EACH side, strict: a swing needs two later 4h bars, so the newest two bars can never be the last swing. "Beyond" means a close strictly beyond. If a close is beyond both swings, the more recent swing decides. Confirms: M4.
3. **Sampling frequency behind each z-score.** "Every rate is a 90-day z-score or percentile". Aggressor imbalance and the alt residual: per 15m bar (<= 8,640 samples). OI change, perp basis, realized vol: hourly (<= 2,160). Funding: one per SETTLED print (~270). Every statistic scores the current value against the PRIOR window (strictly before the bar, excluding the current). Confirms: M7 (first time a full 90 days exists).
4. **Volume-profile session and early-session behaviour.** "Session profile, 50 bins, 70% value area". Same 00:00 UTC session as the VWAP; 50 equal bins over the session low..high; a bar's volume is spread across its range by overlap; POC ties go to the lowest bin and is the bin centre; the value area grows from the POC by the larger neighbouring bin (ties: above) to >= 70%, and its low/high are bin EDGES (single-bin expansion, not the Market Profile two-row rule). Fewer than 8 session bars -> NULL for POC, value area and every distance. Confirms: M4.
5. **ATR timeframe.** "Every distance is stored in ATR units". The ATR is the 4h Wilder ATR(14), because the stop (chandelier) and the swing are 4h objects. `ATR_PERIOD = 14` is owner-confirmed. Confirms: M4.
6. **Yang-Zhang window and annualisation.** "Yang-Zhang vol versus 90 days". 24 consecutive 1h bars per estimate (25 are read), sliding hourly; the percentile ranks the current estimate among the prior 90 days of such overlapping estimates (so adjacent samples are strongly correlated and the effective sample count is far below the nominal one). Annualised with 365 days. Confirms: M5 (same-tenor IV minus realized needs the same convention).
7. **OI period.** "Price up and OI up = new risk ... Four states". Six states (the four quadrants plus `price_up_oi_flat` and `price_down_oi_flat`; owner-confirmed, not reopened), computed on the 1h OI series over one period against the price change over the same hour; dead zone 0.25 sigma of the prior 90-day one-period OI changes. A reading older than two periods is NULL. Confirms: M4.
8. **Trend family "keep the previous family".** Stateless: the most recent decisive reading (>= 0.40 or <= 0.25) among the last 336 hourly readings. See Cold start. Confirms: M4/M7.
9. **CVD "price makes a 20-bar high or low".** Close strictly beyond the prior 19 closes; "CVD does not" = the cumulative line is not strictly beyond its own prior-19 extreme. Confirms: M4.
10. **Pullback reset three-valued.** NULL when the family is trend but RSI or VWAP cannot be read and nothing else triggers; FALSE when the family is chop. Confirms: M4.
11. **Residual "on the signal bar".** The beta fit uses returns that ended at or before the signal bar's OPEN (the signal bar's own return is excluded, which is the lag); `beta_fit_end = bar_close - 15m`. Confirms: M4.

### Report (the eight points)

**Causality.** Every window goes through `stats.prior_window`: `available_at <= as_of` and `bar_close - 90d <= t < t_current` (strictly before, excluding the observation scored); no function receives a full-sample or centred statistic; `series.visible` re-applies `available_at` in every pure function (two independent layers: the SQL gate and the filter). `test_features_causality.py` runs each rolling statistic three ways (truncated, full data evaluated at T, future made absurd) at ordinary, 4h and midnight bar closes and requires equality, requires the value not to be NULL, and has a positive control showing the newest print does move it.

**Cold start, live and replay.** Wilder ATR and RSI always use the LAST 200 visible bars of a contiguous run (hence identical to the last bit however the process started). The trend family is derived from the data, not carried in memory: the most recent decisive hourly efficiency within the last 336 readings; none decisive, or the latest efficiency not computable -> NULL (never defaulted). Replay and live therefore agree by construction. Real BTCUSDT: the family is `chop` at 0.019.

**Yang-Zhang.** Annualised. `k = 0.34 / (1.34 + (n+1)/(n-1))` with n = 24 (k = 0.14009316); `sigma2_yz = sigma2_o + k*sigma2_c + (1-k)*sigma2_rs` (overnight, open-to-close, Rogers-Satchell); `vol = sqrt(sigma2_yz * 8760)` for 1h bars (365 x 24). A fraction (0.42 = 42 %/year). Hand-computed n=2 test: variance 0.1033793388, annualised 30.0932385687.

**Verification.** `cd repo/api; & '.\.venv\Scripts\python.exe' -m pytest` -> `780 passed in 140.78s` (498 existing + 282 new). Tree: only the files above are new or modified; nothing committed or pushed.

**Real-data run** (BTCUSDT, replay source, production policy, bar close 2026-10-02 18:15 UTC, hash `33b81f0a54be035f1b197f1248e6bff7bbc15c32c32dd66bf329b85bc739c01d`): ATR(4h,14) 998.27; price 84,856.1; aggressor imbalance 0.6495; OI quadrant `price_down_oi_down`, OI-change z -0.87; trend efficiency 0.0191, family chop; YZ vol 41.84 %/yr at the 0.795 percentile; perp basis -3.43 bps (z 1.49); CVD delta -4,643; session VWAP -1.07 ATR; POC 86,436 (-1.58 ATR), value area 85,342..86,857; squeeze off (ratio 2.75); 4h swing break LONG against 84,379.8 (anchor 2026-10-01 04:00), last swing low 83,136.6; RSI 32.2; chandelier stop 84,254.8, distance 0.60 ATR; perp-spot disagreement 0.035 ATR. An independent re-implementation of efficiency (0.01908706), Yang-Zhang (0.4183731) and ATR (998.2663245) from the raw database bars agrees with the engine to the printed digits (this is a cross-check of the engine against a second implementation, not where any expected value in the tests came from).

**NULL on the real data and why:** `aggressor_imbalance_z` (the database holds 1.0 day of 15m bars, the policy wants 7); `funding_z` (3 settled prints); `swing_vwap_distance_atr` (the swing anchor 2026-10-01 08:00 predates the available 15m bars, which start 2026-10-01 17:15; the 4h series is 200 bars deep, the 15m series 100); `beta_vs_btc`, `beta_fit_end`, `alt_residual_z_signal_bar` (BTC is the reference); `funding_rate_current`, `funding_carry_bps_horizon` (replay row; a live row would read the predicted print); `event_flag`, `active_event_kinds` (the calendar table is empty); `shadow_metrics` `{}` (0 force orders); `cvd_divergence_against` (no divergence); the four always-NULL columns. A relaxed diagnostic policy (span 0.5 day, 5 samples; NOT production, not stored) gives `aggressor_imbalance_z` 1.75; `funding_z` stays NULL (2 prior samples).

**Plausibility and an observation for M4.**
- Vol: 41.8 %/yr is ordinary for BTC; the 0.795 percentile is among only 177 hourly samples spanning 7.4 days (the 1h history starts 2026-09-24), barely over the 7-day minimum, so treat it as provisional until M7 has the 90 days. The sample range is 13 % to 47 %.
- Efficiency vs price: the last 25 hourly closes run 84,166 -> 86,769 and back to 84,746: net -120 on a 6,303 path, 0.019, correctly chop (a round trip). The current reading itself is decisive (<= 0.25), so the family is `chop` directly, not carried; the carry path (a reading between the bands) is exercised by the hand-built tests, not by this bar.
- Chandelier: the 4h swing break is long (close above 84,379.8) and the post-break high is 87,249.6; price has since fallen 2.4 ATR from that high with RSI 32, so the stop sits only 0.60 ATR below price. **A "swing break" is a state ("the last close is beyond the last swing"), not an event ("this bar broke it").** `swing_break = true` here is about a day old. M4 must decide whether it triggers only on the bar of the break or on the state; this entry does not decide it. The short-direction chandelier (diagnostic) is 1.28 ATR.

---

## M3.1 - Pre-M4 decisions: engine params in rule_hash, swing break event and state

- Date: 2026-10-03
- Model: Claude Opus 5.5

Settles the two "Open before M4" items from the M3 entry and HANDOFF. M4 has not been started.

### Files created or modified
- `repo/api/app/core/hashing.py` - `frozen_engine_params()`; `rule_hash_input` / `rule_hash` take an optional `engine_params` mapping and hash it under its own `engine_params` key. `canonical_serialize` is unchanged.
- `repo/api/app/core/constants.py` - `RULE_VERSION = 'phase1-v2'` (was `phase1-v1`). No other value moved.
- `repo/api/app/core/features/params.py` - docstring only: it now says these values ARE hashed. No value changed.
- `repo/api/app/core/features/levels.py` - `SwingState.break_is_new`, `swing_break_event(state, bar_close)`, docstring section "Swing break: event and state".
- `repo/api/app/core/features/assemble.py` - fills `swing_break_state`, `swing_break_event`; `swing_break_direction` gets a NULL reason when there is no break.
- `repo/api/app/core/features/columns.py` - `swing_break` -> `swing_break_state`, new `swing_break_event`; 54 feature columns (was 53).
- `repo/api/app/models/features.py` - the same rename and new column; comments on all three swing-break columns.
- `repo/api/alembic/versions/20261003_1541_1c329301cc23_swing_break_event_and_state.py` - hand-written: renames the column (no drop/add), adds `swing_break_event`, updates comments; reversible.
- Tests: `test_hashing.py` (+7: payload guard, guard negative control, types, payload layout, sensitivity, fractional-second refusal, version pin), `test_ingest_config.py` (lags and settle margin added to the "cannot change the rule hash" test), `test_features_golden.py` (+5 hand-worked event/state cases), `test_features_causality.py` (+5 whole-row event/state tests; the `swing_state` extractor also returns the event), `test_features_determinism.py` (+2: stateless cold start / live == replay; both columns are in the hash), `test_features_null.py` and `test_features_columns.py` (renamed column, 63/54 counts).

### Contracts frozen
**Rule hash payload (supersedes the M0 derivation):**
```
sha256(canonical_serialize({
    "rule_version": RULE_VERSION,
    "constants":     {NAME: value for public UPPERCASE names in app.core.constants except RULE_VERSION},
    "engine_params": frozen_engine_params(),
}))
```
- `frozen_engine_params()` lists every public UPPERCASE value of `app.core.features.params` EXPLICITLY. Two are converted: `FUNDING_MAX_AGE` (timedelta) -> `FUNDING_MAX_AGE_SECONDS` (int, 57600; a fractional second raises `ValueError`), and `PRODUCTION_POLICY` -> `{"window_days": 90, "min_span_days": 7.0, "min_samples": 30}` (its dataclass fields). Everything else goes in as is.
- **Guard:** `tests/test_hashing.py::test_every_public_engine_param_is_in_the_rule_hash_payload` fails if `params.py` gains (or loses) a public UPPERCASE name that `frozen_engine_params()` does not cover. Imported modules (`C`) are excluded. Adding a parameter means adding it to `frozen_engine_params()`, which changes `rule_hash`.
- **Not hashed:** anything in `app/config.py` (the OI/funding publication lags, the kline settle margin, `WATCHLIST`, poll intervals, staleness grace). These are infrastructure, and a test proves changing them leaves `rule_hash()` unchanged.
- `rule_hash(constants=None, rule_version=None, engine_params=None)`, `rule_hash_input(...)` with the same arguments, and `frozen_engine_params() -> dict[str, Any]` are the signatures.
- **`rule_hash()` at `phase1-v2` = `26ba07a110202114f8117f1be5a1551ded290c300dd2712325f3920a53256915`** (was `d6b4f0de9e7ead4f1ff802c5a5d23f8a5fc4a937081a5b90896be741e97867f4` at `phase1-v1` without engine params). No candidate or label rows existed, so nothing carries the old hash.

**Swing break: three columns on `feature_row`** (all nullable bool/text, NULL when the 4h series is not current or has no confirmed swing):
- `swing_break_direction` - `long` | `short`: the side of the latest completed 4h close that is strictly beyond the broken swing S (the M3 rule for picking S is unchanged: the last confirmed 2-bar fractal high/low, the more recent one when a close is beyond both). NULL, with a reason, when no close is beyond S. It applies to the event and the state alike.
- `swing_break_event` - TRUE iff the latest completed 4h close is beyond S AND the previous 4h close was not beyond S AND this 15m bar's close IS that 4h close (`bar_close == run[-1].close_time`, i.e. the 4h boundary). FALSE on every other 15m bar. No grace window. It does not look at the chandelier.
- `swing_break_state` - the direction permission for the 15m triggers. TRUE iff a 4h close is beyond S (direction not NULL) AND `chandelier_stop_distance_atr > 0` (the column value, rounded to its 6-decimal scale, so the stored row agrees with itself). FALSE when no close is beyond S (a 4h close moved back through S, or there never was a break) or when the distance is <= 0. NULL, with a reason, when a break exists but ATR or the signal-bar price is missing, so the stop cannot be checked.
- `chandelier_stop_price` / `chandelier_stop_distance_atr` are still filled whenever there is a direction, including when the state is FALSE because the stop is breached (the distance then shows by how much).
- **Both are stateless.** Each is a pure function of the bars visible at `bar_close` (the last `SWING_HISTORY_BARS` 4h bars, the signal 15m bar, ATR from the last `ATR_HISTORY_BARS` 4h bars). Nothing is carried between bars, so a live row, a replay row and a process started cold produce the same values (tested).
- How M4 reads them: the setup is "direction = `swing_break_direction` while `swing_break_state` is TRUE"; a rule that wants "the bar of the break" reads `swing_break_event`. The candidate `trigger` vocabulary (`TRIGGERS`, including `'swing_break'`) is unchanged; whether a `swing_break` trigger fires on the event or on the state is M4's rule, and these columns support either.
- Pure API: `levels.SwingState.break_is_new: bool` (latest 4h close beyond `broken`, previous one not), `levels.swing_break_event(state, bar_close) -> bool`.
- Alembic head is `1c329301cc23`. `feature_row` has 63 columns, 54 of them features.

### Deferred to a later milestone
- Every M4 item from HANDOFF (frozen rules, setup-family vocabulary, locked-book hash) -> M4. Not started here.
- Per-coin options OI floor and "large expiry" -> M5. Independent sample = group first row vs mean -> M8.

### Deviations from the plan
- **The M3 contract line "`swing_break` is false and the direction NULL when no swing is broken" is superseded** by the three-column contract above. The column is renamed, not reinterpreted in place, so no reader of the old name can silently get the new meaning.
- **`swing_break_direction` NULL now carries a reason** ("no 4h close beyond the last swing"). M3 left it NULL without one; the NULL-reason invariant in `test_features_null.py` now covers it.
- **`feature_hash` values change** for every row (a new column is in the mapping): the real-data BTCUSDT row at 2026-10-02 18:15 is now `db3603ceef7e3894e9d0dc74b8c74ae1e732c8f32ccf5bb78aaf3ab66b35240f` (M3 reported `33b81f0a...`). There were 0 `feature_row` rows in `signals`, so nothing stored is stale.

### Ambiguities (spec quotes and choices)
1. **"A 4h close beyond the last 2-bar fractal swing" - which S the state is measured against.** The state keeps the M3 definition: S is the last CONFIRMED swing as of the latest 4h bar. If price breaks S, runs, and then a new swing forms beyond the break (e.g. a higher swing high two bars after a post-break peak), the latest close is measured against the NEW swing, so the state can end before price has gone back through the ORIGINAL S. Remembering the original S would need memory across bars or a longer search, so it was left as is. The chandelier usually ends the state around the same time in that pattern anyway. Decide in M4/M7 if replay shows it matters.
2. **"Previous 4h close not beyond S"** is checked against the same S the latest close broke. If S was confirmed only by the latest bar, the previous bar is one of its confirming bars and cannot have closed beyond it, so the event is well-defined there too.
3. **Stop exactly at price.** `chandelier_stop_distance_atr == 0` ends the state (the brief says "<= 0"); tested.

### Verification
- `cd repo/api; & '.\.venv\Scripts\python.exe' -m pytest -q` -> **`800 passed in 153.22s`** (780 before + 20 new; no skips).
- `python -m alembic upgrade head` -> `7cd7d7f2e800 -> 1c329301cc23`; `downgrade -1` and `upgrade head` again round-trip cleanly; `alembic current` = `1c329301cc23 (head)`. `test_migration_matches_models` (compare_metadata against `public`) finds an empty diff.
- Real data, `python -m app.worker.feature_job --coins BTCUSDT --at 2026-10-02T18:15:00Z --source replay --dry-run`: `swing_break_direction` long, `swing_break_state` TRUE, `swing_break_event` FALSE (the break M3 noted is about a day old), chandelier 84,254.80, distance 0.602343 ATR. These match M3's numbers.
