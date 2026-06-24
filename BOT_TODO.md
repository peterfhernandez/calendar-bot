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

## Phase 8b — Portfolio Tracker

- [x] Implement `portfolio/tracker.py`
  - [x] Fetch account summary from Deribit REST API (`/private/get_account_summary`) on startup and after every position change
  - [x] Track available cash = equity − used margin (via `available_funds` from Deribit API)
  - [x] Track used margin = sum of net debits on open positions (from SQLite)
  - [x] Track unrealized P&L (MTM from Deribit `floating_profit_loss`) and realized P&L today (from SQLite)
  - [x] Provide `portfolio_view()` returning a formatted snapshot for logging and the terminal dashboard
  - [x] Reconcile Deribit reported margin against SQLite position table; log discrepancies > 10%
- [x] Integrate portfolio tracker into `strategy/sizer.py`
  - [x] `available_cash` is passed as `portfolio_value` from decision engine after refresh; sizer already rejects tiny sizes via `MIN_NET_DEBIT` and `_MIN_QTY` checks
- [x] Integrate portfolio tracker into `strategy/decision.py`
  - [x] `scan_tick` calls `portfolio.refresh()` before sizing; skips entry if `available_cash == 0` and credentials are set
  - [x] `portfolio_value` updated to `available_cash` when refresh returns a positive value
- [x] Integrate portfolio tracker into `monitor/loop.py`
  - [x] Log a portfolio snapshot after each scan cycle via `portfolio_view()`
- [x] Write unit tests `tests/test_portfolio.py` (24 tests)
  - [x] Mock Deribit REST responses; verify available_cash calculation
  - [x] Verify reconciliation warning fires when equity and SQLite totals diverge
- [x] Add `scratch/scratch_portfolio.py` — connects to paper API and prints live portfolio snapshot

---

## Phase 8c — Liquidity Gate

- [x] Add liquidity config parameters to `config.py`: `MIN_LEG_BID_SIZE`, `MIN_LEG_ASK_SIZE`, `MAX_LEG_SPREAD_PCT = 0.05`, `MAX_ENTRY_PREMIUM = 0.10`, `COMBO_FILL_TIMEOUT_SEC = 30`
- [x] Update `strategy/scanner.py` coarse filter
  - [x] Reject candidates where either leg has zero bid or zero ask in the cache
- [x] Add liquidity gate to `strategy/decision.py` (`_check_liquidity_gate`, runs just before order submission)
  - [x] Check `bid_size >= MIN_LEG_BID_SIZE` and `ask_size >= MIN_LEG_ASK_SIZE` for both legs (requires adding `bid_size`/`ask_size` to `TickerSnapshot`)
  - [x] Check `(ask - bid) / mid <= MAX_LEG_SPREAD_PCT` for both legs
  - [x] Check `net_debit <= spread_mid * (1 + MAX_ENTRY_PREMIUM)` — prevents entering positions that start deeply underwater due to bid/ask friction
  - [x] Log and skip any candidate failing the gate; do not retry until next scan cycle
- [x] Update `strategy/scanner.py` unit tests to cover liquidity filter scenarios (4 new tests in `TestScannerLiquidityFilter`)
- [x] Update `strategy/decision.py` unit tests: `TestLiquidityGate` (6 new bid/ask size tests added, 88 total passing)

---

## Phase 8d — Trading Mode (Test vs Live) and Combo Orders

### Trading mode

- [x] Replace `DERIBIT_PAPER = True/False` with `TRADING_MODE = "paper" | "test" | "live"` in `config.py`
  - [x] Default value is `"paper"`
  - [x] Add derived constants `DERIBIT_WS_URL` and `DERIBIT_REST_URL` (test URL for paper+test, live URL for live)
  - [x] `DERIBIT_PAPER` kept as a backwards-compatible alias (`not _LIVE`); primary checks use `TRADING_MODE`
- [x] Update `data/deribit_feed.py` to read `DERIBIT_WS_URL` from config (no hard-coded URLs)
- [x] Update `execution/executor.py` to branch on `TRADING_MODE`:
  - [x] `"paper"` → dry-run path: log the order, return a simulated fill, never call the Deribit API
  - [x] `"test"` or `"live"` → real order submission using `DERIBIT_WS_URL` / `DERIBIT_REST_URL`
- [x] Add separate `.env` keys: `DERIBIT_TEST_CLIENT_ID`, `DERIBIT_TEST_CLIENT_SECRET` (shared by paper + test), `DERIBIT_LIVE_CLIENT_ID`, `DERIBIT_LIVE_CLIENT_SECRET`; `config.py` selects the right pair
- [x] Add prominent startup banner in `bot.py`:
  - [x] Paper: `*** PAPER MODE — data from test.deribit.com, no orders placed ***`
  - [x] Test: `*** TEST MODE — orders will be placed on test.deribit.com ***`
  - [x] Live: `*** LIVE MODE — REAL MONEY on www.deribit.com ***`
- [x] `bot.py` refuses to start when `TRADING_MODE == "live"` and `DAILY_LOSS_LIMIT` is not set
- [x] All `scratch_*.py` scripts check `TRADING_MODE` at startup and abort with an error if it is `"live"`
- [x] Update unit tests: any test that previously checked `DERIBIT_PAPER` now checks `TRADING_MODE`; paper mode tests added

