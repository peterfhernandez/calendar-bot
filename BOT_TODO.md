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

- [x] Add `get_all_closed_trades(db_path: Path = DB_PATH) -> list[CalendarTrade]` — all rows with `date_close IS NOT NULL`, ordered by `date_close ASC, id ASC`

### `telegram_cmd/pnl_chart.py` (new module)

- [x] `matplotlib.use("Agg")` set at import time, before `pyplot` is imported
- [x] `build_cumulative_series(closed_trades) -> list[tuple[datetime, float]]` — pure function, running sum of `t.pnl` ordered by `date_close`
- [x] `compute_unrealized(open_trades, cache: ChainCache) -> tuple[float, int]` — returns `(total_unrealized_pnl, open_count)`; reuses the mid-price / `last_spread_value` fallback logic already in `telegram_cmd/handlers.py` (factor the shared bits out to a helper rather than duplicating, e.g. move `_mid()` and the stale-cache fallback into a shared `telegram_cmd/_pnl_common.py` or keep the calc in `handlers.py` and import it)
- [x] `render_pnl_chart(closed_trades, open_trades, cache) -> io.BytesIO` — builds the black realized-PnL line, appends the dotted green unrealized segment when `open_count > 0`, draws a y=0 reference line, formats the y-axis as `$X`, rotates/thins x-axis date labels for readability with many trades, returns a seeked-to-0 `BytesIO` containing PNG bytes

### `telegram_cmd/handlers.py`

- [x] `handle_pnl(update, context, cache, db_path)` — fetches `get_all_closed_trades()` and `get_open_trades()`, calls `render_pnl_chart()`, sends via `update.message.reply_photo(photo=buf, caption=...)`; caption includes total realized PnL, total unrealized PnL, open trade count, and the combined total
- [x] Reply with a plain text message instead of a photo when there is no history at all (no closed trades and no open trades)

### `telegram_cmd/listener.py`

- [x] Add `("pnl", "Equity curve: realized PnL (black) + unrealized PnL (dotted green), N open trades")` to `COMMAND_REGISTRY`
- [x] Wrap and register `cmd_pnl` the same way as `cmd_positions` (needs `cache` injected, no `engine` dependency)

### `requirements.txt`

- [x] Add `matplotlib>=3.8`

### Tests and scratch

- [x] `tests/test_state.py` — `TestGetAllClosedTrades`: returns only closed trades, ordered by `date_close`, empty list when none closed
- [x] `tests/test_telegram_cmd.py` — `TestHandlePnl`: mocks `reply_photo`; verifies it is called with PNG bytes (`b"\x89PNG"` header) when history exists; verifies caption contains realized/unrealized/open-count figures; verifies the no-history case falls back to `reply_text`; verifies the dotted unrealized segment is omitted when there are no open positions
- [x] Unit tests for `build_cumulative_series` and `compute_unrealized` directly (no rendering involved) covering: single trade, multiple same-day closes, mixed win/loss sequence, zero open positions, stale-cache fallback
- [x] `scratch/scratch_pnl_chart.py` — loads real (or, if empty, synthetic) closed trades from the paper DB, renders the chart, and saves it to `scratch/pnl_chart_preview.png` for visual inspection. Aborts if `TRADING_MODE == "live"` (per project convention: scratch files never run against live trading). Run with `python -m scratch.scratch_pnl_chart` from the repo root.

---

## Phase 17 — Cross Portfolio Margin (X:PM) Entry Gate

Completed. New entry gate that rejects a candidate if adding it to the current portfolio would push the account's margin utilization to a level that risks a margin call, using Deribit's actual Cross Portfolio Margin (X:PM) numbers rather than a local approximation of debit-at-risk.

### Key implementation details

**Paper mode:** Gate is a no-op (returns None immediately) so paper trading is not blocked by the absence of a funded test account. Margin data is only checked in `test` and `live` modes.

**Live margin simulation:** `PortfolioTracker.simulate_margin()` calls Deribit's `private/get_margins` API with candidate legs (instrument_name, amount, price) and returns projected initial/maintenance margin in USD, or None on any failure.

**Fallback proxy:** When the live API call fails or is unavailable, uses a conservative local formula: `projected_util = (current_maintenance_margin + candidate_net_debit × qty) / equity`. This is a safety backstop, deliberately conservative since it doesn't include correlation offsets that the real PM calculation might allow.

**Fail-closed in test/live:** When margin data is unavailable in test/live mode, the gate rejects candidates to prevent margin calls. In paper mode, the gate is a no-op regardless of data availability.

### Changes made

### `portfolio/tracker.py`

- [x] `maintenance_margin_usd` property — tracks current maintenance margin from `get_account_summary`
- [x] `margin_utilization_pct` property — `maintenance_margin_usd / equity_usd`
- [x] `MarginImpact` dataclass — holds `projected_initial_margin_usd` and `projected_maintenance_margin_usd`
- [x] `simulate_margin(legs)` — calls Deribit `private/get_margins` endpoint; extracts margin from response and converts to USD using spot prices; returns None on any failure
- [x] Enhanced `_rest_post()` to support optional `bearer_token` parameter for authenticated POST requests

### `bot.py`

- [x] Instantiated `PortfolioTracker` in `_run()` when credentials are configured
- [x] Passed `portfolio=tracker` to `BotLoop()` for use by DecisionEngine

### `config.py`

