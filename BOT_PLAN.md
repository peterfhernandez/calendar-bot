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
| Leg risk on close (one leg fills, other times out) | Close operations unwind partial fills: if near fills but far times out, reverse-sell near at market; if far fills but near times out, reverse-buy far at market. Deribit API errors on close are caught and retried up to 3 times; on 4th failure, position is force-closed to prevent naked leg accumulation |
| Unbounded retry on failed close/roll (naked leg accumulation) | Track failed close/roll attempts per position in `_close_roll_failures` dict; cap at 3 retries, then **intended to** force-close/mark-stuck on 4th failure. **Known gap (Phase 18, not yet fixed):** the "mark as stuck" branches reset `_close_roll_failures` to zero immediately after marking, and `get_open_trades()` never filters out `close_status == 'close_stuck'` positions, so the position keeps being monitored and the same failing close keeps retrying indefinitely rather than actually stopping. Confirmed in test-mode logs: trade_id=3 re-marked "stuck" 40 times over ~29 hours; trade_id=5 stuck 6+ days before a manual `/close_manually`. |
| Close order rejected by exchange (`-32602 Invalid params`) | **Known gap (Phase 18, not yet fixed):** `execution/executor.py` blanket-rounds every order price to 4 decimals and never fetches an instrument's actual Deribit tick size (`tick_size_steps` scales with option premium). Far-leg close orders — typically priced higher than the near leg — land in a coarser tick band and get rejected by Deribit's JSON-RPC layer before any business-logic check runs. Confirmed in test-mode logs: 636 occurrences of `Deribit error -32602: Invalid params`, 100% on far-leg close submissions, 0% on near-leg. See Phase 18 below for the fix design. |
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
| **`/pnl` equity-curve chart (16)** | **1–1.5 days** | **Not started** |
| **Cross Portfolio Margin entry gate (17)** | **2–3.5 days** | **Not started** |
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

### Related observation — BTC/ETH entry skew in test mode (not a bug)

Test-mode entries were 6 BTC to 1 ETH. This traces to `test.deribit.com`'s ETH options book being much thinner than BTC's: ETH candidates were rejected by the liquidity gate (`MAX_LEG_SPREAD_PCT`) roughly 2.7x more often than BTC (27,042 vs 10,035 skips in the log history) despite `ASSETS = ["BTC", "ETH"]` weighting both equally. This is a test-exchange liquidity artifact, not a config or scanner defect, and is not expected to persist on the live orderbook — no action needed, called out here only so it isn't mistaken for a Phase 18 bug.

### New/changed files (planned)

| File | Change |
| --- | --- |
| `execution/executor.py` | + tick-size lookup/cache; round all order prices to valid ticks at every price-producing call site |
| `strategy/decision.py` | Stop resetting `_close_roll_failures` on mark-stuck; skip re-evaluation of `close_stuck` positions; ensure force-close paths compute real PnL |
| `db/state.py` | Possibly extend `get_open_trades()` (or add a variant) to exclude `close_status == 'close_stuck'` from routine monitor re-evaluation |
| `tests/test_executor.py` | + tick-size rounding tests |
| `tests/test_decision.py` | Rewrite the two "fourth failure" tests to assert frozen/excluded behavior instead of reset-and-retry; + zero-PnL regression test |
| `scratch/scratch_tick_size_close.py` | New — demonstrates the tick-size bug and fix against test.deribit.com (read-only ticker calls; no live orders unless `TRADING_MODE` explicitly allows it) |

### Status

Not started — this phase documents root causes found via log/DB forensics on 2026-07-07. Implementation is tracked as an open checklist in BOT_TODO.md Phase 18.
