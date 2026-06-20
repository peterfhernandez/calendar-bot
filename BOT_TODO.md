# Calendar Spread Bot — TODO

Progress tracker for building the `calendar-bot` project.
See [BOT_PLAN.md](BOT_PLAN.md) for full design details and [README.md](README.md) for an overview.

---

## Phase 0 — Project Setup

- [x] Create `calendar-bot/` repo and `git init`
- [x] Set up Python virtual environment (3.11+)
- [x] Create folder scaffold: `core/`, `data/`, `strategy/`, `execution/`, `monitor/`, `db/`, `backtest/`, `alerts/`, `tests/`
- [x] Add `requirements.txt` with: `websockets`, `aiohttp`, `apscheduler`, `scipy`, `numpy`, `pytest`
- [x] Add `.gitignore` (venv, `*.db`, `.env`, `__pycache__`)
- [x] Create `config.py` with all tuneable parameters (assets, horizons, sizing, stop/TP, scheduler intervals)
- [x] Create `bot.py` entry point stub

---

## Phase 1 — Port Core Logic

Files have already been copied over from optionsStrat. Files need to be adapted...

- [x] Port `market/pricing.py` → `core/pricing.py` (Black-Scholes, breakevens, prob-of-profit)
- [x] Port `trading/fee_calculator.py` → `core/fees.py`
- [x] Port `strategies/calendar.py` → `core/calendar_engine.py` (spread valuation, stop/TP check, P&L at expiry)
- [x] Port `database/calendar_db.py` → `db/state.py`
- [x] Write unit tests for ported pricing functions (`tests/test_pricing.py`)
- [x] Write unit tests for calendar engine (`tests/test_calendar_engine.py`)
- [x] Write unit tests for fees functions (`tests/test_fees.py`)
- [x] Write unit tests for state engine (`tests/test_state.py`)
- [x] Remove any unused code/functions from the ported files. Update all the comments and docstrings

---

## Phase 2 — Live Data Feed

- [x] Implement `data/deribit_feed.py`
  - [x] Deribit WebSocket connection with authentication
  - [x] Subscribe to ticker channels for target instruments
  - [x] Fetch option chain (all strikes/expiries for an asset)
  - [x] Extract spot price, mark IV, bid/ask per instrument
  - [x] Reconnect logic with exponential backoff
- [x] Implement `data/chain_cache.py`
  - [x] In-memory cache with configurable TTL (default 30s)
  - [x] Thread-safe read/write
  - [x] Stale-data detection and warning
- [x] Write integration tests against Deribit paper API (`tests/test_feed.py`)
- [x] Add `data/debug_viewer.py` — live terminal dashboard for the feed

---

## Phase 3 — Scanner and Ranker

- [x] Implement `strategy/scanner.py`
  - [x] Enumerate all valid near/far expiry pairs per asset
  - [x] Filter by min OI, min IV contango, min prob of profit
  - [x] Score each candidate: EV = P(profit) × avg_win − P(loss) × net_debit
  - [x] Return ranked list of `CalendarCandidate` objects
- [x] Implement `strategy/sizer.py`
  - [x] Calculate position size from portfolio value and `MAX_LOSS_PCT`
  - [x] Enforce `MAX_POSITIONS` concurrent limit
  - [x] Enforce correlation limits (skip if same asset + similar strike already open)
- [x] Write unit tests for scanner and sizer (`tests/test_scanner.py`)
- [x] Remove any unused functions from the scanner.py module. Update comments and docstrings.
- [x] Add `scratch_scan.py` — test the scanner: manual debug script in repo's root
- [x] Move `scratch_scan.py` to the strategy folder

---

## Phase 4 — Decision State Machine

- [x] Implement `strategy/decision.py`
  - [x] States: IDLE → SCAN → RANK → ENTER → MONITOR → {ROLL | CLOSE} → IDLE
  - [x] Entry gate: run scanner, validate through sizer, approve or skip
  - [x] Monitor gate: check stop/TP on each tick; trigger close or alert
  - [x] Roll logic: if near leg approaches expiry and setup still valid, roll to new near leg
  - [x] Hard daily loss limit: halt all trading if exceeded
