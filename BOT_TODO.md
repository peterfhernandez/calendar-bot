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

- [ ] Implement `strategy/decision.py`
  - [ ] States: IDLE → SCAN → RANK → ENTER → MONITOR → {ROLL | CLOSE} → IDLE
  - [ ] Entry gate: run scanner, validate through sizer, approve or skip
  - [ ] Monitor gate: check stop/TP on each tick; trigger close or alert
  - [ ] Roll logic: if near leg approaches expiry and setup still valid, roll to new near leg
  - [ ] Hard daily loss limit: halt all trading if exceeded
- [ ] Write state machine unit tests (`tests/test_decision.py`)

---

## Phase 5 — Execution Hardening

- [ ] Implement `execution/executor.py` (hardened port of `trading/executor.py`)
  - [ ] Submit spread as combo order (both legs simultaneously) to avoid leg risk
  - [ ] Enforce slippage bound: reject fill if price > X% from mid
  - [ ] Retry on transient failures (network timeout, rate limit)
- [ ] Implement `execution/order_manager.py`
  - [ ] Track order lifecycle: submitted → partial fill → filled → cancelled
  - [ ] Reconcile order state against Deribit REST API on startup
  - [ ] Detect stuck orders and cancel after timeout
- [ ] Write executor unit tests with mocked Deribit client (`tests/test_executor.py`)
- [ ] Remove unused code from copied files. Update comments and docstrings.

---

## Phase 6 — Scheduler and Monitor Loop

- [ ] Implement `monitor/loop.py`
  - [ ] APScheduler jobs: scan every 5 min, monitor every 1 min
  - [ ] Graceful shutdown on SIGINT/SIGTERM
  - [ ] Log all events to rotating file + console
- [ ] Wire `bot.py` to start the scheduler and data feed

---

## Phase 7 — Alerts

- [ ] Implement `alerts/notifier.py`
  - [ ] Email alert (smtplib) for stop-loss, take-profit, daily loss limit, errors
  - [ ] Optional Telegram alert (python-telegram-bot)
  - [ ] Alert deduplication (don't spam same alert)
- [ ] Configure alert recipients in `config.py`

---

## Phase 8 — Backtesting

- [ ] Implement `backtest/loader.py`
  - [ ] Ingest historical Deribit option chain snapshots (CSV or JSON)
  - [ ] Normalise to same schema as `chain_cache.py`
- [ ] Implement `backtest/engine.py`
  - [ ] Replay chain snapshots through scanner + decision engine
  - [ ] Record all trades, P&L, and decision points
  - [ ] Output summary: win rate, avg P&L, max drawdown, Sharpe
- [ ] Run backtest across at least 2 distinct vol regimes before going live

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

- `strategy/scratch_scan.py` — manual debug script; connects to Deribit paper feed, waits 15s for chain data, then runs the scanner and prints ranked candidates. Run with `python -m strategy.scratch_scan` from the repo root.
- Keep optionsStrat repo for manual/paper trading — the bot is a separate project
- Any bug fixed in optionsStrat `strategies/calendar.py` or `trading/executor.py` should be ported to `calendar-bot` core modules
- Do not switch to live trading until Phase 9 is fully complete
