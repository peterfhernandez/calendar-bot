# Calendar Spread Bot

An automated trading bot that systematically identifies, enters, monitors, and exits **calendar spread** positions on the [Deribit](https://www.deribit.com) cryptocurrency options exchange.

---

## What it does

A calendar spread (also called a time spread or horizontal spread) buys a longer-dated option and sells a shorter-dated option at the **same strike**. The trade profits when the underlying stays near the strike at near-leg expiry, harvesting the difference in time decay between the two legs.

This bot automates the full lifecycle:

1. **Scan** — every 5 minutes, evaluates BTC and ETH option chains for calendar spread opportunities across multiple strike/expiry combinations
2. **Rank** — scores each candidate on IV term structure (contango), probability of profit, expected value, and liquidity
3. **Enter** — places spread orders on the top-ranked opportunity within risk limits (max 2% of portfolio per trade, max 3 concurrent positions)
4. **Monitor** — checks open positions every minute; alerts on stop-loss (spread value < 50% of debit paid) or take-profit (> 150% of debit)
5. **Roll or Close** — at near-leg expiry, either rolls to a new near leg if the setup is still favourable, or closes the full position and logs the result

---

## Strategy edge

- **Theta decay differential** — the short near leg decays faster than the long far leg, especially in the final week before expiry
- **IV term structure** — the bot only enters when front-month IV is elevated relative to back-month IV (contango ≥ 2%), maximising the premium collected on the near leg
- **Defined risk** — maximum loss is the net debit paid at entry; there is no unlimited downside

---

## Key parameters

| Parameter | Default | Description |
|---|---|---|
| Assets | BTC, ETH | Underlyings to trade |
| Near leg | 7–14 days | Short option expiry |
| Far leg | 30–60 days | Long option expiry |
| Min IV contango | 2% | Front IV must exceed back IV by at least this |
| Min prob of profit | 45% | Entry filter |
| Max loss per trade | 2% of portfolio | Position sizing |
| Stop loss | 50% of debit | Auto-close trigger |
| Take profit | 150% of debit | Auto-close trigger |

---

## Architecture

The bot is a separate project (`calendar-bot/`) that ports the core pricing, fee, and broker logic from [optionsStrat](../optionsStrat) and adds a live data feed, ranking engine, decision state machine, and hardened order execution.

See [BOT_PLAN.md](BOT_PLAN.md) for the detailed implementation plan and scaffolding.

---

## Paper trading first

The bot defaults to Deribit's **paper trading** environment (`DERIBIT_PAPER = True`). Validate performance for at least 4–6 weeks in paper mode before switching to live. Key things to verify in paper mode:

- Scanner selects setups that actually profit at expiry
- Stop-loss and take-profit triggers fire correctly
- Roll logic produces better outcomes than outright close
- Daily loss limit halts the bot as expected

---

## Requirements

- Python 3.11+
- Deribit account (paper or live) with API key
- Dependencies: `websockets`, `aiohttp`, `apscheduler`, `scipy`, `numpy`

---

## Relationship to optionsStrat

| optionsStrat | calendar-bot |
|---|---|
| Manual paper trading UI | Fully automated |
| Single position at a time | Up to 3 concurrent positions |
| Interactive spot/IV input | Live Deribit WebSocket feed |
| SQLite state | SQLite state (same schema) |
| Black-Scholes pricing | Same (ported) |
| Deribit executor | Same (hardened for unattended use) |
