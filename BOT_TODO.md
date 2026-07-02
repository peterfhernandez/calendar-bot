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

- [x] Add `1` to `NEAR_DAYS_OPTIONS` in `config.py`
- [x] Update `strategy/scanner.py` to enforce valid near/far pairs: near < far (prevents 1d/1d or 7d/7d)
- [x] Confirm scanner correctly pairs 1d near with 7d and 14d far legs only (not 30d+ which would be unusual)
- [x] Update scanner unit tests to cover 1d near-leg pairs (6 new tests in `TestOneDayNearLeg`)

---

## Phase 8f — Notification Wiring

- [x] Wire notifier calls into `strategy/decision.py`
  - [x] `notify_entry(trade)` after successful fill in `scan_tick`
  - [x] `notify_stop(trade, pnl)` when stop-loss triggers in `monitor_tick`
  - [x] `notify_take_profit(trade, pnl)` when take-profit triggers in `monitor_tick`
  - [x] `notify_roll(trade)` when near leg is rolled in `monitor_tick`
  - [x] `notify_close(trade, pnl)` when position closes at expiry in `monitor_tick`
  - [x] `notify_daily_limit(daily_pnl)` when daily loss limit is breached
  - [x] `notify_error(exc)` in exception handlers in `bot.py` and `monitor/loop.py`
  - [x] `notify_warning(msg)` when individual-leg fallback is used (method added to Notifier; wired at call sites)
