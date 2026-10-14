# Local event calendar

The owner keeps the event calendar here and loads it into `event_calendar` with `app.worker.calendar_import`. Nothing in this folder is downloaded or generated: copy `events_template.csv`, fill it from a source you trust, and import it.

## Columns

| Column | Meaning |
|--------|---------|
| `kind` | `unlock`, `funding_settlement` (slice features only) or `cpi`, `fomc`, `expiry` (stand-down kinds) |
| `coin` | Binance USDT symbol (`SOLUSDT`). Required for `unlock` and `expiry`, must be empty for `cpi` and `fomc` |
| `event_time` | ISO 8601 with an offset, e.g. `2026-11-12T13:30:00+00:00`. A value without an offset is rejected. Stored in UTC |
| `is_large` | empty, `true` or `false`. Empty means undecided; for an `expiry` it counts as large and stands the book down |
| `notional_usd` | empty or a non-negative number (unlock or expiry size in USD) |
| `title` | optional free text |
| `feed_source` | required: where the date came from (site, document, date checked) |

Examples in prose: a SOL unlock on 1 December 2026 at noon UTC worth 50 million USD is `unlock`, `SOLUSDT`, `2026-12-01T12:00:00+00:00`, empty `is_large`, `50000000`, a title, and the page you took it from. A CPI print has kind `cpi`, an empty coin and the release time with its offset.

## Commands (from `repo/api`)

```powershell
& '.\.venv\Scripts\python.exe' -m app.worker.calendar_import --file data\calendar\events.csv --dry-run
& '.\.venv\Scripts\python.exe' -m app.worker.calendar_import --file data\calendar\events.csv
& '.\.venv\Scripts\python.exe' -m app.worker.calendar_import --coverage --days 90
```

Always dry-run first. The importer validates every row before writing any and exits non-zero on any error. It only inserts; a row already stored (same `kind`, `coin`, `event_time`) is left alone, so re-importing is safe, and a wrong date has to be fixed in the database by hand.

## Warnings

- **The first calendar row of any kind changes live features.** While the table is empty, `event_flag` on every feature row is NULL ("unknown"). Once any row is visible, unlocks included, it reads FALSE ("no event") for every coin, even with no CPI or FOMC dates loaded. That changes `feature_hash` and the `event_flag` pool cell of new candidates (not `rule_hash`, not stand-down). The importer refuses the first load without `--allow-event-flag-flip`.
- **Stand-down kinds stand the live book down.** A `cpi` or `fomc` row stands every coin down from 30 minutes before to 30 minutes after; a large `expiry` from 15 minutes before to 15 minutes after. The row's coin is ignored, so a BTC expiry stands ETH and the alts down too. The importer refuses these kinds without `--allow-stand-down-kinds`.
- **`available_at`.** By default every imported row is known from the import instant, so replay of earlier bars does not see it. `--available-at 2026-10-01T00:00:00+00:00` sets an earlier knowledge time for dates that were published in advance; replay then sees the rows from that instant, and re-running features over bars already stored reports hash mismatches. It cannot be in the future.
- `--coverage` is read-only. It lists every watchlist coin except BTCUSDT and ETHUSDT with its unlock row count and next unlock, and prints the coins with no unlock row under "missing unlock calendar". The spec requires unlock dates for an alt before it stays in the book in Phase 2.