- [x] Write state machine unit tests (`tests/test_decision.py`)

---

## Phase 5 — Execution Hardening

- [x] Implement `execution/executor.py` (hardened port of `trading/executor.py`)
  - [x] Submit spread as combo order (both legs simultaneously) to avoid leg risk
  - [x] Enforce slippage bound: reject fill if price > X% from intended limit price
  - [x] Retry on transient failures (network timeout, rate limit)
- [x] Implement `execution/order_manager.py`
  - [x] Track order lifecycle: submitted → partial fill → filled → cancelled
  - [x] Reconcile order state against Deribit REST API on startup
  - [x] Detect stuck orders and cancel after timeout
- [x] Write executor unit tests with mocked Deribit client (`tests/test_executor.py`)
- [x] Remove unused code from copied files. Update comments and docstrings.
- [x] Add `execution/scratch_executor.py` — end-to-end verification script (9 scenarios, no live orders placed)

---

## Phase 6 — Scheduler and Monitor Loop

- [x] Implement `monitor/loop.py`
  - [x] APScheduler jobs: scan every 5 min, monitor every 1 min
  - [x] Graceful shutdown on SIGINT/SIGTERM
  - [x] Log all events to rotating file + console
- [x] Wire `bot.py` to start the scheduler and data feed
- [x] Add `monitor/scratch_loop.py` — test the loop

---

## Phase 7 — Alerts