- [x] Add startup self-test in `bot.py`: send "Bot started" notification on launch; log warning if it fails but do not abort
- [x] Verify all alert config keys present in `config.py`: `ALERT_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- [x] Add `scratch/scratch_notify_live.py` — sends a real test alert via configured SMTP and Telegram; confirms delivery end-to-end

---

## Phase 8g — Per-Asset Threshold Overrides

- [x] Add `ASSET_OVERRIDES` dict to `config.py` with SOL-specific relaxed thresholds
  - [x] `MIN_OI_NEAR: 10`, `MIN_OI_FAR: 10` (global: 100) — SOL has lower open interest
  - [x] `MAX_LEG_SPREAD_PCT: 0.20` (global: 0.05) — SOL market makers quote wider spreads
  - [x] `MAX_ENTRY_PREMIUM: 0.20` (global: 0.10) — wider legs naturally push entry cost above mid
  - [x] `MIN_IV_CONTANGO: 0.01` (global: 0.02) — SOL IV term structure is less stable
- [x] Add `asset_config(asset, key)` helper to `config.py`: priority = explicit call arg > `ASSET_OVERRIDES` > global default
- [x] Update `strategy/scanner.py` — resolve OI, contango, and pop thresholds per-asset inside the scan loop
- [x] Update `strategy/decision.py` `_check_liquidity_gate()` — use `asset_config()` for `MAX_LEG_SPREAD_PCT` and `MAX_ENTRY_PREMIUM`
- [x] Add `TestAssetOverrides` (5 tests) to `tests/test_scanner.py`
- [x] Add `TestAssetOverridesLiquidityGate` (5 tests) to `tests/test_decision.py`
- [x] Add `scratch/scratch_asset_overrides.py` — shows effective thresholds per asset and demonstrates SOL passing filters that BTC/ETH fail

---

## Phase 8h — Separate Data-Collector Asset List

- [x] Add `COLLECTOR_ASSETS` to `config.py` as an explicit variable independent of `ASSETS`
  - [x] `ASSETS` controls what the **bot** scans, enters, and manages positions for (default: `["BTC", "ETH"]`)
  - [x] `COLLECTOR_ASSETS` controls what the **data collector** gathers snapshots for (default: `["BTC", "ETH", "SOL"]`)
  - [x] `COLLECTOR_ASSETS` can be a superset of `ASSETS` so data is collected for assets not yet traded
- [x] Update `backtest/data_collector.py` to read `config.COLLECTOR_ASSETS` directly (remove `getattr` fallback)
- [x] Update `collect.py` module docstring and `--assets` help text to reference `COLLECTOR_ASSETS`
- [x] Update `README.md` key parameters table to document both `ASSETS` and `COLLECTOR_ASSETS`
- [x] Update `BOT_PLAN.md` configuration block to include `COLLECTOR_ASSETS`

---

## Phase 8k — Trading Fee Integration

Deribit charges **0.03% of the underlying index price** per leg per trade (min 0.0003 BTC/ETH/SOL per contract). Combo/spread orders receive a 100% taker discount on the cheaper leg. Delivery fees apply at expiry for non-daily/non-weekly options (0.015% of underlying, capped at 12.5% of option value). SOL maker fees are 0%. Fees must be modelled accurately in all three trading modes (paper, test, live).

- [x] Add fee constants to `config.py`
  - [x] `OPTIONS_FEE_PCT = 0.0003` — taker/maker rate per leg (BTC and ETH options)
  - [x] `OPTIONS_MIN_FEE_BTC = 0.0003` — minimum fee in BTC per contract
  - [x] `OPTIONS_MIN_FEE_ETH = 0.0003` — minimum fee in ETH per contract
  - [x] `OPTIONS_MIN_FEE_SOL = 0.0003` — minimum fee in SOL per contract (taker); maker = 0
  - [x] `SOL_MAKER_FEE_PCT = 0.0` — SOL options maker fee is zero
  - [x] `OPTIONS_DELIVERY_FEE_PCT = 0.00015` — delivery fee for monthly/quarterly options at expiry
  - [x] `OPTIONS_DELIVERY_FEE_CAP = 0.125` — delivery fee capped at 12.5% of option market value
  - [x] `COMBO_CHEAP_LEG_DISCOUNT = 1.0` — 100% taker fee discount on cheaper leg of a combo order
  - [x] No delivery fee for daily options (1d near) or weekly options (7d near)

- [x] Update `core/fees.py` with actual Deribit fee schedule
  - [x] `leg_fee(asset, spot, qty, is_maker, option_price)` — returns fee in USD; applies per-leg rate, minimum fee floor, and 12.5% cap
  - [x] `entry_fees(asset, spot, qty, near_price, far_price, via_combo)` — total entry cost; applies combo cheap-leg discount when `via_combo=True`
  - [x] `exit_fees(asset, spot, qty, near_price, far_price)` — total exit cost for closing both legs
  - [x] `roll_fees(asset, spot, qty, near_price, new_near_price)` — fee to close old near + open new near leg
  - [x] `delivery_fee(asset, spot, qty, option_price, expiry_days)` — 0 for daily/weekly, 0.015% otherwise (capped)
  - [x] `round_trip_fees(asset, spot, qty, near_price, far_price, via_combo)` — entry + exit fees combined; used in EV calculation

- [x] Update `strategy/scanner.py` to deduct round-trip fees from EV score
  - [x] Compute `fee_drag = round_trip_fees(asset, spot, qty=1, near_price, far_price, via_combo=True)` per candidate
  - [x] Adjust `ev_score` to `(ev - fee_drag) / net_debit` before storing on candidate
  - [x] Log fee drag alongside EV score when a candidate is evaluated

- [x] Update `strategy/sizer.py` to include fees in max-loss calculation
  - [x] True cost per trade = `net_debit × qty + entry_fees + exit_fees` via `effective_cost_per_unit`
  - [x] Size so that true cost ≤ `available_cash × MAX_LOSS_PCT`
  - [x] Expose `estimated_fees` on the `SizeResult` return value

- [x] Update `strategy/decision.py` for fee-aware gating and P&L
  - [x] Roll decision: compute `roll_fees`; only proceed with roll if `expected_theta_gain > roll_fees`; close instead if uneconomic
  - [x] Stop/TP/expiry close: compute `exit_fees` and log fee-inclusive net P&L; record `close_fees` in DB
  - [x] `fees_paid_today` property accumulates entry + exit + roll fees across the session

- [x] Update `execution/executor.py` paper dry-run path to simulate fees
  - [x] Return `fees_paid` (from `entry_fees()`) alongside the fill result so callers can track cumulative costs
  - [x] Fee simulation uses the same `fees.py` functions as test/live

- [x] Update `monitor/loop.py` to report accumulated fees
  - [x] Add `fees_paid_today` to the per-cycle log line alongside `unrealized_pnl` and `daily_pnl`

- [x] Update `portfolio/tracker.py` to track fee totals
  - [x] Add `fees_paid_today` and `fees_paid_total` fields (sourced from closed-trade records in SQLite)
  - [x] Include both in the `portfolio_view()` snapshot

- [x] Update `backtest/engine.py` to apply fees to every simulated action
  - [x] `total_fees` field added to `BacktestResult` (sum of `open_fees + close_fees` across all trades)
  - [x] `print_summary()` includes `fees=` column in per-regime output

- [x] Write/update unit tests (400 passing)
  - [x] `tests/test_fees.py` — 44 tests: per-leg fee, combo discount, delivery fee, SOL zero-maker, fee cap, round-trip total, legacy API
  - [x] `tests/test_scanner.py` — `TestFeeAdjustedEV` (3 tests): fee drag reduces ev_score; `estimated_fees` returned by sizer
  - [x] `tests/test_decision.py` — `TestFeeIntegration` (6 tests): fees_paid_today tracking; roll fee gate mocked; close_fees recorded in DB
  - [x] `tests/test_executor.py` — `TestPaperFeeSimulation` (3 tests): paper fill returns `fees_paid`; amounts match `fees.py`

- [x] Add `scratch/scratch_fees.py` — demonstrates fee calculation across all scenarios
  - [x] Entry fees (with and without combo discount) for BTC, ETH, SOL
  - [x] Expiry: near leg OTM (no delivery fee), near leg ITM with daily option (no delivery fee), near leg ITM with monthly option (delivery fee applies)
  - [x] Roll fees vs theta gain comparison — shows break-even roll threshold
  - [x] Early close (stop-loss / take-profit) — gross vs net P&L after fees
  - [x] Aborts if `TRADING_MODE == "live"`

---

## Phase 9a — Telegram Command Listener

Adds incoming command handling so the operator can query and control the bot from their phone via the same Telegram chat already used for outgoing notifications.

### `bot.py` — CLI and startup

- [x] Add `--drain` argparse flag to `bot.py`
  - [x] When present, sets `config.DRAIN_MODE = True` before the event loop starts
  - [x] Provides a command-line alternative to setting the `DRAIN_MODE` env var (e.g. `python bot.py --drain`)
- [x] Import and instantiate `TelegramCommandListener` in `bot.py`
  - [x] Skip silently if `config.TELEGRAM_TOKEN` is not set
  - [x] Add `asyncio.create_task(listener.start(), name="telegram_cmd")` to the tasks list
  - [x] Call `await listener.stop()` in the `finally:` block before cancelling remaining tasks

### `strategy/decision.py` — pause/resume

- [x] Add `paused: bool` flag to `DecisionEngine.__init__` (default `False`)
- [x] Add `pause()` method — sets the flag; logs a warning that scanning and monitoring are paused
- [x] Add `resume()` method — clears the flag; logs info that normal operation has resumed
- [x] `scan_tick()` — return immediately (no-op) when `paused` is `True`
- [x] `monitor_tick()` — return immediately (no-op) when `paused` is `True`
- [x] Add `TestPauseResume` (4 tests) to `tests/test_decision.py`: pause blocks scan; pause blocks monitor; resume re-enables scan; resume re-enables monitor

### `telegram_cmd/` package

- [x] Create `telegram_cmd/__init__.py`
- [x] Create `telegram_cmd/listener.py` — `TelegramCommandListener`
  - [x] Accepts `engine: DecisionEngine`, `cache: ChainCache`, and `db` reference in constructor
  - [x] Builds a `python-telegram-bot` Application using `config.TELEGRAM_TOKEN`
  - [x] Security middleware: drop (no reply) any update whose `effective_chat.id` does not match `int(config.TELEGRAM_CHAT)`
  - [x] Registers all command handlers from `handlers.py`
  - [x] `async def start()` — initialises application and starts long-polling loop
  - [x] `async def stop()` — cleanly shuts down the polling loop
- [x] Create `telegram_cmd/handlers.py`
  - [x] `handle_positions(update, context)` — query `db/state.py` for open trades; fetch current bid/ask mid from cache for each leg; compute unrealized PnL = `(far_mid − near_mid) × qty − net_debit × qty`; format one line per trade; fall back to DB entry value with a note if the cache is stale or the instrument is missing
  - [x] `handle_closed_today(update, context)` — query DB for trades with `closed_at >= midnight UTC`; reply "N trades closed today. Total realized PnL: $X.XX"
  - [x] `handle_new_today(update, context)` — query DB for trades with `opened_at >= midnight UTC`; reply "N new positions opened today: \<instrument pairs\>"
  - [x] `handle_status(update, context)` — reply with: trading mode, drain mode on/off, bot paused/running, uptime since process start, open position count, daily PnL
  - [x] `handle_portfolio(update, context)` — query DB for open trades; fetch live IV and OI for both legs from the chain cache; reply with one line per trade showing: asset, strike, near expiry date, far expiry date, net debit paid, fees paid, EV score at entry, near IV, far IV, near OI, far OI; note "(cache stale)" next to any leg whose IV or OI cannot be retrieved
  - [x] `handle_stop_bot(update, context)` — call `engine.pause()`; reply "Bot paused — monitoring stopped. Use /start_bot to resume."
  - [x] `handle_start_bot(update, context)` — call `engine.resume()`; reply "Bot resumed — scanning and monitoring restarted."
  - [x] `handle_start_drain(update, context)` — set `config.DRAIN_MODE = True`; call `engine.resume()` if paused; reply "Drain mode activated — no new entries or rolls; existing positions will close at stop/TP/expiry."

### Tests and scratch

- [x] Create `tests/test_telegram_cmd.py`
  - [x] Mock `db.state` to return known trade sets; verify `/positions`, `/closed_today`, `/new_today`, `/portfolio` format replies correctly
  - [x] Verify `/status` reply includes mode, drain, paused state, and position count
  - [x] Verify unknown chat ID produces no reply (security check)
  - [x] Verify `/stop_bot` calls `engine.pause()` and `/start_bot` calls `engine.resume()`
  - [x] Verify `/start_drain` sets `config.DRAIN_MODE = True`
- [x] Create `scratch/scratch_telegram_cmd.py`
  - [x] Starts `TelegramCommandListener` with a real token; prints each received command and its reply
  - [x] Aborts if `TRADING_MODE == "live"`
  - [x] Run with `python -m scratch.scratch_telegram_cmd` from the repo root

---

## Phase 9b — Telegram Command Menu and /help

Telegram can display a suggestion menu (the list of commands that pops up when you type `/` in the chat) and a `/help` command that prints all available commands with descriptions. Both are driven by the same command registry so the menu and the help text are always in sync.

### How the Telegram command menu works

When a user types `/` in a chat with a bot, Telegram shows a suggestion list only if the bot has registered its commands via the `setMyCommands` Bot API method. This registration is stored on Telegram's servers and persists across bot restarts. `python-telegram-bot` exposes this as `await application.bot.set_my_commands(commands)`.

The best approach is to call `set_my_commands()` once at startup inside `listener.py` so the menu is always in sync with whatever commands are registered — no manual BotFather steps required (though BotFather's `/setcommands` would also work as a one-time manual alternative).

### Changes to `telegram_cmd/listener.py`

- [x] Define a module-level `COMMAND_REGISTRY` list of `(command, description)` tuples covering all implemented commands
- [x] In `TelegramCommandListener.start()`, call `await self._app.bot.set_my_commands(COMMAND_REGISTRY)` after initialising the application — this pushes the command list to Telegram's servers so the `/` menu is populated
- [x] Register `/help` handler alongside the other command handlers

### Changes to `telegram_cmd/handlers.py`

- [x] `handle_help(update, context)` — iterates `COMMAND_REGISTRY` and replies with a formatted list: `/<command> — <description>` on each line

### Tests (continued)

- [x] Add to `tests/test_telegram_cmd.py`
  - [x] Verify `handle_help` reply contains every command in `COMMAND_REGISTRY`
  - [x] Verify `set_my_commands` is called during `start()` with the full registry (mock the bot)

---

## Phase 9c — Telegram Command Improvements

Updated existing commands and added new runtime-control commands.

### Updated commands

- [x] `/portfolio` — simplified output: removed IV and OI; added EV at entry and current spread value
- [x] `/positions` — expiry range format `ddMMMYY→ddMMMYY`; full option type (`Put`/`Call` not `P`/`C`); `ev=` at start of each line
- [x] `/new_trades` (renamed from `/new_today`) — AEST midnight; per-trade details: trade id, asset, debit, ev, strike, expiry range
- [x] `/close_trades` (renamed from `/closed_today`) — AEST midnight; per-trade details: trade id, asset, debit, pnl, close reason
- [x] `/status` — PnL computed from AEST midnight (from DB, not accumulated counter); added session PnL line (since bot start)

### New commands

- [x] `/start_with_assets Asset1,Asset2,...` — override `config.ASSETS` at runtime and resume the bot
- [x] `/drain_and_new portfolio=N assets=A,B` — close existing positions (no rolls) while still allowing new entries; optional portfolio value override and asset list override

### Supporting changes

- [x] Add `ev_score` column to `calendar_trades` table (`db/state.py`) with backward-compatible migration
- [x] Store `ev_score` from `CalendarCandidate` when creating trade records (`strategy/decision.py`)
- [x] Add `get_trades_opened_today_aest` / `get_trades_closed_today_aest` helpers (`db/state.py`)
- [x] Add `DRAIN_AND_NEW_MODE: bool` and `PORTFOLIO_OVERRIDE: float | None` to `config.py`
- [x] Add `_session_pnl` accumulator and `session_pnl` property to `DecisionEngine` (`strategy/decision.py`)
- [x] `DRAIN_AND_NEW_MODE` causes scan_tick to run (new entries allowed) but monitor closes instead of rolling
- [x] `PORTFOLIO_OVERRIDE` bypasses live portfolio tracker for sizing decisions when set
- [x] Update `COMMAND_REGISTRY` in `telegram_cmd/listener.py` — now the single source of truth for all 11 commands
- [x] Update `tests/test_telegram_cmd.py` — tests rewritten for all renamed/new handlers
- [x] Add `get_trades_opened_since` / `get_trades_closed_since` helpers for session mode (`db/state.py`)
- [x] `/positions` reformatted to single line with `ev=` at the end; `ev=N/A` for pre-tracking trades (ev_score == 0.0)
- [x] Telegram shutdown `ConnectTimeout` fixed — `ApplicationBuilder` configured with `get_updates_connect_timeout=5.0` and `get_updates_read_timeout=5.0`

---

## Phase 9d — Log Hygiene (Telegram Noise and Secret Redaction)

- [x] Suppress high-frequency `httpx` / `httpcore` / `telegram` INFO logs that flood the log with a `getUpdates` line every few seconds
  - [x] In `configure_logging()` (`monitor/loop.py`), set `httpx`, `httpcore`, `telegram.ext.Updater`, and `telegram.vendor.ptb_urllib3` loggers to `WARNING` — errors and warnings still appear, but polling calls are silenced
- [x] Add `_SecretRedactor` log filter (`monitor/loop.py`)
  - [x] Reads `config.TELEGRAM_TOKEN` and `config.TELEGRAM_CHAT` once at startup
  - [x] Applied to the root logger so it covers both the console and rotating-file handlers
  - [x] Replaces any occurrence of the literal token or chat ID in a log record with `<redacted>` before the record reaches a handler
  - [x] Never raises — if config import fails the filter is skipped silently so logging setup cannot crash the bot

---

## Phase 15 — Stale Cache Fallback for Telegram Positions

When the `/positions` Telegram command is executed, the cache may show "stale" data if more than 30 seconds have passed since the last WebSocket update for a particular strike (common during low-volume periods). Instead of showing `sv=N/A (stale cache)`, the bot now stores the last known spread value from the previous monitor tick and displays it as a fallback.

### Implementation

- [x] Add `last_spread_value REAL NOT NULL DEFAULT 0.0` column to `calendar_trades` table
  - [x] New field in `CalendarTrade` dataclass (`db/state.py`)
  - [x] Database schema update with backward-compatible migration
  - [x] New `update_last_spread_value(trade_id, spread_value)` function in `db/state.py`

- [x] Update `_monitor_position()` to store the spread value after each calculation
  - [x] Call `update_last_spread_value()` after computing spread status in `strategy/decision.py`
  - [x] Logged as DEBUG if update fails (non-blocking)

- [x] Update `/positions` handler to use last_spread_value as fallback
  - [x] When cache is stale (`near_snap` or `far_snap` is None), check `t.last_spread_value`
  - [x] If last_spread_value > 0, display it with an asterisk suffix `sv=$X.XX*` to indicate cached value
  - [x] Only fall back to `sv=N/A (stale cache)` if both cache and last_spread_value are unavailable

- [x] Test coverage
  - [x] Updated `_make_trade()` test helper with `last_spread_value` parameter
  - [x] All 474 tests passing

### Result

Telegram `/positions` output now shows a reasonable spread value even during cache staleness periods, improving visibility and reducing "N/A" noise.

---

## Phase 16 — `/pnl` Equity Curve Chart

New Telegram command that renders the bot's full trading history as an equity-curve image: cumulative realized P&L (all-time closed trades) as a black line, with current unrealized P&L from open positions appended as a dotted green line, delivered to the chat as a PNG.

### Design

- x-axis: `date_close` (chronological); y-axis: cumulative net P&L in USD
- Black solid line: running sum of `pnl` over all closed trades ordered by `date_close`. `pnl` is already net of fees (Phase 13) and includes roll P&L (Phase 14), so no extra adjustment is needed — plot the raw column.
- Dotted green line: a single segment from the last realized point `(last date_close, cumulative_realized)` to `(now, cumulative_realized + total_unrealized)`, where `total_unrealized` is the sum of `(spread_val − net_debit×qty − open_fees) + roll_pnl` across all open trades (same formula as `handle_portfolio`), using live mid prices from `ChainCache` with a `last_spread_value` fallback (same pattern as `/positions`, Phase 15)
- Chart legend/caption states the open position count (e.g. "3 open trades") next to the unrealized line
- Rendered with `matplotlib` using the non-interactive `Agg` backend (no display server on the bot host) and returned as an in-memory PNG (`io.BytesIO`), never written to disk in the live path
- Empty states handled explicitly: no closed trades yet → chart shows a flat zero line with a caption note instead of erroring; no open trades → dotted segment and "open trades" note are omitted

### `db/state.py`

- [ ] Add `get_all_closed_trades(db_path: Path = DB_PATH) -> list[CalendarTrade]` — all rows with `date_close IS NOT NULL`, ordered by `date_close ASC, id ASC`

### `telegram_cmd/pnl_chart.py` (new module)

- [ ] `matplotlib.use("Agg")` set at import time, before `pyplot` is imported
- [ ] `build_cumulative_series(closed_trades) -> list[tuple[datetime, float]]` — pure function, running sum of `t.pnl` ordered by `date_close`
- [ ] `compute_unrealized(open_trades, cache: ChainCache) -> tuple[float, int]` — returns `(total_unrealized_pnl, open_count)`; reuses the mid-price / `last_spread_value` fallback logic already in `telegram_cmd/handlers.py` (factor the shared bits out to a helper rather than duplicating, e.g. move `_mid()` and the stale-cache fallback into a shared `telegram_cmd/_pnl_common.py` or keep the calc in `handlers.py` and import it)
- [ ] `render_pnl_chart(closed_trades, open_trades, cache) -> io.BytesIO` — builds the black realized-PnL line, appends the dotted green unrealized segment when `open_count > 0`, draws a y=0 reference line, formats the y-axis as `$X`, rotates/thins x-axis date labels for readability with many trades, returns a seeked-to-0 `BytesIO` containing PNG bytes

### `telegram_cmd/handlers.py`

- [ ] `handle_pnl(update, context, cache, db_path)` — fetches `get_all_closed_trades()` and `get_open_trades()`, calls `render_pnl_chart()`, sends via `update.message.reply_photo(photo=buf, caption=...)`; caption includes total realized PnL, total unrealized PnL, open trade count, and the combined total
- [ ] Reply with a plain text message instead of a photo when there is no history at all (no closed trades and no open trades)

### `telegram_cmd/listener.py`

- [ ] Add `("pnl", "Equity curve: realized PnL (black) + unrealized PnL (dotted green), N open trades")` to `COMMAND_REGISTRY`
- [ ] Wrap and register `cmd_pnl` the same way as `cmd_positions` (needs `cache` injected, no `engine` dependency)

### `requirements.txt`

- [x] Add `matplotlib>=3.8`

### Tests and scratch

- [ ] `tests/test_state.py` — `TestGetAllClosedTrades`: returns only closed trades, ordered by `date_close`, empty list when none closed
- [ ] `tests/test_telegram_cmd.py` — `TestHandlePnl`: mocks `reply_photo`; verifies it is called with PNG bytes (`b"\x89PNG"` header) when history exists; verifies caption contains realized/unrealized/open-count figures; verifies the no-history case falls back to `reply_text`; verifies the dotted unrealized segment is omitted when there are no open positions
- [ ] Unit tests for `build_cumulative_series` and `compute_unrealized` directly (no rendering involved) covering: single trade, multiple same-day closes, mixed win/loss sequence, zero open positions, stale-cache fallback
- [ ] `scratch/scratch_pnl_chart.py` — loads real (or, if empty, synthetic) closed trades from the paper DB, renders the chart, and saves it to `scratch/pnl_chart_preview.png` for visual inspection. Aborts if `TRADING_MODE == "live"` (per project convention: scratch files never run against live trading). Run with `python -m scratch.scratch_pnl_chart` from the repo root.

---

## Phase 17 — Cross Portfolio Margin (X:PM) Entry Gate

New entry gate that rejects a candidate if adding it to the current portfolio would push the account's margin utilization to a level that risks a margin call, using Deribit's actual Cross Portfolio Margin (X:PM) numbers rather than a local approximation of debit-at-risk.

### Why this is different from the existing sizing/risk checks

`strategy/sizer.py` already caps position size against `MAX_LOSS_PCT` and `MAX_TOTAL_RISK_PCT` of portfolio value, using `net_debit × qty` (capital paid) as the risk measure. That protects capital-at-risk for a bounded-loss long calendar spread held to expiry, but it is **not the same number Deribit uses to decide whether to liquidate the account**. Under Cross Portfolio Margin, Deribit stress-tests the *entire* portfolio — all currencies, all instruments — across a grid of underlying price-move buckets (±16% by default, 9 buckets) crossed with volatility-up/same/down scenarios, plus an extended tail table for far-OTM short exposure, a delta shock, and a roll shock; the worst simulated scenario becomes the Initial Margin, and Maintenance Margin is a fraction of that (default factor 0.80). This is a materially different, and generally larger and more volatile, number than the sum of position debits — it is also driven by the *whole* portfolio's composition (e.g. correlated strikes/expiries across BTC and ETH are netted together), not the candidate in isolation. Two margin-call/liquidation incidents are already logged in this file (2026-06-22 and 2026-07-01 — see Bug Fixes) despite `MAX_TOTAL_RISK_PCT` being respected in both cases, which is exactly the gap this gate closes.

### Why the model is not reimplemented locally

Deribit publishes the X:PM model in detail (see `support.deribit.com/hc/en-us/articles/25944756247837-Portfolio-Margin` and the PME whitepaper at `statics.deribit.com/files/DeribitPortfolioMarginModel.pdf`), but it is a large exchange-side risk engine: per-currency-pair price range buckets, volatility shock curves, an extended table with per-bucket dampeners and margin multipliers, delta shock and roll shock formulas that depend on live thresholds, and cross-currency haircuts for non-core collateral. All of these parameters are queryable live via `public/pme/get_params` and can change without notice — a local reimplementation would drift from the exchange's real number and give false confidence. This gate instead asks Deribit for the real, current margin figures rather than re-deriving them.

### Design

**Primary check — ask Deribit for the real number.** Before entering, call a Deribit margin-simulation endpoint with the candidate's near and far leg orders to get the account's *projected* initial/maintenance margin if the trade were placed. The exact endpoint/response shape must be confirmed against the live API at implementation time (`docs.deribit.com/api-reference` is a JS-rendered app that could not be scraped during this analysis session) — likely `private/get_margins` or equivalent, taking `instrument_name`/`amount`/`price` per leg. **First implementation task: a scratch script against `test.deribit.com` that calls the candidate endpoint(s) and prints the raw response**, so the real schema is confirmed before the gate is built against it.

**Fallback check — conservative local proxy**, used whenever the simulation call fails, is unavailable, or credentials are absent:

1. Reject immediately if the account's *current* `maintenance_margin / equity` (from `get_account_summary`, already fetched by `PortfolioTracker`) exceeds `MAX_MARGIN_UTILIZATION_PCT` — don't add risk to an account that is already stressed.
2. Otherwise, approximate the *projected* utilization as `(current_maintenance_margin + candidate_net_debit × qty) / equity` — the candidate's own max loss is a floor on its margin contribution — and reject if that projected ratio exceeds `MAX_MARGIN_UTILIZATION_PCT`. This is a conservative floor, not the true PM number (PM can give correlation offsets that lower the real requirement, or extended-table/delta-shock effects that raise it) — it is a safety backstop, not a replacement for the primary check.

`MAX_MARGIN_UTILIZATION_PCT` defaults to `0.80`, matching Deribit's own default Maintenance Margin Factor, so the bot self-limits before reaching Deribit's liquidation threshold rather than at it.

**Live-vs-paper behaviour:** in `test`/`live` mode with no usable margin data (API error, no credentials), the gate **fails closed** — reject the candidate and log a warning — since this gate exists specifically to prevent real liquidations. In `paper` mode (dry-run, no real account/margin), the gate is a no-op by default so paper trading is not blocked by the absence of a funded test account; `MARGIN_GATE_ENABLED` can force it on in paper mode for testing.

### Prerequisite: `PortfolioTracker` is not currently wired into the running bot

`strategy/decision.py`'s `DecisionEngine` already accepts an optional `portfolio: PortfolioTracker` and calls `portfolio.refresh()` at the top of `scan_tick()` when one is supplied (Phase 8b), and `monitor/loop.py`'s `BotLoop` already forwards a `portfolio` argument through — but `bot.py`'s `_run()` never actually constructs a `PortfolioTracker` or passes `portfolio=` to `BotLoop(...)`. This gate needs live margin data, so wiring `PortfolioTracker` into `bot.py` is a prerequisite, not optional.

### `portfolio/tracker.py`

- [ ] Capture `maintenance_margin` per currency in `_refresh_from_api()` alongside the existing `initial_margin` fetch (same `get_account_summary` response, no extra API call)
- [ ] Add `maintenance_margin_usd` field to `PortfolioState` and a corresponding property on `PortfolioTracker`
- [ ] Add `margin_utilization_pct` property — `maintenance_margin_usd / equity_usd`, or `0.0` when equity is zero/unavailable
- [ ] Add `simulate_margin(legs: list[tuple[str, float, float]]) -> MarginImpact | None` — calls the Deribit margin-simulation endpoint (schema confirmed by the scratch script below) for the candidate's near/far leg `(instrument_name, amount, price)`; returns `None` on any failure so callers fall back to the local proxy rather than raising
- [ ] `MarginImpact` dataclass: `projected_initial_margin_usd`, `projected_maintenance_margin_usd`

### `bot.py`

- [ ] Instantiate `PortfolioTracker` in `_run()` (using `config.DERIBIT_CLIENT_ID`/`SECRET`) and pass `portfolio=tracker` to `BotLoop(...)` — closes the wiring gap above
- [ ] Startup log line includes current margin utilization when a tracker is attached

### `config.py`

- [ ] `MAX_MARGIN_UTILIZATION_PCT = 0.80` — ceiling on `maintenance_margin / equity`, current and projected-after-entry
- [ ] `MARGIN_GATE_ENABLED = True` — kill switch; when `False` the gate always passes (escape hatch if the simulation endpoint proves unreliable)
- [ ] `MARGIN_GATE_REQUIRED_LIVE = True` — in `test`/`live` mode, missing/failed margin data blocks entry (fail closed); paper mode does not require it

### `strategy/decision.py`

- [ ] `_check_margin_gate(candidate: CalendarCandidate) -> str | None` — mirrors `_check_liquidity_gate`'s signature and style (returns a rejection reason string or `None`); tries `self._portfolio.simulate_margin(...)` first, falls back to the local proxy formula, applies the fail-open/fail-closed rule based on `config.TRADING_MODE`
- [ ] Call `_check_margin_gate()` in `scan_tick()`'s RANK loop immediately after the existing `_check_liquidity_gate()` call — same skip-and-continue pattern (one rejected candidate does not stop the scan)
- [ ] Call `_check_margin_gate()` in `_try_roll()` alongside the existing `_check_liquidity_gate()` reuse — a roll changes portfolio composition and can trigger a margin call on its own (this is exactly how the 2026-07-01 incident occurred)
- [ ] Log rejections at INFO (not DEBUG, unlike the liquidity gate) — a margin-gate rejection is a higher-signal event worth surfacing without needing debug logging enabled

### Tests and scratch

- [ ] `scratch/scratch_margin_probe.py` — **run first, before writing the gate.** Connects to `test.deribit.com` with paper/test credentials, calls the candidate margin-simulation endpoint for a small hypothetical order, and prints the raw JSON response so the real schema can be confirmed and `simulate_margin()` built against it. Aborts if `TRADING_MODE == "live"`.
- [ ] `tests/test_portfolio.py` — `TestMaintenanceMargin` (capture, property, `margin_utilization_pct` calculation, zero-equity edge case) and `TestSimulateMargin` (mocked REST success, mocked REST failure returns `None`, credentials absent returns `None`)
- [ ] `tests/test_decision.py` — `TestMarginGate`: candidate approved when utilization is low; candidate rejected when current utilization already exceeds `MAX_MARGIN_UTILIZATION_PCT`; candidate rejected when projected utilization (via proxy) exceeds the ceiling; `simulate_margin` result takes precedence over the proxy when available; fails open in paper mode with no tracker; fails closed in test/live mode with no tracker or a failed API call; `MARGIN_GATE_ENABLED = False` always passes; roll path also invokes the gate
- [ ] `scratch/scratch_margin_gate.py` — end-to-end demonstration against the paper/test account: prints current margin utilization, then runs a few synthetic candidates through `_check_margin_gate()` showing approve/reject outcomes and reasons. Aborts if `TRADING_MODE == "live"`. Run with `python -m scratch.scratch_margin_gate` from the repo root.

---

## Validation Phases

### Validation Phase 1 — Paper Trading Validation

- [ ] Run bot in paper mode (`DERIBIT_PAPER = True`) for minimum 4 weeks
- [ ] Verify scanner selects setups that profit at expiry
- [ ] Verify stop-loss and take-profit triggers fire correctly
- [ ] Verify roll logic outcomes vs outright close
- [ ] Verify daily loss limit halts the bot
- [ ] Review all logs; tune `config.py` parameters as needed

---

### Validation Phase 2 — Live Deployment

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
- `scratch/scratch_notify_live.py` — sends real test alerts via the configured SMTP and Telegram channels. Requires ALERT_EMAIL/SMTP_USER/SMTP_PASS and/or TELEGRAM_TOKEN/TELEGRAM_CHAT set in .env. Aborts if TRADING_MODE is "live". Run with `python -m scratch.scratch_notify_live` from the repo root.
- `scratch/scratch_asset_overrides.py` — demonstrates per-asset threshold overrides: prints effective thresholds for BTC, ETH, and SOL side by side, then shows SOL candidates passing OI, spread, and entry-premium filters that BTC/ETH fail. Run with `python -m scratch.scratch_asset_overrides` from the repo root.
- `scratch/scratch_fees.py` — demonstrates the Deribit fee model: entry fees with/without combo discount for BTC/ETH/SOL, delivery fees (daily/weekly exempt, monthly charged), roll fee vs theta gain break-even, and early-close gross vs net P&L. Aborts if TRADING_MODE is "live". Run with `python -m scratch.scratch_fees` from the repo root.
- `scratch/scratch_telegram_cmd.py` — starts `TelegramCommandListener` with a real token and prints each received command and its reply. Aborts if `TRADING_MODE == "live"`. Run with `python -m scratch.scratch_telegram_cmd` from the repo root.
- `scratch/scratch_pnl_chart.py` — renders the `/pnl` equity-curve chart from the paper DB's real (or synthetic, if empty) trade history and saves it to `scratch/pnl_chart_preview.png`. Aborts if `TRADING_MODE == "live"`. Run with `python -m scratch.scratch_pnl_chart` from the repo root.
- `scratch/scratch_margin_probe.py` — probes the real Deribit margin-simulation endpoint against test.deribit.com and prints the raw response, used to confirm the API schema before `PortfolioTracker.simulate_margin()` is implemented. Aborts if `TRADING_MODE == "live"`. Run with `python -m scratch.scratch_margin_probe` from the repo root.
- `scratch/scratch_margin_gate.py` — prints current account margin utilization and runs synthetic candidates through `_check_margin_gate()` to demonstrate approve/reject outcomes. Aborts if `TRADING_MODE == "live"`. Run with `python -m scratch.scratch_margin_gate` from the repo root.
- Do not switch to live trading until Phase 9 is fully complete

---

## Phase 10 — Offline Error Tracking

- [x] Update `data/deribit_feed.py` — suppress repeated reconnect warnings
  - [x] Add `_offline: bool` and `_retry_count: int` to `DeribitFeed.__init__`
  - [x] Log `WARNING` once on first disconnect; `DEBUG` on subsequent attempts; `INFO` on reconnect with attempt count
- [x] Update `portfolio/tracker.py` — suppress repeated REST failure warnings
  - [x] Add `_api_offline: bool` and `_api_fail_count: int` to `PortfolioTracker.__init__`
  - [x] Log `WARNING` once on first failure; `DEBUG` on subsequent attempts; `INFO` on recovery with attempt count
  - [x] Per-currency `except` in `_refresh_from_api` re-raises to the outer handler (was logging once per asset per cycle)
- [x] Add `TestDeribitFeedOfflineTracking` (3 tests) to `tests/test_feed.py`
  - [x] First failure sets `_offline` flag
  - [x] Repeated failures increment `_retry_count`
  - [x] Recovery clears flag and resets counter
- [x] Add `TestOfflineTracking` (5 tests) to `tests/test_portfolio.py`
  - [x] First failure sets `_api_offline` flag
  - [x] Repeated failures increment `_api_fail_count`
  - [x] Only the first failure logs at WARNING; subsequent failures log at DEBUG
  - [x] Recovery clears offline flag and resets counter
  - [x] Recovery logs at INFO with the attempt count

---

## Phase 11 — Secret Leak Prevention in Logs

Ensures that no credentials from `.env` ever appear in log files, and provides a one-time script to scrub any existing log files that may have captured secrets before these fixes.

### Root-cause fixes (prevent generation)

- [x] Fix `portfolio/tracker.py` `_authenticate()` — switch Deribit auth from GET (credentials in URL query string) to POST (credentials in JSON body), so the URL that appears in any HTTP error message contains no secrets
  - [x] Add `_rest_post(url, payload)` helper alongside the existing `_rest_get`
  - [x] `_authenticate()` now calls `_rest_post` with `client_id` and `client_secret` in the body
- [x] Remove `logger.info("Authenticating as %s", self.client_id)` from `data/deribit_feed.py` — replaced with a no-argument `logger.debug(...)` that logs no credential values

### Belt-and-suspenders redaction

- [x] Expand `_SecretRedactor` in `monitor/loop.py` to cover all secrets from `.env`, not just the Telegram pair
  - [x] `DERIBIT_TEST_CLIENT_ID` and `DERIBIT_TEST_CLIENT_SECRET`
  - [x] `DERIBIT_LIVE_CLIENT_ID` and `DERIBIT_LIVE_CLIENT_SECRET`
  - [x] `SMTP_USER` and `SMTP_PASS`
  - [x] `TELEGRAM_TOKEN` and `TELEGRAM_CHAT` (already covered; now consolidated with the others)
  - [x] Filter still skips blank/whitespace strings so it cannot accidentally redact empty-string matches

### Test updates

- [x] Update `tests/test_portfolio.py` — auth now goes via `_rest_post`; all tests that previously relied on `_rest_get` routing `public/auth` URLs now patch `_rest_post` instead
  - [x] Add `_fake_rest_post` module-level helper returning a valid token response
  - [x] `TestAvailableCashCalculation._run_refresh_mocked` — add `patch("portfolio.tracker._rest_post")` alongside existing `_rest_get` patch; remove auth-URL branch from `fake_rest_get`
  - [x] `TestOfflineTracking` — failure tests now trigger `_rest_post` to raise; recovery tests patch both `_rest_post` and `_rest_get`
  - [x] `TestReconciliation._run_with_margin` — add `_rest_post` patch
  - [x] `TestApiRefresh.test_api_failure_leaves_cached_state_unchanged` — patch `_rest_post` to raise instead of `_rest_get`

### One-time log scrub

- [x] Add `scratch/scrub_logs.py` — reads all secrets from `.env`, then rewrites every `logs/bot.log*` file in place, replacing any occurrence of a secret with `<redacted>`
  - [x] Reads `.env` via the same key names as `config.py` (no dependency on config module — safe to run standalone)
  - [x] Processes all rotation files: `bot.log`, `bot.log.1`, … `bot.log.5`
  - [x] Reports count of replacements per file so the operator can see what was found
  - [x] Skips files that do not exist; exits cleanly if the `logs/` directory is absent
  - [x] Dry-run mode (`--dry-run`) prints what would be replaced without writing

---

## Phase 12 — Parallel Mode Isolation (--env, --db, --log)

Allows running a paper-mode and a test-mode bot instance side by side on the same machine, each with its own credentials, database, and log file.

### How it works

`bot.py` now accepts four flags that are pre-parsed before any module-level imports:

| Flag | Env var set | Default |
| --- | --- | --- |
| `--env FILE` | `BOT_ENV_FILE` | `.env` |
| `--db PATH` | `BOT_DB_PATH` | `db/calendar_bot.db` |
| `--log PATH` | `BOT_LOG_FILE` | `logs/bot.log` |
| `--config FILE` | `BOT_CONFIG_FILE` | *(none)* |

Pre-parsing (setting os.environ before importing `config` or `db.state`) ensures that module-level code (`TRADING_MODE`, `DB_PATH`, etc.) sees the right values at import time — no import-order tricks required.

The `.env.test` file includes `TRADING_MODE=test` plus any per-instance overrides. Only credentials and mode need to differ from `.env`; all other parameters are shared.

`--config` points to a plain Python override file that is `exec`'d at the end of `config.py`. It can reassign any config variable — only set what differs from the defaults (e.g. `ASSETS`, `MAX_POSITIONS`, `MAX_LOSS_PCT`).

### Usage

```bash
# Terminal 1 — paper mode (default)
python bot.py

