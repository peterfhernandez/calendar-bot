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
| `/info trade_id=N` | Check current position status on Deribit with live bid/ask prices and unrealized P&L |
| `/close trade_id=N` | Retry closing a stuck position (resets the close_stuck flag so bot tries again on next monitor tick) |
| `/close_manually trade_id=N spread=VALUE` | Manually close a stuck position with a user-provided spread value when automatic close fails |
| `/pnl` | Equity curve chart (PNG): cumulative realized P&L (black line) + current unrealized P&L (dotted green) |
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
| Leg risk on close (one leg fills, other times out) | Close operations unwind partial fills: if near fills but far times out, reverse-sell near at market; if far fills but near times out, reverse-buy far at market. Deribit API errors on close are caught and retried up to 3 times; on 4th failure, position is marked `close_stuck` with a single operator alert and excluded from monitoring until manually cleared (Phase 19) |
| Unbounded retry on failed close/roll (naked leg accumulation) | Track failed close/roll attempts per position in `_close_roll_failures` dict; cap at 3 retries, then mark `close_stuck` on the 4th failure with a single operator alert. Fixed in Phase 18 (`get_open_trades()` excludes `close_stuck` positions from routine monitoring) and Phase 19 (retry ladder restored after a regression; counter cleared on mark-stuck and on `/close` so operator retries start fresh). |
| Close order rejected by exchange (`-32602 Invalid params`) | Fixed in Phase 18: `execution/executor.py` fetches and caches each instrument's tick size and rounds every submitted order price to a valid tick (previously a blanket 4-decimal round caused 636 `-32602 Invalid params` rejections, 100% on far-leg close submissions). |
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
| Roll profit not captured in final P&L | DB tracks `roll_pnl` for each position; `_close_position()` includes roll profit in final net P&L calculation; `/positions` and `/portfolio` display roll P&L separately; new near-leg candidates validated with liquidity gate and EV recalculated at roll time |
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

### 14. Telegram UX Polish and Reliability *(Phase 10)*

Incremental improvements to the Telegram command interface discovered during paper trading operation.

#### `/positions` — single-line format with `ev=` at end

Each open trade is formatted on a single line for easy scanning in Telegram:

```
#28 BTC 55000 Put  10Jul26→28Aug26   entry=$161.43  sv=$160.84  PnL=$-0.59   ev=N/A
```

- `ev=N/A` is shown for trades that predate EV tracking (the `ev_score` column was added mid-run; existing rows have `ev_score=0.0` as the default sentinel).

#### EV tracking — `ev_score` column

A new `ev_score REAL NOT NULL DEFAULT 0.0` column was added to `calendar_trades` via a backward-compatible `ALTER TABLE … ADD COLUMN` migration. `DecisionEngine` passes `candidate.ev_score` to `create_calendar_trade` at entry so the EV at entry is permanently recorded. Handlers display it via `_fmt_ev(ev_score)` which returns `"N/A"` for the `0.0` sentinel.

#### `/new_trades` and `/closed_trades` — `[today|session]` option

Both commands accept an optional mode argument:

- `/new_trades` or `/new_trades today` — trades opened since AEST midnight
- `/new_trades session` — trades opened since the bot process started
- `/closed_trades` and `/closed_trades session` — same for closed trades

Uses `get_trades_opened_since(engine.start_time)` / `get_trades_closed_since(engine.start_time)` helpers in `db/state.py`.

#### Shutdown `ConnectTimeout` fix

`python-telegram-bot` v21 makes a final `getUpdates` call during the shutdown cleanup pass. With the default 30 s read timeout this produced a noisy `ConnectTimeout` warning in the logs on every bot restart. Fixed by setting `get_updates_connect_timeout=5.0` and `get_updates_read_timeout=5.0` on `ApplicationBuilder` — the cleanup call times out quickly instead of hanging.

### 15. Log Hygiene — Telegram Noise and Secret Redaction *(Phase 9d)*

Two operational issues discovered during paper trading:

1. **`getUpdates` log spam** — `python-telegram-bot` long-polls the Telegram Bot API every few seconds. The `httpx` library logs each HTTP request at INFO level, producing thousands of identical lines per day like:
   ```
   2026-06-28 09:14:25 [INFO] httpx: HTTP Request: POST https://api.telegram.org/bot<TOKEN>/getUpdates "HTTP/1.1 200 OK"
   ```
   These lines are operationally useless and crowd out meaningful log entries.

2. **Token exposed in logs** — the URL logged by httpx contains the literal Telegram bot token, which is a credential. Anyone with access to the log file can use it to impersonate the bot.

#### Fix — `configure_logging()` in `monitor/loop.py`

**Silence noisy loggers:**

```python
for noisy in ("httpx", "httpcore", "telegram.ext.Updater",
              "telegram.vendor.ptb_urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
```

This suppresses all INFO/DEBUG output from these libraries. Real errors (4xx, 5xx, connection failures) log at WARNING or higher and are still visible.

**`_SecretRedactor` log filter:**

A `logging.Filter` subclass is installed on the root logger so it covers both the console and the rotating-file handler. It reads `TELEGRAM_TOKEN` and `TELEGRAM_CHAT` once at startup and replaces any occurrence of those literal strings in a log record with `<redacted>` before the record reaches any handler.

This acts as a belt-and-suspenders safety net: even if a future library or a new code path logs a URL or message containing the token, it is scrubbed before hitting disk or the terminal.

The filter never raises — if the config import fails for any reason, the filter is skipped silently so that logging setup cannot crash the bot.

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
| **Offline error tracking (10)** | **0.5 day** | **Done** |
| **Telegram UX polish + reliability (10)** | **0.5 day** | **Done** |
| **Log hygiene — noise + secret redaction (9d)** | **< 0.5 day** | **Done** |
| **Secret leak prevention in logs (11)** | **< 0.5 day** | **Done** |
| **Parallel mode isolation — --env/--db/--log/--config (12)** | **< 0.5 day** | **Done** |
| **Fee-inclusive PnL display (13)** | **< 0.5 day** | **Done** |
| **`/pnl` equity-curve chart (16)** | **1–1.5 days** | **Done** |
| **Cross Portfolio Margin entry gate (17)** | **2–3.5 days** | **Done** |
| Testing + paper trading validation | 3–5 days | Not started |
| **Total remaining** | **~6–8.5 days** | |

---

## Phase 10 — Offline Error Tracking

When the bot runs without internet access (e.g. during development, network outages, or in CI), the reconnect loops in `DeribitFeed` and `PortfolioTracker` previously logged a warning on every retry attempt — potentially dozens of lines per minute flooding the logs and making real errors hard to spot.

### Problem

| Component | Old behaviour | Frequency |
| --- | --- | --- |
| `data/deribit_feed.py` | `WARNING Feed disconnected (...); reconnecting in Xs` | Every 1s → 2s → 4s → … → 60s, indefinitely |
| `portfolio/tracker.py` | `WARNING Could not fetch BTC summary: ...` (once per currency) | Every scan cycle (default: every 5 min) |

### Solution: log on state transitions only

Both components now track an `_offline` flag and only log when the connectivity state changes:

| Event | Log level | Message |
| --- | --- | --- |
| First failure | `WARNING` | "Feed offline (...) — retrying every Xs" / "Portfolio API offline (...)" |
| Subsequent failures | `DEBUG` | "Feed still offline (attempt N, next in Xs)" — suppressed at default log level |
| Recovery | `INFO` | "Feed reconnected after N attempt(s)" / "Portfolio API back online after N failed attempt(s)" |

### Changes

**`data/deribit_feed.py`**
- Added `_offline: bool` and `_retry_count: int` to `DeribitFeed.__init__`
- `start()` loop: logs `WARNING` on first `OSError`/`WebSocketException`; `DEBUG` on repeats; `INFO` on clean reconnect

**`portfolio/tracker.py`**
- Added `_api_offline: bool` and `_api_fail_count: int` to `PortfolioTracker.__init__`
- `refresh()`: logs `WARNING` on first REST failure; `DEBUG` on repeats; `INFO` on recovery
- Per-currency `except` in `_refresh_from_api` now re-raises so the outer handler owns the single warning (previously each currency logged separately, producing one warning per asset per cycle)

### Tests

- `tests/test_feed.py` — `TestDeribitFeedOfflineTracking` (3 tests): first failure sets flag; repeated failures increment `_retry_count`; recovery clears flag and counter
- `tests/test_portfolio.py` — `TestOfflineTracking` (5 tests): first failure sets flag; repeated failures increment count; first failure is WARNING/repeats are DEBUG; recovery clears flag; recovery logs INFO with retry count

---

## Phase 11 — Secret Leak Prevention in Logs

Credentials from `.env` must never appear in `logs/bot.log*`. Two root-cause fixes eliminate the known leakage paths; a `_SecretRedactor` expansion provides belt-and-suspenders coverage; and a one-time scrub script cleans any existing log files.

### Root-cause fix 1 — Auth URL no longer contains credentials (`portfolio/tracker.py`)

The original `_authenticate()` encoded `client_id` and `client_secret` as URL query parameters:
```
/api/v2/public/auth?grant_type=client_credentials&client_id=XXX&client_secret=YYY
```
Any HTTP error (401, 429, 503) would raise `RuntimeError(f"HTTP {code} from {url}: {body}")`, embedding both credentials in the exception message. That message then appeared in `logger.warning()` and in `logger.exception()` tracebacks.

**Fix:** `_authenticate()` now calls a new `_rest_post(url, payload)` helper that sends credentials as a JSON POST body. The URL in any error message is simply `/api/v2/public/auth` — no query parameters.

### Root-cause fix 2 — Client ID removed from feed log (`data/deribit_feed.py`)

`DeribitFeed._authenticate()` logged `logger.info("Authenticating as %s", self.client_id)` on every WebSocket reconnect. Replaced with `logger.debug("Authenticating with Deribit WebSocket API")` — no credential value is logged.

### Belt-and-suspenders — expanded `_SecretRedactor` (`monitor/loop.py`)

The existing `_SecretRedactor` filter (installed on the root logger, covering both console and file handlers) previously only redacted `TELEGRAM_TOKEN` and `TELEGRAM_CHAT`. It now redacts all secrets loaded from `.env`:

| Secret | Source |
| --- | --- |
| `DERIBIT_TEST_CLIENT_ID` | config |
| `DERIBIT_TEST_CLIENT_SECRET` | config |
| `DERIBIT_LIVE_CLIENT_ID` | config |
| `DERIBIT_LIVE_CLIENT_SECRET` | config |
| `SMTP_USER` | config |
| `SMTP_PASS` | config |
| `TELEGRAM_TOKEN` | config (was already covered) |
| `TELEGRAM_CHAT` | config (was already covered) |

Blank values are still excluded so an unconfigured credential cannot accidentally redact every log line.

### One-time log scrub (`scratch/scrub_logs.py`)

A standalone script rewrites every `logs/bot.log*` rotation file in place:

- Loads secrets from `.env` using the same key names as `config.py` (no import of bot modules — safe to run without dependencies installed)
- Also checks `os.environ` so it works when variables are exported rather than in a file
- Reports per-file replacement counts so the operator can see what was found
- `--dry-run` mode prints findings without writing
- Notes that `.gz` compressed rotations (if any) must be deleted manually

---

## Phase 13 — Fee-Inclusive PnL Display

All PnL figures visible to the operator — Telegram commands, internal engine accumulators, and the DB `pnl` field — previously reported gross PnL (spread price movement only). The `open_fees` and `close_fees` columns were correctly populated but never subtracted from any PnL metric. At BTC spot $100k, round-trip fees of ~$60 per trade are material relative to calendar spread premiums of $150–$200. A position showing `PnL=+$0.60` could actually be `PnL=-$2.94` net of the entry fee already paid.

### Problem summary

| Metric | Old formula | Fee-inclusive? |
| --- | --- | --- |
| DB `pnl` on close | `sv - debit × qty` | No fees at all |
| `_today_pnl` / `_session_pnl` | sum of DB `pnl` | No fees |
| `_unrealized_pnl` (monitor tick) | `sv - debit × qty` | No fees |
| `/positions` PnL | `spread_val - debit × qty` | No fees |
| `/portfolio` PnL | `curr_val - debit × qty` | Shows fees separately, not deducted |
| `/status` PnL today | `sum(closed pnl) + unrealized` | No fees |
| Close alert PnL | gross `pnl` | No fees |

### Fixes

**`strategy/decision.py` — `_close_position()`**

```python
# Before
pnl = gross_pnl  # P&L stored in DB is gross (pre-close-fees); fees tracked separately

# After
net_pnl = gross_pnl - open_fees_usd - close_fees_usd  # all fees deducted
pnl = net_pnl  # stored as true net P&L (entry + exit fees already deducted)
```

`_today_pnl`, `_session_pnl`, and the DB `pnl` column all receive `net_pnl` automatically.

**`strategy/decision.py` — `_monitor_position()`**

```python
# Before
unrealized = sv - pos.get("net_debit", 0.0) * pos.get("qty", 1.0)

# After
unrealized = (
    sv
    - pos.get("net_debit", 0.0) * pos.get("qty", 1.0)
    - pos.get("open_fees", 0.0)
)
```

Open fees are already paid at entry so they belong in the unrealized cost basis immediately. This flows into `engine._unrealized_pnl` and through to `/status` PnL today and PnL since start.

**`telegram_cmd/handlers.py` — `handle_positions()`**

```python
# Before
unr_pnl = spread_val - t.net_debit * t.qty
pnl_pct = (unr_pnl / (t.net_debit * t.qty) * 100) if t.net_debit else 0.0

# After
cost_basis = t.net_debit * t.qty + t.open_fees
unr_pnl    = spread_val - cost_basis
pnl_pct    = (unr_pnl / cost_basis * 100) if cost_basis else 0.0
```

**`telegram_cmd/handlers.py` — `handle_portfolio()`**

```python
# Before
pnl = curr_val - t.net_debit * t.qty

# After
pnl = curr_val - t.net_debit * t.qty - t.open_fees
```

Previously `Fees: $3.11` appeared on the line above a PnL that ignored it; now the two figures are consistent.

**`telegram_cmd/handlers.py` — `handle_status()`**

Added a `Fees (session): $X.XX` line so the operator can see cumulative session fees alongside the net PnL figures.

---

## Phase 16 — `/pnl` Equity-Curve Chart

Every existing Telegram command is text-only. `/pnl` is the first command that returns an image: a chart of the bot's entire trading history, so the operator can see the shape of the equity curve at a glance (drawdowns, streaks, overall trend) rather than scrolling `/closed_trades` output.

### Requirements recap

- All historical closed trades and their PnL
- Accumulated realized gains/losses as a black line
- Current unrealized PnL from open positions as a dotted green line, labelled with the open trade count
- Delivered as an image inside Telegram, not a text message

### Data flow

