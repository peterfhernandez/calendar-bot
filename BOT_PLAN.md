# Calendar Spread Bot — Implementation Plan

## Overview

An automated trading bot that systematically scans, enters, monitors, and closes calendar spread positions on Deribit (crypto options exchange), using the optionsStrat repo as its foundation.

---

## What We Already Have (Reusable from optionsStrat)

| Module | Source | What it provides |
| --- | --- | --- |
| Pricing math | `market/pricing.py` | Black-Scholes, breakevens, prob-of-profit |
| Calendar logic | `strategies/calendar.py` | Spread valuation, stop/TP evaluation, P&L at expiry |
| Fee model | `trading/fee_calculator.py` | Per-leg fee estimates |
| Broker client | `access.py` | Deribit API wrapper (paper + live) |
| Order execution | `trading/executor.py` | Enter/roll/close trades |
| Monitor loop | `automation/monitor.py`, `automator.py` | Polling and status checks |
| Database | `database/calendar_db.py` | SQLite state persistence |

Roughly 60–70% of the non-trivial logic is already there.

---

## What Needs to Be Built

### 1. Scanner / Opportunity Ranker *(medium effort)*

Scores calendar setups across assets and strikes. Key signals:

- IV term structure (front-month IV vs back-month IV) — prefer high contango
- Days-to-expiry matching (1d/7d, 1d/14d, 7d/30d, 14d/45d, etc.)
- Probability of profit derived from breakeven finding
- Expected value = P(profit) × avg_win − P(loss) × max_loss
- Liquidity filter: both legs must meet minimum bid/ask size and OI thresholds (see §8)

### 2. Market Data Feed *(medium effort)*

Replaces the current interactive spot/IV input with a live polling loop:

- Fetches real-time spot, bid/ask, and IV per instrument from Deribit WebSocket API
- Caches option chains with configurable refresh cadence (e.g. 30s during active hours)
- Detects regime shifts (IV spike, liquidity gaps) to pause trading

### 3. Risk / Position Sizing Engine *(medium effort)*

Replaces the static `BUDGET_USD / spot` quantity with:

- Per-trade max loss as % of **available cash** (not total portfolio notional)
- Maximum concurrent positions (especially if multi-asset)
- Correlation limits (e.g. avoid BTC + ETH calendars at the same strike simultaneously)
- Reads available cash from the Portfolio module (§9); refuses to enter if cash is insufficient

### 4. Decision Engine / State Machine *(medium effort)*

Replaces interactive menus with a rule-based engine:

```text
SCAN → RANK → VALIDATE → ENTER → MONITOR → { ROLL | CLOSE }
```

Stop/TP conditions already exist in `check_calendar_status` — need to be called autonomously and acted on without human input.

Liquidity gate (applied at VALIDATE, before entry):

- Both near and far legs must have `bid_size >= MIN_LEG_BID_SIZE` and `ask_size >= MIN_LEG_ASK_SIZE`
- Both legs must have `open_interest >= MIN_OI_NEAR / MIN_OI_FAR`
- Bid/ask spread on each leg must be `<= MAX_LEG_SPREAD_PCT` of mid — wide spreads indicate illiquidity and inflate entry cost
- Any candidate failing the liquidity gate is skipped and logged; not retried until the next scan cycle

### 5. Execution Hardening — Combo vs Individual Legs *(medium–hard)*

#### Decision: use combo orders as the primary execution method

Deribit supports **combo orders** (also called spread orders) that submit both legs atomically at a net debit/credit price. The platform matches the spread as a unit, so either both legs fill or neither does.

**Why combo orders are strongly preferred for calendar spreads:**

| Factor | Combo order | Individual legs |
| --- | --- | --- |
| Leg risk | None — both fill or neither | Real — near leg may fill while far leg does not, leaving naked short exposure |
| Slippage | One spread mid to track | Two separate mids; errors accumulate |
| Fill logic | Exchange matches spread book | Must re-price and retry the second leg after the first fills |
| Complexity | Simple lifecycle | Requires leg-pairing state, partial fill handling, and a cancel-and-unwind path |

**When individual legs might be considered:**

Crypto option books on Deribit are occasionally thin on the combo book (fewer market makers quote calendar spreads directly). In that case:

- The combo may show a wider effective spread than entering legs individually at their own bids/asks
- Individual legs allow price improvement on each leg independently