# Terminal 2 — test mode, separate DB and log
python bot.py --env .env.test --db calendar_bot_test.db --log logs/bot_test.log
```

Or put `BOT_DB_PATH`, `BOT_LOG_FILE`, and `BOT_CONFIG_FILE` directly in `.env.test` to avoid repeating them on the command line:

```bash
python bot.py --env .env.test
```

### Changes

- [x] Add `_preparse_argv()` to `bot.py` — pre-parses `--env`, `--db`, `--log`, `--config` and sets `BOT_ENV_FILE`, `BOT_DB_PATH`, `BOT_LOG_FILE`, `BOT_CONFIG_FILE` in `os.environ` before any imports
- [x] Register `--env`, `--db`, `--log`, `--config` in argparse so they appear in `--help`
- [x] Update `config.py` `_load_env()` — use `os.environ.setdefault` (don't overwrite pre-loaded vars); read `BOT_ENV_FILE` as the default path
- [x] Update `db/state.py` `DB_PATH` — read from `BOT_DB_PATH` env var; fall back to `db/calendar_bot.db`
- [x] Update `monitor/loop.py` `configure_logging()` — read `BOT_LOG_FILE` env var for the rotating log file path; fall back to `{log_dir}/bot.log`
- [x] Add config override support to `config.py` — `exec` `BOT_CONFIG_FILE` into the config namespace at the end of module load; raises `SystemExit` if the file is specified but not found

### `.env.test` template

```ini
# Deribit test exchange — shared key pair used by both paper and test modes
TRADING_MODE=test
DERIBIT_TEST_CLIENT_ID=<your test client id>
DERIBIT_TEST_CLIENT_SECRET=<your test client secret>