- [x] `MAX_MARGIN_UTILIZATION_PCT = 0.80` — ceiling on `maintenance_margin / equity` (matches Deribit's default)
- [x] `MARGIN_GATE_ENABLED = True` — gate can be disabled via config
- [x] `MARGIN_GATE_REQUIRED_LIVE = True` — gate fails closed in test/live mode when margin data unavailable

### `strategy/decision.py`

- [x] `_check_margin_gate(candidate)` — checks if adding candidate would breach margin ceiling
  - Paper mode: returns None (no-op)
  - Test/live mode: checks current utilization, tries live simulation API, falls back to proxy formula
  - Rejects if current OR projected utilization exceeds MAX_MARGIN_UTILIZATION_PCT
- [x] Called in `scan_tick()` after liquidity gate to reject over-leveraged candidates
- [x] Called in `_try_roll()` to prevent rolls that would breach margin ceiling
- [x] Logs rejections at INFO level

### Tests

- [x] `tests/test_portfolio.py::TestMaintenanceMargin::test_simulate_margin_success` — mocks API call and verifies margin calculation
- [x] `tests/test_portfolio.py::TestMaintenanceMargin::test_simulate_margin_no_credentials` — verifies returns None without credentials
- [x] `tests/test_portfolio.py::TestMaintenanceMargin::test_simulate_margin_empty_legs` — verifies returns None for empty legs
- [x] `tests/test_portfolio.py::TestMaintenanceMargin::test_simulate_margin_api_failure` — verifies graceful error handling
- [x] `tests/test_decision.py::TestMarginGate::test_margin_gate_disabled_when_flag_false` — verifies gate disabled by config
- [x] `tests/test_decision.py::TestMarginGate::test_margin_gate_noop_in_paper_mode` — verifies paper mode no-op
- [x] `tests/test_decision.py::TestMarginGate::test_margin_gate_no_portfolio_tracker` — verifies behavior without tracker
- [x] `tests/test_decision.py::TestMarginGate::test_margin_gate_rejects_when_current_utilization_high` — verifies current util check
- [x] `tests/test_decision.py::TestMarginGate::test_margin_gate_accepts_when_utilization_low` — verifies approval when safe
- [x] `tests/test_decision.py::TestMarginGate::test_margin_gate_uses_live_simulation_when_available` — verifies live API takes precedence

### Scratch scripts

- [x] `scratch/scratch_margin_gate.py` — end-to-end demonstration

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

- [x] **Telegram commands `/info`, `/close`, `/close_manually` had invalid f-string syntax** — the `/info` handler at lines 470 and 476 in `telegram_cmd/handlers.py` used invalid Python f-string syntax: `{value:.4f if condition else 'N/A'}`, which cannot apply a format specifier to a conditional expression. Fixed by: (1) pre-formatting the value conditionally before the f-string: `f"{value:.4f}" if value is not None else "N/A"`; (2) adding unit tests `test_handle_info_displays_position_status` and `test_handle_info_handles_missing_cache` to verify the handlers work correctly. The `/close` and `/close_manually` commands were already working correctly and properly wired; they just lacked test coverage. Added comprehensive tests to verify all three commands work end-to-end. Tests: `TestHandleInfo` (2 tests) and existing `test_handle_close_resets_close_stuck_flag`, `test_handle_close_manually_clears_notification_flag` in `tests/test_telegram_cmd.py`.
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
- [x] **Expired near leg close missing mark_position_close_stuck call** — the "near leg expired" code path in `_monitor_position()` (lines 745–788) did NOT call `mark_position_close_stuck()` when retries were exhausted, unlike the stop/tp paths which did. This meant stuck positions from expired instruments were force-closed silently in the DB without notifying the user. Fixed by: (1) replacing the force-close path with a call to `mark_position_close_stuck()`; (2) adding the user notification logic with the `_notified_stuck` deduplication set to prevent spam; (3) ensuring consistency with stop/tp behavior. Tests: `test_fourth_stop_close_failure_force_closes` and `test_tp_close_retry_limit` in `tests/test_decision.py` (both now pass).
- [x] **Telegram error handling raises instead of logging** — `alerts/notifier.py` `_post_telegram()` method (line 397) raised `RuntimeError` on the final failed HTTP attempt instead of logging and returning False as designed. This caused async tasks to crash silently without propagating error info. Fixed by: replacing all `raise RuntimeError(...)` calls with `logger.error(...)` and `return False`. This allows callers to handle failures gracefully without crashing. Tests: `test_post_telegram_api_error_logged_not_raised` in `tests/test_notifier.py` (now passes).
- [x] **Windows temp directory cleanup PermissionError with SQLite locks** — two test functions (`test_handle_close_resets_close_stuck_flag` and `test_handle_close_manually_clears_notification_flag` in `tests/test_telegram_cmd.py`) failed on Windows because SQLite connections remained open when `TemporaryDirectory.__exit__` tried to delete temp files, causing `PermissionError: [WinError 32]`. Fixed by explicitly closing all database connections before the temp directory context exits using `get_connection(db_path).close()`. Tests: both now pass on Windows.
- [x] **Phase 22 tick-size lookup called the wrong Deribit RPC method — broke 100% of order submissions** — the Phase 22d close/roll price fix (commit `75e0a4a`) added a tick-size lookup that called `public/get_instruments` (plural, list-all-for-a-currency, no `instrument_name` param) instead of `public/get_instrument` (singular, accepts `instrument_name`, returns one object) at three call sites in `execution/executor.py`: `get_instrument()`, `_fetch_tick_info()`, and `_fetch_and_cache_tick_size()`. The plural endpoint returned a list, so parsing raised `'list' object has no attribute 'get'`, which a generic `except` swallowed as a warning and fell back to naive 4-decimal rounding — the exact off-tick price the fix was meant to prevent. Deribit rejected the resulting orders with `-32602 Invalid params`, and because entry retries only cover `OSError`/`WebSocketException`/`TimeoutError`, every entry died as `ENTER rejected by executor` (candidates ranked and approved, but no submission ever landed) and the same broken lookup blocked the close/roll path (expired near legs had to be closed manually). Fixed by calling `public/get_instrument` at all three sites and parsing the returned instrument object directly (no `{"instruments": [...]}`/list assumption). Tests: `test_tick_size_cache_populated` updated for the singular shape and asserts the method name; new `test_fetch_tick_info_parses_singular_endpoint_object` in `tests/test_executor.py`. Scratch: `scratch/scratch_rpc_method_fix.py`.

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

## Phase 17 — Telegram Notification Reliability Improvements

**Problem:** Notifications for position entries/closes were not being reliably delivered, especially overnight. Fire-and-forget async tasks had no error tracking or retry logic.

**Fixes Implemented:**

- [x] **Retry logic** — Telegram sends now retry up to 2 times with 1-second delays on transient failures (timeouts, rate limits)
- [x] **Better error tracking** — Added callback-based task result tracking; failed sends now log ERROR level messages
- [x] **Startup verification** — Prominent warning logged if TELEGRAM_TOKEN or TELEGRAM_CHAT is missing/unconfigured
- [x] **Enhanced logging** — Entry/close/roll notifications now log success (INFO) and failure (ERROR) with trade_id
- [x] **Diagnostic tools**
  - [x] `scratch/diagnose_telegram_notifications.py` — automated checks for configuration, API connectivity, message delivery
  - [x] `TELEGRAM_DEBUGGING_GUIDE.md` — comprehensive troubleshooting guide with common failure modes and solutions

**Why notifications were missing overnight:**

1. No retry on transient failures (network timeouts)
2. Fire-and-forget async tasks had no way to report failures
3. Missing startup verification meant credential issues went unnoticed
4. Weak error logging made failures hard to diagnose
5. Possible `.env` file missing or misconfigured

**Testing notification delivery:**

```bash
# 1. Run diagnostic to verify setup
python3 scratch/diagnose_telegram_notifications.py

# 2. Start bot and watch logs
python bot.py
tail -f logs/bot.log | grep -E "(notification|Telegram|ERROR)"

# 3. Check that startup shows:
# INFO Telegram notifications enabled for chat 123456789

# 4. Trigger a position close and verify:
# INFO Notification queued for position close: type=close trade_id=42
# INFO Telegram message sent to chat 123456789 (subject: ...)
```

If notifications still fail to arrive:

1. Run `python3 scratch/diagnose_telegram_notifications.py` for automated diagnosis
2. Check `logs/bot.log` for ERROR messages with ⚠️ prefix
3. Verify `.env` has valid TELEGRAM_TOKEN and TELEGRAM_CHAT
4. See TELEGRAM_DEBUGGING_GUIDE.md for detailed solutions

---

## Phase 17b — Paper Mode Portfolio Isolation

In paper mode, the bot should be completely isolated from Deribit. All portfolio metrics (equity, available cash, unrealized P&L, margin) should be calculated from the SQLite database and live cache prices, with zero REST API calls to Deribit.

**Root Cause:** `portfolio/tracker.py` unconditionally calls Deribit REST APIs when credentials are configured, regardless of trading mode. This causes:
1. Unnecessary API overhead in paper mode
2. Reconciliation warnings comparing Deribit margin with DB margin (confusing when paper mode shouldn't touch Deribit)
3. Portfolio snapshots showing zero equity/available_cash in DB-only fallback mode
4. "RECONCILE MISMATCH" warnings that are not actionable in paper mode

### Implementation

- [x] **Item 1: Import TRADING_MODE** — Add `TRADING_MODE` from config to `portfolio/tracker.py`
  - [x] Import statement at top of file
  - [x] Use in trading-mode checks

- [x] **Item 2: Skip API in paper mode** — Modify `refresh()` to return early if `TRADING_MODE == "paper"`, after calculating SQLite values but before any Deribit API calls
  - [x] Check `TRADING_MODE == "paper"` before calling `_refresh_from_api()`
  - [x] Ensure SQLite calculations (`_used_margin`, `_realized_pnl_today`, `_open_position_count`, `_fees_paid_today`, `_fees_paid_total`) always run
  - [x] Skip reconciliation entirely in paper mode

- [x] **Item 3: Calculate unrealized P&L from cache in paper mode** — Implement `_calculate_unrealized_pnl_from_cache()` that:
  - [x] Queries open positions from SQLite
  - [x] Fetches live spread values from `ChainCache` for each position
  - [x] Sums `(spread_value - net_debit * qty)` per position
  - [x] Falls back to 0.0 if cache unavailable (consistent with current behavior)
  - [x] Called in paper mode when Deribit API is skipped

- [x] **Item 4: Implement DB-only portfolio calculation** — Create `_calculate_db_only_portfolio()` that:
  - [x] Computes `equity_usd` from: initial capital + realized P&L today + unrealized P&L from cache
  - [x] Computes `available_cash` from DB-only metrics (no Deribit API call)
  - [x] Returns dict with equity, available_cash, and unrealized_pnl
  - [x] Called in paper mode after skipping API calls

- [x] **Item 5: Add safety guards and docstrings** 
  - [x] Add explicit `if TRADING_MODE != "paper"` check at start of `_refresh_from_api()` as belt-and-suspenders
  - [x] Add explicit `if TRADING_MODE != "paper"` guard in `simulate_margin()` for clarity
  - [x] Update class docstring to document paper vs test/live behavior
  - [x] Update method docstrings noting which are test/live only

### Test Coverage

- [x] `TestPaperModePortfolioIsolation::test_no_deribit_api_calls_in_paper_mode` — verify zero REST API calls to Deribit in paper mode
- [x] `TestPaperModePortfolioIsolation::test_no_reconciliation_warning_in_paper_mode` — verify no RECONCILE MISMATCH warnings logged
- [x] `TestPaperModePortfolioIsolation::test_equity_calculated_from_db_in_paper_mode` — verify non-zero equity from DB calculation
- [x] `TestPaperModePortfolioIsolation::test_unrealized_pnl_from_cache_in_paper_mode` — verify unrealized P&L calculated from live cache prices
- [x] `TestPaperModePortfolioIsolation::test_test_mode_still_uses_deribit_api` — regression: verify test/live modes still call Deribit API

### Expected Results

In **paper mode**:
- ✅ Zero Deribit REST API calls in `portfolio/tracker.py`
- ✅ Portfolio snapshot shows realistic equity and unrealized P&L (calculated from DB + cache)
- ✅ No reconciliation warnings or mismatches
- ✅ All values come from SQLite + live cache, never from Deribit

In **test/live mode**:
- ✅ Behavior unchanged — full Deribit API integration continues
- ✅ Reconciliation still runs to verify DB matches Deribit
- ✅ Margin simulation API calls proceed normally

---

## Phase 17c — Notification Spam Prevention for Stuck Positions

- [x] **Notification deduplication** — Add `_notified_stuck: set[int]` to track positions already notified
- [x] **Stop-loss guard** — Only send `notify_close_stuck()` once per stuck position, not every monitor tick
- [x] **Take-profit guard** — Same deduplication applied to TP close retry limit path
- [x] **Reset on user intervention** — Clear notification flag when user runs `/close` or `/close_manually` commands
- [x] **Test coverage** — Added `test_handle_close_resets_close_stuck_flag` and `test_handle_close_manually_clears_notification_flag`

**Result:** Users receive exactly one notification when a position becomes stuck, preventing message spam during long retry loops. They can be notified again if the position gets stuck after manual reset.

---

## Phase 18 — Close-Order Reliability & Stuck-Position Retry Bugfixes

**Status:** All four bugs completed. Bugs 1-3 fixed via commit 46a9627, based on test-mode analysis of `db/calendar_bot_test.db` and `logs/bot_test.log*` (test-mode run under `config_test.py`, 2026-06-28 → 2026-07-07). Bug 4 identified 2026-07-11 via a separate test-mode DB/log analysis and fixed the same day — open-position instrument names are now unioned into the feed's ticker-subscription list on every connect and reconnect. Root causes and solutions detailed in BOT_PLAN.md Phase 18.

### Bug 1 — Far-leg close order rejected by Deribit (`-32602 Invalid params`)

- [x] **Root cause:** `execution/executor.py` never fetches an instrument's actual tick size (Deribit's option tick size scales with premium level via `tick_size_steps`). Every order price — entry, close, roll, unwind — is blanket-rounded with `round(price, 4)` (see `_index_price`, and the price args in `_async_close_spread`, `_async_enter_spread`, `_async_roll_near_leg`). Near-leg close orders happen to land on valid ticks and never fail (0 failures logged); far-leg close orders are typically priced higher (more time value) and fall in a coarser tick band, so the 4-decimal price is invalid and Deribit's JSON-RPC layer rejects the request before it reaches business-logic validation — hence the raw `-32602` code rather than a Deribit-specific error like `10009`/`11044`.
- [x] **Fix:** add a helper that fetches (and caches, per instrument) `tick_size` / `tick_size_steps` via `public/get_instruments` or the ticker response, and round every order price to the nearest valid tick (floor for buys, ceiling for sells, or per Deribit convention) before submission. Apply to **all** price-producing call sites in `executor.py`: `_async_enter_spread` (individual-leg fallback), `_async_enter_spread_combo`, `_async_close_spread` (near + far), `_async_roll_near_leg` (close + open), and the near-leg unwind/flatten paths (`FLATTEN-NEAR`, `UNWIND-NEAR`, `UNWIND-FAR`). Implemented via `_round_to_tick()` helper function and tick-size cache in `execution/executor.py`.
- [x] **Test coverage:** Added `TestTickSizeRounding` in `tests/test_executor.py` with 4 tests asserting tick-size rounding logic, comparison of per-instrument tick sizes, and regression coverage.
- [x] **Scratch verification:** Covered by test suite; no separate scratch script needed.

### Bug 2 — Stuck-position retry counter reset allows an unbounded retry loop

- [x] **Root cause:** Positions marked `close_stuck` were not excluded from routine monitoring, causing repeated re-evaluation and 40+ redundant "marked stuck" DB writes over hours (observed in trade_id=3).
- [x] **Fix:** Modified `get_open_trades()` in `db/state.py` to exclude positions with `close_status == 'close_stuck'`. This automatically prevents stuck positions from being re-evaluated in monitor ticks until the user intervenes via `/close` or `/close_manually`.
- [x] **DB-level fix:** Applied via the `close_status != 'close_stuck'` filter in the `get_open_trades()` query; no need for a new result value.
- [x] **Update existing tests:** Tests updated to reflect the new behavior where stuck positions are excluded from monitoring.

### Bug 3 — Force-closed position recorded with `pnl=0.0` instead of real P&L

- [x] **Root cause:** When executor failed to close, positions were recorded with fake `pnl=0.0` instead of using the last known mark-to-market value.
- [x] **Fix:** Modified `_close_position()` in `strategy/decision.py` to mark positions as stuck (instead of recording zero PnL) when the executor fails to close. This preserves actual P&L data and signals that manual intervention is needed.
- [x] **Test coverage:** Tests updated and passing; no regressions in existing test suite.

### Bug 4 — Feed subscription window silently drops IV coverage for long-dated open positions on reconnect

- [x] **Root cause:** `data/deribit_feed.py::fetch_instruments()` (~line 163-184) rebuilds the WS ticker-subscription list from `config.NEAR_DAYS_OPTIONS[0]`/`config.FAR_DAYS_OPTIONS[-1]` on every connect *and* every reconnect (`_connect_and_stream()` re-runs it after every WS drop). trade_id=1 (BTC 56000 Put, far leg `BTC-28AUG26-56000-P`, far_days=51) was opened 2026-07-09 08:35 when `config_test.py`'s `FAR_DAYS_OPTIONS` still included `45`; a same-day 08:04 commit trimmed it to `[7, 14]` for scanner purposes, but that value also drives the feed's subscription window, so the open position's far leg fell outside it. Subscription counts dropped from 588 BTC instruments (wide enough to cover the 51-day leg) to 584 after a reconnect at 19:06:33 on 07-09, then to 302 after the 07-10 07:56 restart — after which `ChainCache.get_chain()`'s 30s TTL (`CHAIN_CACHE_TTL_SEC`) drops the far leg entirely and `strategy/decision.py::_get_iv()` (~line 1225) returns `None` forever, producing the "No IV for trade 1 — skipping status check" warning (line 794) on every monitor tick since — stop-loss/take-profit monitoring silently disabled for this position for over a day.
- [x] **Fix:** Union the day-window candidate list with the exact `near_instrument`/`far_instrument` names of every open position, recomputed on every reconnect (not just startup). Implemented via option (b) — `DeribitFeed` stays free of DB-layer knowledge: (1) new `get_open_instrument_names(db_path) -> list[str]` helper in `db/state.py` returns distinct near/far instrument names across all open positions (including `close_stuck` ones, which are still open on the exchange and need price coverage); (2) `DeribitFeed.__init__` accepts an optional `extra_instruments` zero-argument callable; (3) the subscription pass (new `_subscribe_all()`, run by `_connect_and_stream()` on every connect and reconnect) invokes the callable, dedupes against the day-window lists, and subscribes the remainder — a provider failure is logged and never takes down the feed; (4) `bot.py` passes `extra_instruments=get_open_instrument_names` when constructing the feed. Note: Phase 8i addressed a related but distinct gap (feed's *asset* list not covering open positions outside `config.ASSETS`) — this fix addresses the instrument-level day-window problem.
- [x] **Test coverage:** Added `TestFeedOpenPositionCoverage` (7 tests) to `tests/test_feed.py` — open-position instruments are subscribed on initial connect, stay subscribed across a simulated reconnect, drop out once the position closes, are not duplicated when already inside the day window, and a provider failure or absent provider leaves the day-window subscription intact. Added `TestGetOpenInstrumentNames` (6 tests) to `tests/test_state.py` for the DB helper (both legs returned, closed positions excluded, stuck positions included, dedup/sort, NULL legs skipped).
- [x] **Scratch verification:** Added `scratch/scratch_feed_open_position_coverage.py` demonstrating a position past the window boundary staying subscribed across a simulated reconnect, and being dropped after close (read-only, no live orders; refuses to run when `TRADING_MODE == "live"`).

### Related observation (not a bug, documented for context)

- Test-mode entries skewed heavily toward BTC (6 of 7 trades) because `test.deribit.com`'s ETH options order book is much thinner than BTC's — ETH candidates were rejected by the liquidity gate (`MAX_LEG_SPREAD_PCT`) roughly 2.7x more often than BTC (27,042 vs 10,035 skips) despite equal `ASSETS` weighting. This is a test-exchange liquidity artifact, not a config or code defect, and is not expected to hold on the live orderbook — no action needed, noted here so it isn't mistaken for one of the bugs above during Phase 18 work.

## Phase 20 — Centralize Scattered Config Into `config.py`

**Status:** Complete — all six sub-phases (20a–20f) implemented and tested; 565 tests passing (35 new in `tests/test_config_centralization.py`). Full audit and root-cause detail in BOT_PLAN.md Phase 20. The audit found ~94 hardcoded config-like values outside `config.py` across 6 categories, plus 2 functional bugs (SOL orders never reconciled on restart; debug-viewer cache TTL diverging from `CHAIN_CACHE_TTL_SEC`) — both fixed. The shared logging helper lives in `core/logging_setup.py` (`setup_logging()` + `SecretRedactor`); `monitor/loop.py::configure_logging` is now a thin wrapper around it. The `bot.py` DEBUG override for `strategy.decision`/`strategy.sizer` moved into `config.LOG_LEVEL_OVERRIDES`. Demonstration: `python -m scratch.scratch_config_centralization` (22 offline checks, no live orders).

### 20a — Centralize logging config

- [x] Add `LOGGING` section to `config.py`: `LOG_LEVEL`, `LOG_FORMAT`, `LOG_DATE_FORMAT`, `LOG_FILE_MAX_BYTES`, `LOG_BACKUP_COUNT`, `LOG_DIR`, `NOISY_LOGGERS` (dict of logger name → level)
- [x] Add a shared `setup_logging()` helper (new or in an existing shared module) that reads from `config.LOGGING`
- [x] Replace the 5 independently-hardcoded `logging.basicConfig` calls in `monitor/loop.py`, `collect.py`, `backtest/data_collector.py`, `data/deribit_feed.py`, `data/debug_viewer.py` with calls to `setup_logging()`
- [x] Move the `httpx`/`httpcore`/`telegram.ext.Updater`/`telegram.vendor.ptb_urllib3` → WARNING suppression list (currently only in `monitor/loop.py`) into `config.NOISY_LOGGERS`
- [x] Move the `bot.py`-only DEBUG override for `strategy.decision`/`strategy.sizer` into config (or a documented CLI flag) so it isn't a silent hardcoded exception
- [x] Update/add tests asserting `setup_logging()` reads level/format/rotation from config

### 20b — Fix fake-configurable values

- [x] Add real `config.py` keys (with existing defaults preserved) for the 6 `getattr(config, "X", default)` calls that reference nonexistent keys: `SLIPPAGE_LIMIT_PCT` (`execution/executor.py`), `ORDER_TIMEOUT_SEC` (`execution/executor.py`), `MAX_ORDER_RETRIES` (`execution/executor.py`), `STUCK_ORDER_TIMEOUT_SEC` (`execution/order_manager.py`), `INITIAL_CAPITAL` (`portfolio/tracker.py`), `COLLECTOR_INTERVAL_SEC` (`backtest/data_collector.py`)
- [x] Switch those 6 call sites from `getattr(config, "X", default)` to a direct `config.X` reference
- [x] Remove the 4 redundant `getattr(config, "X", default)` calls that already shadow an existing config key with a matching fallback: `MAX_FAR_DAYS_FOR_1D_NEAR` (`strategy/scanner.py`), `MIN_NET_DEBIT` and `MAX_QTY` (`strategy/sizer.py`), `COMBO_FILL_TIMEOUT_SEC` (`execution/executor.py`) — import directly instead
- [x] Update tests that relied on the `getattr` fallback defaults to import from config instead

### 20c — Centralize network/timeout/retry/alert constants

- [x] Add to `config.py`: `DERIBIT_WS_PING_INTERVAL`, `DERIBIT_WS_PING_TIMEOUT`, `DERIBIT_WS_OPEN_TIMEOUT`, `DERIBIT_WS_MAX_SIZE`, `RPC_TIMEOUT_SEC`, `ORDER_RETRY_DELAYS`, `ALERT_COOLDOWN_SEC`, `SMTP_TIMEOUT_SEC`, `TELEGRAM_TIMEOUT_SEC`
- [x] Point `execution/executor.py`, `execution/order_manager.py`, `data/deribit_feed.py` at the new `config.DERIBIT_WS_*`/`RPC_TIMEOUT_SEC` constants instead of their independently-duplicated literals
- [x] Fix `alerts/notifier.py` to import `config.SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD` instead of re-reading `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS` from the environment directly
- [x] Add `SMTP_FROM` to `config.py`'s alert block (currently invisible to config, only read in `alerts/notifier.py`)
- [x] Wire `Notifier(cooldown_sec=...)`'s default to `config.ALERT_COOLDOWN_SEC`
- [x] Update/add tests for notifier config sourcing and WS/RPC constant usage

### 20d — Fix the two functional config-bypass bugs (can ship independently, first)

- [x] Fix `execution/order_manager.py::_fetch_deribit_open_orders` to iterate `config.ASSETS` instead of the hardcoded `"BTC"`/`"ETH"` currency loop, so SOL orders are reconciled on restart
- [x] Fix `data/chain_cache.py::ChainCache.__init__` and `data/debug_viewer.py` to default their TTL from `config.CHAIN_CACHE_TTL_SEC` instead of independently hardcoded values (30.0 / 60.0)
- [x] Remove the dead duplicate Deribit WS-URL constants in `data/deribit_feed.py` (`_WS_PAPER`/`_WS_LIVE`, confirmed unused)
- [x] Remove the dead duplicate hostname constants in `backtest/data_collector.py` (`_PAPER_HOST`/`_LIVE_HOST`, confirmed unused)
- [x] Add a regression test asserting SOL orders are included in reconciliation after this fix
- [x] Add a regression test asserting `debug_viewer`'s cache TTL matches `config.CHAIN_CACHE_TTL_SEC` when not overridden

### 20e — Move business-logic magic numbers into config

- [x] Add to `config.py`: `STRIKE_INCREMENT_TABLE`, `FAR_LEG_SPREAD_TABLE`, `NEAR_DAY_TOLERANCE`, `FAR_DAY_TOLERANCE`, `ROLL_TRIGGER_DAYS`, `POSITION_FAILURE_RETRY_CAP`, `RECONCILE_THRESHOLD_PCT`, `MIN_CONTRACT_SIZE`, `DEFAULT_PORTFOLIO_VALUE`, `EV_SAMPLE_COUNT`, `BREAKEVEN_SCAN_STEPS`
- [x] Update `core/pricing.py` (`strike_increment()`, `adjust_far_leg_price()`) to read the increment/spread tables from config
- [x] Update `core/calendar_engine.py` (breakeven scan resolution/range, the undocumented 70% warn threshold) to read from config
- [x] Update `strategy/scanner.py` (DTE tolerances, EV sample count/range, scan-range fallback) to read from config
- [x] Update `strategy/decision.py` (`_ROLL_TRIGGER_DAYS`, the `failure_count >= 3` retry cap used in 4 places) to read from config
- [x] Update `strategy/sizer.py` (`_MIN_QTY`, `_STRIKE_CORRELATION_PCT`) and `execution/executor.py`'s duplicate min-contract-size constant to share `config.MIN_CONTRACT_SIZE`
- [x] Update `portfolio/tracker.py`'s `_RECONCILE_THRESHOLD` to read `config.RECONCILE_THRESHOLD_PCT`
- [x] Consolidate the `10_000.0` default portfolio value hardcoded in `bot.py`, `execution/executor.py`, and `backtest/engine.py` into `config.DEFAULT_PORTFOLIO_VALUE`
- [x] Update/add tests for each moved constant (existing test suites should still pass with config-sourced values equal to today's literals)

### 20f — Paths, timezone, and date-format cleanup

- [x] Add `DB_PATH`, `HISTORIC_DATA_DB_PATH`, `TIMEZONE`, `DATE_FORMAT` to `config.py`
- [x] Update `db/state.py` to source its SQLite path and `ZoneInfo("Australia/Sydney")` from `config.DB_PATH`/`config.TIMEZONE` instead of a local `BOT_DB_PATH` env read and a hardcoded timezone
- [x] Update `backtest/data_collector.py` to source its DuckDB/schema paths from `config.HISTORIC_DATA_DB_PATH` (or a documented override)
- [x] Update `telegram_cmd/pnl_chart.py` to source its date format from `config.DATE_FORMAT`
- [x] Confirm `bot.py`'s `--env`/`--db`/`--log`/`--config` pre-parser env-var bootstrap (`BOT_ENV_FILE`/`BOT_DB_PATH`/`BOT_LOG_FILE`/`BOT_CONFIG_FILE`) still works unchanged — this shim intentionally predates config.py's import and is not part of the bypass being fixed
- [x] Update/add tests for `db/state.py` path/timezone sourcing

## Phase 21 — Fix Runaway Deep-ITM Calendar Churn & Close-Status Tracking Bug

**Status:** Complete — all six sub-phases (21a–21f) implemented and tested; 27 new unit tests, all passing (full suite 588 passing; the only failures are 4 pre-existing environmental Windows SQLite temp-dir `PermissionError` cases in `test_telegram_cmd.py`, unrelated to this phase). Offline demo: `python -m scratch.scratch_deep_itm_churn` (10 checks, no live orders). Also fixed a regression from the prior commit that early-imported `SPREAD_WARN_PCT`/`BREAKEVEN_SCAN_RANGE` into `core/calendar_engine.py`, breaking runtime `config` overrides — restored to late-binding `config.X` access. Root-cause detail in BOT_PLAN.md Phase 21. Found by analysing the 2026-07-14 paper-mode run (`db/calendar_bot.db`, `logs/bot.log`): 131 trades opened and closed same-day (91 on ETH 1400 Call, 23 on two deep-ITM BTC put strikes), net phantom paper P&L +$224,247. Cause: `strategy/scanner.py`'s EV ranking divides by `net_debit`, which is near-zero for deep ITM/OTM calendar spreads, producing ratios 2-3 orders of magnitude above real candidates and always winning the ranking; those same near-zero-debit positions then trip the percentage-of-debit stop/TP thresholds on ordinary quote noise within a single 60-second monitor tick, close, and get re-entered on the very next 5-minute scan since the correlation gate only checks currently-open positions. A separate, unrelated bug was also found: `db/state.py::close_calendar_trade()` (the normal auto-close path) never sets `close_status`, so every auto-closed trade still shows `close_status='open'` in the database despite `result`/`date_close`/`pnl` being correct. Also found in passing: `config_test.py` was never updated for Phase 20 and is missing ~45 config keys that phase added to `config.py` (harmless today since `config_test.py` is `exec`'d into `config.py`'s namespace and missing keys just inherit, but it's drifted from the parity the file's own header comment implies).

### 21a — Cap the EV ranking sort key

- [x] Add `EV_SCORE_RANKING_CAP` to `config.py` (e.g. `2.0`)
- [x] In `strategy/scanner.py::scan()`, rank candidates by `(c.ev_score > config.EV_SCORE_RANKING_CAP, -c.ev_score)` — demoting above-cap (near-zero-debit) candidates below every in-range one. Note: a plain `min(c.ev_score, cap)` was rejected because a capped degenerate (2.0) would still out-rank a legitimate 0.4 candidate, failing the regression below; demotion is required to satisfy it.
- [x] Confirm the `MIN_EV` accept/reject gate in `strategy/decision.py` still compares against the uncapped `ev_score` — only ranking order changes
- [x] Add a regression test: a synthetic near-zero-debit candidate with `ev_ratio=17.0` must not sort ahead of a normal candidate with `ev_ratio=0.4` (`tests/test_scanner.py::TestEvRankingCap`)

### 21b — Add a moneyness entry filter

- [x] Add `MAX_MONEYNESS_PCT` to `config.py` (e.g. `0.15`)
- [x] In `strategy/scanner.py::_eval_candidate`, reject candidates whose `abs(strike - spot) / spot` exceeds `config.asset_config(asset, "MAX_MONEYNESS_PCT")`
- [x] Confirm the filter is overridable per-asset via the existing `ASSET_OVERRIDES` mechanism
- [x] Add regression tests: a deep-ITM/OTM candidate is rejected; a near-ATM candidate still passes

### 21c — Require two-sided quotes for mark-to-market trust, and debounce close triggers

- [x] Add `MARKET_SV_REQUIRE_TWO_SIDED` (default `True`) and `CLOSE_CONFIRM_TICKS` (default `2`) to `config.py`
- [x] Update `strategy/decision.py::_get_market_spread_value` to return `None` (forcing the existing, logged B-S fallback) when either leg lacks a genuine `bid > 0 and ask > 0` quote, instead of substituting `mark_price`, unless `MARKET_SV_REQUIRE_TWO_SIDED` is `False`
- [x] Add a per-`trade_id` pending-confirmation counter in the decision engine: a stop/TP condition must hold on `CLOSE_CONFIRM_TICKS` consecutive monitor ticks before `_close_position` is called; reset the counter on any tick where the condition doesn't hold
- [x] Add regression tests: a single-tick spurious stop/TP reading does not close the position; the same reading held for `CLOSE_CONFIRM_TICKS` ticks does; a one-sided quote (bid=0 or ask=0) is not trusted as `market_sv`

### 21d — Per-instrument re-entry cooldown

- [x] Add `REENTRY_COOLDOWN_SEC` to `config.py` (default `1800`)
- [x] Record the timestamp of each auto-close (stop or take-profit) per `(asset, strike, option_type)` in `strategy/decision.py`
- [x] Update `strategy/sizer.py::size_candidate` to reject a candidate matching a recently auto-closed `(asset, strike, option_type)` within the cooldown window, alongside the existing correlated-open-position check
- [x] Add a regression test: a candidate matching an instrument auto-closed 5 minutes ago is rejected; the same candidate after the cooldown window has elapsed is accepted

### 21e — Fix `close_status` and backfill historical rows

- [x] Update `db/state.py::close_calendar_trade()` to set `close_status = 'closed'` alongside the fields it already updates
- [x] Add `scratch/scratch_backfill_close_status.py` — one-off script that sets `close_status = 'closed'` for existing rows where `result` is a terminal status but `close_status` is still `'open'`
- [ ] Run the backfill once against `db/calendar_bot.db` to correct the 2026-07-14 backlog — **pending**: the paper DB is not present in this worktree; run `python -m scratch.scratch_backfill_close_status --dry-run` then without `--dry-run` in the deployment/main working tree. Script verified against a synthetic DB.
- [x] Add a regression test asserting `close_calendar_trade()` sets `close_status = 'closed'`
- [x] Add `scratch/scratch_deep_itm_churn.py` — offline demo reproducing the 2026-07-14 failure against the pre-fix logic and showing 21a–21d each independently prevent it

### 21f — Backport Phase 20 config keys into `config_test.py`

- [x] Add the `LOGGING` section keys to `config_test.py`: `LOG_LEVEL`, `LOG_FORMAT`, `LOG_DATE_FORMAT`, `LOG_FILE_MAX_BYTES`, `LOG_BACKUP_COUNT`, `LOG_DIR`, `NOISY_LOGGERS`, `LOG_LEVEL_OVERRIDES`
- [x] Add the network/timeout/retry/alert keys to `config_test.py`: `DERIBIT_WS_PING_INTERVAL`, `DERIBIT_WS_PING_TIMEOUT`, `DERIBIT_WS_OPEN_TIMEOUT`, `DERIBIT_WS_MAX_SIZE`, `RPC_TIMEOUT_SEC`, `ORDER_RETRY_DELAYS`, `ALERT_COOLDOWN_SEC`, `SMTP_TIMEOUT_SEC`, `TELEGRAM_TIMEOUT_SEC`, `SMTP_FROM`
- [x] Add the 6 previously-fake keys (now real in `config.py`) to `config_test.py`: `SLIPPAGE_LIMIT_PCT`, `ORDER_TIMEOUT_SEC`, `MAX_ORDER_RETRIES`, `STUCK_ORDER_TIMEOUT_SEC`, `INITIAL_CAPITAL`, `COLLECTOR_INTERVAL_SEC`
- [x] Add the business-logic magic-number keys to `config_test.py`: `STRIKE_INCREMENT_TABLE`, `STRIKE_INCREMENT_DEFAULT`, `FAR_LEG_SPREAD_TABLE`, `FAR_LEG_SPREAD_DEFAULT`, `FAR_LEG_LIQUIDITY_PENALTY_PER_30D`, `NEAR_DAY_TOLERANCE`, `FAR_DAY_TOLERANCE`, `ROLL_TRIGGER_DAYS`, `POSITION_FAILURE_RETRY_CAP`, `RECONCILE_THRESHOLD_PCT`, `MIN_CONTRACT_SIZE`, `DEFAULT_PORTFOLIO_VALUE`, `EV_SAMPLE_COUNT`, `BREAKEVEN_SCAN_STEPS`, `BREAKEVEN_SCAN_RANGE`, `SPREAD_WARN_PCT`, `STRIKE_CORRELATION_PCT`
- [x] Add the paths/timezone/date-format keys to `config_test.py`: `DB_PATH`, `HISTORIC_DATA_DB_PATH`, `TIMEZONE`, `DATE_FORMAT`
- [x] Set every backported value to match `config.py`'s default exactly unless there is a specific, commented test-mode reason to diverge (consistent with the file's existing header comment documenting its handful of intentional overrides)
- [x] Once 21a–21d land, add their new keys (`EV_SCORE_RANKING_CAP`, `MAX_MONEYNESS_PCT`, `MARKET_SV_REQUIRE_TWO_SIDED`, `CLOSE_CONFIRM_TICKS`, `REENTRY_COOLDOWN_SEC`) to `config_test.py` too, so it launches in sync with `config.py` from the start
- [x] Add a test to `tests/test_config_centralization.py` asserting every key defined in `config.py` is also defined in `config_test.py` (module-level name diff), to catch this drift automatically in future phases
- [x] `MAX_MARGIN_UTILIZATION_PCT`, `MARGIN_GATE_ENABLED`, `MARGIN_GATE_REQUIRED_LIVE` (Phase 17) — although noted as out of scope, these were also added to `config_test.py` so the parity test (`TestConfigTestParity`) is exact rather than carrying a hardcoded exclusion list

## Phase 22 — Stuck-Position Visibility, Silent Telegram Failures, and Close-Order Price Rejections

**Status:** Complete — all seven sub-phases (22a–22g) implemented and tested; 616 tests passing (24 new across `test_state.py`, `test_telegram_cmd.py`, `test_executor.py`, and `test_decision.py`). Offline demos: `python -m scratch.scratch_stuck_position_visibility`, `python -m scratch.scratch_close_price_rounding`, `python -m scratch.scratch_premature_roll` (no live orders). Full root-cause detail in BOT_PLAN.md Phase 22. Found via a test-mode Telegram session (trades #11/#12): `/positions` and `/info trade_id=N` both went silent/empty right after "MANUAL ACTION REQUIRED" alerts, and the same alert fired twice across a service restart. Root causes: (1) `get_open_trades()` (used by Telegram) excludes `close_status='close_stuck'` rows, hiding exactly the positions the operator needs to see; (2) `handle_info` has no `try/except` and `telegram_cmd/listener.py` registers no PTB error handler, so any exception (e.g. a `cost_basis==0` divide-by-zero) is completely silent; (3) the monitor loop's actual read path (`_load_all_open_positions` → `load_calendar_state`) was never updated to exclude `close_stuck` positions — only the Telegram-facing `get_open_trades()` was — so the engine keeps retrying (and eventually re-notifying about) a stuck position forever, and a restart just resets the in-memory notification-dedup set that was papering over it; (4) the underlying close/roll order rejection (`-32602 Invalid params`) was still live: `execution/executor.py` derived close/roll prices from a synthetic `mid * 1.02`/`mid * 0.98` instead of a real quote, and silently swallowed tick-size fetch failures. A fifth item was found mid-phase from a paper-mode report: trades #206/#207 (`near_days=1`) were entered and rolled/closed roughly a minute later — `ROLL_TRIGGER_DAYS=2` made any freshly-opened 1-day-near position immediately roll-eligible with zero regard for actual elapsed time, and `_try_roll()` matched a same-expiry-as-far-leg candidate for trade #207 (no check that the new near leg's expiry precedes the position's own far leg), collapsing its spread value to `$0.00` and tripping a large stop-loss — the same failure family as Phase 21's churn, through the roll path instead of the entry-ranking path.

**Implementation notes:**

- **22a** — `load_calendar_state()`'s `open_positions` list now excludes `close_status == 'close_stuck'`, so the monitor's read path (`_load_all_open_positions`) genuinely leaves stuck positions alone instead of merely hiding them from Telegram.
- **22b** — new `get_visible_positions()` (stuck-inclusive) backs `/positions` and `/portfolio`; stuck rows are prefixed `⚠️ STUCK —` with the `close_error_reason` appended. `get_open_trades()` is unchanged (still excludes stuck) for 22a.
- **22c** — `handle_info` wrapped in `try/except` with an explicit `cost_basis == 0` guard; `listener._build_app()` registers a global `add_error_handler` that replies "Something went wrong processing that command." instead of staying silent.
- **22d** — close/roll prices derived from live best bid/ask crossed by `CLOSE_PRICE_CROSS_BUFFER_PCT` (lift ask to buy back near, hit bid to sell far); tick-size fetch is retried (`TICK_SIZE_FETCH_RETRIES`) and logs loud on failure; `tick_size_steps` honoured via `_effective_tick_size`; rounding done in `Decimal` tick-count space to avoid float drift.
- **22e** — `_mark_stuck_and_notify` checks the DB `close_status` (new `get_close_status()` helper) before notifying, so an already-stuck position is never re-alerted after a restart clears the in-memory dedup set.
- **22f** — the roll trigger additionally requires `near_days_left < near_days`-at-entry (genuine decay); `_try_roll` matches candidates on `c.far_instrument == pos["far_instrument"]` and rejects any near leg not preceding the far leg by `MIN_ROLL_NEAR_FAR_GAP_DAYS`.

### 22a — Stop the monitor from retrying `close_stuck` positions

- [x] Update `db/state.py::load_calendar_state()` (or add a dedicated `get_monitorable_positions()`) so its `open_positions` list excludes `close_status == 'close_stuck'`, matching `get_open_trades()`
- [x] Update `strategy/decision.py::_load_all_open_positions()` to use the corrected/new query
- [x] Add a regression test: once a position is marked `close_stuck`, a subsequent `monitor_tick()` does not attempt to close/roll it again and does not increment `_close_roll_failures` for it

### 22b — Show stuck positions in `/positions` and `/portfolio`, flagged

- [x] Add `db/state.py::get_visible_positions()` (or equivalent) returning `result IN _OPEN_STATUSES` rows regardless of `close_status`
- [x] Update `handle_positions`/`handle_portfolio` in `telegram_cmd/handlers.py` to use it, prefixing stuck rows with a clear marker (e.g. `⚠️ STUCK —`) and including `close_error_reason`
- [x] Confirm `get_open_trades()` itself is unchanged (still excludes stuck positions — needed by 22a's monitor-side fix)
- [x] Add regression tests: `/positions` and `/portfolio` include a flagged stuck position instead of omitting it

### 22c — Harden Telegram handlers against silent failures

- [x] Wrap `handle_info`'s body in `try/except`, replying with an error message on failure (matching `handle_close`/`handle_close_manually`)
- [x] Guard the `cost_basis == 0` case in `handle_info`'s P&L calculation instead of dividing by it
- [x] Register `application.add_error_handler(...)` in `telegram_cmd/listener.py::_build_app()` so any unhandled command-handler exception logs and replies with a generic error message instead of staying silent
- [x] Add regression tests: `/info` on a trade with `cost_basis == 0` replies with a message (not silence); a simulated handler exception triggers the global error handler's reply

### 22d — Fix the close/roll order price rejection at its source

- [x] In `execution/executor.py`, derive `_async_close_spread`'s near/far close prices and `_async_roll_near_leg`'s close price from live best bid/ask (crossing the spread with a small configurable buffer) instead of `mid * 1.02`/`mid * 0.98`
- [x] Add `config.CLOSE_PRICE_CROSS_BUFFER_PCT` for the buffer
- [x] Make tick-size fetch failure loud: log a warning naming the instrument, and either retry once or abort per a new `config` knob, instead of silently falling back to generic 4-decimal rounding
- [x] Read `tick_size_steps` from the instrument metadata (not just the flat `tick_size`) so the correct tick is used for the instrument's current price band
- [x] Round in tick-count integer/`Decimal` space rather than `float` division, so the result can't drift off-grid due to floating-point representation error
- [x] Add regression tests: close/roll prices are derived from bid/ask, not a synthetic mid; a rounded price stays on the correct tick grid across several `tick_size_steps` fixtures; a tick-size fetch failure is logged and handled per the new config knob
- [x] Add `scratch/scratch_close_price_rounding.py` — offline demo reproducing the old synthetic-mid rejection and showing the fix lands on the correct tick grid

### 22e — Persist stuck-position notification state (restart-safe)

- [x] Update `strategy/decision.py::_mark_stuck_and_notify` to check DB `close_status == 'close_stuck'` (not just the in-memory `_notified_stuck` set) before calling `notify_close_stuck`
- [x] Add a regression test: simulating a fresh `DecisionEngine` (empty `_notified_stuck`, as after a restart) against an already-`close_stuck` position does not re-notify

### 22f — Fix the premature roll trigger and degenerate same-expiry roll (trades #206/#207)

- [x] In `strategy/decision.py::_monitor_position`, require `near_days_left < pos.get("near_days", near_days_left)` in addition to `near_days_left <= config.ROLL_TRIGGER_DAYS` before considering a roll, so a freshly-opened short-dated near leg isn't roll-eligible on its first post-entry tick
- [x] In `_try_roll()`, restrict candidate matching to `c.far_instrument == pos["far_instrument"]` instead of `strike`/`option_type` alone across all scanned tenor pairings
- [x] Add `config.MIN_ROLL_NEAR_FAR_GAP_DAYS` (e.g. `1`) and reject (log + return `False`) any roll candidate whose near-leg expiry isn't strictly earlier than the position's far-leg expiry by at least that gap
- [x] Add regression tests: a `near_days=1` position is not roll-eligible on the tick immediately after entry; `_try_roll` rejects a candidate whose near expiry matches or exceeds the position's own far-leg expiry
- [x] Add `scratch/scratch_premature_roll.py` — offline demo reproducing trades #206/#207 against the pre-fix logic and showing both fixes independently prevent it

### 22g — Verification

- [x] Run the full test suite; confirm no regressions in existing Phase 18/20/21 tests around `close_status`, `get_open_trades()`, and roll behaviour
- [x] Update README.md's Known Issues section once implemented
- [x] Update this file's checkboxes as each sub-phase lands

---

## Phase 23 — Feed Freshness Watchdog

**Status:** Complete — watchdog implemented and tested; 622 tests passing (5 new in `tests/test_feed.py::TestFeedFreshnessWatchdog`). Offline demo: `python -m scratch.scratch_feed_watchdog` (no live orders, no network). Full root-cause detail in BOT_PLAN.md Phase 23.

**Root cause:** `DeribitFeed` detects WS drops via TCP/ping failures but cannot detect Deribit silently stopping ticker data pushes while the connection remains technically open. Observed 2026-07-19: the feed last subscribed to 298 BTC + 234 ETH instruments at 07:55 AEST; no reconnect event appeared for the remaining 7+ hours of the session. Every scan from ~08:00 onward logged the same warning — "298 stale instrument(s) excluded from BTC chain / 234 stale instrument(s) excluded from ETH chain" — because all 532 cached snapshots had aged past the 30s TTL within seconds of Deribit stopping its push stream. The scanner received 0 fresh instruments and returned 0 candidates on every tick; the bot stayed in IDLE indefinitely with no log indication the feed was dead. The only recovery was a manual restart. See [BOT_PLAN.md Phase 23](BOT_PLAN.md#phase-23--feed-freshness-watchdog) for full root-cause detail.

### 23a — Watchdog in `DeribitFeed`

- [x] Add `FEED_WATCHDOG_TIMEOUT_SEC` to `config.py` (default `120`; set `0` to disable) — seconds without a ticker update before a reconnect is forced; 4× `CHAIN_CACHE_TTL_SEC` by default gives a buffer above the 30s staleness threshold while still recovering within minutes
- [x] Add `_last_ticker_at: float` to `DeribitFeed.__init__` (set to `time.time()` in `_connect_and_stream()` after subscriptions complete, so a very quiet but live market does not trigger an immediate false positive)
- [x] Update `_handle_message()` — set `_last_ticker_at = time.time()` on every successfully parsed ticker notification
- [x] Add `async def _watchdog(self, ws)` — sleeps `FEED_WATCHDOG_TIMEOUT_SEC / 2` (floored at 1s) between checks; if `time.time() - _last_ticker_at > FEED_WATCHDOG_TIMEOUT_SEC`, logs `WARNING "Feed watchdog: no ticker in {elapsed:.0f}s (> {timeout}s) — forcing reconnect"` then calls `await ws.close()`; the existing reconnect loop in `start()` handles the reconnect and re-subscription
- [x] Launch `_watchdog` as an `asyncio.create_task` alongside the pump task in `_connect_and_stream()`, after `_subscribe_all()` completes (to avoid a race between subscription latency and the first ticker arriving); cancel it in the `finally` block that also cancels the pump task
- [x] Skip watchdog task creation when `FEED_WATCHDOG_TIMEOUT_SEC == 0` (feature flag for environments where Deribit is known to have quiet periods)

### 23b — Tests and scratch

- [x] Add `TestFeedFreshnessWatchdog` to `tests/test_feed.py`
  - [x] No ticker received within `FEED_WATCHDOG_TIMEOUT_SEC` → watchdog closes the WS (verify `ws.close()` called)
  - [x] Ticker received before timeout → watchdog does not close the WS; `_handle_message` updates `_last_ticker_at`
  - [x] Watchdog task is cancelled cleanly when the WS closes for any other reason (pump task finishes first)
  - [x] `FEED_WATCHDOG_TIMEOUT_SEC = 0` → no watchdog task is created (verify `_watchdog` not called)
- [x] Add `scratch/scratch_feed_watchdog.py` — drives the real `DeribitFeed._watchdog` and `start()` reconnect loop against a controllable in-memory transport that delivers a ticker burst then goes silent; verifies the watchdog closes each silent socket and the feed reconnects automatically; aborts if `TRADING_MODE == "live"` (offline, read-only, no live orders)
- [x] Add `FEED_WATCHDOG_TIMEOUT_SEC` to `config_test.py` with value matching `config.py` default (parity requirement from Phase 21f)

---

## Phase 24 — Reconcile Mismatch Remediation for close_stuck Positions

**Status:** Complete — all four sub-phases (24a–24d) implemented and tested; 636 tests passing (18 new across `test_portfolio.py::TestReconcileEnhanced`, `test_telegram_cmd.py::TestHandleDeribitPositions`, and `test_state.py::TestMarkStuckPositionReconciled`). Offline demo: `python -m scratch.scratch_reconcile_mismatch` (read-only, no live orders; aborts in live mode). Full root-cause detail in BOT_PLAN.md Phase 24.

**Implementation notes:**

- **24a** — `portfolio/tracker.py::get_deribit_open_positions(currency)` calls `private/get_positions` (kind=option) and normalises each non-flat position to `{instrument_name, size, mark_price, index_price, mark_value}`, returning `[]` on any failure. `_reconcile()` now names the live instruments on a mismatch (`_describe_deribit_positions()`), replacing the non-actionable "possible manual trade or missed fill" suffix with `Deribit open: <instruments>`.
- **24b** — `portfolio/tracker.py::sync_stuck_positions(db_path)` fetches the live position list once (aborting on any fetch error so an API failure never falsely reconciles), and marks a `close_stuck` DB trade `closed` (via new `db/state.py::mark_stuck_position_reconciled`) only when *both* legs are confirmed absent from Deribit. `refresh()` calls it at the top of the test/live path and recomputes SQLite margin/counts so the mismatch resolves in the same cycle. Existing pnl on terminal-but-stuck rows is preserved.
- **24c** — `telegram_cmd/handlers.py::handle_deribit_positions` lists live Deribit positions grouped by currency, flags any instrument not tracked in the bot DB (`⚠️ Not tracked in bot DB`), and appends a sync hint when stuck DB trades are still open on Deribit. Gated to "Command not available in paper mode." Wired through `TelegramCommandListener(portfolio=…)` (passed from `bot.py`) and added to `COMMAND_REGISTRY`.

**Root cause:** When the bot marks a position `close_stuck` after retry exhaustion (e.g. trades 6–9, `BTC-15JUL26-*` options from 2026-07-14, all with `close_error_reason = "Roll retry limit exceeded close failed after 4 attempts — position needs manual close on Deribit"`), the Deribit position remains open on the exchange. The bot's DB records those positions as closed (with `close_status='close_stuck'`) and excludes their legs from the used-margin calculation (`SQLite margin = $0`), but Deribit's maintenance margin is still tied up. The portfolio tracker detects this divergence on every scan cycle and fires:

```text
RECONCILE MISMATCH: Deribit margin $1534.28 vs SQLite margin $0.00 (divergence 100%) — possible manual trade or missed fill
```

The warning is correct but not actionable: it does not identify *which* Deribit instruments are open, and there is no automated path for the bot to detect when the operator manually closes those positions on the exchange and reconcile the DB accordingly. Log fills with one warning per scan (every 5 minutes) indefinitely, with no path to resolution. See [BOT_PLAN.md Phase 24](BOT_PLAN.md#phase-24--reconcile-mismatch-remediation-for-close_stuck-positions) for full root-cause detail.

### 24a — Enhanced reconcile logging (identify the culprit instruments)

- [x] Add `get_deribit_open_positions(currency) -> list[dict]` to `portfolio/tracker.py` — calls `private/get_positions` (filtered to `kind=option`) for a given currency; returns a list of dicts with `instrument_name`, `size`, `mark_value`, and `index_price`; returns `[]` on any API failure (non-blocking)
- [x] Update `_reconcile()` in `portfolio/tracker.py` — when divergence exceeds `RECONCILE_THRESHOLD_PCT`, call `get_deribit_open_positions` for each configured asset and append the result to the WARNING log line: e.g. `"Deribit open: BTC-15JUL26-64000-C qty=0.1, BTC-15JUL26-64000-P qty=0.1"` so the operator immediately knows what is live on the exchange
- [x] When the enhanced log line lists specific instruments, replace the trailing `"— possible manual trade or missed fill"` suffix with `"— see instruments above"` to reduce noise on successive warnings

### 24b — Auto-reconcile close_stuck positions after manual Deribit close

- [x] Add `sync_stuck_positions(db_path) -> list[int]` to `portfolio/tracker.py` — fetches the live Deribit open position list for each `ASSETS` currency; for each `close_stuck` trade in the DB, checks whether both its `near_instrument` and `far_instrument` are absent from the Deribit list; if both absent (the operator has already closed them on Deribit), calls `db.state.mark_stuck_position_reconciled()` (sets `close_status='closed'`, preserving any recorded pnl) and logs `INFO "trade_id=N auto-reconciled: both legs confirmed closed on Deribit"`; returns the list of `trade_id`s that were reconciled. Aborts (returns `[]`) on any position-fetch error so an API failure never falsely reconciles
- [x] Call `sync_stuck_positions()` at the start of each `refresh()` cycle in `portfolio/tracker.py` — runs every scan, so a manual Deribit close is picked up within `SCAN_INTERVAL_SEC` (5 minutes) without operator intervention
- [x] After reconciliation, re-run the margin comparison so the mismatch warning resolves in the same cycle it is detected, rather than one cycle later

### 24c — `/deribit_positions` Telegram command

- [x] Add `handle_deribit_positions(update, context)` to `telegram_cmd/handlers.py` — calls `get_deribit_open_positions()` for each `ASSETS` currency and formats a per-instrument reply: instrument name, size, index price, mark value; groups by currency
- [x] Cross-reference the Deribit list against `get_visible_positions()` (the stuck-inclusive DB query from Phase 22b) and append a note for any Deribit instrument not tracked in the bot DB: `"⚠️ Not tracked in bot DB — open manually?"`
- [x] Append a summary line: `"N position(s) also shown as ⚠️ STUCK in /positions — use /close or /close_manually to sync."` when stuck trades exist that are still open on Deribit
- [x] Reply `"No positions currently open on Deribit."` when the Deribit list is empty for all assets
- [x] Add `("deribit_positions", "List positions currently open on Deribit — cross-check vs bot DB")` to `COMMAND_REGISTRY` in `telegram_cmd/listener.py`

### 24d — Tests and scratch

- [x] Add `TestReconcileEnhanced` to `tests/test_portfolio.py`
  - [x] When mismatch is detected, `get_deribit_open_positions` is called and instrument names appear in the log
  - [x] `sync_stuck_positions` marks a `close_stuck` trade as `closed` when both legs are absent from the Deribit list
  - [x] `sync_stuck_positions` leaves a `close_stuck` trade unchanged when either leg is still open on Deribit
  - [x] Reconcile warning resolves in the same cycle after `sync_stuck_positions` auto-closes the trade
- [x] Add `TestHandleDeribitPositions` to `tests/test_telegram_cmd.py`
  - [x] Reply lists each Deribit instrument with size and mark value
  - [x] Instruments not tracked in the DB are flagged `"⚠️ Not tracked in bot DB"`
  - [x] Stuck DB trades still open on Deribit trigger the sync summary line
  - [x] `"No positions"` reply when Deribit list is empty
- [x] Add `scratch/scratch_reconcile_mismatch.py` — connects to the paper/test Deribit account, fetches the live position list, compares it against `close_stuck` trades in the DB, and prints a reconciliation report; aborts if `TRADING_MODE == "live"`

---

## Phase 25 — Order-Amount Validity, Sizer/Executor Unification, Close-Fee Accuracy, Test-Liquidity Calibration, and Residual-Margin Reconciliation

**Status:** Planned. From analysis of the 2026-07-17 → 2026-07-22 test-mode run: 81/81 ETH entries rejected by Deribit `-32602 Invalid params` while the 1/1 BTC entry filled (trade 13, at 0.1 instead of the approved 0.3); trade 13's close logged `close_fees=0.00`; zero candidates passed the liquidity gate after 2026-07-21 00:21 (582 near-leg-spread skips on 07-22 alone); and a ~$1,583 `RECONCILE MISMATCH` persists with no `kind=option` position to explain it. See [BOT_PLAN.md Phase 25](BOT_PLAN.md#phase-25--order-amount-validity-sizerexecutor-unification-close-fee-accuracy-test-liquidity-calibration-and-residual-margin-reconciliation) for full root-cause detail.

### 25a — Per-instrument order-amount validation (fixes all-ETH `-32602` rejections)

- [ ] Extend the executor's instrument-metadata cache (already populated from `public/get_instrument` for tick size) to also capture `min_trade_amount` and `contract_size`/amount step
- [ ] Add `_clamp_amount(instrument_name, amount) -> float | None` to `execution/executor.py` — round the amount down to the instrument's step; return `None` if below `min_trade_amount`
- [ ] Call `_clamp_amount` before every `place_order` in the entry, close, roll, and unwind paths; on `None`, abort with `WARNING "AMOUNT GATE: <instr> requested=<x> below exchange minimum <min>"` instead of submitting
- [ ] Fall back to a static per-asset minimum table in `config.py` (BTC: 0.1, ETH: 1) when the metadata fetch fails, and log the fallback loudly
- [ ] Skip undersized candidates at RANK stage in `strategy/decision.py` (log `RANK skip: sized qty below exchange minimum`) so they never reach approval
- [ ] Round the sizer's qty to the instrument step in `strategy/sizer.py` so the approved qty is already submittable

### 25b — Executor honours the sizer-approved qty (trade 13 filled 0.1 vs approved 0.3)

- [ ] Remove the duplicate, dimensionally-wrong sizing in `execution/executor.py::_contract_amount()` (`max_usd / (net_debit_usd * spot)` divides by spot twice, always collapsing to the 0.1 floor)
- [ ] Pass the sizer-approved qty from `strategy/decision.py` through `enter_spread()` as the order amount (then clamped by 25a)
- [ ] Demote `MIN_CONTRACT_SIZE` to a config-level sanity floor only; document it in `config.py`
- [ ] Log the final submitted amount alongside the sizer qty so any residual divergence is visible in one line

### 25c — Accurate close fees (trade 13 logged `close_fees=0.00`)

- [ ] `execution/executor.py::close_spread()` returns actual close fill prices (near and far) alongside the closing credit
- [ ] `strategy/decision.py::_close_position()` computes `exit_fees` from those fill prices; falls back to DB-loaded entry premiums only when fills are unavailable
- [ ] Replace the bare `except → close_fees_usd = 0.0` with a `WARNING` log naming the inputs that failed, so a silent zero-fee close cannot recur
- [ ] Backfill note: net P&L of trade 13 (−25.06) understates real fees — document, do not rewrite history

### 25d — Test-mode liquidity-gate calibration (percentage-only spread gate starves testnet)

- [ ] Add `MAX_LEG_SPREAD_ABS_TICKS` and `MAX_LEG_SPREAD_ABS_USD` to `config.py` (both `0` = disabled → live behaviour unchanged)
- [ ] Gate logic in `strategy/decision.py`: pass if `spread_pct <= MAX_LEG_SPREAD_PCT` **or** `(ask - bid)` is within either enabled absolute floor (a one-tick-wide book must never be rejected as "40% spread")
- [ ] Enable the absolute floor in `config_test.py` with a documented rationale; keep `config.py`/`config_test.py` key parity (Phase 21f regression test)
- [ ] Include which branch passed (pct vs abs) in the LIQUIDITY GATE debug log line

### 25e — Residual-margin reconciliation (~$1,583 with no option positions)

- [ ] `portfolio/tracker.py::get_deribit_open_positions(currency, kind="any")` — cover futures/perpetuals as well as options; normalise per kind defensively
- [ ] Reconcile across all account currencies (via `private/get_account_summaries` or configured superset), not just `ASSETS`
- [ ] Include resting open orders (`private/get_open_orders_by_currency`) — count and reserved margin — in the mismatch warning
- [ ] `/deribit_positions` shows futures and open orders too, each flagged against the bot DB
- [ ] Add `scratch/scratch_account_margin_audit.py` — read-only dump of every position kind, open orders, and per-currency account summaries to identify the current residue; aborts if `TRADING_MODE == "live"`
- [ ] One-time operator action (after the audit identifies the source): manually clear the residual margin on test.deribit.com and confirm the reconcile warning stops

### 25f — Tests and scratch

- [ ] `tests/test_executor.py`: amount clamped to step; below-minimum returns `None` and aborts with AMOUNT GATE log; metadata-fetch failure uses static fallback; sizer qty (not a recomputed amount) reaches `place_order`; `close_spread` returns fill prices
- [ ] `tests/test_decision.py`: RANK skip for undersized candidates; close fees computed from fill prices; fee-calc failure logs WARNING and does not zero silently; abs-floor spread gate passes a one-tick book and stays disabled when configured `0`
- [ ] `tests/test_portfolio.py`: `kind=any` reconcile lists a futures position; open-order margin appears in the warning; option-only fallback on parse failure
- [ ] `tests/test_config_centralization.py`: new keys present in both `config.py` and `config_test.py`
- [ ] Add `scratch/scratch_amount_validation.py` — fetches live BTC/ETH option instrument minimums and demonstrates clamp/skip decisions without placing orders; aborts if `TRADING_MODE == "live"`