**Conclusion:** Always attempt combo first. If the combo does not fill within `COMBO_FILL_TIMEOUT_SEC` and both individual legs have sufficient liquidity (bid and ask sizes both ≥ `MIN_LEG_BID_SIZE`), fall back to sequential individual legs. The fallback must cancel the near-leg order immediately if the far-leg order fails, to prevent naked exposure. This fallback should be logged as a warning and never used if liquidity is thin on either leg.

Implementation:

- Submit both legs as a combo/spread order to eliminate leg risk
- Retry and fill-detection logic
- Slippage bounds — reject if fill price > `MAX_SLIPPAGE_PCT`% from mid
- Order lifecycle tracking: open → partial fill → filled → cancelled
- Individual-leg fallback with mandatory unwind on partial fill failure

### 6. Scheduling / Reliability *(light effort)*

- Cron or event loop (asyncio or APScheduler) to drive scan/monitor cycles
- Reconnect logic for Deribit WebSocket drops
- Alerts (email/Telegram) on errors or large P&L moves

### 7. Backtesting Harness *(optional but strongly recommended)*

Replay historical option chain snapshots through the scanner + decision engine before going live. No historical data handling exists in the current repo.

### 8. Liquidity Filtering *(new — light effort)*

Liquidity is evaluated at two points: scanner (initial filter) and decision engine (final gate before entry). Two-stage filtering prevents wasted API calls on illiquid strikes.

**Scanner stage (coarse filter):**

- `open_interest >= MIN_OI_NEAR` and `open_interest >= MIN_OI_FAR`
- Both legs must have a non-zero bid and ask in the cache

**Decision gate (fine filter, applied just before order submission):**

- `bid_size >= MIN_LEG_BID_SIZE` — ensures there is actual size to hit
- `ask_size >= MIN_LEG_ASK_SIZE` — ensures there is actual size to lift
- `(ask - bid) / mid <= MAX_LEG_SPREAD_PCT` — wide-spread legs inflate cost and signal thin books
- Both legs must pass; failing one leg fails the whole calendar

Config parameters (to add to `config.py`):

```python
MIN_LEG_BID_SIZE    = 1      # minimum bid size (contracts) per leg
MIN_LEG_ASK_SIZE    = 1      # minimum ask size (contracts) per leg
MAX_LEG_SPREAD_PCT  = 0.15   # reject if bid/ask spread > 15% of mid on either leg
COMBO_FILL_TIMEOUT_SEC = 30  # seconds to wait for combo fill before fallback
```

### 9. Portfolio Tracker *(new — medium effort)*

Provides a real-time view of account state and feeds available capital into the sizing engine.

**What it tracks:**

- **Cash balance** — USD equivalent available on Deribit (equity minus margin in use)
- **Used margin** — sum of net debits paid for open positions (the max-loss amount at risk)
- **Unrealized P&L** — MTM gain/loss on open positions
- **Realized P&L today** — closed-trade P&L since midnight UTC
- **Open positions** — list of active calendar spreads with entry cost, current value, and status

**How it works:**

- On startup and after every position change, fetches account summary from Deribit REST API (`/private/get_account_summary`)
- Reconciles reported equity against the SQLite position table to detect any discrepancy
- `available_cash = equity - used_margin` is passed to `sizer.py` so that sizing is always based on real deployable capital, not a static config budget
- A `portfolio_view()` method returns a formatted snapshot for logging or the terminal dashboard

**Integration points:**

- `strategy/sizer.py` — receives `available_cash` from portfolio; replaces the static `BUDGET_USD` approach
- `strategy/decision.py` — checks `available_cash > min_trade_cost` before entering; skips if cash is too low
- `monitor/loop.py` — logs a portfolio snapshot at each scan cycle
- `data/debug_viewer.py` — can display portfolio state in the live terminal dashboard

**New file:** `portfolio/tracker.py`

### 10. Notification Wiring *(new — light effort)*

The `alerts/notifier.py` module is implemented but not wired into the live execution path. Notifications need to be connected at every decision point.

**Where notifications must fire:**

| Event | Notifier call |
| --- | --- |
| Position entered (scan_tick) | `notify_entry(trade)` |
| Stop-loss triggered (monitor_tick) | `notify_stop(trade, pnl)` |
| Take-profit triggered (monitor_tick) | `notify_take_profit(trade, pnl)` |
| Near-leg rolled (monitor_tick) | `notify_roll(trade)` |
| Position closed at expiry (monitor_tick) | `notify_close(trade, pnl)` |
| Daily loss limit breached | `notify_daily_limit(daily_pnl)` |
| Bot error / exception | `notify_error(exc)` |
| Combo fill timeout, fallback used | `notify_warning(msg)` |