# Isolate state and logs from the paper-mode instance
BOT_DB_PATH=calendar_bot_test.db
BOT_LOG_FILE=logs/bot_test.log
BOT_CONFIG_FILE=config_test.py   # optional — omit if no strategy overrides needed

# Alerts — point to same or different endpoints
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT=<chat id>
```

### `config_test.py` example

```python
# config_test.py — strategy overrides for the test-mode instance.
# Only set variables that differ from config.py defaults.
ASSETS        = ["BTC"]   # trade only BTC while testing
MAX_POSITIONS = 1         # one position at a time for controlled observation
MAX_LOSS_PCT  = 0.005     # 0.5% max loss per trade (half the paper default)
```

---

## Phase 8i — Feed Asset Expansion for Open Positions

- [x] Update `bot.py` to expand the feed subscription beyond `config.ASSETS`
  - [x] On startup, call `list_assets_with_open_positions()` from `db/state.py`
  - [x] Pass the union of `config.ASSETS` and open-position assets to `DeribitFeed`
  - [x] Log any "extra" assets added so the operator can see the expansion
  - [x] Entry path (scanner) remains restricted to `config.ASSETS` only — no new trades entered for expanded assets
  - [x] Fix: bot was emitting "No spot for BTC — skipping monitor" for open BTC trades when BTC was not in `ASSETS`

---

## Phase 8j — Drain Mode

- [x] Add `DRAIN_MODE` boolean to `config.py` (reads `DRAIN_MODE` env var; default `False`)
- [x] `strategy/decision.py` — `scan_tick()` returns immediately without scanning or entering when `DRAIN_MODE` is `True`
- [x] `strategy/decision.py` — `_monitor_position()` closes positions outright rather than rolling when `DRAIN_MODE` is `True` and the near leg is within the roll-trigger window
- [x] `bot.py` — prints a drain mode banner alongside the trading mode banner
- [x] Add `TestDrainMode` (5 tests) to `tests/test_decision.py`

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
- [x] **Roll loop fires every monitor tick (observed on trade_id=27 and trade_id=29)** — after a successful roll, neither the DB nor the in-memory position dict was updated with the new near leg, so `_days_left` kept reading the stale expiry and re-triggering the roll every minute. trade_id=27 rolled 207 times over 3.5 hours before the roll failed and the position was closed with pnl=$0 despite being profitable. Five fixes applied: (1) `db/state.py` — added `update_near_leg()` to persist new near instrument and expiry after roll; (2) `_try_roll` in `strategy/decision.py` — updates `pos["near_instrument"]` and `pos["expiry_near"]` in-memory immediately after a successful roll; (3) `CalendarExecutor.roll_near_leg` in `execution/executor.py` — added paper-mode short-circuit (was hitting the live API unnecessarily); (4) `_rolled_this_tick: set[int]` guard added to `DecisionEngine` — positions already rolled in the current monitor pass are skipped as belt-and-suspenders; (5) `_try_roll` — skips roll if the scanner returns the same near instrument currently held (no-op roll guard). Tests: `TestRollFixes` (7 tests) in `tests/test_decision.py`; `TestUpdateNearLeg` (5 tests) in `tests/test_state.py`.
- [x] **Test/live mode used DryRunExecutor instead of CalendarExecutor** — `bot.py` was not passing an executor to `BotLoop`, so `DecisionEngine` silently fell back to `DryRunExecutor` in all modes. In test mode this meant all fills were simulated locally and logged as `[DRY-RUN]` while no orders were sent to Deribit. Fixed by instantiating `CalendarExecutor` in `bot.py` when `TRADING_MODE != "paper"` and passing it to `BotLoop`.
- [x] **Stale open orders left on exchange after unclean shutdown** — when the bot was killed mid-fill (e.g. during the individual-leg fallback), open limit orders were left sitting on Deribit with no process to cancel them. Added `_cancel_open_orders()` to `bot.py` which calls `private/cancel_all_by_currency` for each configured asset at startup in test/live mode, clearing any orders from a prior session before the new session begins.
- [x] **`CalendarExecutor._run()` fails when called from within the bot's event loop** — `_run()` called `asyncio.run()` unconditionally. Since `_scan_job` and `_monitor_job` are `async def` (running inside `AsyncIOScheduler`'s event loop), calling `asyncio.run()` from within them raised `RuntimeError: asyncio.run() cannot be called from a running event loop`, causing every real order attempt to fail silently. Fixed in `execution/executor.py`: `_run()` now detects an active event loop via `asyncio.get_running_loop()` and, when one exists, runs the coroutine in a `ThreadPoolExecutor` thread where `asyncio.run()` is safe. Standalone (test/paper) calls fall through to the original `asyncio.run()` path. Tests: `TestRunInsideEventLoop` (2 tests) in `tests/test_executor.py`.
- [x] **Close-after-roll retry loop (margin call incident 2026-07-01)** — when `_async_close_spread` failed (e.g. due to Deribit API error or far-leg timeout), `_try_roll` and `_try_close` would retry every monitor tick without limit. trade_id=4 failed to close on 2026-07-01 06:00:06 UTC (far-leg timeout), accumulated as naked long call, and triggered a 100% margin call 4.5 minutes later. Added `_close_roll_failures: dict[int, int]` to `DecisionEngine` to track failed attempts; `_try_roll` and `_try_close` now cap retries at 3 attempts and force a position close on the 4th failure with logging. Counters are cleared on successful roll/close and garbage-collected when positions close. Tests: `TestCloseAfterRollRetryLimit` (8 tests) in `tests/test_decision.py`.
- [x] **`_async_close_spread` crashes on Deribit API error and leaves legs unclosed** — the near and far leg close orders at `execution/executor.py` lines 639 and 652 were not wrapped in try-except, so any API error (e.g. `-32602 Invalid params`) crashed the function and returned `None`. Worse, a partial fill (near closed but far timed out) left a naked position open. Three fixes: (1) Wrapped both `place_order()` calls in try-except to catch `RuntimeError` from Deribit API errors; (2) If the near leg fills but far times out, immediately submit a reverse sell to unwind the near leg; (3) If the far leg fills but near times out, immediately submit a reverse buy to unwind the far leg. This prevents naked leg exposure that can trigger unintended margin calls. Tests: `TestAsyncCloseSpreadPartialFill` (6 tests) in `tests/test_executor.py`.
- [x] **Deploy workflow does not kill stale bot processes** — the GitHub Actions PowerShell script in `.github/workflows/deploy.yml` used `Get-Process -Name python` to find bot processes, but `Get-Process` does not populate the `CommandLine` property by default so the filter `$_.CommandLine -like '*bot.py*'` always failed silently. Result: new bot process started while stale process still held the port, causing errors. Three fixes: (1) Changed to `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` which exposes `CommandLine` correctly; (2) Fixed path typos: `coderepoo` → `coderepo`, `bot_paper` → `bot-paper`; (3) Added `-WorkingDirectory` and `Start-Sleep -Seconds 2` between kill and restart for clean process shutdown. Tests: none (PowerShell workflow manual validation required).
- [x] **Expired near leg close stuck in retry loop (issue #3, trade_id=4)** — when a near leg expires on Deribit (e.g. 3JUL26 as of 2026-07-01), the bot attempts to close but Deribit rejects with `-32602 Invalid params` (cannot trade expired instruments). The close-on-expiry path in `_monitor_position()` was not using the retry-limit logic that the roll path had; it would retry every monitor tick forever. Fixed by: (1) applying `_close_roll_failures` tracking to the expiry-close path; (2) capping failed attempts at 3 retries; (3) on 4th failure, force-closing the position by marking it as closed in the DB without calling the executor (which would fail anyway), breaking the infinite retry loop. This prevents naked leg accumulation and margin call risk from stuck positions. Tests: `TestCloseAfterExpiryRetryLimit` (5 tests) in `tests/test_decision.py`. Scratch: `scratch/scratch_expired_near_retry_limit.py`.
- [x] **Stop-loss and take-profit close stuck in retry loop (trade_id=5 incident)** — when a stop-loss or take-profit close failed (e.g. error 10019 locked_by_admin), the stop/TP close paths in `_monitor_position()` did not use the retry-limit logic, causing 69+ consecutive close attempts over 1+ hours. Unlike expiry closes which used `_try_close()` (which had retry tracking), stop and TP closes called `_close_position()` directly without retry limiting. Fixed by: (1) adding `_close_roll_failures` tracking to both `if status == "stop":` and `if status == "tp":` blocks in `_monitor_position()`; (2) capping failed attempts at 3 retries; (3) on 4th failure, force-closing the position by calling `close_calendar_trade()` directly without executor retry. Tests: `TestStopTpCloseRetryLimit` (4 tests) in `tests/test_decision.py`.

---

## Phase 14 — Roll P&L Tracking and EV Recalculation

Previously, when a near leg was rolled, the profit/loss realized from closing the old near leg was not captured in the final position P&L. The formula `spread_value - net_debit` compared the final spread value to the original entry debit, missing all intermediate roll cash flows. Additionally, new near-leg candidates were not validated or scored for EV at roll time.

### Root causes fixed

**`db/state.py` — New tracking columns**

- Added `roll_pnl REAL NOT NULL DEFAULT 0.0` — tracks cumulative profit from rolling near legs
- Added `ev_score_initial REAL NOT NULL DEFAULT 0.0` — stores EV at entry (initial scan)
- Added `ev_score_at_roll REAL NOT NULL DEFAULT 0.0` — stores EV of new near leg at roll time
- Updated `update_near_leg()` to accept and accumulate `roll_pnl` and store `ev_score_at_roll`
- Migration adds all three columns to existing databases with backward-compatible defaults

**`strategy/decision.py` — Roll validation and P&L inclusion**

- `_try_roll()` now validates new candidate passes `_check_liquidity_gate()` (same gates as entry)
- Recalculates EV for new candidate before rolling and passes it to DB
- Calculates roll P&L: `(old_near_sell_price - new_near_bid_price) * qty`
- Immediately adds roll_pnl to `_today_pnl` and `_session_pnl` when roll succeeds
- Logs roll details including roll_pnl and new EV at roll time
- `_close_position()` now includes roll_pnl in final P&L: `net_pnl = gross_pnl + roll_pnl - fees`
- Logs both gross and roll P&L separately at close time

**`telegram_cmd/handlers.py` — Roll P&L visibility**

- `/positions` — shows separate roll P&L: `roll=$X.XX` alongside unrealized PnL when a roll has occurred
- `/positions` — displays both `ev_init=X.XXXX` (entry EV) and `ev_roll=X.XXXX` (roll EV, if rolled)
- `/portfolio` — shows total P&L including roll: `PnL=$(unr + roll)`
- `/portfolio` — breaks down `Fees:` and `Roll PnL:` as separate line items
- `/portfolio` — displays both `EV_init:` and `EV_roll:` when roll has occurred

### Entry logging improvements

- `ENTER` log now includes `ev=X.XXXX` to show EV at entry
- `ROLL` log now includes `roll_pnl=X.XX` and `ev_new=X.XXXX` to show realized profit and new EV
- `CLOSE` log now includes `roll_pnl=X.XX` and `ev_initial=X.XXXX` for full lifecycle visibility

### Example

Position #42 BTC 60K Put, 1d→7d:

```text
ENTER filled: trade_id=42 BTC Put strike=60000 qty=1.0 debit=0.0060 fees=0.00012 ev=0.0385
[24 hours later, near leg at 2 days to expiry]
ROLL trade_id=42 → new near=BTC-3JAN26-60000-P roll_pnl=+0.0008 roll_fees=0.00015 ev_new=0.0421
[more time passes]
CLOSE trade_id=42 gross_pnl=+0.0015 roll_pnl=+0.0008 close_fees=0.00012 net_pnl=+0.0011 ev_initial=0.0385
```

The `net_pnl=+0.0011` correctly includes the roll profit of `+0.0008` that was locked in during the roll, plus the spread movement P&L of `+0.0015`, minus fees.

---

## Phase 13 — Fee-Inclusive PnL Display

All PnL metrics (Telegram commands, internal engine accumulators, DB `pnl` field) previously reported gross PnL — the pure spread price movement — without deducting fees. The `open_fees` and `close_fees` columns existed and were correctly populated, but were never subtracted from any PnL figure. At BTC spot $100k with ~$30–$60 per round-trip in fees, this materially overstated profitability: a position showing `PnL=$+0.60` was actually `PnL=$-2.94` after fees.

### Root causes fixed (continued)

**`strategy/decision.py` — `_close_position()`**

- Changed `pnl = gross_pnl` → `pnl = gross_pnl - open_fees_usd - close_fees_usd`
- DB `pnl` column now stores true net P&L; `_today_pnl` and `_session_pnl` accumulate net values
- Misleading comment "open_fees already deducted at entry" replaced with accurate description
- Close alert notifications (`notify_stop`, `notify_take_profit`) automatically receive net PnL since they read `pnl`

**`strategy/decision.py` — `_monitor_position()`**

- `unrealized` now deducts `open_fees` from cost basis: `sv - net_debit*qty - open_fees`
- `engine._unrealized_pnl` (fed to `/status` and internal status) is now fee-inclusive for open positions

**`telegram_cmd/handlers.py` — `handle_positions()`**

- PnL formula: `unr_pnl = spread_val - (net_debit*qty + open_fees)` — open fees in cost basis
- PnL%: denominator is `net_debit*qty + open_fees` (total capital deployed including entry fees)

**`telegram_cmd/handlers.py` — `handle_portfolio()`**

- PnL formula: `pnl = curr_val - net_debit*qty - open_fees`
- Previously showed `Fees: $3.11` on the line above a PnL that ignored those fees; now consistent

**`telegram_cmd/handlers.py` — `handle_status()`**

- Added `Fees (session): $X.XX` line showing cumulative fees paid since bot start
- `/status` PnL today and PnL since start now correctly reflect net figures (closed: DB net pnl; open: unrealized net of open fees)

### What remains gross vs net

| Metric | Before | After |
| --- | --- | --- |
| DB `pnl` field | gross (no fees) | net (open + close fees deducted) |
| `_today_pnl` / `_session_pnl` | gross | net |
| `_unrealized_pnl` | gross | net of open fees |
| `/positions` PnL | gross | net of open fees |
| `/portfolio` PnL | gross (fees shown separately) | net of open fees |
| `/status` PnL today | gross | net |
| `/status` PnL since start | gross | net |
| Close alert PnL | gross | net |
| `fees_paid_today` tracking | correct (unchanged) | correct (unchanged) |

### Tests

- [x] `TestFeeIntegration.test_close_pnl_deducts_open_and_close_fees` — `_today_pnl` after close equals `gross - open_fees - close_fees`
- [x] `TestFeeIntegration.test_unrealized_deducts_open_fees` — `monitor_tick` daily_pnl deducts `open_fees` from each open position
- [x] `TestDailyPnlUnrealized.test_daily_pnl_includes_unrealized` — comment updated; result unchanged (open_fees=0.0 in fixture)
- [x] `TestHandlePositions.test_positions_pnl_deducts_open_fees` — `/positions` PnL is negative when open_fees exceed price gain
- [x] `TestHandlePortfolio.test_portfolio_pnl_deducts_open_fees` — `/portfolio` PnL deducts open_fees
- [x] `TestHandleStatus.test_status_shows_fees_session` — `/status` reply contains "Fees" and the session amount

### Automate pull after PR

- after a Pull Request is merged to master, the windows runner will pull changes locally and restart the bot (all bots)
- restarts paper and testnet bots
