# Calendar Spread Bot — Implementation Plan

## Overview

An automated trading bot that systematically scans, enters, monitors, and closes calendar spread positions on Deribit (crypto options exchange), using the optionsStrat repo as its foundation.

---

## What We Already Have (Reusable from optionsStrat)

| Module | Source | What it provides |
|---|---|---|
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
- Days-to-expiry matching (7d/30d, 14d/45d, etc.)
- Probability of profit derived from breakeven finding
- Expected value = P(profit) × avg_win − P(loss) × max_loss

### 2. Market Data Feed *(medium effort)*

Replaces the current interactive spot/IV input with a live polling loop:
- Fetches real-time spot, bid/ask, and IV per instrument from Deribit WebSocket API
- Caches option chains with configurable refresh cadence (e.g. 30s during active hours)
- Detects regime shifts (IV spike, liquidity gaps) to pause trading

### 3. Risk / Position Sizing Engine *(medium effort)*

Replaces the static `BUDGET_USD / spot` quantity with:
- Per-trade max loss as % of portfolio
- Maximum concurrent positions (especially if multi-asset)
- Correlation limits (e.g. avoid BTC + ETH calendars at the same strike simultaneously)

### 4. Decision Engine / State Machine *(medium effort)*

Replaces interactive menus with a rule-based engine:

```
SCAN → RANK → VALIDATE → ENTER → MONITOR → { ROLL | CLOSE }
```

Stop/TP conditions already exist in `check_calendar_status` — need to be called autonomously and acted on without human input.

### 5. Execution Hardening *(medium–hard)*

The current executor places market orders naively. For unattended trading:
- Submit both legs as a **combo/spread order** to eliminate leg risk
- Retry and fill-detection logic
- Slippage bounds — reject if fill price > X% from mid
- Order lifecycle tracking: open → partial fill → filled → cancelled

### 6. Scheduling / Reliability *(light effort)*

- Cron or event loop (asyncio or APScheduler) to drive scan/monitor cycles
- Reconnect logic for Deribit WebSocket drops
- Alerts (email/Telegram) on errors or large P&L moves

### 7. Backtesting Harness *(optional but strongly recommended)*

Replay historical option chain snapshots through the scanner + decision engine before going live. No historical data handling exists in the current repo.

---

## Project Scaffolding

### Repository Layout

```
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
│   └── test_backtest.py
├── config.py               # all tuneable parameters (thresholds, assets, sizing)
├── bot.py                  # entry point / scheduler
├── requirements.txt
└── README.md
```

### Bootstrap Steps

```bash
# 1. Create the repo
mkdir calendar-bot && cd calendar-bot
git init

# 2. Create the virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install core dependencies
pip install websockets aiohttp apscheduler scipy numpy

# 4. Copy reusable modules from optionsStrat
cp ../optionsStrat/market/pricing.py core/pricing.py
cp ../optionsStrat/trading/fee_calculator.py core/fees.py
cp ../optionsStrat/database/calendar_db.py db/state.py

# 5. Stub out each new module with __init__.py and placeholder classes

# 6. Wire up bot.py as the scheduler entry point
```

### Key Dependencies

| Package | Purpose |
|---|---|
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
|---|---|
| Leg risk on entry (one leg fills, other doesn't) | Use Deribit combo orders |
| IV collapse after entry | Check IV term structure before entering; set max IV drop stop |
| Liquidity gaps on crypto calendars | Enforce minimum OI/volume thresholds per strike |
| Overfitting scanner to recent market | Backtest across at least 2 vol regimes |
| Position stuck as "far leg only" (illiquid) | Already modeled in optionsStrat — good foundation |
| WebSocket disconnection mid-trade | Reconnect with state reconciliation against Deribit REST API |
| Runaway losses in volatile market | Hard daily loss limit; halt + alert if breached |

---

## Decision Engine State Machine

```
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
┌─────────┐   fails risk check    ┌──────────────┐
│  RANK   │──────────────────────►│  SKIP trade  │
└────┬────┘                       └──────────────┘
     │ approved
     ▼
┌─────────┐   order rejected      ┌──────────────┐
│  ENTER  │──────────────────────►│  LOG & RETRY │
└────┬────┘                       └──────────────┘
     │ filled
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
CLOSE → log result → IDLE
```

---

## Configuration Parameters (config.py)

```python
# Assets to trade
ASSETS = ["BTC", "ETH"]

# Calendar horizons
NEAR_DAYS_OPTIONS = [7, 14]
FAR_DAYS_OPTIONS  = [30, 45, 60]

# Entry filters
MIN_IV_CONTANGO   = 0.02    # front IV must be >= back IV + 2%
MIN_POP           = 0.45    # minimum probability of profit
MIN_OI_NEAR       = 100     # minimum open interest on near strike
MIN_OI_FAR        = 100     # minimum open interest on far strike

# Sizing
MAX_LOSS_PCT      = 0.02    # max 2% of portfolio per trade
MAX_POSITIONS     = 3       # max concurrent open calendars

# Stop / take-profit (mirrors optionsStrat CALENDAR_STOP_PCT)
STOP_PCT          = 0.50    # close if spread worth < 50% of debit
TAKE_PROFIT_PCT   = 1.50    # close if spread worth > 150% of debit

# Scheduler
SCAN_INTERVAL_SEC    = 300   # 5 minutes
MONITOR_INTERVAL_SEC = 60    # 1 minute

# Broker
DERIBIT_PAPER     = True     # set False for live trading
DAILY_LOSS_LIMIT  = 500      # USD — halt bot if exceeded
```

---

## Estimated Effort

| Layer | Effort |
|---|---|
| Port + clean core modules | 1–2 days |
| Live WebSocket data feed | 3–5 days |
| Scanner / ranker | 2–3 days |
| Decision state machine | 2–3 days |
| Execution hardening | 3–5 days |
| Scheduling + alerts | 1–2 days |
| Backtesting harness | 3–5 days |
| Testing + paper trading validation | 3–5 days |
| **Total** | **~3–6 weeks solo** |
