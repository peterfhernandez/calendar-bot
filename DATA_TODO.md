# Historical Data — TODO

Progress tracker for building the historical data pipeline.
See [DATA_PLAN.md](DATA_PLAN.md) for full design details.

Data is stored in `backtest/historic_data/options.duckdb`.

---

## Phase D1 — Database Setup

- [x] Install DuckDB: `pip install duckdb` and add to `requirements.txt`
- [x] Create `backtest/sql/` folder
- [x] Write `backtest/sql/schema.sql`
  - [x] `option_chain` table with all `TickerSnapshot` fields
  - [x] Indexes on `(ts)`, `(asset, ts)`, `(instrument, ts)`
  - [x] `collection_runs` metadata table
- [x] Create `backtest/historic_data/` folder (add `.gitkeep`; add `*.duckdb` to `.gitignore`)
- [x] Run `schema.sql` to initialise `backtest/historic_data/options.duckdb`
- [x] Verify schema with DuckDB CLI: `DESCRIBE option_chain;`

---

## Phase D2 — Data Collector

- [x] Implement `backtest/data_collector.py`
  - [x] Poll `GET /api/v2/public/get_book_summary_by_currency` for BTC and ETH
  - [x] Poll `GET /api/v2/public/get_index_price` for spot price
  - [x] Normalise API response to `option_chain` schema
  - [x] Write snapshot rows to DuckDB in a single transaction
  - [x] Log each run to `collection_runs` table (rows added, status, timestamps)
  - [x] Configurable poll interval (default 5 minutes via `COLLECTOR_INTERVAL_SEC`)
  - [x] Graceful error handling: log and retry on HTTP errors; never crash the loop
- [ ] Manual smoke test: run collector for 30 minutes; confirm rows appear in DB
- [x] Write `backtest/sql/summary_stats.sql` — row counts, date range, assets

---

## Phase D3 — SQL Query Scripts

- [x] Write `backtest/sql/query_frames.sql` — pull frames for a given asset + date range
- [x] Write `backtest/sql/data_gaps.sql` — find gaps > N minutes between consecutive timestamps
- [x] Write `backtest/sql/instrument_coverage.sql` — list instruments and their data span

---

## Phase D4 — DB-Backed Loader

- [x] Implement `backtest/data_loader_db.py`
  - [x] `load_frames_from_db(db_path, asset, start, end)` returns `list[Frame]`
  - [x] Groups rows by timestamp into frames (same output shape as `loader.py`)
  - [x] Warns if gap between consecutive frames exceeds `max_gap_minutes`
  - [x] Compatible with `BacktestEngine.run()` — no changes needed to engine
- [x] Write unit tests `tests/test_data_loader_db.py`
  - [x] Test with an in-memory DuckDB populated with synthetic rows
  - [x] Verify frame grouping, ordering, and gap warnings

---

## Phase D5 — Validation with Sample Data

- [ ] Download a free 1-day Tardis sample for BTC options (<https://tardis.dev/datasets>)
- [ ] Map Tardis CSV columns to `option_chain` schema; import via `loader.py` or direct insert
- [ ] Run `BacktestEngine` on the Tardis sample — confirm zero errors
- [ ] Cross-check one timestamp: compare DB output row vs Tardis CSV row field by field
- [ ] Run `data_gaps.sql` on sample — confirm no unexpected gaps

---

## Phase D6 — Scheduled Collection

- [x] Decide where to run collector: **standalone** (`collect.py`) or **alongside bot** (`bot.py --collect`)
- [x] Write a standalone `collect.py` entry point that runs the collector loop independently
- [x] Add `--collect` flag to `bot.py` to run the collector as a concurrent task alongside the bot
  - [x] `--once` flag for smoke-testing (collects one snapshot and exits)
  - [x] `--assets`, `--interval`, `--db` overrides
  - [x] `--log-level` and `--log-file` (rotating, 10 MB × 5) options
  - [x] Graceful SIGINT/SIGTERM shutdown (finishes current cycle, then exits cleanly)
  - [x] Hourly DB summary logged for health monitoring
- [ ] Confirm collector survives a 24-hour run without memory growth or errors
- [ ] Set a calendar reminder to check coverage after 2 weeks and 4 weeks

---

## Phase D7 — Backtest with Real Data

- [ ] After 4+ weeks of collected data: run `summary_stats.sql` to confirm coverage
- [ ] Run `data_gaps.sql` — document any significant gaps and their cause
- [ ] Run full `BacktestEngine` over collected data using `data_loader_db.py`
- [ ] Compare results to synthetic-data backtest from Phase 8 (BOT_TODO)
- [ ] Tune scanner/decision parameters if results differ significantly

---

## Notes

- The Deribit public REST API requires no API key for market data endpoints
- DuckDB `.duckdb` files must be added to `.gitignore` (they will be large) ✓
- `loader.py` (CSV/JSON) is **not replaced** — keep it for one-off sample files
- At 5-minute cadence, expect ~130,000 rows/day per asset (~260,000/day for BTC+ETH)
- At that rate, 12 weeks of data ≈ ~22 million rows — well within DuckDB's comfort zone
- Timestamps are stored as naive UTC in DuckDB (tzinfo stripped before insert) to avoid
  local-timezone shift on Windows. All query code strips tzinfo before passing to DuckDB.
- Do not run the collector against the Deribit **live** endpoint unless `DERIBIT_PAPER = False`
  — paper endpoint (`test.deribit.com`) is fine for data collection since market data is identical
- Scratch: `scratch/scratch_data_collector.py` — connects to Deribit paper API, collects one ETH
  snapshot, prints summary stats and the first 3 ticker rows. Run with
  `python -m scratch.scratch_data_collector` from the repo root.
- Scratch: `scratch/scratch_collect.py` — smoke-tests `collect.py --once` via subprocess; verifies
  rows are written and the second run appends more rows. Run with
  `python -m scratch.scratch_collect` from the repo root.
- `config.py` optional overrides: `COLLECTOR_INTERVAL_SEC` (default 300), `COLLECTOR_ASSETS`
  (default same as `ASSETS`)