```
db/state.py: get_all_closed_trades()  ──▶  cumulative realized series (black line)
db/state.py: get_open_trades()        ──┐
data/chain_cache.py: ChainCache       ──┴─▶  total unrealized PnL + open count (dotted green segment)
                                            │
                                            ▼
                              telegram_cmd/pnl_chart.py: render_pnl_chart()
                                            │
                                            ▼
                          telegram_cmd/handlers.py: handle_pnl() → reply_photo()
```

No new database columns are needed. `pnl` on `calendar_trades` is already net of fees (Phase 13) and already includes roll P&L (Phase 14) by the time a trade is closed, so the black line is a straight running sum of an existing column — the only new query is `get_all_closed_trades()`, an unbounded version of the existing `get_trades_closed_since()` pattern, ordered by `date_close`.

The unrealized figure reuses the exact formula already used by `/portfolio` and `/positions`: `(spread_val − net_debit×qty − open_fees) + roll_pnl` per open trade, where `spread_val` comes from live `ChainCache` mid prices with the `last_spread_value` stale-cache fallback added in Phase 15. Rather than duplicate that formula a third time, it's factored into a shared helper that both `handle_portfolio` and the new `compute_unrealized()` call.

### Why matplotlib, why `Agg`, why in-memory

- `matplotlib` is the natural choice for a two-series line chart with mixed line styles (solid black / dotted green) and is a mature, well-documented dependency — no new service or external API needed.
- The bot runs headless (Windows service / background process, no display attached), so the backend must be set to the non-interactive `Agg` renderer **before** `pyplot` is imported anywhere in the process. This is set once at the top of the new `telegram_cmd/pnl_chart.py` module.
- The chart is rendered straight to an `io.BytesIO` PNG buffer and handed to `update.message.reply_photo()` — nothing is written to disk in the live code path, keeping the command side-effect-free and avoiding any cleanup/temp-file logic. (The scratch script is the one place a PNG is saved to disk, for visual inspection during development.)

### Chart construction

- x-axis: `date_close` of each closed trade, chronological
- y-axis: cumulative net PnL in USD, with a horizontal reference line at $0
- Black solid line: running sum of `pnl` across `get_all_closed_trades()`
- Dotted green line: a two-point segment `(last realized date, cumulative realized)` → `(now, cumulative realized + total unrealized)`, drawn only when `open_count > 0`
- Legend/caption: realized total, unrealized total, combined total, and open trade count in plain text under the image (Telegram photo caption), so the headline numbers are readable without zooming into the image
- No closed trades yet: `handle_pnl` skips the chart entirely and replies with text ("No closed trades yet." plus unrealized summary if any positions are open) — an empty chart with no data series is not a useful image

### New/changed files

| File | Change |
| --- | --- |
| `db/state.py` | + `get_all_closed_trades()` |
| `telegram_cmd/pnl_chart.py` | new — `build_cumulative_series()`, `compute_unrealized()`, `render_pnl_chart()` |
| `telegram_cmd/handlers.py` | + `handle_pnl()` |
| `telegram_cmd/listener.py` | + `/pnl` entry in `COMMAND_REGISTRY`, `cmd_pnl` wiring |
| `requirements.txt` | + `matplotlib>=3.8` |

### Risks / open questions

- **Trade volume vs. chart readability** — with hundreds of closed trades the x-axis will need date-tick thinning/rotation; not a blocker, just a rendering-polish detail to get right in implementation.
- **Telegram photo size limits** — a single-figure PNG line chart is well within Telegram's 10 MB photo limit; no compression tuning expected to be necessary.
- **Testing rendered images** — unit tests assert on the PNG header bytes and on the pure data-prep functions (`build_cumulative_series`, `compute_unrealized`) rather than pixel content, consistent with how the rest of the test suite avoids asserting on rendered output.

---

## Phase 17 — Notification Spam Prevention for Stuck Positions

### Problem

When a position failed to close after reaching the 4th retry attempt, it was marked as stuck and a notification was sent. However, on every subsequent monitor tick (~1 minute), the position would still be stuck, and the notification code would fire again, sending the same alert repeatedly. This resulted in dozens of identical notifications over hours, overwhelming the user with spam and making it impossible to respond in a timely manner.

### Root Cause

The notification logic in `_monitor_position()` checked if the failure count reached 3 (indicating 4th attempt) and sent `notify_close_stuck()` without tracking whether that position had already been notified. Since the failure counter persisted across ticks, the same notification would fire on every monitor cycle.

### Solution: Set-Based Deduplication

**`strategy/decision.py` — `_notified_stuck` tracking**

- Added `self._notified_stuck: set[int] = set()` to `DecisionEngine.__init__()`
- Before sending `notify_close_stuck()` in stop-loss retry limit path: check `if trade_id not in self._notified_stuck and self._notifier:`
- Before sending `notify_close_stuck()` in take-profit retry limit path: same guard
- After successful notification, add `trade_id` to the set: `self._notified_stuck.add(trade_id)`
- Result: notification sent exactly once per position, on the monitor tick where it first becomes stuck

**`telegram_cmd/handlers.py` — Reset flag on user intervention**

- `/close` handler: call `engine._notified_stuck.discard(trade_id)` after resetting the close_stuck flag
  - Allows user to be notified again if the position gets stuck again after the retry attempt
- `/close_manually` handler: same cleanup, since position is now resolved
  - User can be notified again if they run the command and it still fails

### Test Coverage

- `test_handle_close_resets_close_stuck_flag()` — verifies `/close` clears the notification flag
- `test_handle_close_manually_clears_notification_flag()` — verifies `/close_manually` clears the flag
- Updated `TestStopTpCloseRetryLimit` tests to verify the new behavior with `mark_position_close_stuck` mocking

### Result

Users receive exactly **one notification** when a position becomes stuck in a retry loop, preventing message spam. The notification flag is cleared when the user intervenes via `/close` or `/close_manually`, allowing them to be notified again if it gets stuck after reset.

This directly addresses the critical feedback: **"I do not want a message to be sent every minute. When a position is marked as stuck, no further notifications should be sent"**
## Phase 17 — Cross Portfolio Margin (X:PM) Entry Gate

Two incidents already in this file's Bug Fixes section (the 2026-06-22 absurd-quantity halt and the 2026-07-01 close-after-roll margin call) happened despite `MAX_LOSS_PCT` and `MAX_TOTAL_RISK_PCT` being respected. That is because those sizing checks bound *capital paid* for a position, not the *margin Deribit actually requires to hold it*. On a Cross Portfolio Margin (X:PM) account the two numbers are different in kind, not just in magnitude — this phase adds a gate that checks the number Deribit actually uses to decide whether to liquidate the account.

### What Deribit's Cross Portfolio Margin actually computes

Per Deribit's own documentation (`support.deribit.com/hc/en-us/articles/25944756247837-Portfolio-Margin`, and the Portfolio Margin Engine whitepaper at `statics.deribit.com/files/DeribitPortfolioMarginModel.pdf`), X:PM does not margin positions individually. It:

1. Treats the whole account — every currency, every instrument, and equity itself — as one portfolio in a single risk matrix.
2. Stress-tests that portfolio against a grid of underlying price moves (9 buckets spanning a per-currency-pair Price Range, e.g. ±16% for BTC in 4 steps each side of zero) crossed with volatility-up / same / down scenarios.
3. Adds an Extended Table for tail risk on far-OTM short option exposure (moves like -66%, +500%, with a per-bucket dampener and margin multiplier so only large short-option books are affected).
4. Adds a Delta Shock (large uncorrelated directional exposure) and a Roll Shock (near-dated exposure that would need to be "rolled" through an adverse move), both computed per currency pair / base currency in USD.
5. Takes the worst simulated scenario as Initial Margin; Maintenance Margin = Initial Margin × a factor (default 0.80). Breaching the Maintenance Margin ratio triggers liquidation.

The critical implication for this bot: **the margin cost of a new calendar spread depends on what else is already in the portfolio**, because price-move and vol-shock P&L nets across positions in the same bucket before the worst-case is taken. A candidate that looks fine as an isolated debit can still be the one that tips an already-stressed portfolio over the Maintenance Margin line — and, conversely, a portfolio-margin-aware system could sometimes approve slightly more than a naive debit-sum check would allow, when the new position is genuinely offsetting. This bot only needs the *reject dangerous entries* half of that, not the optimization half.

### Decision: query the exchange, don't reimplement the model

The parameters behind every scenario above (Price Range per currency pair, Volatility Range Up/Down, Extended Dampener, Min Expiry Delta Shock / Annualised % Move Risk, Delta Total Liquidity Shock Threshold, Max Delta Shock) are live exchange settings, fetchable via `public/pme/get_params`, and can be changed by Deribit without notice. A local reimplementation of the risk matrix would need to track all of them, reproduce the exact bucket/dampener/haircut arithmetic, and would still silently drift the moment Deribit tunes a parameter — for a gate whose entire purpose is preventing forced liquidation, that drift risk is unacceptable. The gate instead asks Deribit's own API for the account's projected margin including the hypothetical new position, and only falls back to a local approximation when that call is unavailable.

**Open item carried into BOT_TODO:** `docs.deribit.com/api-reference` renders via client-side JavaScript and could not be scraped during this planning session, so the exact "what-if margin" endpoint and its request/response schema (most likely `private/get_margins`, taking `instrument_name`/`amount`/`price`) is not yet confirmed. Implementation starts with a scratch probe against `test.deribit.com` to nail down the real schema before `PortfolioTracker.simulate_margin()` is written against it — this is called out explicitly as the first task in Phase 17 rather than assumed.

### Fallback proxy, when the live simulation call isn't available

```
current_utilization   = maintenance_margin / equity
projected_utilization = (maintenance_margin + candidate.net_debit × qty) / equity

reject if current_utilization   > MAX_MARGIN_UTILIZATION_PCT
reject if projected_utilization > MAX_MARGIN_UTILIZATION_PCT
```

This adds the candidate's own max loss (a purchased calendar spread's loss is bounded at the debit paid) as a floor on its margin contribution. It is deliberately a *safety backstop*, not a stand-in for the real PM number — it can be more conservative than Deribit's actual requirement (no correlation offset credit) in some cases and less conservative in others (it doesn't capture delta/roll shock contributions from a new leg), which is exactly why the primary path always tries the live simulation call first.

`MAX_MARGIN_UTILIZATION_PCT` defaults to `0.80` — the same ratio Deribit uses as its own default Maintenance Margin Factor — so the bot's self-imposed ceiling matches the exchange's own headroom assumption rather than an arbitrary number.

### Fail-open vs fail-closed

The gate's behaviour when margin data can't be obtained depends on whether real money is at risk:

| Mode | No `PortfolioTracker` / API failure |
| --- | --- |
| `paper` | Gate no-ops (there is no real account to liquidate) unless `MARGIN_GATE_ENABLED` is forced |
| `test` / `live` | Gate fails **closed** — reject the candidate, log a warning |

This mirrors the project's existing convention (`DERIBIT_PAPER`/`TRADING_MODE` gating on every scratch script) that paper mode is where experimentation is safe and test/live mode is where the bot must default to caution.

### A wiring gap this phase must close first

`DecisionEngine` already accepts an optional `portfolio: PortfolioTracker` (Phase 8b) and calls `portfolio.refresh()` at the top of `scan_tick()` when one is attached; `BotLoop` already forwards a `portfolio` constructor argument through to it. But `bot.py`'s `_run()` never actually constructs a `PortfolioTracker` or passes it to `BotLoop(...)` — the live account-linking code has existed since Phase 8b and has simply never been turned on. This gate is the first feature that actually needs it, so wiring `PortfolioTracker` into `bot.py` is listed as a prerequisite step in Phase 17 rather than a separate phase.

### Where the gate is enforced

`strategy/decision.py` gets a new `_check_margin_gate(candidate)`, deliberately mirroring the existing `_check_liquidity_gate(candidate)` — same `str | None` rejection-reason return, same call site pattern. It is invoked in two places:

- `scan_tick()`'s RANK loop, immediately after `_check_liquidity_gate()` — a rejected candidate is skipped, not fatal to the scan pass.
- `_try_roll()`, alongside the existing `_check_liquidity_gate()` reuse there — because a roll changes portfolio composition just as much as a fresh entry does, and the 2026-07-01 incident was specifically a post-roll margin call.

### New/changed files

| File | Change |
| --- | --- |
| `portfolio/tracker.py` | + `maintenance_margin_usd`, `margin_utilization_pct`, `simulate_margin()`, `MarginImpact` |
| `bot.py` | instantiate `PortfolioTracker`, pass `portfolio=` to `BotLoop` |
| `config.py` | + `MAX_MARGIN_UTILIZATION_PCT`, `MARGIN_GATE_ENABLED`, `MARGIN_GATE_REQUIRED_LIVE` |
| `strategy/decision.py` | + `_check_margin_gate()`, called from `scan_tick()` and `_try_roll()` |

### Implementation (Completed)

**API schema confirmed:** The Deribit `private/get_margins` endpoint takes a `legs` parameter with a list of objects containing `instrument_name`, `amount`, and `price`. The response includes `initial_margin` and `maintenance_margin` in the base currency (BTC, ETH, SOL), which are then converted to USD using current spot prices.

**Key changes made:**

1. **`portfolio/tracker.py`**
   - Enhanced `_rest_post()` to support optional `bearer_token` parameter for authenticated REST calls
   - Implemented `simulate_margin(legs)` to call Deribit's margin simulation API
   - Extracts `initial_margin` and `maintenance_margin` from response and converts to USD
   - Gracefully returns `None` on any API failure so callers fall back to local proxy
   - Logs simulation results at DEBUG level for troubleshooting

2. **`bot.py`**
   - Now instantiates `PortfolioTracker` when credentials are configured
   - Passes `portfolio=tracker` to `BotLoop()` so margin gate can access margin data

3. **`strategy/decision.py`**
   - Implemented `_check_margin_gate(candidate)` mirroring `_check_liquidity_gate()` pattern
   - Paper mode: returns `None` immediately (no-op, no real account to protect)
   - Test/live mode: checks both current and projected margin utilization
   - Tries live simulation API first, falls back to conservative proxy formula
   - Called in `scan_tick()` RANK loop after liquidity gate
   - Called in `_try_roll()` to prevent rolls that breach margin ceiling
   - Logs rejections at INFO level for operator visibility

4. **Tests added**
   - `test_simulate_margin_success`: Verifies margin API call and USD conversion
   - `test_simulate_margin_no_credentials`: Verifies graceful handling without credentials
   - `test_simulate_margin_empty_legs`: Verifies behavior with no legs provided
   - `test_simulate_margin_api_failure`: Verifies API error handling and None return
   - `TestMarginGate` (6 tests): Comprehensive testing of gate behavior in all scenarios
     - Gate disabled/enabled via config
     - Paper mode no-op
     - No portfolio tracker fallback
     - Current utilization checks
     - Projected utilization (proxy formula)
     - Live simulation API precedence

**Status:** ✅ Phase 17 complete. All margin gate functionality implemented and tested. Gate is now active and prevents entries/rolls that would breach Cross Portfolio Margin utilization ceiling.

### Risks / considerations

