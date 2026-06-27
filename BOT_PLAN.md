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

### 13. Telegram Command Listener *(new — light effort)* [Phases 9a + 9b]

The bot already sends outgoing Telegram notifications via `alerts/notifier.py`. This phase adds incoming command handling so the operator can query and control the bot from their phone via the same Telegram chat.

**Architecture**

`TelegramCommandListener` runs as a fourth asyncio task in `bot.py` alongside the feed, loop, and (optionally) the data collector. It long-polls the Telegram Bot API using `python-telegram-bot` v21 (already in `requirements.txt`). When the bot shuts down, the listener task is cancelled in the existing `finally:` block.

Security: every incoming update checks `update.effective_chat.id == int(config.TELEGRAM_CHAT)`. Messages from any other chat ID are silently dropped — no reply is sent.

**`/stop_bot` and `/start_bot` — pause/resume without process restart**

Rather than killing and restarting the OS process (which would take the listener down with it), these commands add a `paused` flag to `DecisionEngine`:

- `/stop_bot` — sets the flag; `scan_tick()` and `monitor_tick()` return immediately without acting; the feed, portfolio tracker, and listener all remain alive
- `/start_bot` — clears the flag; normal scanning and monitoring resumes
- The listener is never affected by the pause state

**`--drain` CLI flag**

`bot.py` gains a `--drain` argparse flag that sets `config.DRAIN_MODE = True` before the event loop starts — a command-line alternative to setting the env var. The `/start_drain` Telegram command achieves the same effect at runtime on a running process.

**Commands**

| Command | Response |
| --- | --- |
| `/positions` | One line per open trade: `ev=` at start, strike and full type (`Put`/`Call`), expiry range `ddMMMYY→ddMMMYY`, entry cost, current spread value, unrealized PnL |
| `/portfolio` | One line per open trade: asset, strike, expiry range, debit, fees, EV at entry, current spread value, unrealized PnL (no IV or OI) |
| `/new_trades` | Trades entered today AEST — per trade: id, asset, debit, ev, strike, type, expiry range |
| `/close_trades` | Trades closed today AEST — per trade: id, asset, debit, pnl, close reason |
| `/status` | Trading mode, drain/drain-and-new mode, paused state, uptime, open count, today AEST PnL, session PnL since bot start |
| `/stop_bot` | Pauses scan/monitor ticks; feed and listener remain alive |
| `/start_bot` | Resumes scan/monitor ticks |
| `/start_drain` | Sets `DRAIN_MODE = True`; no new entries or rolls; existing positions close at stop/TP/expiry |
| `/start_with_assets BTC,ETH,...` | Override `config.ASSETS` at runtime and resume scanning |
| `/drain_and_new [portfolio=N] [assets=A,B]` | Close existing positions outright (no rolls) but allow new entries; optional portfolio value and asset list override |
| `/help` | Lists every available command with a one-line description |

**`/start_with_assets`**

Sets `config.ASSETS` to the provided comma-separated list, clears drain mode, and resumes the engine if paused. Useful for switching the bot to trade a different asset set without restarting the process.

**`/drain_and_new`**

A hybrid mode distinct from `/start_drain`:

| Behaviour | `/start_drain` | `/drain_and_new` |
| --- | --- | --- |
| New entries | Blocked | Allowed |
| Near-leg rolling | Blocked | Blocked |
| Existing positions | Close at stop/TP/expiry | Close at stop/TP/expiry |
| Portfolio override | No | Optional (`portfolio=N`) |
| Asset list override | No | Optional (`assets=A,B`) |

Sets `config.DRAIN_AND_NEW_MODE = True` and optionally `config.PORTFOLIO_OVERRIDE` and `config.ASSETS`. Clears `DRAIN_MODE`. The `PORTFOLIO_OVERRIDE` bypasses the live PortfolioTracker so sizing uses the specified USD value instead of the live account cash.

**Telegram command menu (`setMyCommands`)**

When a user types `/` in the chat, Telegram shows a suggestion list only if the bot has registered its commands via the `setMyCommands` Bot API method. This registration is stored on Telegram's servers and persists across bot restarts. `listener.py` calls `await app.bot.set_my_commands(COMMAND_REGISTRY)` once during `start()`, pushing the full command list to Telegram automatically — no manual BotFather setup required. The `COMMAND_REGISTRY` is the single source of truth used by both `set_my_commands` and the `/help` handler, so the menu and the help text are always in sync.

**New file:** `telegram_cmd/` package

**Changes to existing files:**