### Combo orders and fallback

- [x] Implement combo order path in `execution/executor.py` (`_async_enter_spread_combo`)
  - [x] Create combo via `private/create_combo`, submit at net debit limit price
  - [x] Poll for fill up to `COMBO_FILL_TIMEOUT_SEC`; return fill details (with `via_combo=True`) on success
- [x] Implement individual-leg fallback in `execution/executor.py`
  - [x] Only triggered if combo times out or is unavailable
  - [x] Submit near leg; on success submit far leg; on far-leg failure immediately cancel near leg
  - [x] Log a WARNING whenever the fallback path is used
- [x] `COMBO_FILL_TIMEOUT_SEC` already in `config.py`
- [x] Update executor unit tests: `TestComboOrder`, `TestIndividualLegFallback`, `TestFallbackCancelsNearOnFarFailure` (300 tests passing)
- [x] Update `scratch/scratch_executor.py` with combo order (test 10) and fallback (test 11) scenarios

---

## Phase 8e — 1-Day Near Legs

- [ ] Add `1` to `NEAR_DAYS_OPTIONS` in `config.py`
- [ ] Update `strategy/scanner.py` to enforce valid near/far pairs: near < far (prevents 1d/1d or 7d/7d)
- [ ] Confirm scanner correctly pairs 1d near with 7d and 14d far legs only (not 30d+ which would be unusual)
- [ ] Update scanner unit tests to cover 1d near-leg pairs

---

## Phase 8f — Notification Wiring

- [ ] Wire notifier calls into `strategy/decision.py`
  - [ ] `notify_entry(trade)` after successful fill in `scan_tick`
  - [ ] `notify_stop(trade, pnl)` when stop-loss triggers in `monitor_tick`
  - [ ] `notify_take_profit(trade, pnl)` when take-profit triggers in `monitor_tick`
  - [ ] `notify_roll(trade)` when near leg is rolled in `monitor_tick`
  - [ ] `notify_close(trade, pnl)` when position closes at expiry in `monitor_tick`
  - [ ] `notify_daily_limit(daily_pnl)` when daily loss limit is breached
  - [ ] `notify_error(exc)` in exception handlers in `bot.py` and `monitor/loop.py`
  - [ ] `notify_warning(msg)` when individual-leg fallback is used
- [ ] Add startup self-test in `bot.py`: send "Bot started" notification on launch; log warning if it fails but do not abort
- [ ] Verify all alert config keys present in `config.py`: `ALERT_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- [ ] Add `scratch/scratch_notify_live.py` — sends a real test alert via configured SMTP and Telegram; confirms delivery end-to-end

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
- `scratch/scratch_entry_gate.py` — demonstrates the liquidity gate: 7 scenarios covering per-leg spread rejection, entry premium rejection (including the live trade_id=5 scenario), and a clean candidate that passes all checks. Run with `python -m scratch.scratch_entry_gate` from the repo root.
- `scratch/scratch_sizer_fixes.py` — demonstrates the two fixes for the 2026-06-22 halt: (1) near-zero debit guard in sizer, (2) negative spread value clamped to zero. Run with `python -m scratch.scratch_sizer_fixes` from the repo root.
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
- [x] **Near-zero debit produces absurd quantity (halt incident 2026-06-22)** — sizer divided `max_loss_usd / net_debit` with `net_debit=0.0091`, yielding 22,062 contracts. Added `MIN_NET_DEBIT = 0.10` to `config.py`; `strategy/sizer.py` now rejects candidates below this floor. Added `MAX_QTY = 100.0` as a hard cap so any debit that slips past the floor still cannot produce a runaway position size. Tests: `TestSizerSafetyGuards` in `tests/test_scanner.py`. Scratch: `scratch/scratch_sizer_fixes.py`.
- [x] **Entry premium gate (2026-06-23 churn diagnosis)** — trade_id=5 entered at $60.60/unit when market spread mid was ~$40.25 (51% premium over fair value), stopped out in 31 minutes. Two new config params: `MAX_LEG_SPREAD_PCT = 0.05` (tightened from 0.15) and `MAX_ENTRY_PREMIUM = 0.10`. `_check_liquidity_gate` in `strategy/decision.py` blocks any candidate where either leg's bid/ask spread exceeds 5% of mid, or where net_debit exceeds spread_mid by more than 10%. Tests: `TestLiquidityGate` in `tests/test_decision.py`. Scratch: `scratch/scratch_entry_gate.py`.
- [x] **Inverted market spread triggers phantom $52M loss (halt incident 2026-06-22)** — when `near_mid > far_mid` (stale/thin data), `_get_market_spread_value` returned a negative value; multiplied by the absurd 22k-contract qty this produced a ~$52M phantom loss that breached the daily loss limit. Fixed by clamping `max(0.0, far_mid - near_mid)` before multiplying — a calendar spread value cannot be negative. Tests: `TestNegativeSpreadValueClamped` in `tests/test_decision.py`. Scratch: `scratch/scratch_sizer_fixes.py`.