- **API rate limits** — Deribit may rate-limit frequent margin simulation calls; monitor logs for failures and adjust scan frequency if needed
- **Spot price resolution** — Margin API response uses base currency (BTC/ETH/SOL), requiring live spot price to convert to USD; relies on `_resolve_spot()` function which fetches from public API
- **Proxy accuracy** — Fallback formula is deliberately conservative; live simulation API provides more accurate X:PM numbers and is always preferred when available

---

## Phase 17b — Paper Mode Portfolio Isolation

### Problem

In paper mode, the bot should be **completely isolated from Deribit's test exchange**. All portfolio metrics (equity, available cash, unrealized P&L, margin utilization) should be calculated from the SQLite database and live cache prices, with zero REST API calls to Deribit.

Currently, `portfolio/tracker.py` unconditionally calls Deribit REST APIs when credentials are configured, regardless of trading mode. This causes:

1. **Unnecessary API overhead** — paper mode makes REST calls even though it doesn't need live account data
2. **Reconciliation confusion** — warnings comparing Deribit margin with DB-calculated margin appear in paper mode logs, despite paper mode being purely simulated
3. **False portfolio snapshots** — equity and available_cash show zero or stale values from fallback mode instead of calculated values
4. **Non-actionable warnings** — "RECONCILE MISMATCH" alerts are printed even though paper mode should never trust Deribit's numbers anyway

### Architecture

**Paper mode (TRADING_MODE == "paper"):**

- `portfolio/tracker.py` skips all Deribit REST API calls (`_refresh_from_api()` returns early)
- Equity is calculated as: `initial_capital + realized_pnl_today + unrealized_pnl_from_cache`
- Unrealized P&L comes from live `ChainCache` mid-prices: `sum((spread_value - net_debit*qty) for each open position)`
- Available cash = equity − sum of net debits on open positions (from SQLite)
- No reconciliation warnings are emitted
- `simulate_margin()` is no-op (paper has no margin)
- Result: bot sees a realistic portfolio view based purely on DB + cache, never touching Deribit

**Test/live modes (TRADING_MODE == "test" | "live"):**

- Behavior unchanged — full Deribit REST API integration continues
- Reconciliation runs to verify DB margin matches Deribit's reported margin
- Margin simulation API calls proceed normally (Phase 17 functionality)
- Initial capital still comes from Deribit's reported equity on startup
- Result: DB is synchronized with Deribit's actual account state

### Why this matters

**During paper trading validation**, logs should not be cluttered with Deribit API calls or reconciliation warnings — the whole point of paper mode is to prove the bot's logic in isolation before going live. Paper mode logs should be clean and focused on decision logic, not account reconciliation.

**For operators switching between modes**, it's immediately obvious from the absence of API noise that paper mode is truly isolated. Absence of reconciliation warnings signals "this is pure simulation, not touching the live/test account."

### Implementation

**`portfolio/tracker.py` changes:**

1. **Item 1:** Import `TRADING_MODE` from `config` at module top
2. **Item 2:** In `refresh()`, add early return after SQLite calculations if `TRADING_MODE == "paper"`:
   - `_used_margin`, `_realized_pnl_today`, `_open_position_count`, `_fees_paid_today`, `_fees_paid_total` always calculated
   - `_refresh_from_api()` skipped entirely
   - Reconciliation skipped entirely
3. **Item 3:** Implement `_calculate_unrealized_pnl_from_cache()`:
   - Query open positions from SQLite
   - For each position, fetch live spread value from `ChainCache`
   - Sum `(spread_value - net_debit*qty)` across all positions
   - Fall back to 0.0 if cache unavailable
4. **Item 4:** Implement `_calculate_db_only_portfolio()`:
   - Compute `equity_usd = initial_capital + realized_pnl_today + unrealized_pnl_from_cache`
   - Compute `available_cash = equity_usd - sum(net_debit*qty for each open position)`
   - Return dict with `equity_usd`, `available_cash`, `unrealized_pnl`
5. **Item 5:** Add safety guards and docstrings:
   - Guard at start of `_refresh_from_api()`: `if TRADING_MODE != "paper": ...` (belt-and-suspenders)
   - Guard in `simulate_margin()`: `if TRADING_MODE != "paper": ...` with early return
   - Update class docstring documenting paper vs test/live behavior
   - Update method docstrings noting which paths are test/live only

### Test coverage

New test class `TestPaperModePortfolioIsolation` in `tests/test_portfolio.py`:

- `test_no_deribit_api_calls_in_paper_mode` — mock `_rest_get` and `_rest_post` to verify zero calls when `TRADING_MODE=="paper"`
- `test_no_reconciliation_warning_in_paper_mode` — verify no log records at WARNING level for "RECONCILE MISMATCH"
- `test_equity_calculated_from_db_in_paper_mode` — verify non-zero equity computed from DB + cache
- `test_unrealized_pnl_from_cache_in_paper_mode` — verify unrealized P&L calculated from live cache prices
- `test_test_mode_still_uses_deribit_api` — regression test: verify test/live modes still call Deribit API

### Expected results

**Paper mode:**
- ✅ Zero Deribit REST API calls in `portfolio/tracker.py`
- ✅ Portfolio snapshot shows realistic equity and available cash
- ✅ Unrealized P&L reflects current spread values from cache
- ✅ No reconciliation warnings
- ✅ All metrics come from SQLite + cache only

**Test/live mode:**
- ✅ Behavior unchanged — full Deribit API integration continues
- ✅ Reconciliation runs as before
- ✅ Margin simulation proceeds normally

---

## Bug Fixes (Post-Implementation)

Three bugs were discovered and fixed during test execution (2026-07-04):

### Bug 1: Expired near leg close missing mark_position_close_stuck call

**Location:** `strategy/decision.py`, lines 745–788

**Problem:** The "near leg expired" code path in `_monitor_position()` called `close_calendar_trade()` to force-close positions when the retry limit was exhausted, but did NOT call `mark_position_close_stuck()` to notify the user. The stop-loss and take-profit code paths (lines 819–895) DID call `mark_position_close_stuck()` for consistency. This caused an inconsistent behavior where stuck positions from expired near legs were closed silently without user notification.

**Fix:** Replaced the force-close path with a call to `mark_position_close_stuck()` and added the user notification logic with the `_notified_stuck` deduplication set (mirroring the stop/tp behavior). The position is now marked as stuck rather than force-closed, allowing the user to manually intervene on Deribit if needed.

**Tests affected:**
- `tests/test_decision.py::TestStopTpCloseRetryLimit::test_fourth_stop_close_failure_force_closes`
- `tests/test_decision.py::TestStopTpCloseRetryLimit::test_tp_close_retry_limit`

Both tests create positions with `expiry_near="3JUL26"` and run on 2026-07-04, causing `near_days_left <= 0` to trigger the expired path. Tests expected `mark_position_close_stuck()` to be called but it wasn't until this fix.

### Bug 2: Telegram error handling raises instead of logging

**Location:** `alerts/notifier.py`, lines 395–413

**Problem:** The `_post_telegram()` method was designed to return a boolean indicating success/failure, but on the final failed HTTP attempt (after retry) it raised `RuntimeError` instead of logging and returning False. This caused async notification tasks to crash silently without propagating error information, making it impossible to detect notification failures.

**Fix:** Replaced all `raise RuntimeError(...)` statements with `logger.error(...)` and `return False`. The method now consistently returns False on failure, allowing callers to handle the error gracefully and log it for debugging.

**Tests affected:**
- `tests/test_notifier.py::TestTelegramDispatch::test_post_telegram_api_error_logged_not_raised`

Test expected `asyncio.run(Notifier._post_telegram(...))` to complete without raising, but the function raised RuntimeError until this fix.

### Bug 3: Windows temp directory cleanup PermissionError with SQLite locks

**Location:** `tests/test_telegram_cmd.py`, lines 905–974

**Problem:** Two test functions used `tempfile.TemporaryDirectory()` as a context manager without properly closing SQLite database connections before the temp directory cleanup phase. On Windows, SQLite file locks prevent the temp directory from being deleted, causing `PermissionError: [WinError 32]` ("The process cannot access the file because it is being used by another process"). On Linux, the error was suppressed due to weaker file locking.

**Fix:** Explicitly close all database connections before the temp directory context exits using `get_connection(db_path).close()` in a try-except block at the end of each test.

**Tests affected:**

- `tests/test_telegram_cmd.py::test_handle_close_resets_close_stuck_flag`
- `tests/test_telegram_cmd.py::test_handle_close_manually_clears_notification_flag`

Both tests now properly release SQLite connections before cleanup, allowing the temp directory to be deleted on Windows.

---

## Phase 18 — Close-Order Reliability & Stuck-Position Retry Bugfixes

### How this was found

A post-hoc analysis of `db/calendar_bot_test.db` and `logs/bot_test.log*` (test-mode run under `config_test.py`, 2026-06-28 → 2026-07-07) turned up three distinct, compounding bugs in the close/retry path. Of the 4 closed trades in the test DB, 3 went through an abnormal close (retry-failed, stuck, or manual) rather than a clean auto-close — the opposite of what Phase 17c's "one notification, then quiet" design intended to signal. See BOT_TODO.md Phase 18 for the actionable checklist; this section is the root-cause writeup.

### Bug 1 — Far-leg close orders rejected with `-32602 Invalid params`

**Symptom:** `logs/bot_test.log*` contains 636 occurrences of `Deribit error -32602: Invalid params`. Every one of the 75 explicit `Failed to submit far close order` log lines is on the **far** leg; `Failed to submit near close order` never once appears across four log files. The failure rate is 100% far / 0% near, consistent across every asset, strike, and expiry that attempted a close in this run — not an occasional edge case.

**Root cause:** `execution/executor.py` has no concept of an instrument's tick size. Deribit's option tick size is not constant — it scales with the option's premium level (`tick_size_steps` in the instrument metadata: e.g. a coarser tick applies once price crosses a threshold). Every price the executor sends is produced by a blanket `round(price, 4)` (`_index_price()`, and directly in `_async_close_spread`, `_async_enter_spread`, `_async_roll_near_leg`, and the unwind/flatten helpers) with no awareness of the actual tick increment Deribit expects for that specific instrument at that specific price level.

Calendar spreads buy the longer-dated (far) leg and sell the shorter-dated (near) leg; the far leg carries more time value and is therefore priced higher, on average landing in a coarser tick band than the near leg. A price that isn't a multiple of the instrument's real tick size fails Deribit's own JSON-RPC parameter schema validation — which is why the error is the generic `-32602 Invalid params` (a request-shape rejection) rather than one of the business-rule codes also seen in these logs (`10009 not_enough_funds`, `11044 not_open_order`, both of which *do* reach Deribit's order-matching logic before failing).

**Fix:**

1. Add a per-instrument tick-size lookup (via `public/get_instruments` or the `tick_size`/`tick_size_steps` field already present on ticker responses used elsewhere in the executor) with a short-lived cache to avoid a round trip on every order.
2. Round every price the executor submits to the nearest valid tick before calling `place_order()` — near and far legs must each use their *own* instrument's tick size, not a shared constant. Apply at every price-producing call site: `_async_enter_spread` (individual-leg fallback), `_async_enter_spread_combo`, `_async_close_spread` (both legs), `_async_roll_near_leg` (close + open), and the `FLATTEN-NEAR` / `UNWIND-NEAR` / `UNWIND-FAR` emergency-unwind orders.
3. Add regression tests that replay the exact instrument/price pairs from trade_id=3, 4, and 5's logs against a mocked coarse tick size and assert the submitted price is valid.

### Bug 2 — The retry-cap safety net doesn't actually stop retrying

**Symptom:** trade_id=3 (BTC 59000 Put) triggered its stop-loss on 2026-07-05 and was not fully closed until 2026-07-06 11:32:42 — roughly **29 hours** later. The `ERROR strategy.decision: trade_id=3 stop-loss close failed 3 times — marking as stuck for manual intervention` log line fired **40 separate times** across that window, each with a fresh `mark_position_close_stuck()` DB write. trade_id=5 (BTC 50000 Put) was stuck from 2026-06-30 until a manual `/close_manually` on 2026-07-06 — over 6 days.

**Root cause:** two gaps compound:

1. In `strategy/decision.py`, every "mark as stuck" branch (near-leg-expired ~line 745, stop-loss ~line 818, take-profit ~line 857) calls `mark_position_close_stuck()` and then **immediately** does `self._close_roll_failures.pop(trade_id, None)` — clearing the in-memory retry counter back to zero in the same breath it declares the position unrecoverable.
2. `mark_position_close_stuck()` (`db/state.py`) only ever writes to the `close_status` column. `get_open_trades()` — the query `monitor_tick()` uses to decide what to re-evaluate every cycle — filters exclusively on `result`, which is never touched by the stuck-marking path. So a position marked "stuck" is functionally indistinguishable from a healthy open position on the very next monitor tick: it gets re-evaluated, the same close order fails again (Bug 1), the freshly-zeroed counter climbs back to 3, and the position gets marked "stuck" *again*.

The Telegram-alert half of Phase 17c's fix does work — `_notified_stuck` correctly prevents the user from being paged on every one of those 40 cycles — but the underlying retry loop, redundant DB writes, and continued uncontrolled market exposure on the position were never actually addressed. Notably, `tests/test_decision.py::test_fourth_failure_force_closes_position` currently **asserts** `4 not in engine._close_roll_failures` after the 4th failure — i.e. the existing test suite encodes this bug as intended behavior, which is why it was never caught.

**Fix (recommended approach):** stop re-evaluating a position for stop/tp/roll once it is genuinely marked `close_stuck`, until a human clears it via the existing `/close` or `/close_manually` commands (which already call `reset_close_stuck_position()`). Concretely: either filter `close_status != 'close_stuck'` into the monitor loop's open-position query, or check the flag at the top of `_monitor_position()` and skip straight through. This is simpler and safer than "keep retrying on a slower cadence," and it matches the "needs manual intervention" message already sent to the operator — once Bug 1 is fixed, most closes should succeed on the first or second attempt anyway, so this path should rarely trigger in practice. `tests/test_decision.py`'s two "fourth failure" tests need to be rewritten to assert the new behavior (counter/position frozen, not reset-and-retry).

### Bug 3 — A force-closed trade was recorded with `pnl=0.0`

**Symptom:** trade_id=1 (ETH 1400 Put) in the test DB has `result='Loss (Stop retry limit exceeded)'` and `pnl=0.0`, despite `last_spread_value=57.13` sitting just under `net_debit=57.42` — a real (small) loss, not a breakeven.

**Root cause:** `_close_position()` in `strategy/decision.py` only ever writes a DB result when `self._executor.close_spread(pos)` returns a real credit; if it returns `None` the function returns `"...close FAILED"` immediately and never calls `close_calendar_trade()`. That means this specific row was written by a different, likely now-removed, code path — `git log -- strategy/decision.py` shows several rounds of retry-logic changes (`740895e`, `d0cc03a`, `4f4c2b6`, `f5dc9c9`) that plausibly replaced whatever force-close path produced this exact result string. Wherever that logic ends up, the takeaway is the same: a position can be force-closed and recorded without a confirmed fill price, and today that silently defaults PnL to 0 instead of using the last known mark.