| File | Change |
| --- | --- |
| `bot.py` | Add `--drain` flag; instantiate and start `TelegramCommandListener`; stop it in `finally:` |
| `strategy/decision.py` | Add `pause()` / `resume()` methods and `paused` flag; `scan_tick()` and `monitor_tick()` return immediately when paused |

### 12. Trading Fee Integration *(new — medium effort)*

Fees on Deribit are material relative to calendar spread premiums and must be accounted for at every stage of the trade lifecycle — not just at entry.

#### Deribit fee schedule (options)

| Asset | Taker fee | Maker fee | Minimum fee |
| --- | --- | --- | --- |
| BTC options | 0.03% of index | 0.03% of index | 0.0003 BTC/contract |
| ETH options | 0.03% of index | 0.03% of index | 0.0003 ETH/contract |
| SOL options | 0.03% of index | **0%** | 0.0003 SOL/contract (taker only) |

**Combo/spread order discount:** For taker combo orders, the cheaper leg receives a **100% fee discount** — only the more expensive leg is charged. For maker combo orders, rebates are reduced by 50%.

**Delivery fees** (charged at expiry when an option is ITM and settled):

| Instrument | Delivery fee |
| --- | --- |
| Daily options (1d near leg) | **0%** — no delivery fee |
| Weekly options (7d near leg) | **0%** — no delivery fee |
| All other options (14d, 30d, 45d, 60d) | 0.015% of underlying, capped at 12.5% of option value |

**Fee cap:** No single-leg fee can exceed 12.5% of the option's current market value. This protects against oversized fees on very cheap or deep-OTM options.

#### Fee scenarios for calendar spreads

| Scenario | Legs touched | Approximate fee (BTC at $100k, 1 contract) |
| --- | --- | --- |
| Entry via combo order | 2 (discount on cheap leg) | ~$30–$45 |
| Entry via individual legs | 2 | ~$60 |
| Close at expiry — near OTM | 1 (close far only) | ~$30 |
| Close at expiry — near ITM, daily/weekly | 1 (close far) + 0 delivery | ~$30 |
| Close at expiry — near ITM, monthly | 1 (close far) + delivery on near | ~$45 |
| Roll near leg | 2 (close old near + open new near) | ~$60 additional |
| Early close (stop-loss / take-profit) | 2 | ~$60 |

#### Integration points

**`core/fees.py`** — Central fee calculation module. Functions:

- `leg_fee(asset, spot, qty, is_maker, option_price)` — per-leg fee in USD; applies rate, min floor, and 12.5% cap
- `entry_fees(asset, spot, qty, near_price, far_price, via_combo)` — total entry cost; applies combo cheap-leg discount
- `exit_fees(asset, spot, qty, near_price, far_price)` — total exit cost for closing both legs
- `roll_fees(asset, spot, qty, near_price, new_near_price)` — cost to close old near + open new near
- `delivery_fee(asset, spot, qty, option_price, expiry_days)` — 0 for daily/weekly, else 0.015% capped
- `round_trip_fees(asset, spot, qty, near_price, far_price, via_combo)` — entry + exit combined; used in EV

**`config.py`** — New fee constants:

```python
OPTIONS_FEE_PCT           = 0.0003   # 0.03% — taker/maker rate per leg (BTC, ETH)
OPTIONS_MIN_FEE_BTC       = 0.0003   # minimum fee in BTC per contract
OPTIONS_MIN_FEE_ETH       = 0.0003   # minimum fee in ETH per contract
OPTIONS_MIN_FEE_SOL       = 0.0003   # minimum fee in SOL per contract (taker)
SOL_MAKER_FEE_PCT         = 0.0      # SOL options maker fee is zero
OPTIONS_DELIVERY_FEE_PCT  = 0.00015  # 0.015% delivery fee for monthly+ options
OPTIONS_DELIVERY_FEE_CAP  = 0.125    # cap at 12.5% of option value
COMBO_CHEAP_LEG_DISCOUNT  = 1.0      # 100% taker discount on cheaper combo leg
```

**`strategy/scanner.py`** — Deduct `round_trip_fees` from EV before comparing to `MIN_EV`. Candidates that are profitable before fees but negative after fees are rejected at scan time.

**`strategy/sizer.py`** — True max-loss = `net_debit × qty + entry_fees + exit_fees`. Sizing enforces this against `available_cash × MAX_LOSS_PCT`, not just the raw debit.

**`strategy/decision.py`** — Three fee-aware changes:

1. Entry gate: reject if `net_debit × qty + entry_fees > available_cash × MAX_LOSS_PCT`
2. Roll gate: compute `roll_fees`; only roll if estimated theta gain exceeds roll cost; close instead if uneconomic
3. P&L reporting: log fee-inclusive net P&L at every stop, TP, and expiry close

