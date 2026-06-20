# Historical Data Plan — Calendar Spread Bot

Plan for downloading, storing, and retrieving historical Deribit option chain
data to support the backtesting harness in `backtest/`.

See [DATA_TODO.md](DATA_TODO.md) for the task tracker.

---

## What Data the Backtest Needs

The existing `backtest/loader.py` ingests "frames" — snapshots of the full
option chain at a single point in time.  Each row in a frame is one
`TickerSnapshot`:

| Field | Description |
|---|---|
| `timestamp` | Unix epoch (seconds) |
| `instrument` | Deribit instrument name, e.g. `BTC-27JUN25-100000-C` |
| `asset` | `BTC` or `ETH` |
| `spot` | Underlying index price (USD) |
| `mark_price` | Deribit mark price (USD) |
| `mark_iv` | Implied volatility from mark price (decimal, e.g. `0.80`) |
| `bid` | Best bid (USD) |
| `ask` | Best ask (USD) |
| `open_interest` | Open interest (contracts) |

The backtest replays these frames chronologically.  For realistic results you
need at least **6–12 months** of data covering multiple volatility regimes.

---

## Can This Be Done for Free?

**Yes — Deribit provides a free public REST API with no authentication
required for historical data.**  Key endpoints:

| Endpoint | What it returns |
|---|---|
| `GET /api/v2/public/get_instruments` | All active instruments for an asset + kind |
| `GET /api/v2/public/get_book_summary_by_currency` | Summary (mark price, IV, bid, ask, OI) for all options on an asset — one call per asset per snapshot |
| `GET /api/v2/public/get_index_price` | Spot index price for BTC or ETH |
| `GET /api/v2/public/get_tradingview_chart_data` | OHLCV candles for a single instrument (useful for spot/index history) |

**The catch:** Deribit's REST API returns *current* data, not a time machine.
For true historical snapshots you have two complementary approaches:

### Approach A — Self-Collect Going Forward (Free, Recommended)

Write a lightweight collector that polls `get_book_summary_by_currency` every
5–15 minutes and persists each snapshot.  After a few weeks you have your own
archive.  This is the cleanest approach because:

- No third-party dependency
- Data is in exactly the schema `loader.py` expects
- Costs nothing

**Minimum viable history before live trading:** 8 weeks of self-collected data
(covers at least one near-expiry cycle per leg horizon).

### Approach B — Third-Party Historical Data (Free Tier Available)

Several providers offer Deribit option chain history:

| Provider | Free Tier | Data Available |
|---|---|---|
| **Tardis.dev** | 1-day free samples per instrument | Full Deribit option chain, tick-level. Paid plans from ~$19/month |
| **Deribit History (CSV exports)** | Free via the web UI for your own account trades | Trade history only, not full chain |
| **Amberdata** | Free trial | Options data including IV surface |
| **Kaiko** | No free tier | Institutional-grade |

Tardis is the most practical: their free daily samples let you test the
pipeline end-to-end before committing to a paid subscription.

### Recommendation

Use **Approach A** as the primary path.  Build the collector now; it runs
unattended alongside (or before) the live bot.  Supplement with a **Tardis
free sample** to validate the pipeline with real historical data immediately.

---

## Storage: Why Not SQLite?

SQLite is already used for bot state (`db/state.py`) and is fine for that
workload (hundreds of rows).  Historical option chain data is a different
problem:

- A single BTC chain snapshot has **300–600 instrument rows**
- At a 5-minute cadence, one day = ~288 snapshots × ~450 rows = **~130,000
  rows/day**
- 6 months of BTC + ETH data ≈ **45–60 million rows**

SQLite handles 60M rows, but query performance degrades without careful
indexing, and it does not support concurrent writes from a collector process
and a backtest process.

### Recommended Storage: DuckDB