**Fix:** any force-close path (expired-near-leg, roll-retry-exceeded, or a to-be-added stuck-position timeout) must compute PnL from the last known mark-to-market value (`last_spread_value`, or a fresh `check_calendar_status` call) rather than defaulting to zero when `close_spread()` returns `None`. `close_error_reason` already exists on the schema to flag "this PnL is an estimate, not a confirmed fill" — use it. Add a regression test replaying trade_id=1's exact inputs and asserting the recorded PnL is non-zero.

### Bug 4 — Feed subscription window silently drops IV coverage for long-dated open positions on reconnect

**Symptom:** the test-mode bot's one open position, trade_id=1 (BTC 56000 Put calendar, near leg `17JUL26` / far leg `28AUG26`), has logged `WARNING strategy.decision: No IV for trade 1 — skipping status check` on every single monitor tick since 2026-07-09 21:23:49, and continuously since a bot restart on 2026-07-10 07:56 — over a day of stop-loss/take-profit monitoring silently disabled for this position.

**Root cause:** `data/deribit_feed.py::fetch_instruments()` (~line 163-184) rebuilds the WS ticker-subscription list every time the feed connects — on the initial `start()` call *and* on every reconnect, since `_connect_and_stream()` is re-invoked after every WS drop in `start()`'s retry loop. The list is filtered to a fixed calendar-day window derived from strategy config:

```python
min_ms = config.NEAR_DAYS_OPTIONS[0]  * 86_400_000
max_ms = config.FAR_DAYS_OPTIONS[-1]  * 86_400_000
names = [r["instrument_name"] for r in result
         if min_ms <= (r["expiration_timestamp"] - now) <= max_ms * 2]
```

With `NEAR_DAYS_OPTIONS=[1,7,14]` and `FAR_DAYS_OPTIONS=[7,14]` in `config_test.py`, this is a 1–28 calendar-day window. Trade 1's far leg (`BTC-28AUG26-56000-P`) was opened 2026-07-09 08:35 with `far_days=51` (confirmed by direct query of `db/calendar_bot_test.db`'s `calendar_trades` table, alongside `near_days=9`), because at that moment `config_test.py`'s `FAR_DAYS_OPTIONS` still included `45` (i.e. `[7, 14, 30, 45]`). A commit at 2026-07-09 08:04 ("shorten FAR_DAYS_OPTIONS to bias toward short holds") trimmed it to `[7, 14]` — a reasonable change for the *scanner's* candidate matching, but the same config value also drives the feed's subscription window, and nobody intended it to retroactively affect WS coverage of an *already-open* position's far leg.

Confirmed from `logs/test/bot-test-stderr.log`: subscription counts dropped from 588 BTC instruments (07:37-08:04 on 07-09, wide enough to include the 51-day far leg — no "No IV" warnings during this window) to 584 (after a WS reconnect at 19:06:33 on 07-09, "No IV" warnings begin shortly after at 21:23:49) to just 302 BTC instruments after the 07:56 restart on 07-10 (warnings persist continuously from then on). Once the far-leg instrument is no longer subscribed, `data/chain_cache.py::ChainCache.get_chain()` excludes it once its last-seen timestamp exceeds the 30s TTL (`CHAIN_CACHE_TTL_SEC`), so it vanishes from the chain almost immediately. `strategy/decision.py::_get_iv()` (~line 1225) then returns `None` on every call, forever, producing the endless "No IV for trade %d — skipping status check" warning at line 794. This is a real monitoring/risk gap (stop-loss/take-profit silently skipped indefinitely), not just log noise.

**Fix (implemented):**

1. Decoupled the WS subscription coverage of open positions from the scanner's day-window config. The subscription pass — factored out of `_connect_and_stream()` into a new `DeribitFeed._subscribe_all()` — always unions the day-window candidate lists with the exact `near_instrument`/`far_instrument` names of every currently-open position, regardless of that position's remaining days-to-expiry.
2. New `db/state.py::get_open_instrument_names(db_path) -> list[str]` helper returns the distinct instrument-name strings across all open positions without loading full state. It deliberately *includes* positions marked `close_stuck` — they are still open on the exchange and need live price coverage for `/info` and manual intervention — so it queries on `_OPEN_STATUSES` alone, unlike `get_open_trades()` which excludes stuck positions from routine monitoring (Bug 2).
3. The union is recomputed on *every* reconnect, not just at bot startup: `_subscribe_all()` runs inside `_connect_and_stream()`, which `start()`'s retry loop re-invokes after every WS drop. The subscription bookkeeping map (`_instruments`) is reset at the top of each pass so extras recorded on a previous connection are correctly re-subscribed on the new one.
4. The design question was resolved as option (b), keeping `DeribitFeed` free of DB-layer knowledge: `DeribitFeed.__init__` gains an optional `extra_instruments` zero-argument callable, and `bot.py` passes `extra_instruments=get_open_instrument_names`. The feed invokes the callable on each subscription pass inside `_open_position_extras()`, which dedupes against the already-subscribed day-window names and never raises — a provider failure is logged at WARNING and the day-window subscription proceeds unaffected.
5. Note the precedent: Phase 8i expanded the feed's *asset* list to cover open positions outside `config.ASSETS`, but operated at the asset level, not the specific-instrument/DTE level — it did not address this problem. Both mechanisms now run together in `bot.py`.
6. Regression test coverage: `TestFeedOpenPositionCoverage` (7 tests) in `tests/test_feed.py` asserts open-position instruments are subscribed on initial connect, remain subscribed across a simulated reconnect, drop out after the position closes, are not duplicated when already inside the day window, and that a failing or absent provider leaves day-window behaviour unchanged. `TestGetOpenInstrumentNames` (6 tests) in `tests/test_state.py` covers the DB helper. `scratch/scratch_feed_open_position_coverage.py` demonstrates a position past the window boundary staying subscribed across a reconnect (read-only, no live orders).

### Related observation — BTC/ETH entry skew in test mode (not a bug)

Test-mode entries were 6 BTC to 1 ETH. This traces to `test.deribit.com`'s ETH options book being much thinner than BTC's: ETH candidates were rejected by the liquidity gate (`MAX_LEG_SPREAD_PCT`) roughly 2.7x more often than BTC (27,042 vs 10,035 skips in the log history) despite `ASSETS = ["BTC", "ETH"]` weighting both equally. This is a test-exchange liquidity artifact, not a config or scanner defect, and is not expected to persist on the live orderbook — no action needed, called out here only so it isn't mistaken for a Phase 18 bug.

### New/changed files

| File | Change |
| --- | --- |
| `execution/executor.py` | + tick-size lookup/cache; round all order prices to valid ticks at every price-producing call site |
| `strategy/decision.py` | Stop resetting `_close_roll_failures` on mark-stuck; skip re-evaluation of `close_stuck` positions; ensure force-close paths compute real PnL |
| `db/state.py` | Possibly extend `get_open_trades()` (or add a variant) to exclude `close_status == 'close_stuck'` from routine monitor re-evaluation |
| `tests/test_executor.py` | + tick-size rounding tests |
| `tests/test_decision.py` | Rewrite the two "fourth failure" tests to assert frozen/excluded behavior instead of reset-and-retry; + zero-PnL regression test |
| `scratch/scratch_tick_size_close.py` | New — demonstrates the tick-size bug and fix against test.deribit.com (read-only ticker calls; no live orders unless `TRADING_MODE` explicitly allows it) |
| `data/deribit_feed.py` | + `extra_instruments` provider param, `_subscribe_all()`, `_open_position_extras()` — unions open-position `near_instrument`/`far_instrument` names into the WS ticker-subscription list on every connect and reconnect, independent of the day-window config |
| `db/state.py` | + `get_open_instrument_names(db_path) -> list[str]` helper returning instrument names across all open positions (including `close_stuck`) |
| `bot.py` | Passes `extra_instruments=get_open_instrument_names` into `DeribitFeed.__init__` |
| `tests/test_feed.py` | + `TestFeedOpenPositionCoverage` — regression tests asserting open-position instruments stay subscribed across a simulated reconnect even when outside the day window |
| `tests/test_state.py` | + `TestGetOpenInstrumentNames` — DB helper coverage (open/closed/stuck/dedup/NULL legs) |
| `scratch/scratch_feed_open_position_coverage.py` | New — demonstrates a position near/past the window boundary staying subscribed across a reconnect (read-only, no live orders) |

### Status

Bugs 1-3: implemented via commit 46a9627. Bug 4: identified 2026-07-11 via test-mode DB/log analysis (subscription-window gap affecting trade_id=1) and fixed the same day — open-position instrument names are unioned into the feed's ticker-subscription list on every connect and reconnect via the `extra_instruments` provider (`db/state.py::get_open_instrument_names`, wired in `bot.py`). All four Phase 18 bugs are now closed; checklist in BOT_TODO.md Phase 18.

## Phase 20 — Centralize Scattered Config Into `config.py`

### Problem

`config.py` (199 lines) is meant to be the single source of truth for tunable parameters — its own header comment and the "Configuration Variables" rule in CLAUDE.md ("Configuration variables must be set in config.py, not within the modules") say so explicitly. An audit of every module outside `config.py` (`alerts/`, `backtest/`, `core/`, `data/`, `db/`, `execution/`, `monitor/`, `portfolio/`, `strategy/`, `telegram_cmd/`, `bot.py`, `collect.py`) found roughly **94 distinct hardcoded config-like values** that violate this rule, plus two functional bugs that only exist because of the bypass. None of these are style nits in isolation, but together they mean a config change (e.g. "raise the alert cooldown" or "increase the WS ping timeout") requires hunting through business-logic files instead of editing one place, and in two cases the bypass is actively wrong.

### Audit summary

| Category | Count | Representative examples |
| --- | --- | --- |
| Logging | 16 | 5 independently hardcoded log-format strings (`monitor/loop.py`, `collect.py`, `backtest/data_collector.py`, `data/deribit_feed.py`, `data/debug_viewer.py`); duplicated 10MB/5-backup rotating-file settings; a `bot.py`-only DEBUG override for `strategy.decision`/`strategy.sizer` with no config knob |
| Timeouts / retries / backoff / poll intervals / cache TTLs | ~40 | WS `ping_interval`/`ping_timeout`/`open_timeout`/`max_size` duplicated across `execution/executor.py`, `execution/order_manager.py`, `data/deribit_feed.py`; `alerts/notifier.py`'s `cooldown_sec=300` with no config equivalent; six `getattr(config, "X", default)` calls referencing keys that **do not exist** in config.py at all (`SLIPPAGE_LIMIT_PCT`, `ORDER_TIMEOUT_SEC`, `MAX_ORDER_RETRIES`, `STUCK_ORDER_TIMEOUT_SEC`, `INITIAL_CAPITAL`, `COLLECTOR_INTERVAL_SEC`) |
| Hardcoded URLs / hostnames / file paths | 7 | DB paths (`db/state.py`, `backtest/data_collector.py`); a dead duplicate of the Deribit WS URLs in `data/deribit_feed.py`; a **live** duplicate in `execution/order_manager.py` that bypasses `config.DERIBIT_WS_URL` entirely |
| Magic-number thresholds in business logic | 16 | Strike-increment lookup table and DTE-based spread model in `core/pricing.py`; roll-trigger days and retry caps in `strategy/decision.py`; DTE tolerance windows and EV sample counts in `strategy/scanner.py` |
| Env-var reads outside config.py | 7 | `alerts/notifier.py` independently re-reads all 5 SMTP env vars (`SMTP_HOST/PORT/USER/PASS/FROM`) instead of importing `config.SMTP_*` — can silently diverge from config.py's own values; `SMTP_FROM` isn't represented in config.py at all |
| Other duplicated constants | 8 | Default portfolio value `10_000.0` hardcoded independently in `bot.py`, `execution/executor.py`, `backtest/engine.py`, and `portfolio/tracker.py`'s `INITIAL_CAPITAL` fallback; Deribit minimum contract size `0.1` duplicated in `strategy/sizer.py` and `execution/executor.py` |
| **Total** | **~94** | |

Two findings are functional bugs, not just organization debt, and should be fixed regardless of how the rest of the phase is sequenced:

1. **`execution/order_manager.py`'s order-reconciliation loop hardcodes `"BTC"`/`"ETH"` instead of iterating `config.ASSETS`** — SOL orders are never reconciled against Deribit on restart, even though `COLLECTOR_ASSETS` and the per-asset override block already treat SOL as a first-class asset.
2. **`data/debug_viewer.py` and `data/chain_cache.py` hardcode their own cache TTLs**, ignoring `config.CHAIN_CACHE_TTL_SEC` — the debug viewer's cache can silently disagree with the live bot's cache freshness.

### Plan

The work is split into six independently-shippable sub-phases so each can be reviewed and tested without blocking on the rest:

**19a — Logging.** Add a `LOGGING` section to `config.py`: `LOG_LEVEL`, `LOG_FORMAT`, `LOG_DATE_FORMAT`, `LOG_FILE_MAX_BYTES`, `LOG_BACKUP_COUNT`, `LOG_DIR`, `NOISY_LOGGERS` (dict of logger name → level for the `httpx`/`httpcore`/`telegram.ext.Updater`/`telegram.vendor.ptb_urllib3` suppression list currently only in `monitor/loop.py`). Add a single shared `setup_logging()` helper and point `monitor/loop.py`, `collect.py`, `backtest/data_collector.py`, `data/deribit_feed.py`, and `data/debug_viewer.py` at it instead of each defining its own `logging.basicConfig`.

**19b — Fake-configurable values.** For every `getattr(config, "X", default)` call where `X` doesn't actually exist in `config.py`, add the real key with that default value, then switch the call site to a direct `config.X` reference. Also remove the 4 redundant `getattr` calls in `strategy/scanner.py`, `strategy/sizer.py`, and `execution/executor.py` that shadow keys already defined in `config.py` (`MAX_FAR_DAYS_FOR_1D_NEAR`, `MIN_NET_DEBIT`, `MAX_QTY`, `COMBO_FILL_TIMEOUT_SEC`) with a matching fallback — import the config value directly instead.

**19c — Network/timeout/retry constants.** Add `DERIBIT_WS_PING_INTERVAL`, `DERIBIT_WS_PING_TIMEOUT`, `DERIBIT_WS_OPEN_TIMEOUT`, `DERIBIT_WS_MAX_SIZE`, `RPC_TIMEOUT_SEC`, `ORDER_RETRY_DELAYS`, `ALERT_COOLDOWN_SEC`, `SMTP_TIMEOUT_SEC`, `TELEGRAM_TIMEOUT_SEC` to `config.py`. Fix `alerts/notifier.py` to import `config.SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD` instead of re-reading the env vars directly, and add the missing `SMTP_FROM` to `config.py`'s alert block.