**`execution/executor.py`** — Paper dry-run path deducts simulated fees using the same `fees.py` functions as test/live. Paper P&L must match real economics so paper trading results are meaningful.

**`monitor/loop.py`** — Report `fees_paid_today` alongside unrealized P&L in every cycle log.

**`portfolio/tracker.py`** — Track `fees_paid_today` and `fees_paid_total`; include in `portfolio_view()`.

**`backtest/engine.py`** — Apply fees at every simulated event (entry, roll, exit, delivery). Add `total_fees` to backtest summary output so each vol regime report shows the true fee drag.

#### Why this matters

At BTC = $100,000, each calendar spread entry costs ~$30–$60 in fees — potentially 10–30% of the net debit on a tight spread. A stop-loss at 50% of debit loses $100 gross but $160 net after round-trip fees. Without fee modelling, EV scores are overstated, position sizing is too aggressive, and roll decisions can destroy value (the roll-loop bug that fired 207 times would have been commercially fatal in a live account).

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
│   └── notifier.py         # email / Telegram notifications (outgoing)
├── telegram_cmd/
│   ├── __init__.py
│   ├── listener.py         # TelegramCommandListener — long-poll loop, start/stop
│   └── handlers.py         # per-command handler functions
├── tests/
│   ├── test_pricing.py
│   ├── test_scanner.py
│   ├── test_decision.py
│   ├── test_executor.py
│   ├── test_backtest.py
│   ├── test_portfolio.py   # new
│   └── test_telegram_cmd.py # new
├── scratch/
│   ├── scratch_scan.py
│   ├── scratch_decision.py
│   ├── scratch_loop.py
│   ├── scratch_notifier.py
│   ├── scratch_backtest.py
│   ├── scratch_three_fixes.py
│   ├── scratch_two_fixes.py
│   ├── scratch_notify_live.py  # new — sends real test alert
│   ├── scratch_portfolio.py    # new — prints live portfolio snapshot
│   └── scratch_telegram_cmd.py # new — starts listener, fires test commands
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
| Unauthorized bot control via Telegram | Every incoming update is checked against `config.TELEGRAM_CHAT`; messages from any other chat ID are silently dropped; `TELEGRAM_TOKEN` must be kept secret |
| Wrong environment | Startup banner clearly states PAPER / TEST / LIVE; bot refuses to start in live mode without DAILY_LOSS_LIMIT; scratch scripts abort if TRADING_MODE == "live" |
| Fee drag erodes profitability | All EV scores deducted for round-trip fees before entry; sizer includes fees in max-loss; paper mode simulates fees identically to live |
| Roll loop accumulating fees | Roll gate checks that theta gain exceeds `roll_fees` before proceeding; uneconomic rolls close the position instead |
| Backtest overstates returns | `backtest/engine.py` applies entry, roll, exit, and delivery fees to every trade; summary reports include `total_fees` per regime |

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

# Fee model (Deribit schedule — do not change without verifying against support.deribit.com/hc/en-us/articles/25944746248989)
OPTIONS_FEE_PCT           = 0.0003   # 0.03% per leg per trade (BTC and ETH options, taker and maker)
OPTIONS_MIN_FEE_BTC       = 0.0003   # minimum fee in BTC per contract
OPTIONS_MIN_FEE_ETH       = 0.0003   # minimum fee in ETH per contract
OPTIONS_MIN_FEE_SOL       = 0.0003   # minimum fee in SOL per contract (taker only)
SOL_MAKER_FEE_PCT         = 0.0      # SOL options maker fee is zero
OPTIONS_DELIVERY_FEE_PCT  = 0.00015  # 0.015% of underlying at expiry for monthly+ options
OPTIONS_DELIVERY_FEE_CAP  = 0.125    # cap: delivery fee never exceeds 12.5% of option value
COMBO_CHEAP_LEG_DISCOUNT  = 1.0      # taker combo orders: 100% discount on the cheaper leg
# No delivery fee for daily (1d) or weekly (7d) options — only monthly and longer

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
| **Trading fee integration** | **1–2 days** | **Done** |
| **Telegram command listener (9a)** | **0.5–1 day** | **Done** |
| **Telegram command menu + /help (9b)** | **0.5 day** | **Done** |
| **Telegram command improvements (9c)** | **0.5 day** | **Done** |
| Testing + paper trading validation | 3–5 days | Not started |
| **Total remaining** | **~4–6 days** | |