**Configuration** (already in `config.py`, verify all are present):

- `ALERT_EMAIL` — recipient address
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

**Verification steps:**

- Add a `scratch/scratch_notify_live.py` that sends a test alert via the real SMTP/Telegram config
- Add a startup self-test in `bot.py` that sends a "Bot started" notification; if it fails, log a warning but do not abort

### 11. Trading Mode — Paper, Test, and Live *(new — light effort)*

The bot supports three operational modes selected by `TRADING_MODE` in `config.py`:

| Mode | Data feed | Order execution | Real money? | API keys |
| --- | --- | --- | --- | --- |
| `"paper"` | test.deribit.com | Dry-run only (no orders sent) | No | `DERIBIT_TEST_CLIENT_ID` / `SECRET` |
| `"test"` | test.deribit.com | Orders placed on test.deribit.com | No | `DERIBIT_TEST_CLIENT_ID` / `SECRET` |
| `"live"` | <www.deribit.com> | Orders placed on <www.deribit.com> | **Yes** | `DERIBIT_LIVE_CLIENT_ID` / `SECRET` |

**Paper mode** is the default and safest starting point. It connects to the test exchange to get real market structure and pricing, but the executor runs in dry-run mode — orders are logged and simulated locally without ever being sent to Deribit. This is the mode used by all scratch scripts and backtesting.

**Test mode** uses the same test exchange but actually submits orders. Use this to verify the full order lifecycle (combo submission, fill detection, order manager reconciliation) before risking real capital.

**Live mode** connects to the production exchange and places real orders with real money.

**Implementation requirements:**

- `data/deribit_feed.py` reads `DERIBIT_WS_URL` from config; the URL is the same for `"paper"` and `"test"` (test exchange) and different for `"live"`
- `execution/executor.py` checks `TRADING_MODE`:
  - `"paper"` → dry-run path (log the order, return a simulated fill, never call the API)
  - `"test"` or `"live"` → real order submission path, using the appropriate REST/WS URL
- `config.py` exposes `DERIBIT_WS_URL` and `DERIBIT_REST_URL` as derived constants so no other module hard-codes a URL
- On startup, `bot.py` prints a prominent banner identifying the active mode:
  - `*** PAPER MODE — data from test.deribit.com, no orders placed ***`
  - `*** TEST MODE — orders will be placed on test.deribit.com ***`
  - `*** LIVE MODE — REAL MONEY on www.deribit.com ***`
- `bot.py` refuses to start in `"live"` mode if `DAILY_LOSS_LIMIT` is not set to a positive value
- Scratch scripts (`scratch_*.py`) check `TRADING_MODE` and abort if it is `"live"` — scratch scripts must never touch the live exchange

**Separate API keys:**

Test and live accounts are entirely separate on Deribit. Both key pairs are stored in `.env` (never committed):

```python
DERIBIT_TEST_CLIENT_ID=...
DERIBIT_TEST_CLIENT_SECRET=...
DERIBIT_LIVE_CLIENT_ID=...
DERIBIT_LIVE_CLIENT_SECRET=...
```

`config.py` selects the right pair: test keys for `"paper"` and `"test"` modes; live keys for `"live"` mode.

---

## Project Scaffolding

### Repository Layout