**19d — Fix the two config-bypass bugs.** Change `execution/order_manager.py`'s reconciliation loop to iterate `config.ASSETS` instead of a hardcoded `("BTC", "ETH")` tuple. Change `data/chain_cache.py`'s and `data/debug_viewer.py`'s default TTL to read from `config.CHAIN_CACHE_TTL_SEC`. Remove the dead duplicate WS-URL/hostname constants in `data/deribit_feed.py` and `backtest/data_collector.py`.

**19e — Business-logic magic numbers.** Add `STRIKE_INCREMENT_TABLE`, `FAR_LEG_SPREAD_TABLE`, `NEAR_DAY_TOLERANCE`, `FAR_DAY_TOLERANCE`, `ROLL_TRIGGER_DAYS`, `POSITION_FAILURE_RETRY_CAP`, `RECONCILE_THRESHOLD_PCT`, `MIN_CONTRACT_SIZE`, `DEFAULT_PORTFOLIO_VALUE`, `EV_SAMPLE_COUNT`, `BREAKEVEN_SCAN_STEPS` to `config.py`. Update `core/pricing.py`, `core/calendar_engine.py`, `strategy/scanner.py`, `strategy/decision.py`, and `strategy/sizer.py` to import these instead of embedding the literals.

**19f — Paths and misc.** Add `DB_PATH`, `HISTORIC_DATA_DB_PATH`, `TIMEZONE` (for `db/state.py`'s AEST-hardcoded queries), and `DATE_FORMAT` (for `telegram_cmd/pnl_chart.py` and the log date format) to `config.py`; wire `db/state.py` and `telegram_cmd/pnl_chart.py` to use them.

Each new config key gets a short comment explaining what it controls and its safe range, consistent with the existing style in `config.py`. Sub-phases are ordered so 19d (the two functional bugs) can be pulled forward and shipped independently of the rest if desired — it has no dependency on 19a/19b/19c/19e/19f.

### New/changed files

| File | Change |
| --- | --- |
| `config.py` | + `LOGGING` section (incl. `NOISY_LOGGERS` and `LOG_LEVEL_OVERRIDES`), network/timeout constants, business-logic threshold constants, `DB_PATH`/`HISTORIC_DATA_DB_PATH`/`TIMEZONE`/`DATE_FORMAT`, missing `SMTP_FROM`, and the 6 previously-fake keys (`SLIPPAGE_LIMIT_PCT`, `ORDER_TIMEOUT_SEC`, `MAX_ORDER_RETRIES`, `STUCK_ORDER_TIMEOUT_SEC`, `INITIAL_CAPITAL`, `COLLECTOR_INTERVAL_SEC`) |
| `core/logging_setup.py` | New — shared `setup_logging()` helper and `SecretRedactor` filter reading from `config.LOG_*` |
| `monitor/loop.py`, `collect.py`, `backtest/data_collector.py`, `data/deribit_feed.py`, `data/debug_viewer.py` | Replace independent `logging.basicConfig` calls with the shared `setup_logging()` helper; `monitor/loop.py::configure_logging` is now a thin backwards-compatible wrapper |
| `alerts/notifier.py` | Import `config.SMTP_*` instead of re-reading env vars; use `config.ALERT_COOLDOWN_SEC`/`config.SMTP_TIMEOUT_SEC`/`config.TELEGRAM_TIMEOUT_SEC` |
| `execution/order_manager.py` | Reconciliation loop iterates `config.ASSETS` (SOL now reconciled); endpoint and WS connect params sourced from `config.DERIBIT_WS_URL`/`config.DERIBIT_WS_*` |
| `execution/executor.py` | WS/RPC timeout constants sourced from config; redundant `getattr` fallbacks removed; min contract size and default portfolio value from config |
| `data/chain_cache.py`, `data/debug_viewer.py` | Default TTL sourced from `config.CHAIN_CACHE_TTL_SEC` |
| `data/deribit_feed.py`, `backtest/data_collector.py` | Dead duplicate WS-URL/hostname constants removed |
| `core/pricing.py`, `core/calendar_engine.py` | Strike-increment table, spread model, breakeven-scan constants, and warn threshold sourced from config |
| `strategy/scanner.py`, `strategy/decision.py`, `strategy/sizer.py` | DTE tolerances, roll-trigger days, retry caps, EV sample count sourced from config; redundant matching `getattr` fallbacks removed |
| `portfolio/tracker.py` | `_RECONCILE_THRESHOLD` and paper-mode `INITIAL_CAPITAL` sourced from config |
| `bot.py` | `--portfolio` default from `config.DEFAULT_PORTFOLIO_VALUE`; per-module DEBUG overrides from `config.LOG_LEVEL_OVERRIDES` |
| `backtest/engine.py` | Default portfolio value from `config.DEFAULT_PORTFOLIO_VALUE` |
| `db/state.py` | `DB_PATH`/`TIMEZONE` sourced from config instead of a local `BOT_DB_PATH` env read and hardcoded `ZoneInfo("Australia/Sydney")` (BOT_DB_PATH override still honoured via `config.DB_PATH`) |
| `telegram_cmd/pnl_chart.py` | Date format sourced from `config.DATE_FORMAT` |
| `tests/test_config_centralization.py` | New — 35 tests asserting the config-sourced values are actually used, incl. the SOL-reconciliation and cache-TTL regression tests |
| `scratch/scratch_config_centralization.py` | New — offline demo (22 checks): constants match config, SOL reconciliation fix, TTL fix, late-binding behaviour changes |

### Status

Complete — all six sub-phases implemented; 565 tests passing (530 existing + 35 new). Checklist in BOT_TODO.md Phase 20. Verified end-to-end by `python -m scratch.scratch_config_centralization` (no live orders) and a real `collect.py --once` snapshot run using the shared logging setup.

---

## Phase 21 — Fix Runaway Deep-ITM Calendar Churn & Close-Status Tracking Bug

### Problem

Analysis of the paper-mode run on 2026-07-14 (`db/calendar_bot.db`, `logs/bot.log`) found 131 trades opened and closed the same day — 91 of them the same instrument (ETH 1400 Call), plus 23 more on two BTC deep-ITM put strikes (100000, 110000). Net phantom paper P&L for the day was +$224,247, most of it from the ETH 1400 Call churn alone. This was not a liquidity or volatility event; it is a bug in how the scanner ranks candidates and how the monitor decides a position has hit stop/take-profit.

**Root cause — a ranking formula that is unstable near zero debit.** `strategy/scanner.py::_eval_candidate` (line 271) computes `ev_ratio = ev_net / net_debit`, and `scan()` sorts candidates by that ratio descending with no ceiling. A calendar spread struck deep in-the-money or deep out-of-the-money has almost no time-value difference between its near and far leg — both legs price close to intrinsic — so `net_debit` can be a few dollars or even cents even though the position notional (and the fees on it) is large. Dividing by that near-zero debit produces an EV score two to three orders of magnitude above every legitimate near-the-money candidate (observed: `ev=17.0769` against a `MIN_EV` gate of `0.25`), so the degenerate candidate always sorts first and gets entered on every scan where it reappears.

**Why it closes almost instantly.** Stop-loss and take-profit are evaluated as *percentage of debit paid* (`core/calendar_engine.py::check_calendar_status`, `STOP_PCT`/`TAKE_PROFIT_PCT` = 50%/150%). With debit near zero, that percentage is hypersensitive: a quote move that is immaterial in dollar terms reads as an enormous percentage swing. Confirmed directly in the log for trade_id=104: entered 12:19:53 at a $1.78/contract debit ($178 total for qty=100); the very next monitor tick, 60 seconds later, read the live market spread value at $3,559.54 — a 2000%-of-debit take-profit — using `strategy/decision.py::_get_market_spread_value`'s real bid/ask mid-quotes, not even the documented-as-unreliable Black-Scholes fallback. The mirror failure shows up in the BTC 100000/110000 puts: `last_spread_value` reads exactly `0.0` on every one of those 23 trades, instantly tripping the stop-loss for a near-total loss. Both are consistent with deep ITM/OTM strikes having thin, erratic quotes on Deribit's testnet (`test.deribit.com`) — real for a live order book, but not a reliable signal for a 60-second mark-to-market decision.

**The loop.** Open → thin/erratic quote reads as an instant TP or stop → position closes → the correlation gate in `strategy/sizer.py::size_candidate` (`STRIKE_CORRELATION_PCT`, currently ±5%) only checks *currently open* positions, so once the position has closed it no longer blocks anything → the scanner's unstable EV ranking re-selects the same cheap, illiquid strike on the very next 5-minute scan → repeat. This ran unattended for ~6 hours before the bot was stopped.

**Secondary bug — `close_status` never reaches `'closed'` on a normal close.** `db/state.py::close_calendar_trade()` (the function called for every stop-loss, take-profit, expiry, and forced close) updates `date_close`, `spot_close`, `pnl`, `result`, and `close_fees`, but never sets `close_status`. Only `mark_position_manually_closed()` (the `/close_manually` path) sets `close_status = 'closed'`. As a result, all 131 trades from the 2026-07-14 run — despite `result` correctly showing `Win (Auto TP)` / `Loss (Auto Stop)` and `date_close` being set — still show `close_status = 'open'` in the database. Nothing observed today reads `close_status` to decide whether a position is open (`list_assets_with_open_positions` and the sizer's correlation check both key off `result`/an explicit open-position query, not this column), so this bug did not cause the churn — but any future tooling that trusts `close_status` (the stuck-position dashboard already keys off `close_status = 'close_stuck'`) would misreport every auto-closed trade as still open.

**Unrelated drift found while reviewing `config.py` for this phase — `config_test.py` was never updated for Phase 20.** `config_test.py` (used as `BOT_CONFIG_FILE` for the parallel test-mode instance) is a standalone file, `exec`'d into `config.py`'s already-populated namespace after `config.py` runs — so keys it doesn't mention still resolve correctly via inheritance, and nothing is functionally broken today. But the file's own header comment documents a fixed, explicit set of intentional overrides (loosened OI/spread/EV thresholds for rapid order-lifecycle mechanics testing), and it hasn't been touched since roughly Phase 16/17: it predates the entire Phase 20 config-centralization effort and is missing all ~45 keys that phase added to `config.py` (logging, WS/RPC timeouts, business-logic tables, DB paths, timezone, date format, etc.). Since this phase is already touching config-adjacent files, backporting those keys now — as explicit, commented values rather than silent inheritance — keeps the test config file honest about what it actually runs with, matching how it already documents its handful of intentional overrides.

### Plan

Six independently-shippable sub-phases. 21a and 21b are the primary fix for the churn (either alone would have prevented the ETH 1400 Call loop; both are wanted because they close different gaps). 21c and 21d are defense-in-depth so a similar degenerate case can't reproduce the same failure through a different door. 21e is the unrelated secondary bug. 21f is unrelated config-hygiene found in passing.

**21a — Stop the EV-ranking singularity.** Cap the value used for *sorting* candidates at a configurable ceiling (`EV_SCORE_RANKING_CAP`, e.g. `2.0`) so a blown-up ratio from a near-zero-debit candidate can no longer out-rank every legitimate setup; the uncapped `ev_score` is still what's compared against `MIN_EV` for the accept/reject gate, so this only changes ranking order, not the entry threshold. Add a regression test asserting a synthetic near-zero-debit candidate with `ev_ratio=17.0` never sorts ahead of a normal candidate with `ev_ratio=0.4`.

**21b — Add a moneyness entry filter.** Add `MAX_MONEYNESS_PCT` to `config.py` (e.g. `0.15`) and reject candidates in `strategy/scanner.py::_eval_candidate` whose strike is more than that fraction away from spot. Deep ITM/OTM calendar spreads have converged near/far pricing, meaning the near/far theta differential the strategy is designed to harvest doesn't meaningfully exist there — this is the structural fix, since it stops these candidates from ever being scored at all rather than trying to rank them correctly. Applies per-asset through the existing `ASSET_OVERRIDES` mechanism.

**21c — Require genuine two-sided quotes before trusting a live mark for stop/TP, and debounce single-tick triggers.** `strategy/decision.py::_get_market_spread_value` currently accepts a leg's `mark_price` as a stand-in whenever `bid`/`ask` isn't both positive — on a thin testnet book that's often a stale or synthetic number. Add `MARKET_SV_REQUIRE_TWO_SIDED` (default `True`): when either leg lacks a genuine two-sided quote, `_get_market_spread_value` returns `None` (forcing the already-existing, clearly-logged B-S fallback) instead of substituting `mark_price`. Separately, add `CLOSE_CONFIRM_TICKS` (default `2`): a stop/TP condition must be observed on this many consecutive monitor ticks before `_close_position` is actually called, so a single noisy quote can no longer instantly liquidate a position. Persist the pending-confirmation count in-memory per `trade_id` (reset on any tick where the condition doesn't hold).

**21d — Per-instrument re-entry cooldown (safety valve).** Add `REENTRY_COOLDOWN_SEC` (default `1800`) and track the timestamp of the most recent auto-close (stop or TP) per `(asset, strike, option_type)` tuple. `strategy/sizer.py::size_candidate` rejects a candidate that matches a recently-closed instrument within the cooldown window, the same way it already rejects a candidate correlated with a *currently open* position. This is independent of 21a–21c: even if some other degenerate case someday produces a fast false stop/TP, this stops it from immediately reopening on the next scan.

**21e — Fix `close_status` and backfill existing rows.** Update `db/state.py::close_calendar_trade()` to set `close_status = 'closed'` alongside the other fields it already updates. Add a one-off `scratch/scratch_backfill_close_status.py` that sets `close_status = 'closed'` for every existing row where `result` is a terminal state (`Win (Auto TP)`, `Loss (Auto Stop)`, `Loss (Stop)`, `Loss (Early)`, etc. — the same list `_OPEN_STATUSES`/terminal-status set already used elsewhere in `db/state.py`) but `close_status` is still `'open'`; run it once against `db/calendar_bot.db` to correct the 2026-07-14 backlog (paper mode only — this is a data-hygiene fix, not a live trading action).

**21f — Backport Phase 20's config keys into `config_test.py`.** Add explicit, commented values (mirroring `config.py`'s own comments) for every key Phase 20 added, grouped the same way Phase 20's audit grouped them: the `LOGGING` section (`LOG_LEVEL`, `LOG_FORMAT`, `LOG_DATE_FORMAT`, `LOG_FILE_MAX_BYTES`, `LOG_BACKUP_COUNT`, `LOG_DIR`, `NOISY_LOGGERS`, `LOG_LEVEL_OVERRIDES`); network/timeout/retry/alert constants (`DERIBIT_WS_PING_INTERVAL`, `DERIBIT_WS_PING_TIMEOUT`, `DERIBIT_WS_OPEN_TIMEOUT`, `DERIBIT_WS_MAX_SIZE`, `RPC_TIMEOUT_SEC`, `ORDER_RETRY_DELAYS`, `ALERT_COOLDOWN_SEC`, `SMTP_TIMEOUT_SEC`, `TELEGRAM_TIMEOUT_SEC`, `SMTP_FROM`); the 6 previously-fake keys made real (`SLIPPAGE_LIMIT_PCT`, `ORDER_TIMEOUT_SEC`, `MAX_ORDER_RETRIES`, `STUCK_ORDER_TIMEOUT_SEC`, `INITIAL_CAPITAL`, `COLLECTOR_INTERVAL_SEC`); business-logic magic-number tables (`STRIKE_INCREMENT_TABLE`, `STRIKE_INCREMENT_DEFAULT`, `FAR_LEG_SPREAD_TABLE`, `FAR_LEG_SPREAD_DEFAULT`, `FAR_LEG_LIQUIDITY_PENALTY_PER_30D`, `NEAR_DAY_TOLERANCE`, `FAR_DAY_TOLERANCE`, `ROLL_TRIGGER_DAYS`, `POSITION_FAILURE_RETRY_CAP`, `RECONCILE_THRESHOLD_PCT`, `MIN_CONTRACT_SIZE`, `DEFAULT_PORTFOLIO_VALUE`, `EV_SAMPLE_COUNT`, `BREAKEVEN_SCAN_STEPS`, `BREAKEVEN_SCAN_RANGE`, `SPREAD_WARN_PCT`, `STRIKE_CORRELATION_PCT`); and paths/timezone/date format (`DB_PATH`, `HISTORIC_DATA_DB_PATH`, `TIMEZONE`, `DATE_FORMAT`). Values should match `config.py`'s defaults exactly unless there's a specific test-mode reason to diverge (the same rationale the file's existing header comment gives for its handful of deliberate overrides) — this is a parity backfill, not a chance to introduce new test-mode behaviour. Also add the new Phase 21 keys from 21a–21d once those land, so `config_test.py` never drifts again from the moment it's resynced. Note: `MAX_MARGIN_UTILIZATION_PCT`, `MARGIN_GATE_ENABLED`, and `MARGIN_GATE_REQUIRED_LIVE` (Phase 17) are also missing from `config_test.py` but predate Phase 20 and are out of scope for this sub-phase.