[DuckDB](https://duckdb.org) is an embedded, serverless analytical database —
think "SQLite for analytics."

| Property | SQLite | DuckDB |
|---|---|---|
| Embedded (no server) | ✓ | ✓ |
| Python stdlib | ✓ | ✗ (pip install duckdb) |
| Analytical query speed | Moderate | Excellent |
| Columnar storage | ✗ | ✓ |
| Concurrent reads | Limited | ✓ |
| SQL dialect | Standard | Standard + extensions |
| File size for 60M rows | ~6 GB | ~1–2 GB (columnar compression) |

DuckDB uses standard SQL so the query scripts feel identical to SQLite.  The
`duckdb` Python package is a single pip install and the database is a single
`.duckdb` file — no server, no configuration.

**Alternative:** PostgreSQL with TimescaleDB extension is enterprise-grade but
requires a running server and is overkill for a single-developer backtest.

---

## Database Schema

```sql
-- One row per instrument per snapshot timestamp
CREATE TABLE option_chain (
    id            BIGINT PRIMARY KEY,   -- auto-increment
    ts            TIMESTAMP NOT NULL,   -- snapshot time (UTC)
    instrument    VARCHAR   NOT NULL,   -- e.g. BTC-27JUN25-100000-C
    asset         VARCHAR(3) NOT NULL,  -- BTC or ETH
    spot          DOUBLE    NOT NULL,   -- index price (USD)
    mark_price    DOUBLE    NOT NULL,   -- Deribit mark price (USD)
    mark_iv       DOUBLE    NOT NULL,   -- implied vol (decimal)
    bid           DOUBLE    NOT NULL,   -- best bid (USD)
    ask           DOUBLE    NOT NULL,   -- best ask (USD)
    open_interest DOUBLE    NOT NULL    -- open interest (contracts)
);

CREATE INDEX idx_option_chain_ts        ON option_chain (ts);
CREATE INDEX idx_option_chain_asset_ts  ON option_chain (asset, ts);
CREATE INDEX idx_option_chain_instr_ts  ON option_chain (instrument, ts);
```

A **separate metadata table** tracks collection runs:

```sql
CREATE TABLE collection_runs (
    id         INTEGER PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    ended_at   TIMESTAMP,
    asset      VARCHAR(3),
    rows_added INTEGER,
    status     VARCHAR(16)   -- 'ok', 'error', 'partial'
);
```

---

## File Layout

```text
backtest/
├── historic_data/
│   ├── options.duckdb          # main database (all assets)
│   └── samples/                # one-off CSVs / Tardis samples for testing
├── data_collector.py           # polls Deribit and writes to DuckDB
├── data_loader_db.py           # replaces/extends loader.py for DB reads
└── sql/
    ├── schema.sql              # CREATE TABLE statements
    ├── query_frames.sql        # example: pull frames for a date range
    └── summary_stats.sql       # row counts, date coverage, assets
```

The existing `loader.py` (CSV/JSON) is **not replaced** — it remains useful
for one-off Tardis samples.  `data_loader_db.py` adds a new entrypoint that
reads frames directly from DuckDB.

---

## Collector Design

`backtest/data_collector.py` is a standalone script (not part of the live
bot):

1. Calls `GET /api/v2/public/get_book_summary_by_currency` for each asset
   (BTC, ETH) — one HTTP call per asset, returns the full chain
2. Calls `GET /api/v2/public/get_index_price` for spot price
3. Normalises each row into the `option_chain` schema
4. Writes to DuckDB in a single transaction per snapshot
5. Logs the collection run to `collection_runs`
6. Sleeps until the next interval (configurable, default 5 minutes)

The collector writes only to its own database file; the backtest reads from it.
No locking conflicts because DuckDB supports concurrent readers.

Can be scheduled with the existing APScheduler infrastructure or run as a
simple `while True` + `time.sleep` loop independently of `bot.py`.

---

## Loader Design (DB Path)

`backtest/data_loader_db.py` exposes:

```python
def load_frames_from_db(
    db_path: str | Path,
    asset:   str,
    start:   datetime,
    end:     datetime,
    max_gap_minutes: int = 30,   # warn if gap between frames exceeds this
) -> list[Frame]:
    ...
```

Internally runs:

```sql
SELECT ts, instrument, asset, spot, mark_price, mark_iv, bid, ask, open_interest
FROM   option_chain
WHERE  asset = ? AND ts BETWEEN ? AND ?
ORDER  BY ts, instrument
```

Then groups rows by `ts` into `Frame` objects (same structure as `loader.py`
output) so `BacktestEngine.run()` needs no changes.

---

## SQL Query Scripts (`backtest/sql/`)

| Script | Purpose |
|---|---|
| `schema.sql` | Create tables and indexes from scratch |
| `query_frames.sql` | Pull a date range for one asset |
| `summary_stats.sql` | Row counts, earliest/latest ts, assets present |
| `data_gaps.sql` | Identify gaps > N minutes in coverage |
| `instrument_coverage.sql` | Which instruments have data, for how long |

These are standalone `.sql` files that can be run with the DuckDB CLI or
pasted into a Python connection for ad-hoc analysis.

---

## Validation

Before running a backtest from DB data:

1. Run `summary_stats.sql` — confirm expected row counts and date range
2. Run `data_gaps.sql` — identify and document any gaps
3. Load a sample (1 day) and run `BacktestEngine` on it — confirm zero errors
4. Compare a known snapshot from DB output against the Tardis sample CSV to
   verify field mapping

---

## Effort Estimate

| Task | Effort |
|---|---|
| Write `schema.sql` and create DB | 1–2 hours |
| Write `data_collector.py` | 4–8 hours |
| Write `data_loader_db.py` | 2–4 hours |
| Write SQL query scripts | 1–2 hours |
| Download Tardis sample + validate pipeline | 1–2 hours |
| Let collector run and accumulate history | 4–8 weeks elapsed |
| Write tests | 2–4 hours |
| **Total active effort** | **~1–2 days** |

The bottleneck is elapsed time waiting for the collector to accumulate enough
history, not implementation effort.