```text
calendar-bot/
├── core/
│   ├── __init__.py
│   ├── pricing.py          # ported from optionsStrat/market/pricing.py
│   ├── calendar_engine.py  # ported from optionsStrat/strategies/calendar.py
│   └── fees.py             # ported from optionsStrat/trading/fee_calculator.py
├── data/
│   ├── __init__.py
│   ├── deribit_feed.py     # Deribit WebSocket live feed (new)
│   └── chain_cache.py      # option chain cache with TTL (new)
├── strategy/
│   ├── __init__.py
│   ├── scanner.py          # opportunity ranker (new, builds on existing scanner)
│   ├── sizer.py            # position sizing (new)
│   └── decision.py         # state machine: SCAN→RANK→ENTER→MONITOR→CLOSE (new)
├── execution/
│   ├── __init__.py
│   ├── executor.py         # hardened port from optionsStrat/trading/executor.py
│   └── order_manager.py    # fill tracking and lifecycle management (new)
├── monitor/
│   ├── __init__.py
│   └── loop.py             # ported + extended from optionsStrat/automation/monitor.py
├── db/
│   ├── __init__.py
│   └── state.py            # ported from optionsStrat/database/calendar_db.py
├── portfolio/
│   ├── __init__.py
│   └── tracker.py          # account equity, cash, position reconciliation (new)
├── backtest/
│   ├── __init__.py
│   ├── loader.py           # historical chain data ingestion
│   └── engine.py           # replay engine
├── alerts/
│   ├── __init__.py
│   └── notifier.py         # email / Telegram notifications
├── tests/
│   ├── test_pricing.py
│   ├── test_scanner.py
│   ├── test_decision.py
│   ├── test_executor.py
│   ├── test_backtest.py
│   └── test_portfolio.py   # new
├── scratch/
│   ├── scratch_scan.py
│   ├── scratch_decision.py
│   ├── scratch_loop.py
│   ├── scratch_notifier.py
│   ├── scratch_backtest.py
│   ├── scratch_three_fixes.py
│   ├── scratch_two_fixes.py
│   ├── scratch_notify_live.py  # new — sends real test alert
│   └── scratch_portfolio.py    # new — prints live portfolio snapshot
├── config.py               # all tuneable parameters (thresholds, assets, sizing)
├── bot.py                  # entry point / scheduler
├── requirements.txt
└── README.md
```

### Key Dependencies

| Package | Purpose |
| --- | --- |
| `websockets` / `aiohttp` | Deribit WebSocket feed |
| `apscheduler` | Scan/monitor scheduling |
| `scipy` | Black-Scholes and numerical solvers |
| `numpy` | Array maths for breakeven scan |
| `sqlite3` | State persistence (stdlib) |
| `smtplib` / `python-telegram-bot` | Alerts |
| `pytest` | Test suite |

---

## Biggest Risks