### New/changed files

| File | Change |
| --- | --- |
| `config.py` | + `EV_SCORE_RANKING_CAP`, `MAX_MONEYNESS_PCT`, `MARKET_SV_REQUIRE_TWO_SIDED`, `CLOSE_CONFIRM_TICKS`, `REENTRY_COOLDOWN_SEC` |
| `strategy/scanner.py` | Ranking sort key clipped at `EV_SCORE_RANKING_CAP`; `_eval_candidate` rejects strikes outside `MAX_MONEYNESS_PCT` of spot |
| `strategy/decision.py` | `_get_market_spread_value` returns `None` instead of a `mark_price` fallback when a leg lacks a genuine two-sided quote (unless `MARKET_SV_REQUIRE_TWO_SIDED` is `False`); new per-`trade_id` pending-confirmation counter gating stop/TP close on `CLOSE_CONFIRM_TICKS` consecutive ticks; records the auto-close timestamp per `(asset, strike, option_type)` for 21d |
| `strategy/sizer.py` | `size_candidate` gains a re-entry cooldown check against recently auto-closed `(asset, strike, option_type)` tuples, alongside the existing correlated-open-position check |
| `db/state.py` | `close_calendar_trade()` sets `close_status = 'closed'` |
| `config_test.py` | + the ~45 Phase 20 keys it was missing (logging, network/timeout/retry/alert, the 6 previously-fake keys, business-logic tables, paths/timezone/date-format), plus the new Phase 21 keys from 21a–21d, all set to their `config.py` defaults unless a test-mode override is specifically documented |
| `scratch/scratch_backfill_close_status.py` | New — one-off backfill correcting `close_status` on already-closed historical rows |
| `scratch/scratch_deep_itm_churn.py` | New — offline demo reproducing the 2026-07-14 failure against the old logic and showing each fix (21a–21d) independently prevents it |
| `tests/test_scanner.py` | New tests for ranking cap and moneyness filter |
| `tests/test_decision.py` | New tests for two-sided-quote requirement and confirm-ticks debounce |
| `tests/test_sizer.py` | New tests for the re-entry cooldown |
| `tests/test_state.py` | New test asserting `close_calendar_trade()` sets `close_status = 'closed'` |
| `tests/test_config_centralization.py` | New test(s) asserting `config_test.py` defines every key present in `config.py` (prevents this drift recurring silently) |

### Status

Complete — all six sub-phases implemented. `config.py` gains the five new Phase 21
keys (`EV_SCORE_RANKING_CAP`, `MAX_MONEYNESS_PCT`, `MARKET_SV_REQUIRE_TWO_SIDED`,
`CLOSE_CONFIRM_TICKS`, `REENTRY_COOLDOWN_SEC`).

- **21a** — `strategy/scanner.py::scan()` ranks by `(ev_score > EV_SCORE_RANKING_CAP, -ev_score)`,
  demoting above-cap (near-zero-debit) candidates below every in-range one; the uncapped
  `ev_score` still gates `MIN_EV`. A plain `min(ev_score, cap)` was rejected because it would
  still let a capped degenerate (2.0) out-rank a legitimate 0.4 candidate.
- **21b** — `_eval_candidate` rejects strikes more than `MAX_MONEYNESS_PCT` from spot,
  overridable per-asset via `ASSET_OVERRIDES`.
- **21c** — `_get_market_spread_value` returns `None` (forcing the logged B-S fallback) when a
  leg lacks a genuine two-sided quote under `MARKET_SV_REQUIRE_TWO_SIDED`; a per-`trade_id`
  `_pending_close` counter requires `CLOSE_CONFIRM_TICKS` consecutive stop/TP ticks before
  `_close_position` is called, reset on any tick the condition clears.
- **21d** — `_close_position` records auto-close (stop/TP only) timestamps per
  `(asset, strike, option_type)` in `_recent_auto_closes`; `size_candidate` gains a
  `recent_auto_closes`/`reentry_cooldown_sec` check that rejects re-entry within the window.
- **21e** — `close_calendar_trade()` now sets `close_status = 'closed'`;
  `scratch/scratch_backfill_close_status.py` corrects historical rows.
- **21f** — `config_test.py` backfilled with every Phase 17/20/21 key (exact parity with
  `config.py`, verified by `tests/test_config_centralization.py::TestConfigTestParity`).

Also fixed a regression from the prior commit that switched `core/calendar_engine.py` from
late-binding `config.SPREAD_WARN_PCT`/`config.BREAKEVEN_SCAN_RANGE` to early imports, which
broke `config`-override at runtime — restored to `config.X` access at the use sites.

Verified by `python -m scratch.scratch_deep_itm_churn` (10 offline checks, no live orders) and
27 new unit tests across `test_scanner.py`, `test_sizer.py`, `test_decision.py`, `test_state.py`,
and `test_config_centralization.py`. Full suite: 588 passing (4 pre-existing Windows
SQLite temp-dir `PermissionError` failures in `test_telegram_cmd.py` are environmental and
unrelated).

---

## Phase 22 — Stuck-Position Visibility, Silent Telegram Failures, and Close-Order Price Rejections

### Problem

A test-mode run surfaced three related operational bugs around what happens once a position needs manual intervention, plus (found mid-phase, in the paper-mode run) a smaller-scale recurrence of the Phase 21 premature-roll/churn problem through a different mechanism.

**1. `/positions` (and `/portfolio`) hide exactly the positions the operator most needs to see.** `db/state.py::get_open_trades()` — the function every relevant Telegram handler calls — filters with `close_status != 'close_stuck'` (line 401-403), a change from Phase 18 intended to stop the *monitor* from re-evaluating a stuck position forever. But Telegram's `/positions` and `/portfolio` handlers call this same function, so once a position is marked `close_stuck` it silently disappears from both commands — exactly when the operator has just received a "MANUAL ACTION REQUIRED" alert and asks "what's still open?" The two screenshots that prompted this phase show precisely that: trade #11 and #12 both alerted as stuck, then `/positions` immediately after replies "No open positions."

**2. `/info trade_id=N` can fail completely silently.** `telegram_cmd/handlers.py::handle_info` (unlike `handle_close`, `handle_close_manually`, and `handle_pnl`) has no `try/except` around its body, and `telegram_cmd/listener.py::_build_app()` never registers a python-telegram-bot `Application.add_error_handler` — python-telegram-bot's default behaviour for an unhandled exception raised inside a `CommandHandler` callback is to log it internally and send *no* reply at all. `handle_info` computes `unrealized / cost_basis * 100` (line 487) with no guard for `cost_basis == 0`, which is a real possibility for a manually-adjusted or partially-filled position (`net_debit * qty + open_fees` collapsing to zero) — a `ZeroDivisionError` there, or any other exception anywhere else in the handler, currently produces total silence to the user, matching the reported "yields no information" behaviour exactly.

**3. The same "MANUAL ACTION REQUIRED" alert can fire twice after a service restart — because the monitor keeps retrying the stuck position, not just because of a notification-dedup gap.** Tracing why trade #11's alert repeated (07:12 and again at 07:21, spanning a restart at 07:17) found that `strategy/decision.py::DecisionEngine._load_all_open_positions()` — the function the monitor loop actually iterates every tick, as opposed to `get_open_trades()` used by Telegram — calls `db/state.py::load_calendar_state()`, whose `open_positions` list is filtered **only** by `result IN _OPEN_STATUSES` (line 334). It does not check `close_status` at all. `mark_position_close_stuck()` never touches `result` (only `close_status`/`close_error_reason`), so a stuck position's `result` stays `"Open"`/`"Near Leg Rolled"` and it keeps reappearing in `_load_all_open_positions()` — the monitor keeps attempting to close/roll it on every single tick, forever, regardless of `close_status`. Each failed attempt increments `_close_roll_failures`, which gets popped back to zero the moment `_mark_stuck_and_notify()` fires (line 940), so the position re-marks itself stuck and re-queues a notification once `POSITION_FAILURE_RETRY_CAP` failures accumulate again — deduplicated only by the in-memory `self._notified_stuck` set (line 220), which is empty again after every process restart. So the true bug is that Phase 18 updated the read path Telegram uses (`get_open_trades()`) but never updated the read path the engine itself uses (`load_calendar_state()` / `_load_all_open_positions()`) — the monitor was never actually excluding `close_stuck` positions from retry, it was only hidden from view in Telegram. The restart just made the pre-existing repeat-notification loop visible.

**4. The underlying "-32602 Invalid params" close-order rejection (the reason positions get stuck in the first place) is still a live bug, not something Phase 18 fully fixed.** `execution/executor.py::_async_close_spread` and `_async_roll_near_leg` compute the close/roll price as an arbitrary synthetic multiplier — `near_mid * 1.02` to buy back the near leg, `far_mid * 0.98` to sell the far leg (lines 703, 721, 845) — rather than a real quote. Entry legs work reliably because they submit `candidate.near_bid`/`candidate.far_ask` directly, prices that came straight off the live order book and are therefore already tick-aligned by construction; a `mid * 1.02` value has no such guarantee. `place_order()` does try to correct this by rounding to the instrument's `tick_size` (added in Phase 18), but the tick-size fetch is wrapped in a bare `except Exception: pass` (line 198) — a failed fetch silently falls back to a flat 4-decimal `round()` that has no relationship to Deribit's actual tick grid for that instrument's price band, and even a successful fetch only reads the base `tick_size` field, not `tick_size_steps` (Deribit's per-price-band tick overrides), so a close price for an instrument trading above its first tick threshold can still round to an invalid grid point. Combined with float arithmetic (`round(price / tick_size) * tick_size`) that can itself land a hair off the true tick due to floating-point representation error, this reproduces the same deterministic, 100%-of-attempts rejection the prior analysis found (122/122 failures, zero `order_manager.track()` calls for any CLOSE-NEAR order) — Phase 18's README/TODO entry claiming this was "fixed" only addressed the visible symptom (retry-then-mark-stuck), not this root cause.

**5. (Found mid-phase, paper mode) A smaller-scale recurrence of Phase 21's churn, through the roll trigger rather than the EV ranking.** Investigating why paper-mode trades #206 and #207 (both `near_days=1`) were entered and closed/rolled roughly one minute later: `strategy/decision.py::_monitor_position` triggers a roll whenever `near_days_left <= config.ROLL_TRIGGER_DAYS` (line 864), and `ROLL_TRIGGER_DAYS = 2` (`config.py` line 313) — but the bot also enters legitimate `near_days=1` positions (documented in `config.py` as a valid combo, "1d near is only paired with 7d and 14d far"). `_days_left()` computes `near_days_left` purely from today's date vs. the stored expiry label, with no reference to how long the position has actually been held, so a `near_days=1` position has `near_days_left == 1 <= 2` from the moment it is entered — the very first monitor tick after the one-tick entry grace period (`_just_entered`) sees it as already roll-eligible, regardless of real elapsed time. Trade #206 hit this immediately, the resulting roll attempt failed some other gate, and `_monitor_position` fell back to closing the position outright for a small loss (`-$3.87`, "Roll failed — closing"). Trade #207 hit the same premature trigger, but the roll itself *succeeded* — into a new near leg, `BTC-31JUL26-61000-C`, that turned out to have the **same expiry as the position's own (untouched) far leg**, also `BTC-31JUL26-61000-C`. That's because `_try_roll()` (line 1073) matches candidates from `scan()` by `strike`/`option_type` only (line 1084-1087) and takes the top-ranked one (`matches[0]`) — `scan()` returns every configured near/far tenor pairing for that strike, so the winning candidate can come from a completely different (near, far) pairing than the position's own, with no check that its near leg's expiry is actually earlier than *this position's* far leg. The only existing safeguard (line 1100) rejects a candidate whose new near instrument is identical to the position's *current* near instrument — it does not check the candidate against the position's *far* instrument. The result was a zero-width calendar spread (near == far), collapsing its market spread value to exactly `$0.00` (`last_spread_value` recorded as `0.0`) and instantly tripping a large stop-loss (`-$993.88`, `roll_pnl=-$863.69`) — the same "degenerate near-zero/zero-width spread fools the stop/TP machinery" failure mode as Phase 21, arrived at through the roll path instead of the entry-ranking path, which is why 21a-21d's moneyness filter and EV-ranking cap didn't catch it.

### Plan

**22a — Stop the monitor from retrying (and re-notifying about) `close_stuck` positions.** Update `db/state.py::load_calendar_state()` (or add a `db/state.py::get_monitorable_positions()` used specifically by `_load_all_open_positions()`) to exclude `close_status == 'close_stuck'` rows from `open_positions`, matching what `get_open_trades()` already does. This is the actual fix for the repeat-retry-and-repeat-notify loop: once excluded from the engine's own read path, a stuck position is genuinely left alone until `/close` or `/close_manually` clears it, instead of merely being hidden from Telegram while the engine keeps hammering it every tick.

