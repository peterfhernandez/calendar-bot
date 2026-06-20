# Historical Data — TODO

Progress tracker for building the historical data pipeline.
See [DATA_PLAN.md](DATA_PLAN.md) for full design details.

Data is stored in `backtest/historic_data/options.duckdb`.

---

## Phase D1 — Database Setup

- [ ] Install DuckDB: `pip install duckdb` and add to `requirements.txt`
- [ ] Create `backtest/sql/` folder
- [ ] Write `backtest/sql/schema.sql`
  - [ ] `option_chain` table with all `TickerSnapshot` fields
  - [ ] Indexes on `(ts)`, `(asset, ts)`, `(instrument, ts)`
  - [ ] `collection_runs` metadata table
- [ ] Create `backtest/historic_data/` folder (add `.gitkeep`; add `*.duckdb` to `.gitignore`)
- [ ] Run `schema.sql` to initialise `backtest/historic_data/options.duckdb`
- [ ] Verify schema with DuckDB CLI: `DESCRIBE option_chain;`

---

## Phase D2 — Data Collector

- [ ] Implement `backtest/data_collector.py`
  - [ ] Poll `GET /api/v2/public/get_book_summary_by_currency` for BTC and ETH
  - [ ] Poll `GET /api/v2/public/get_index_price` for spot price
  - [ ] Normalise API response to `option_chain` schema
  - [ ] Write snapshot rows to DuckDB in a single transaction
  - [ ] Log each run to `collection_runs` table (rows added, status, timestamps)
  - [ ] Configurable poll interval (default 5 minutes)
  - [ ] Graceful error handling: log and retry on HTTP errors; never crash the loop
- [ ] Manual smoke test: run collector for 30 minutes; confirm rows appear in DB
- [ ] Write `backtest/sql/summary_stats.sql` — row counts, date range, assets

---

## Phase D3 — SQL Query Scripts

- [ ] Write `backtest/sql/query_frames.sql` — pull frames for a given asset + date range
- [ ] Write `backtest/sql/data_gaps.sql` — find gaps > N minutes between consecutive timestamps
- [ ] Write `backtest/sql/instrument_coverage.sql` — list instruments and their data span

---

## Phase D4 — DB-Backed Loader

- [ ] Implement `backtest/data_loader_db.py`
  - [ ] `load_frames_from_db(db_path, asset, start, end)` returns `list[Frame]`
  - [ ] Groups rows by timestamp into frames (same output shape as `loader.py`)
  - [ ] Warns if gap between consecutive frames exceeds `max_gap_minutes`
  - [ ] Compatible with `BacktestEngine.run()` — no changes needed to engine
- [ ] Write unit tests `tests/test_data_loader_db.py`
  - [ ] Test with an in-memory DuckDB populated with synthetic rows
  - [ ] Verify frame grouping, ordering, and gap warnings

---

## Phase D5 — Validation with Sample Data

- [ ] Download a free 1-day Tardis sample for BTC options (<https://tardis.dev/datasets>)
- [ ] Map Tardis CSV columns to `option_chain` schema; import via `loader.py` or direct insert
- [ ] Run `BacktestEngine` on the Tardis sample — confirm zero errors
- [ ] Cross-check one timestamp: compare DB output row vs Tardis CSV row field by field
- [ ] Run `data_gaps.sql` on sample — confirm no unexpected gaps

---

## Phase D6 — Scheduled Collection

- [ ] Decide where to run collector: alongside `bot.py` or as a separate process
- [ ] Add collector invocation to `bot.py` scheduler (APScheduler job, every 5 min)
  — OR —
- [ ] Write a standalone `collect.py` entry point that runs the collector loop independently
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
- DuckDB `.duckdb` files must be added to `.gitignore` (they will be large)
- `loader.py` (CSV/JSON) is **not replaced** — keep it for one-off sample files
- At 5-minute cadence, expect ~130,000 rows/day per asset (~260,000/day for BTC+ETH)
- At that rate, 12 weeks of data ≈ ~22 million rows — well within DuckDB's comfort zone
- Do not run the collector against the Deribit **live** endpoint unless `DERIBIT_PAPER = False`
  — paper endpoint (`test.deribit.com`) is fine for data collection since market data is identical