- [x] Implement `alerts/notifier.py`
  - [x] Email alert (smtplib) for stop-loss, take-profit, daily loss limit, errors
  - [x] Optional Telegram alert (python-telegram-bot)
  - [x] Alert deduplication (don't spam same alert)
- [x] Configure alert recipients in `config.py`

---

## Phase 8 — Backtesting

- [x] Implement `backtest/loader.py`
  - [x] Ingest historical Deribit option chain snapshots (CSV or JSON)
  - [x] Normalise to same schema as `chain_cache.py`
- [x] Implement `backtest/engine.py`
  - [x] Replay chain snapshots through scanner + decision engine
  - [x] Record all trades, P&L, and decision points
  - [x] Output summary: win rate, avg P&L, max drawdown, Sharpe
- [x] Run backtest across at least 2 distinct vol regimes before going live
  - [x] Identify 4 distinct vol regimes
  - [x] Setup a script to run the backtest on these 4 regimes
  - [x] make sure that output summaries are clearly displayed for all regimes. Display: regime name, win rate, avg p&l, max drawdown, sharpe

---

## Phase 9 — Paper Trading Validation

- [ ] Run bot in paper mode (`DERIBIT_PAPER = True`) for minimum 4 weeks
- [ ] Verify scanner selects setups that profit at expiry
- [ ] Verify stop-loss and take-profit triggers fire correctly
- [ ] Verify roll logic outcomes vs outright close
- [ ] Verify daily loss limit halts the bot
- [ ] Review all logs; tune `config.py` parameters as needed

---

## Phase 10 — Live Deployment

- [ ] Switch `DERIBIT_PAPER = False` in config
- [ ] Set up API key in `.env` (never commit)
- [ ] Deploy to always-on server or VPS
- [ ] Set up uptime monitoring (e.g. healthcheck ping)
- [ ] Run with small capital first (reduce `MAX_LOSS_PCT` to 0.5% for first month)
- [ ] Review live performance weekly for first 3 months

---

## Notes

- `scratch/scratch_loop.py` — end-to-end verification script for the BotLoop; runs with fake cache/executor for 12 s then prints a summary. Run with `python -m scratch.scratch_loop` from the repo root.
- `scratch/scratch_scan.py` — manual debug script; connects to Deribit paper feed, waits 15s for chain data, then runs the scanner and prints ranked candidates. Run with `python -m scratch.scratch_scan` from the repo root.
- `scratch/scratch_decision.py` — end-to-end dry-run of the decision engine; connects to the paper feed, runs `scan_tick()` then `monitor_tick()`, and prints a full status report. Uses a separate `db/scratch_decision.db` so it doesn't touch the real database. Run with `python -m scratch.scratch_decision` from the repo root.
- Keep optionsStrat repo for manual/paper trading — the bot is a separate project
- Any bug fixed in optionsStrat `strategies/calendar.py` or `trading/executor.py` should be ported to `calendar-bot` core modules
- `scratch/scratch_notifier.py` — end-to-end verification script for the Notifier; runs 8 sections covering dispatch, deduplication, cooldown expiry, all helper methods, skip-when-unconfigured, and payload correctness (19 checks, no live network calls). Run with `python -m scratch.scratch_notifier` from the repo root.
- `scratch/scratch_backtest.py` — end-to-end verification for the backtesting harness; generates synthetic BTC data for 4 vol regimes (High Vol Contango, Low Vol Weak Contango, IV Spike/Collapse, Stable Sideways), runs BacktestEngine on each, and prints a formatted summary table. Also exercises loader CSV/JSON round-trips and BacktestChainCache. Run with `python -m scratch.scratch_backtest` from the repo root.
- `scratch/scratch_three_fixes.py` — demonstrates three bug fixes: (1) negative-EV trade rejection, (2) correct stale-IV monitor message, (3) daily_pnl reflecting unrealized MTM. Run with `python -m scratch.scratch_three_fixes` from the repo root.
- Do not switch to live trading until Phase 9 is fully complete

---

## Bug Fixes

- [x] **Negative-EV entry filter** — added `MIN_EV = 0.0` to `config.py`; `strategy/decision.py` now rejects any candidate with `ev_score < MIN_EV` before calling the sizer or executor. Tests: `TestNegativeEvFilter` in `tests/test_decision.py`.
- [x] **Misleading monitor OK message on stale IV** — `_monitor_position` returns `("__NO_IV__", 0.0)` when IV is unavailable; `monitor_tick` counts these and emits `"N position(s) skipped — no IV data"` instead of the incorrect `"All positions OK."`. Tests: `TestMonitorSkippedNoIv`.
- [x] **daily_pnl stuck at 0.00** — `_monitor_position` now returns the per-position unrealized MTM P&L `(sv - net_debit) * qty` when the position is held; `monitor_tick` accumulates these into `_unrealized_pnl`; `_status()` returns `_today_pnl + _unrealized_pnl`. Tests: `TestDailyPnlUnrealized`.
- [x] **Instant take-profit on newly-entered positions** — `scan_tick` adds each entered `trade.id` to `_just_entered`; `_monitor_position` skips any position in that set (grace period); `monitor_tick` clears the set after each pass so the position is evaluated normally from the next tick onward. Tests: `TestNewPositionGracePeriod`. Scratch: `scratch/scratch_two_fixes.py`.
- [x] **Realized P&L always 0.00 on close** — `_close_position` now accepts a `spread_value` parameter; when provided, `pnl = (spread_value - net_debit) * qty` is used instead of `executor.close_spread()` return (which in dry-run mode echoes the entry debit, producing zero gain). Stop and TP callers pass the `sv` from `check_calendar_status`; expiry/roll-fail closes fall back to the executor return. Tests: `TestClosePositionPnl`.
- [x] **Spurious TP from B-S spread_value mismatch** — `check_calendar_status` now accepts an optional `market_sv` parameter; `_monitor_position` computes the current spread as `(far_mid - near_mid) * qty` from live cache bid/ask and passes it as `market_sv`. B-S is used only as a fallback when leg prices are absent from the cache. This fixes cases where B-S (using a single uniform IV) computed sv ~10× above the actual market spread (e.g. $2266 vs $178 for a BTC 61000-C calendar), triggering instant spurious TPs. New helper: `_get_market_spread_value`. Tests: `TestMarketSpreadValue`.
- [x] **daily_pnl inflated by double-qty multiplication** — `spread_value()` returns a qty-weighted total (B-S price × qty), but `_monitor_position` was treating it as per-unit and multiplying by qty again: `(sv - net_debit) * qty`. For a position with qty=8.5 this inflated the unrealized P&L by ~8.5×, producing values like $2900 instead of ~$40. Fixed to `sv - net_debit * qty`. Same double-qty bug fixed in the `spread_value` path of `_close_position`. Tests: `TestClosePositionPnl`, `TestDailyPnlUnrealized` (assertions updated to reflect correct formula).