**22b — Make stuck positions visible again in `/positions` and `/portfolio`, flagged rather than hidden.** Add a new `db/state.py` query (e.g. `get_visible_positions()`) that returns every row with `result IN _OPEN_STATUSES` regardless of `close_status`, and update `handle_positions`/`handle_portfolio` to use it, prefixing any `close_status == 'close_stuck'` line with a clear marker (e.g. `⚠️ STUCK — `) and appending the stored `close_error_reason`. `get_open_trades()` itself is left unchanged (still excludes stuck positions) since 22a now depends on that same exclusion semantics being available for the monitor path too — only the Telegram display path changes to a stuck-inclusive query.

**22c — Harden Telegram command handlers against silent failures.** Wrap `handle_info`'s body in `try/except`, replying with a plain error message on failure (matching the pattern already used in `handle_close`/`handle_close_manually`); guard the `cost_basis == 0` case explicitly instead of dividing by it. Independently, register `application.add_error_handler(...)` in `telegram_cmd/listener.py::_build_app()` so any *future* unhandled exception in *any* command handler results in a logged error and a generic "Something went wrong processing that command" reply to the chat, instead of silence — defense-in-depth so this class of bug can't recur in a new handler.

**22d — Fix the close/roll price rejection at its source.** In `execution/executor.py`, replace the synthetic `near_mid * 1.02` / `far_mid * 0.98` close/roll prices with prices derived from the live order book the same way entry legs already are — cross the spread using the actual best ask (to buy back the short near leg) and best bid (to sell the long far leg) from the ticker response, with a small configurable buffer (e.g. `config.CLOSE_PRICE_CROSS_BUFFER_PCT`) rather than an arbitrary percentage of a synthetic mid. Make tick-size handling fail loud instead of quiet: if `get_instruments` can't be fetched, log a warning naming the instrument and either retry once before falling back, or (configurable) abort the close attempt rather than submit a price that is very unlikely to land on the correct grid. Read Deribit's `tick_size_steps` (not just the flat `tick_size`) so the correct tick is used for the instrument's actual current price band. Use integer/`Decimal`-based rounding (round in tick-count space, not floating-point division) so the rounded price can't drift off-grid due to float representation error.

**22e — Persist stuck-position notification state so a restart can't cause a duplicate alert.** Alongside 22a (which removes the root cause), add a DB-backed guard as defense-in-depth: check `close_status == 'close_stuck'` before calling `notify_close_stuck` in `_mark_stuck_and_notify` (a position already marked stuck in the DB has, by definition, already been alerted once — this is naturally idempotent once 22a stops the repeat-retry loop) rather than relying solely on the in-memory `self._notified_stuck` set that a restart clears.