| Risk | Mitigation |
| --- | --- |
| Leg risk on entry (one leg fills, other doesn't) | Use Deribit combo orders; individual-leg fallback only when both legs are liquid and cancels near leg immediately on far-leg failure |
| IV collapse after entry | Check IV term structure before entering; set max IV drop stop |
| Liquidity gaps on crypto calendars | Two-stage liquidity filter: OI in scanner, bid/ask size + spread in decision gate |
| Overfitting scanner to recent market | Backtest across at least 2 vol regimes |
| Position stuck as "far leg only" (illiquid) | Already modeled in optionsStrat — good foundation |
| WebSocket disconnection mid-trade | Reconnect with state reconciliation against Deribit REST API |
| Runaway losses in volatile market | Hard daily loss limit; halt + alert if breached |
| Cash over-commitment | Portfolio tracker enforces `available_cash` check before every entry |
| Silent notification failures | Startup self-test notification; warning logged but bot continues |
| Wrong environment | Startup banner clearly states PAPER / TEST / LIVE; bot refuses to start in live mode without DAILY_LOSS_LIMIT; scratch scripts abort if TRADING_MODE == "live" |

---

## Decision Engine State Machine

```text
┌─────────┐
│  IDLE   │◄──────────────────────────────────────────────┐
└────┬────┘                                               │
     │ scheduler tick                                     │
     ▼                                                    │
┌─────────┐   no opportunities    ┌──────────────┐        │
│  SCAN   │──────────────────────►│  WAIT (idle) │────────┘
└────┬────┘                       └──────────────┘
     │ candidates found
     ▼
┌──────────────┐  fails liquidity / risk / cash  ┌──────────────┐
│  RANK+GATE   │────────────────────────────────►│  SKIP trade  │
└──────┬───────┘                                  └──────────────┘
       │ approved
       ▼
┌─────────────────────────────────────┐
│  ENTER                              │
│  1. Try combo order                 │
│  2. On timeout → individual legs    │
│     (cancel near if far fails)      │
└──────┬──────────────────────────────┘
       │ filled (either path)   order rejected ──► LOG & RETRY
       ▼
┌──────────────┐
│   MONITOR    │◄────────────────────────────────┐
└──────┬───────┘                                  │
       │                                          │
  ┌────┴──────┐                                   │
  │           │                                   │
  ▼           ▼                                   │
STOP/TP    Near expiry                            │
triggered  approaching                            │
  │           │                                   │
  │     ┌─────┴─────┐                             │
  │     │           │                             │
  │   ROLL       CLOSE                            │
  │   near leg   both legs                        │
  │     │           │                             │
  │     └─────┬─────┘                             │
  │           │ if rolling                        │
  │           └───────────────────────────────────┘
  │
  ▼
CLOSE → log result → notify → IDLE
```

---

## Configuration Parameters (config.py)

```python
# Assets the bot will trade (scanner, decision engine, execution)
ASSETS = ["BTC", "ETH"]

# Assets the data collector will gather option-chain snapshots for.
# Can be a superset of ASSETS — useful for collecting data on assets
# (e.g. SOL) that you want to analyse or backtest without trading them yet.
COLLECTOR_ASSETS = ["BTC", "ETH", "SOL"]

# Calendar horizons — near/far day pairs to scan
# Near legs: 1d, 7d, 14d  |  Far legs: 7d, 14d, 30d, 45d, 60d
# Valid combos only (near < far):
NEAR_DAYS_OPTIONS = [1, 7, 14]
FAR_DAYS_OPTIONS  = [7, 14, 30, 45, 60]
# Note: 1d near is only paired with 7d and 14d far (not same-day)

# Entry filters
MIN_IV_CONTANGO   = 0.02    # front IV must be >= back IV + 2%
MIN_POP           = 0.45    # minimum probability of profit
MIN_OI_NEAR       = 100     # minimum open interest on near leg
MIN_OI_FAR        = 100     # minimum open interest on far leg
MIN_EV            = 0.0     # reject candidates with negative expected value

# Liquidity gate (applied just before order submission)
MIN_LEG_BID_SIZE    = 1      # minimum bid size (contracts) per leg
MIN_LEG_ASK_SIZE    = 1      # minimum ask size (contracts) per leg
MAX_LEG_SPREAD_PCT  = 0.15   # reject if (ask-bid)/mid > 15% on either leg
COMBO_FILL_TIMEOUT_SEC = 30  # wait for combo fill before individual-leg fallback

# Sizing
MAX_LOSS_PCT      = 0.02    # max 2% of available cash per trade
MAX_POSITIONS     = 3       # max concurrent open calendars

# Stop / take-profit
STOP_PCT          = 0.50    # close if spread worth < 50% of debit
TAKE_PROFIT_PCT   = 1.50    # close if spread worth > 150% of debit

# Scheduler
SCAN_INTERVAL_SEC    = 300   # 5 minutes
MONITOR_INTERVAL_SEC = 60    # 1 minute

# Trading mode:
#   "paper" → data from test.deribit.com, dry-run execution (no orders sent)
#   "test"  → data from test.deribit.com, orders placed on test.deribit.com
#   "live"  → data from www.deribit.com,  orders placed on www.deribit.com (real money)
TRADING_MODE  = "paper"

# Derived URLs — do not hard-code these elsewhere
_LIVE = TRADING_MODE == "live"
DERIBIT_WS_URL   = "wss://www.deribit.com/ws/api/v2"  if _LIVE else "wss://test.deribit.com/ws/api/v2"
DERIBIT_REST_URL = "https://www.deribit.com"           if _LIVE else "https://test.deribit.com"

DAILY_LOSS_LIMIT  = 500      # USD — halt bot if exceeded (required for live mode)

# Alerts (set in .env, referenced here for documentation)
# ALERT_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#
# API keys (set in .env — never commit)
# Paper and test modes share test-exchange credentials:
# DERIBIT_TEST_CLIENT_ID, DERIBIT_TEST_CLIENT_SECRET
# Live mode uses production credentials:
# DERIBIT_LIVE_CLIENT_ID, DERIBIT_LIVE_CLIENT_SECRET
```

---

## Estimated Effort (remaining)

| Layer | Effort | Status |
| --- | --- | --- |
| Port + clean core modules | 1–2 days | Done |
| Live WebSocket data feed | 3–5 days | Done |
| Scanner / ranker | 2–3 days | Done |
| Decision state machine | 2–3 days | Done |
| Execution hardening | 3–5 days | Done |
| Scheduling + alerts | 1–2 days | Done |
| Backtesting harness | 3–5 days | Done |
| **Portfolio tracker** | **1–2 days** | **Done** |
| **Liquidity gate** | **0.5–1 day** | **Done** |
| **Combo order support + fallback** | **1–2 days** | **Done** |
| **1d near-leg horizon** | **0.5 day** | **Done** |
| **Notification wiring** | **0.5–1 day** | **Done** |
| **test.deribit.com wiring** | **0.5 day** | **Done** |
| Testing + paper trading validation | 3–5 days | Not started |
| **Total remaining** | **~8–14 days** | |