**22f — Fix the premature roll-trigger and degenerate same-expiry roll (paper-mode trades #206/#207).** In `strategy/decision.py::_monitor_position`, require genuine decay before considering a roll: gate the `near_days_left <= config.ROLL_TRIGGER_DAYS` check on `near_days_left < pos.get("near_days", near_days_left)` (the near-tenor recorded at entry) as well, so a freshly-opened short-dated near leg (e.g. `near_days=1`) is not roll-eligible on the very first tick after entry — it becomes eligible only once real time has actually passed and the leg has decayed closer to expiry than it was at entry. In `_try_roll()`, restrict candidate matching to `c.far_instrument == pos["far_instrument"]` (the position's own, unchanged far leg) instead of matching by `strike`/`option_type` alone across every scanned tenor pairing, and explicitly reject (log + return `False`) any candidate whose near-leg expiry is not strictly earlier than that far leg's expiry by at least `config.MIN_ROLL_NEAR_FAR_GAP_DAYS` (new key, e.g. `1`) — this closes the same "degenerate near-zero/zero-width spread fools stop/TP" failure mode Phase 21 fixed on the entry-ranking path, closing it here on the roll path too.

### New/changed files

| File | Change |
| --- | --- |
| `config.py` | + `CLOSE_PRICE_CROSS_BUFFER_PCT`, `TICK_SIZE_FETCH_RETRIES` (or equivalent abort-vs-retry knob), `MIN_ROLL_NEAR_FAR_GAP_DAYS` |
| `db/state.py` | `load_calendar_state()` (or a new `get_monitorable_positions()`) excludes `close_status == 'close_stuck'`; new `get_visible_positions()` for Telegram display (stuck positions included, flagged) |
| `telegram_cmd/handlers.py` | `handle_positions`/`handle_portfolio` use the new stuck-inclusive query and flag stuck rows; `handle_info` wrapped in `try/except` with a `cost_basis == 0` guard |
| `telegram_cmd/listener.py` | `_build_app()` registers a global `add_error_handler` replying with a generic error message on any unhandled command-handler exception |
| `execution/executor.py` | `_async_close_spread`/`_async_roll_near_leg` derive close/roll prices from live best bid/ask instead of a synthetic mid multiplier; tick-size fetch failure is logged and retried/aborted instead of silently swallowed; `tick_size_steps` honoured; rounding done in tick-count space to avoid float drift |
| `strategy/decision.py` | `_mark_stuck_and_notify` checks DB `close_status` before notifying (idempotent restart guard); `_monitor_position`'s roll trigger requires genuine decay (`near_days_left < pos["near_days"]`) in addition to the `ROLL_TRIGGER_DAYS` threshold; `_try_roll()` matches candidates against the position's own far leg and rejects same-or-later-expiry near candidates |
| `tests/test_state.py` | New tests: monitorable-positions query excludes `close_stuck`; visible-positions query includes it, flagged |
| `tests/test_telegram_cmd.py` | New tests: `/positions`/`/portfolio` show a flagged stuck position; `/info` replies with an error instead of silence when `cost_basis == 0`; global error handler replies on a simulated handler exception |
| `tests/test_executor.py` | New tests: close/roll price derived from bid/ask, not mid multiplier; tick-size fetch failure is logged and handled per the new retry/abort config; rounding stays on-grid for `tick_size_steps` fixtures |
| `tests/test_decision.py` | New tests: stuck position is not retried or re-notified across a simulated monitor tick after already being marked stuck; a `near_days=1` position is not roll-eligible on its first post-entry tick; `_try_roll` rejects a candidate whose near expiry matches/exceeds the position's own far leg expiry |
| `scratch/scratch_stuck_position_visibility.py` | New — offline demo: a close_stuck position stays out of the monitor's retry loop but still shows (flagged) in `/positions`/`/portfolio` output |
| `scratch/scratch_close_price_rounding.py` | New — offline demo reproducing the old synthetic-mid rejection and showing the bid/ask-derived price lands on the correct tick grid across several instruments/price bands |
| `scratch/scratch_premature_roll.py` | New — offline demo reproducing trades #206/#207 against the pre-fix logic and showing the decay-gate + far-leg-matching fix prevents both the premature roll and the same-expiry degenerate roll |

### Status

Complete — all seven sub-phases (22a–22g) implemented and tested; 616 tests passing (24 new). Config gains three keys (`CLOSE_PRICE_CROSS_BUFFER_PCT`, `TICK_SIZE_FETCH_RETRIES`, `MIN_ROLL_NEAR_FAR_GAP_DAYS`), mirrored into `config_test.py` for parity.

- **22a** — `db/state.py::load_calendar_state()` excludes `close_status == 'close_stuck'` from `open_positions`, so `_load_all_open_positions()` (the monitor's read path) genuinely stops retrying a stuck position, not just hiding it from Telegram.
- **22b** — new `db/state.py::get_visible_positions()` returns `result IN _OPEN_STATUSES` rows regardless of `close_status`; `handle_positions`/`handle_portfolio` use it and prefix stuck rows `⚠️ STUCK —` with `close_error_reason`. `get_open_trades()` unchanged.
- **22c** — `handle_info` wrapped in `try/except` with an explicit `cost_basis == 0` guard; `listener._build_app()` registers a global `add_error_handler` replying with a generic message instead of silence.
- **22d** — `_async_close_spread`/`_async_roll_near_leg` derive prices from live best bid/ask crossed by `CLOSE_PRICE_CROSS_BUFFER_PCT` (lift ask to buy back near, hit bid to sell far, symmetric on the unwind paths); tick-size fetch retries (`TICK_SIZE_FETCH_RETRIES`) and logs loud on failure; `tick_size_steps` honoured (`_effective_tick_size`); rounding done in `Decimal` tick-count space to avoid float drift.
- **22e** — `_mark_stuck_and_notify` consults the DB `close_status` (new `get_close_status()`) before notifying, so a restart-cleared in-memory dedup set can't produce a duplicate "MANUAL ACTION REQUIRED" alert.
- **22f** — the roll trigger additionally requires `near_days_left < near_days`-at-entry (genuine decay); `_try_roll` matches on `c.far_instrument == pos["far_instrument"]` and rejects a near candidate not preceding the far leg by `MIN_ROLL_NEAR_FAR_GAP_DAYS` (`_expiry_gap_days` helper).
- **22g** — full suite green (616 passing); offline demos `scratch/scratch_stuck_position_visibility.py`, `scratch/scratch_close_price_rounding.py`, `scratch/scratch_premature_roll.py`.

---

## Phase 23 — Feed Freshness Watchdog

### Root cause

`DeribitFeed` detects WS drops via TCP/ping failures (`DERIBIT_WS_PING_INTERVAL = 20s`, `DERIBIT_WS_PING_TIMEOUT = 20s`) but is blind to a different failure mode: Deribit stopping its data pushes while leaving the TCP connection open. In this scenario ping/pong heartbeats continue to succeed — the socket is alive — but no ticker notifications arrive, so all `ChainCache` entries age past their `CHAIN_CACHE_TTL_SEC = 30s` TTL within seconds of the last push. `ChainCache.get_chain()` then returns an empty list, the scanner finds 0 fresh instruments, and the bot idles indefinitely.

Observed 2026-07-19 on the test-mode instance (`logs/bot_test.log`):

- Last subscription event: `2026-07-19 07:55:54 AEST` — "Subscribing to 298 BTC instruments / 234 ETH instruments"
- No subsequent reconnect or subscription event in the log
- Every scan tick from ~08:00 to 15:46+ emits: "298 stale instrument(s) excluded from BTC chain / 234 stale instrument(s) excluded from ETH chain"
- 0 candidates on every scan; bot stays in IDLE with no indication the feed is dead
- Only recovery: manual restart

The count match (298 BTC / 234 ETH stale = 298 BTC / 234 ETH subscribed) confirms the cache was fully populated at subscription time, then never refreshed — the push stream died without the connection closing.

### Why the existing reconnect loop does not help

`DeribitFeed.start()` only catches `websockets.exceptions.ConnectionClosed`, `websockets.exceptions.WebSocketException`, and `OSError`. A silent data blackout raises none of these — the `_pump` coroutine's `async for raw_msg in ws` simply blocks forever waiting for a message that never arrives, and the surrounding reconnect loop never gets control back.

### Solution: freshness watchdog

Track the timestamp of the last ticker update received inside the feed, and if no update arrives within a configurable window, close the WS explicitly. This hands control back to the existing reconnect loop, which resubscribes and populates the cache as normal.

**Why close the WS rather than raise an exception directly?**

Closing the WS causes the `_pump` coroutine's `async for` to terminate cleanly, which propagates out of `_connect_and_stream()` and is caught by the reconnect loop's exception handler as a `ConnectionClosed`. No new reconnect code path is required — the existing backoff/reconnect logic is reused.

**`FEED_WATCHDOG_TIMEOUT_SEC`**

Default `120` (4× `CHAIN_CACHE_TTL_SEC`). The extra headroom above the 30s TTL avoids false positives during momentary quiet periods (e.g. a single asset has very few actively-quoted instruments and the 5-minute subscribe interval produces a burst-then-quiet pattern). At `120s` the worst case is 2 minutes of idle scanning; at `30s` (= 1× TTL) there is a risk of spurious reconnects on the quieter ETH/SOL test-exchange books. Set to `0` to disable the watchdog entirely.

### New/changed files

| File | Change |
| --- | --- |
| `config.py` | + `FEED_WATCHDOG_TIMEOUT_SEC = 120` |
| `config_test.py` | + `FEED_WATCHDOG_TIMEOUT_SEC` (parity) |
| `data/deribit_feed.py` | + `_last_ticker_at: float` initialised at connection; `_handle_message()` updates it on every ticker; + `async _watchdog(ws)` task launched alongside pump task in `_connect_and_stream()`, cancelled on any exception; skipped when `FEED_WATCHDOG_TIMEOUT_SEC == 0` |
| `tests/test_feed.py` | + `TestFeedFreshnessWatchdog` (4 tests) |
| `scratch/scratch_feed_watchdog.py` | New — connects to paper feed, simulates blackout, verifies watchdog reconnect |

### Biggest risks mitigated

| Risk | Mitigation |
| --- | --- |
| False-positive reconnect during a legitimately quiet market | `FEED_WATCHDOG_TIMEOUT_SEC` defaults to 4× TTL; can be raised or disabled via config |
| Watchdog and pump task racing to close the WS | Both tasks are cancelled in the same `except` block; `ws.close()` is idempotent |
| Reconnect loop back-off delaying recovery | Watchdog reconnects are a clean WS close, not an error — back-off resets to 1s on clean exit (`backoff = 1.0` line in `start()`) |

---

## Phase 24 — Reconcile Mismatch Remediation for close_stuck Positions

**Status:** Complete — all three improvements (24a–24c) implemented and tested; 636 tests passing (18 new). Offline demo: `python -m scratch.scratch_reconcile_mismatch` (read-only, no live orders). `portfolio/tracker.py` gains `get_deribit_open_positions()`, `_describe_deribit_positions()`, and `sync_stuck_positions()` (called at the top of every test/live `refresh()`); `_reconcile()` names the live Deribit instruments on a mismatch. `db/state.py` gains `mark_stuck_position_reconciled()`. `telegram_cmd/handlers.py` gains `handle_deribit_positions()`, wired via `TelegramCommandListener(portfolio=…)` from `bot.py` and registered in `COMMAND_REGISTRY`. `sync_stuck_positions()` requires **both** legs absent from the live Deribit list before marking a stuck trade closed and aborts on any position-fetch error, so a transient API failure can never falsely reconcile every stuck trade.

### Root cause

When `_mark_stuck_and_notify()` fires (after `POSITION_FAILURE_RETRY_CAP` failed close/roll attempts), it updates the DB `close_status` to `'close_stuck'` and fires a "MANUAL ACTION REQUIRED" Telegram alert, but it does not successfully close the Deribit position — that is the entire reason the position is stuck. The Deribit account therefore keeps the margin tied up from those legs.

The portfolio tracker's `_reconcile()` compares Deribit's live maintenance margin (from `private/get_account_summary`) against the DB's implied margin (sum of `net_debit` across open non-stuck trades). Because `get_open_trades()` and `load_calendar_state()` both exclude `close_stuck` rows, the DB margin is $0 even though Deribit is holding real collateral. The mismatch is detected as a 100% divergence and logged as a `WARNING` every scan cycle.

Observed 2026-07-19 (test-mode instance):

- Trades 6–9 all carry `close_status='close_stuck'`, `close_error_reason='Roll retry limit exceeded close failed after 4 attempts — position needs manual close on Deribit'`
- Both `date_close` and `pnl` are populated (the bot recorded a closure that never actually executed on the exchange)
- Every scan tick: `"RECONCILE MISMATCH: Deribit margin $1534.28 vs SQLite margin $0.00 (divergence 100%) — possible manual trade or missed fill"`
- The warning string does not name which Deribit instruments are live; the operator has no fast way to find and close them

Three distinct problems:

**1. The warning is not actionable.** The log line says "possible manual trade or missed fill" but gives no indication of what is actually open on Deribit. The operator must log in to the Deribit UI separately to find out.

**2. No auto-recovery when the operator manually closes the position.** Once the operator closes the legs on Deribit, the bot continues to fire the reconcile mismatch warning until its next restart — at which point the DB `close_stuck` trade still exists but the Deribit position is gone, producing a permanent mismatch between the two records.

**3. No Telegram visibility into what Deribit currently holds.** `/positions` and `/portfolio` (even with Phase 22's stuck-inclusive view) only show the bot DB perspective. There is no command to ask "what does Deribit think is open right now?"

### Solution

Three independent improvements, each usable in isolation:

**24a — Name the culprit instruments in the warning.** When `_reconcile()` detects a mismatch, call `private/get_positions` for each configured asset and include the returned instrument names, quantities, and mark values in the `WARNING` log line. This turns an unanswerable "something is wrong" into an actionable "BTC-15JUL26-64000-C qty=0.1 mark=$34 is open on Deribit but not tracked in DB".

**24b — Auto-reconcile after a manual Deribit close.** During each `refresh()` cycle, compare the live Deribit position list against the bot DB's `close_stuck` trades. If both legs of a stuck trade are absent from Deribit's list (the operator has closed them), mark that trade as `close_status='closed'` in the DB and log an `INFO` message. The reconcile warning then resolves automatically in the same or next cycle — no operator action beyond closing the Deribit position is required.

**24c — `/deribit_positions` Telegram command.** On demand, fetch and display the current Deribit position list with a cross-reference against the bot DB. Stuck DB trades still present on Deribit are called out explicitly. This gives the operator the same information they would get from the Deribit UI, directly in their Telegram chat.

### New/changed files

| File | Change |
| --- | --- |
| `portfolio/tracker.py` | + `get_deribit_open_positions(currency) -> list[dict]` (calls `private/get_positions`); updated `_reconcile()` logs instrument names on mismatch; + `sync_stuck_positions(db_path) -> list[int]` auto-closes stuck trades confirmed gone from Deribit; `refresh()` calls `sync_stuck_positions()` before the margin comparison |
| `telegram_cmd/handlers.py` | + `handle_deribit_positions(update, context)` — fetches and formats the Deribit position list with DB cross-reference |
| `telegram_cmd/listener.py` | + `("deribit_positions", "List positions currently open on Deribit — cross-check vs bot DB")` in `COMMAND_REGISTRY` |
| `tests/test_portfolio.py` | + `TestReconcileEnhanced` (4 tests) |
| `tests/test_telegram_cmd.py` | + `TestHandleDeribitPositions` (4 tests) |
| `scratch/scratch_reconcile_mismatch.py` | New — prints live Deribit position list vs `close_stuck` DB trades |

### Biggest risks mitigated

| Risk | Mitigation |
| --- | --- |
| `private/get_positions` fails (network, auth) during reconcile | `get_deribit_open_positions` returns `[]` on any exception (non-blocking); enhanced log line is skipped; existing warning still fires |
| Auto-reconcile marks a trade closed while a partial Deribit position still exists | `sync_stuck_positions` requires **both** `near_instrument` and `far_instrument` absent from the Deribit list before marking closed; a partially-closed leg keeps the trade in `close_stuck` |
| `/deribit_positions` called in paper mode (no Deribit account) | Handler is gated to return `"Command not available in paper mode."` when `TRADING_MODE == "paper"` |

---

## Phase 25 — Order-Amount Validity, Sizer/Executor Unification, Close-Fee Accuracy, Test-Liquidity Calibration, and Residual-Margin Reconciliation

**Status:** Complete — all five fixes (25a–25e) implemented and tested; full suite 656 passing (16 new tests across `test_executor.py`, `test_sizer.py`, `test_decision.py`, `test_portfolio.py`, `test_config_centralization.py`). Config gains four keys (`MAX_LEG_SPREAD_ABS_TICKS`, `MAX_LEG_SPREAD_ABS_USD`, `DEFAULT_MIN_TRADE_AMOUNTS`, `DEFAULT_MIN_TRADE_AMOUNT`), mirrored into `config_test.py` (with the tick floor enabled there). Offline demos: `python -m scratch.scratch_amount_validation` and `python -m scratch.scratch_account_margin_audit` (both read-only, no live orders). Surfaced by analysis of the 2026-07-17 → 2026-07-22 test-mode run (`logs/bot_test.log`). In that window the bot produced 82 RANK approvals (81 ETH, 1 BTC); **all 81 ETH entries were rejected by Deribit with `-32602 Invalid params`**, the single BTC entry filled (trade 13) at a third of its approved size, its close logged `close_fees=0.00`, and since 2026-07-21 00:21 zero candidates have passed the liquidity gate at all (582 near-leg-spread skips on 2026-07-22 alone). A persistent `RECONCILE MISMATCH` (~$1,583 Deribit margin vs $0 SQLite) survives Phase 24's remediation because the margin is not attributable to any `kind=option` position.

**Implementation summary:** 25a — `_AMOUNT_INFO_CACHE` + `_DeribitRPCClient.clamp_amount` / `_fetch_amount_info` + module-level `_clamp_amount_to_step` (Decimal step-count rounding); `place_order(validate_amount=True)` clamps/aborts (`AmountBelowMinimumError` → `AMOUNT GATE` skip), combo placement passes `validate_amount=False`; static fallback `config.DEFAULT_MIN_TRADE_AMOUNTS`; sizer rounds qty to the asset step and rejects sub-minimum qty (the RANK-stage skip). 25b — `_contract_amount()` removed; `_async_enter_spread` submits `candidate.qty`. 25c — `_async_close_spread` returns `(credit, near_close_usd, far_close_usd)`, `close_spread` stashes `last_close_fills`, `_close_position` uses them for `exit_fees` and `WARNING`-logs a fee-calc failure instead of silently zeroing. 25d — `MAX_LEG_SPREAD_ABS_TICKS`/`_USD` absolute floor in `_check_liquidity_gate` (disabled in live, tick floor on in test). 25e — `get_deribit_open_positions(kind=...)` (`"any"` includes futures), `get_deribit_open_orders`, reconcile widened to `_reconcile_currencies()` (COLLECTOR_ASSETS superset) naming futures + open orders, `/deribit_positions` shows them, `scratch/scratch_account_margin_audit.py`.

### Root causes

**25a/25b — `execution/executor.py::_contract_amount()` is dimensionally wrong and duplicates the sizer.** The sizer (`strategy/sizer.py`) computes a correct contract quantity (`max_usd / net_debit_usd`, e.g. `qty=9.5` ETH contracts, `qty=0.3` BTC). The executor then ignores that quantity and recomputes its own order amount as `max_usd / (net_debit_usd * spot)` — dividing by spot a second time even though `net_debit_usd` is already per-contract USD. The result is a near-zero value that is always floored to `MIN_CONTRACT_SIZE = 0.1` and submitted as `amount=0.1`:

- For **BTC** options (Deribit minimum trade amount 0.1 BTC, step 0.1) `amount=0.1` happens to be valid — so trade 13 filled, but at qty 0.1 instead of the sizer-approved 0.3 (a silent 3× under-size).
- For **ETH** options (Deribit minimum trade amount 1 ETH, integer steps) `amount=0.1` is invalid — so **every** ETH order dies with `-32602 Invalid params` before reaching the book. The asset split in the log is a perfect signature: 81/81 ETH rejected, 1/1 BTC filled.

The executor never validates the amount against the instrument's own `min_trade_amount` / amount step (both available from `public/get_instrument`, which the tick-size path already calls), so the invalid amount is discovered only as an opaque exchange rejection.

**25c — Exit fees fall back to 0 silently.** `strategy/decision.py::_close_position()` computes `close_fees_usd = exit_fees(asset, spot, qty, near_price, far_price)` from `pos.get("near_prem")`/`pos.get("far_prem")` inside a bare `try/except` that swallows any error into `close_fees_usd = 0.0`. Trade 13's close logged `close_fees=0.00` on a real two-leg exchange close — net P&L (−25.06) understates true fees. Two defects: the premiums come from the position dict (entry-time values, may be absent) rather than the actual close fill prices returned by `close_spread()`, and the except hides the failure.

**25d — Percentage-only spread gate starves test mode.** The liquidity gate rejects when `(ask-bid)/mid > MAX_LEG_SPREAD_PCT` (10% in `config_test.py`). On test.deribit.com books, near-leg option mids are frequently one or two ticks (e.g. bid 0.002 / ask 0.003 → "40% spread" — one tick wide), so the percentage test rejects instruments whose spread is literally the minimum possible. Result: since 2026-07-21 00:21, every scan's candidates are eliminated at this gate and the test instance can no longer exercise the order path at all. The gate needs an absolute floor: a spread of ≤ N ticks (or ≤ $X) is acceptable regardless of what percentage of a tiny mid it happens to be.

**25e — Residual margin invisible to reconcile.** Deribit reports ~$1,550–1,585 maintenance margin continuously since at least 2026-07-17 with **zero** tracked DB positions and — decisively — while trade 13 was open, Phase 24a's enhanced warning listed *only* trade 13's two legs (`Deribit margin $1625 vs SQLite $41.70`). So ~$1,583 of margin belongs to something `get_deribit_open_positions()` cannot see: that helper filters `private/get_positions` to `kind=option` on `ASSETS` currencies only. Candidates: futures/perpetual positions (e.g. from settlement of expired ITM options into futures on the test account), positions in a non-configured currency, or margin reserved by resting open orders. Because nothing is listed, Phase 24b's auto-reconcile can never fire and the warning repeats every 5 minutes indefinitely.

### Solution

**25a — Per-instrument amount validation.** Extend the executor's instrument-metadata cache (already fetched for tick size via `public/get_instrument`) to capture `min_trade_amount` and `contract_size`/amount step. New helper `_clamp_amount(instrument_name, amount)` rounds the requested amount down to the instrument's step and returns `None` if the result is below `min_trade_amount`. `enter_spread` / close / roll paths call it before every `place_order`; a `None` result aborts the attempt with an explicit `AMOUNT GATE` log line naming the instrument, requested amount, and exchange minimum — turning a cryptic `-32602` into an actionable skip. The decision layer applies the same check at RANK time so undersized candidates are skipped before approval.

**25b — Executor honours the sizer's qty.** Delete the duplicate sizing in `_contract_amount()`; `enter_spread` takes the sizer-approved qty as the order amount (then clamped by 25a). One sizing authority, one number in the logs. `MIN_CONTRACT_SIZE` remains only as a config-level sanity floor.

**25c — Accurate close fees.** `close_spread()` returns the actual close fill prices alongside the credit; `_close_position()` computes `exit_fees` from those fills (falling back to DB-loaded entry premiums only if fills are unavailable), and the bare `except` is replaced by a `WARNING` log naming the failed inputs so a zero-fee close can never pass silently again.

**25d — Absolute spread floor in the liquidity gate.** New config keys `MAX_LEG_SPREAD_ABS_TICKS` (spread ≤ N ticks always passes; default tuned for test) and/or `MAX_LEG_SPREAD_ABS_USD`. Gate logic becomes: pass if `spread_pct <= MAX_LEG_SPREAD_PCT` **or** `(ask-bid) <= abs-floor`. Defaults in `config.py` keep live behaviour unchanged (`MAX_LEG_SPREAD_ABS_TICKS = 0` disables the floor); `config_test.py` enables it so tick-wide testnet books pass. Config-parity test updated (Phase 21f requirement).

**25e — Widen reconcile visibility to all margin sources.** `get_deribit_open_positions()` gains a `kind` parameter and the reconcile/`/deribit_positions` paths fetch `kind=any` (options **and** futures) across all account currencies (from `private/get_account_summaries` or a configured superset), plus a count/value of resting open orders (`private/get_open_orders_by_currency`). The mismatch warning then names whatever actually holds the margin. A new scratch script dumps the full account state (positions of every kind, open orders, per-currency account summaries) for one-shot diagnosis of the current ~$1,583 residue; the operator then clears it manually on test.deribit.com, and `sync_stuck_positions` gains nothing new — the point is that nothing can hold margin invisibly again.

### New/changed files

| File | Change |
| --- | --- |
| `execution/executor.py` | `_contract_amount()` removed (25b); `enter_spread`/close/roll take sizer qty; + `_clamp_amount()` with cached `min_trade_amount`/step from `public/get_instrument` (25a); `close_spread()` returns close fill prices (25c) |
| `strategy/decision.py` | RANK-stage minimum-amount skip (25a); `_close_position()` uses close fills for `exit_fees`, WARNING on fee-calc failure (25c); liquidity gate abs-floor logic (25d) |
| `strategy/sizer.py` | Rounds qty to instrument step at sizing time so the approved qty is already submittable (25a/25b) |
| `config.py` / `config_test.py` | + `MAX_LEG_SPREAD_ABS_TICKS`, `MAX_LEG_SPREAD_ABS_USD` (25d); `MIN_CONTRACT_SIZE` demoted to sanity floor (25b); parity maintained |
| `portfolio/tracker.py` | `get_deribit_open_positions(currency, kind="any")`; reconcile covers futures + all currencies + open-order margin (25e) |
| `telegram_cmd/handlers.py` | `/deribit_positions` shows futures and open orders too (25e) |
| `tests/` | New tests per sub-phase (see BOT_TODO.md Phase 25) |
| `scratch/scratch_amount_validation.py` | New — fetches real instrument minimums for BTC/ETH options, demonstrates clamp/reject decisions (no orders) |
| `scratch/scratch_account_margin_audit.py` | New — dumps positions of every kind, open orders, and account summaries per currency to locate residual margin (read-only) |

### Biggest risks mitigated

| Risk | Mitigation |
| --- | --- |
| Instrument-metadata fetch fails at order time | `_clamp_amount` falls back to a conservative per-asset static minimum table in `config.py` and logs the fallback loudly (never silently submits an unvalidated amount) |
| Honouring sizer qty suddenly submits much larger test orders than the accidental 0.1s did | qty is still bounded by `MAX_LOSS_PCT` sizing, `MAX_QTY`, and the margin gate; test-mode `MAX_LOSS_PCT` is already halved |
| Absolute spread floor lets genuinely illiquid instruments through in live mode | Floor defaults to disabled in `config.py`; enabled only in `config_test.py`; two-sided-quote and OI gates still apply |
| `kind=any` position fetch returns unexpected shapes (futures fields differ from options) | Normalisation keyed per kind with defensive defaults; reconcile falls back to the option-only list on parse failure |
