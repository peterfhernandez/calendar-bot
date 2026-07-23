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
| --- | --- | --- |
| `ASSETS` | BTC, ETH | Underlyings the **bot** scans, enters, and manages positions for |
| `COLLECTOR_ASSETS` | BTC, ETH, SOL | Underlyings the **data collector** gathers option-chain snapshots for — can be a superset of `ASSETS` |
| Near leg | 1–14 days | Short option expiry (1d, 7d, 14d) |
| Far leg | 7–60 days | Long option expiry (7d, 14d, 30d, 45d, 60d) |
| Min IV contango | 2% | Front IV must exceed back IV by at least this |
| Min prob of profit | 45% | Entry filter |
| Max loss per trade | 2% of available cash | Position sizing (based on live account cash, not static budget) |
| Stop loss | 50% of debit | Auto-close trigger |
| Take profit | 150% of debit | Auto-close trigger |
| Min leg bid/ask size | 1 contract | Liquidity gate — both legs must have real size |
| Max leg spread | 5% of mid | Liquidity gate — wide-spread legs are rejected |
| Max entry premium | 10% over spread mid | Liquidity gate — prevents entering deeply underwater due to bid/ask friction |
| Max margin utilization | 80% of equity | Cross Portfolio Margin gate — rejects entries/rolls that would push `maintenance_margin / equity` past this ceiling |
| Options fee | 0.03% of underlying/leg | Deribit taker and maker rate (BTC & ETH); SOL maker fee is 0% |
| Combo leg discount | 100% on cheaper leg | Taker combo orders pay fee on the expensive leg only |
| Delivery fee | 0.015% of underlying | Charged at expiry for monthly+ options ITM; daily/weekly options are exempt |

---

## Architecture

The bot is a separate project (`calendar-bot/`) that ports the core pricing, fee, and broker logic from [optionsStrat](../optionsStrat) and adds a live data feed, ranking engine, decision state machine, and hardened order execution.

Key architectural components:

- **Portfolio tracker** (`portfolio/tracker.py`) — fetches live account equity from Deribit, tracks available cash and used margin, and feeds real deployable capital into the sizing engine. Replaces the static `BUDGET_USD` config parameter.
- **Liquidity gate** (in `strategy/decision.py`) — two-stage filter: coarse OI check in the scanner, then a fine bid/ask size and spread check just before order submission. Both legs must pass or the trade is skipped.
- **Margin gate** (in `strategy/decision.py`, backed by `portfolio/tracker.py`) — before entering or rolling, checks whether the trade would push the account's Cross Portfolio Margin utilization (`maintenance_margin / equity`) past `MAX_MARGIN_UTILIZATION_PCT`. Uses Deribit's own margin-simulation numbers where available, since X:PM margin is a whole-portfolio stress test rather than a simple sum of position debits; falls back to a conservative local estimate otherwise. Fails closed (rejects) in test/live mode when live margin data can't be obtained, since this gate exists specifically to prevent forced liquidation.
- **Per-asset threshold overrides** (`ASSET_OVERRIDES` in `config.py`) — each asset can have its own OI, spread, entry-premium, and IV-contango thresholds. SOL options are thinner than BTC/ETH and use relaxed defaults; BTC and ETH use the tighter global values. Add any asset to `ASSET_OVERRIDES` to tune its filters independently.
- **Combo orders** (in `execution/executor.py`) — both legs submitted atomically via Deribit's combo order API, eliminating leg risk. Falls back to sequential individual legs only if the combo times out and both legs have sufficient liquidity; the fallback cancels the near leg immediately if the far leg fails. Close operations are hardened against partial fills (if near closes but far times out, a reverse sell is submitted; if far closes but near times out, a reverse buy unwinds the partial). API errors on close orders are caught and retried up to 3 times per position; on the 4th failure the position is marked `close_stuck`, a single "MANUAL ACTION REQUIRED" alert is sent, and the position is excluded from routine monitoring until the operator intervenes via `/close` or `/close_manually` (Phase 19 restored this retry ladder after a Phase 18 regression made the first failure mark positions stuck immediately, without any retry or alert).
- **Roll P&L tracking** (`strategy/decision.py`, `db/state.py`) — when a near leg is rolled, the profit from closing the old near leg at a better price is calculated and recorded (`roll_pnl`), then included in the final position P&L when the trade closes. New near-leg candidates at roll time are validated with the same liquidity gate as entry, and EV is recalculated to ensure the new setup remains profitable. Both initial EV (at entry) and roll EV (if rolled) are displayed in position details.
- **Notifications** (`alerts/notifier.py`, wired into `strategy/decision.py`) — every decision point (entry, stop, TP, roll, close, daily limit, error) fires an email and/or Telegram alert with deduplication. Retry logic handles transient failures (network timeouts, rate limits) with 2 attempts and 1-second delays. Stuck-position notifications are sent only once per position to prevent spam during retry loops; the flag is cleared when the user intervenes via `/close` or `/close_manually` commands.
- **Fee model** (`core/fees.py`, wired into scanner, sizer, decision engine, executor, and backtest) — Deribit charges 0.03% of the underlying per leg per trade (minimum 0.0003 BTC/ETH/SOL per contract, capped at 12.5% of option value). Combo orders receive a 100% taker discount on the cheaper leg. Delivery fees of 0.015% apply at expiry for monthly and longer options (daily and weekly near legs are exempt). Fees are deducted from EV scores before entry, included in max-loss sizing, evaluated before each roll (rolls that cost more than the expected theta gain are skipped), and applied in paper mode so paper P&L reflects real economics. All PnL figures — Telegram commands (`/positions`, `/portfolio`, `/status`, `/closed_trades`, `/pnl`), the DB `pnl` column, and internal accumulators — report **net** PnL after deducting entry and exit fees.
- **Log hygiene and secret prevention** (`monitor/loop.py`, `portfolio/tracker.py`, `data/deribit_feed.py`) — `httpx`, `httpcore`, and `telegram` loggers are set to WARNING so the high-frequency `getUpdates` polling calls do not flood the log. A `_SecretRedactor` filter on the root logger replaces any occurrence of any `.env` secret (all Deribit API keys, SMTP credentials, and Telegram token/chat ID) in log output with `<redacted>`. Deribit authentication credentials are sent as a JSON POST body rather than URL query parameters so they cannot appear in HTTP error messages or exception tracebacks. Run `python -m scratch.scrub_logs` once to redact any secrets that may already be present in existing `logs/bot.log*` rotation files.
- **Centralized configuration** (`config.py`, `core/logging_setup.py`) — every tunable value lives in `config.py`: strategy thresholds, WS/RPC timeouts and retry counts, alert cooldowns, logging format/rotation (applied through the shared `setup_logging()` helper used by every entry point), business-logic tables (strike increments, far-leg spread model), retry caps, DB paths, timezone, and date format. Per-instance override files (`--config`) can change any of them without touching module code.
- **Offline error tracking** (`data/deribit_feed.py`, `portfolio/tracker.py`) — repeated connectivity failures are logged only once at `WARNING` level when connectivity is first lost, then suppressed to `DEBUG` for subsequent retries. Recovery is logged at `INFO` with the retry count. This prevents log flooding during network outages or when running without internet access.

See [BOT_PLAN.md](BOT_PLAN.md) for the full design and [BOT_TODO.md](BOT_TODO.md) for progress.

---

## Running parallel instances (paper + test)

To run a paper-mode and a test-mode instance at the same time — for example, to observe live execution on test.deribit.com while the paper bot continues recording simulated trades — give each instance its own env file, database, and log file.

**Step 1 — create `.env.test`**

Copy `.env` and set:

```ini
TRADING_MODE=test
BOT_DB_PATH=calendar_bot_test.db
BOT_LOG_FILE=logs/bot_test.log
BOT_CONFIG_FILE=config_test.py   # optional — omit if no strategy overrides needed
# credentials are the same (both modes share DERIBIT_TEST_* keys)
```

**Step 2 — optionally create `config_test.py`**

A plain Python file that overrides only the strategy parameters you want to change. Anything not listed here inherits from `config.py`.

```python
# config_test.py
ASSETS        = ["BTC"]   # trade only BTC while testing
MAX_POSITIONS = 1         # one position at a time for controlled observation
MAX_LOSS_PCT  = 0.005     # 0.5% max loss per trade (half the paper default)
```

**Step 3 — start both instances**

```bash
# Terminal 1 — paper mode (uses .env, calendar_bot.db, logs/bot.log)
python bot.py

# Terminal 2 — test mode (uses .env.test, calendar_bot_test.db, logs/bot_test.log)
python bot.py --env .env.test
```

You can also override the database, log path, or config file directly on the CLI without modifying the env file:

```bash
python bot.py --env .env.test --db calendar_bot_test.db --log logs/bot_test.log --config config_test.py
```

The four flags (`--env`, `--db`, `--log`, `--config`) are pre-parsed before any module imports so `TRADING_MODE`, `DB_PATH`, the log file path, and all strategy overrides are resolved correctly at startup.

---

## Known issues

Analysis of a test-mode run (`db/calendar_bot_test.db`, `logs/bot_test.log*`, 2026-06-28 → 2026-07-07) surfaced four bugs in the close/retry and feed-coverage paths, all now fixed and tracked in [BOT_TODO.md Phase 18](BOT_TODO.md#phase-18--close-order-reliability--stuck-position-retry-bugfixes) with full root-cause detail in [BOT_PLAN.md](BOT_PLAN.md#phase-18--close-order-reliability--stuck-position-retry-bugfixes):

- **Far-leg close orders could be rejected by Deribit** (`-32602 Invalid params`) because the executor didn't account for per-instrument tick size when rounding order prices. Fixed — order prices are now rounded to each instrument's own tick size.
- **The retry-cap "mark as stuck" safety net didn't stop retrying** — stuck positions weren't excluded from routine monitoring, so the bot could keep retrying the same failing close indefinitely. Fixed — `close_stuck` positions are skipped by the monitor until cleared via `/close` or `/close_manually`.
- **A force-closed position was recorded with `pnl=0.0`** instead of its real loss in one observed case. Fixed — positions the executor can't close are marked stuck rather than recorded with a fake P&L.
- **The WebSocket feed's ticker subscription window could silently stop covering a long-dated open position's far leg after a reconnect**, since the subscription window was tied to the scanner's day-window config rather than to actually-open positions. This disabled stop-loss/take-profit monitoring for the affected position (repeated "No IV for trade N — skipping status check" warnings). Fixed — the feed now unions the near/far instrument names of every open position (from `db/state.py::get_open_instrument_names`) into its subscription list on every connect and reconnect, so open-position legs stay covered regardless of the configured day window.

Positions can still become `close_stuck` when the exchange repeatedly rejects a close (`/positions` or the "MANUAL ACTION REQUIRED" Telegram alert will flag them) — use `/close` to retry or `/close_manually` to resolve them.

**Silent WebSocket data blackout caused the bot to idle indefinitely** — the feed's reconnect logic only triggered on TCP/ping failures. If Deribit stopped pushing ticker data while keeping the connection open (observed 2026-07-19: feed subscribed at 07:55 AEST, no reconnect, all 532 cached snapshots stale by ~08:00, zero candidates on every scan for 7+ hours), no recovery was triggered and the bot sat idle until manually restarted. Now fixed ([BOT_TODO.md Phase 23](BOT_TODO.md#phase-23--feed-freshness-watchdog) / [BOT_PLAN.md Phase 23](BOT_PLAN.md#phase-23--feed-freshness-watchdog)): a background watchdog task inside `DeribitFeed` tracks the timestamp of the last ticker update and, if no update arrives within `FEED_WATCHDOG_TIMEOUT_SEC` (default 120s, 4× the 30s cache TTL; set `0` to disable), closes the WS — handing control to the existing reconnect loop, which resubscribes automatically. The watchdog starts only after the initial subscription pass so a slow first tick can't trip a false positive. Demonstrated offline by `python -m scratch.scratch_feed_watchdog`.

**Reconcile mismatch warnings from `close_stuck` positions were not actionable** — when the bot marks a position `close_stuck` after retry exhaustion (e.g. trades 6–9, `BTC-15JUL26-*` options), the Deribit position stays open and the portfolio tracker detects a margin mismatch every scan cycle. The warning did not name which Deribit instruments were causing it, there was no auto-recovery when the operator manually closed those legs on the exchange, and there was no Telegram command to inspect the live Deribit position list. Now fixed ([BOT_TODO.md Phase 24](BOT_TODO.md#phase-24--reconcile-mismatch-remediation-for-close_stuck-positions) / [BOT_PLAN.md Phase 24](BOT_PLAN.md#phase-24--reconcile-mismatch-remediation-for-close_stuck-positions)): the `RECONCILE MISMATCH` warning now names the live Deribit instruments (`PortfolioTracker.get_deribit_open_positions` + `_describe_deribit_positions`); `PortfolioTracker.sync_stuck_positions` (called at the top of every `refresh()`) auto-marks a `close_stuck` DB trade `closed` — via `db/state.py::mark_stuck_position_reconciled`, preserving recorded pnl — once **both** legs are confirmed gone from Deribit, and aborts on any position-fetch error so an API failure never falsely reconciles; and a new `/deribit_positions` Telegram command lists the live Deribit positions, flags any instrument not tracked in the bot DB, and hints when stuck trades are still open on the exchange. Demonstrated offline by `python -m scratch.scratch_reconcile_mismatch`.

An audit found roughly 94 config-like values (log levels/formats, timeouts, retry counts, magic-number thresholds, hardcoded paths) hardcoded outside `config.py` instead of being centralized there, plus two functional bugs stemming from the bypass (SOL orders never reconciled on restart; a debug-tool cache TTL that ignores `CHAIN_CACHE_TTL_SEC`). All of it is now fixed — [BOT_TODO.md Phase 20](BOT_TODO.md#phase-20--centralize-scattered-config-into-configpy) / [BOT_PLAN.md Phase 20](BOT_PLAN.md#phase-20--centralize-scattered-config-into-configpy) moved every value into `config.py` (logging via a shared `core/logging_setup.py::setup_logging()` helper; WS/RPC timeouts, retry counts, alert cooldowns, strike-increment and spread-model tables, roll/retry thresholds, DB paths, timezone, and date format as documented config keys), fixed both bugs, and added regression tests (`tests/test_config_centralization.py`) plus an offline demo (`python -m scratch.scratch_config_centralization`).

**Runaway churn on deep ITM/OTM calendar spreads** — analysis of the 2026-07-14 paper-mode run (`db/calendar_bot.db`, `logs/bot.log`) found 131 trades opened and closed the same day (91 of them the same ETH 1400 Call instrument, 23 more on two deep-ITM BTC put strikes), driven by a ranking formula in `strategy/scanner.py` that divides expected value by `net_debit` — a value that collapses toward zero for deep in/out-of-the-money strikes, producing EV scores orders of magnitude above any real candidate and guaranteeing the same degenerate, thinly-quoted instrument won every scan. Those same near-zero-debit positions then tripped the percentage-of-debit stop/take-profit thresholds on ordinary quote noise within a single monitor tick, closed almost immediately, and were re-entered on the next 5-minute scan since the correlation gate only checks currently-*open* positions. Now fixed ([BOT_TODO.md Phase 21](BOT_TODO.md#phase-21--fix-runaway-deep-itm-calendar-churn--close-status-tracking-bug) / [BOT_PLAN.md Phase 21](BOT_PLAN.md#phase-21--fix-runaway-deep-itm-calendar-churn--close-status-tracking-bug)) with five layered guards: an EV-ranking cap (`EV_SCORE_RANKING_CAP`) that demotes near-zero-debit degenerates below every real candidate; a moneyness entry filter (`MAX_MONEYNESS_PCT`, per-asset overridable) that rejects strikes far from spot before they are ever scored; a two-sided-quote requirement (`MARKET_SV_REQUIRE_TWO_SIDED`) so a lone synthetic `mark_price` on a thin book is no longer trusted for stop/TP; a close-confirmation debounce (`CLOSE_CONFIRM_TICKS`) requiring the stop/TP condition to hold across consecutive monitor ticks; and a per-instrument re-entry cooldown (`REENTRY_COOLDOWN_SEC`) that blocks immediately reopening a just-auto-closed strike. A separate, unrelated bug found in the same analysis — `db/state.py::close_calendar_trade()` (the normal auto-close path) never set `close_status`, so every auto-closed trade still showed `close_status='open'` despite `result`/`date_close`/`pnl` being correct — is also fixed (the function now sets `close_status='closed'`; `scratch/scratch_backfill_close_status.py` corrects historical rows). Finally, `config_test.py` (the test-mode `--config` override file) was backfilled with the ~45 Phase 17/20/21 keys it had drifted from, now held at exact parity with `config.py` by a regression test.

**Stuck positions vanished from `/positions`/`/portfolio`, `/info` could go silent, alerts could duplicate across a restart, and the underlying close-order rejection was still live** — a test-mode session showed `/positions` reply "No open positions" and `/info trade_id=N` reply with nothing at all immediately after a "MANUAL ACTION REQUIRED" alert for that same trade, and the same alert firing twice across a service restart. Now fixed ([BOT_TODO.md Phase 22](BOT_TODO.md#phase-22--stuck-position-visibility-silent-telegram-failures-and-close-order-price-rejections) / [BOT_PLAN.md Phase 22](BOT_PLAN.md#phase-22--stuck-position-visibility-silent-telegram-failures-and-close-order-price-rejections)): the monitor's own read path (`_load_all_open_positions`/`load_calendar_state`) now excludes `close_stuck` positions, so the engine genuinely stops retrying (and re-notifying about) a stuck position instead of merely hiding it from Telegram; a new stuck-inclusive query (`get_visible_positions()`) backs `/positions` and `/portfolio` so stuck positions are shown flagged (`⚠️ STUCK —` with the error reason) rather than omitted; `handle_info` is wrapped in error handling with an explicit zero-cost-basis guard, and the Telegram listener registers a global error handler so any unhandled command-handler exception replies instead of failing silently; `_mark_stuck_and_notify` consults the DB `close_status` before alerting so a restart-cleared dedup set can't produce a duplicate "MANUAL ACTION REQUIRED" message. The close/roll order rejection that got positions stuck in the first place is fixed at its source: `execution/executor.py` now derives close/roll prices from the live best bid/ask (crossed by `CLOSE_PRICE_CROSS_BUFFER_PCT`) instead of a synthetic `mid * 1.02`/`mid * 0.98`, honours Deribit's per-price-band `tick_size_steps`, rounds in `Decimal` tick-count space to avoid float drift, and logs tick-size fetch failures loudly (with retries) instead of silently swallowing them. A related, smaller-scale bug found in the paper-mode run is also fixed: a freshly-opened 1-day-near position was rolled/closed roughly a minute after entry because `ROLL_TRIGGER_DAYS` (`2`) made it roll-eligible from its very first monitor tick regardless of real elapsed time — the roll trigger now also requires genuine decay (`near_days_left < near_days`-at-entry), and `_try_roll` matches candidates against the position's own far leg and rejects any new near leg not preceding that far leg by `MIN_ROLL_NEAR_FAR_GAP_DAYS`, closing the "degenerate same-expiry spread fools stop/TP" recurrence (trade #207) on the roll path.

**Phase 22's tick-size lookup called the wrong Deribit endpoint, breaking every order submission** — the Phase 22 close/roll price fix added a tick-size lookup that called `public/get_instruments` (plural — the list-all-instruments-for-a-currency endpoint, which doesn't accept `instrument_name`) instead of `public/get_instrument` (singular — which accepts `instrument_name` and returns a single instrument object). The plural endpoint returned a list, so parsing raised `'list' object has no attribute 'get'`, was swallowed by a generic `except`, and the code silently fell back to naive 4-decimal rounding — the exact off-tick-price problem the fix was meant to solve. Deribit then rejected every order with `-32602 Invalid params`; since entry retries only cover `OSError`/`WebSocketException`/`TimeoutError`, each entry died as `ENTER rejected by executor` (scans found candidates but no submission ever landed), and the same broken lookup blocked the close/roll path so expired near legs had to be resolved manually. Now fixed: all three call sites (`get_instrument()`, `_fetch_tick_info()`, and `_fetch_and_cache_tick_size()`) call `public/get_instrument` and parse the returned instrument object directly rather than expecting a `{"instruments": [...]}`/list shape. Demonstrated offline by `python -m scratch.scratch_rpc_method_fix`.

**Every ETH order rejected, BTC orders silently under-sized, close fees logged as zero, testnet liquidity gate starving all entries, and an unexplained ~$1,583 margin residue** — analysis of the 2026-07-17 → 2026-07-22 test-mode run found five further issues, now fixed ([BOT_TODO.md Phase 25](BOT_TODO.md#phase-25--order-amount-validity-sizerexecutor-unification-close-fee-accuracy-test-liquidity-calibration-and-residual-margin-reconciliation) / [BOT_PLAN.md Phase 25](BOT_PLAN.md#phase-25--order-amount-validity-sizerexecutor-unification-close-fee-accuracy-test-liquidity-calibration-and-residual-margin-reconciliation)): (1) the executor's dimensionally-wrong `_contract_amount()` (dividing by spot twice) always collapsed the order amount to the 0.1 floor — valid for BTC options (min 0.1) but rejected with `-32602 Invalid params` for ETH options (min 1 ETH, integer steps), so 81 of 81 ETH approvals failed at the exchange while the single BTC approval filled. Fixed — `_contract_amount()` is removed and the executor submits the sizer-approved qty, clamped per instrument against the live `min_trade_amount`/amount step (`public/get_instrument`, `_clamp_amount_to_step`); a below-minimum amount is skipped with an `AMOUNT GATE` warning instead of a cryptic exchange rejection, and the sizer rounds qty to the asset's step (BTC 0.1, ETH 1) so the approved qty is already submittable. (2) The same duplicate sizing had the executor ignore the sizer-approved qty (trade 13 approved at 0.3, filled at 0.1) — fixed by the same unification. (3) `_close_position()` swallowed exit-fee calculation errors into `close_fees=0.00`, understating net P&L — now `close_spread()` returns the actual close fill prices and `_close_position()` computes `exit_fees` from them, logging a loud `WARNING` (naming the inputs) instead of silently zeroing. (4) The percentage-only spread gate rejected one-tick-wide testnet books as "40–100% spread", starving test mode of all entries — an absolute spread floor (`MAX_LEG_SPREAD_ABS_TICKS`/`MAX_LEG_SPREAD_ABS_USD`, both disabled by default in `config.py`, tick floor enabled in `config_test.py`) now lets minimum-width books pass. (5) ~$1,583 of Deribit margin persisted with no `kind=option` position to explain it, invisible to Phase 24's reconcile — position fetching now widens to futures (`get_deribit_open_positions(kind="any")`), the `COLLECTOR_ASSETS` currency superset, and resting open orders (`get_deribit_open_orders`), all surfaced in the reconcile warning and `/deribit_positions`, with a read-only `scratch/scratch_account_margin_audit.py` to identify any residue.

**Healthy positions liquidated on a single failed roll, doomed-by-construction short-DTE entries, mark-vs-executable valuation gaps, and untracked exchange inventory from failed legged entries** — analysis of the 2026-07-22 → 2026-07-23 test-mode run (trades 14–18) and the 2026-07-22 paper-mode close of trade 208 surfaced five further issues, now fixed ([BOT_TODO.md Phase 26](BOT_TODO.md#phase-26--legged-entry-fill-safety-roll-failure-resilience-entry-tenor-alignment-and-executable-value-monitoring) / [BOT_PLAN.md Phase 26](BOT_PLAN.md#phase-26--legged-entry-fill-safety-roll-failure-resilience-entry-tenor-alignment-and-executable-value-monitoring)): (1) a roll failure closed the position on the **first** failed attempt (`POSITION_FAILURE_RETRY_CAP` never applied on that path), and the roll candidate search inherited entry-grade filters plus a top-match-only selection that could return the currently-held near leg — three healthy positions (110%/101%/98% of debit) were liquidated on one bad scan tick for −162.42/−61.59/−89.65, and paper trade 208 became structurally unrollable once spot drifted its strike past the 15% moneyness cap; the fix makes roll failures retryable up to the cap (a failed roll now *holds* the position), iterates all `scan(roll_for=…)` candidates excluding the held near, relaxes entry-only filters (moneyness/POP/contango) for rolls, and labels such closes honestly (`Closed (Roll Failed)`) instead of `Loss (Auto Stop)`. (2) Entries could open with a near leg already inside `ROLL_TRIGGER_DAYS` (a 2-DTE near matched near-target 1 via the ±3-day tolerance), guaranteeing an immediate roll attempt — a new `MIN_NEAR_DTE_AT_ENTRY` gate (default `ROLL_TRIGGER_DAYS + 1`) rejects them at entry (skipped for rolls; it also makes 1-DTE *entries* non-enterable by default, which is intended). (3) Stop/TP valuation used marks while a real close crosses the spread — monitor said 98–110% of debit, closes recovered ~5–10% of marked value; a new executable-value basis (`SPREAD_VALUE_BASIS`, `"exec"` in `config_test.py`) prices decisions at far-bid − near-ask, both `sv_mark`/`sv_exec` are logged, a pre-close `WARNING` fires when proceeds fall below `CLOSE_PROCEEDS_WARN_PCT` of the mark, and paper-mode closes now realise mark-to-market P&L (via `DryRunExecutor.close_spread` returning `last_spread_value`) instead of closing at entry value (`gross_pnl=0.00`). (4) Cancelling a timed-out entry leg ignored partial fills — cancels only kill the unfilled remainder, and three failed ETH-1750 legged entries left 13 naked short puts plus an unrecorded 5×5 calendar on the exchange with no DB record; the fix (`execution/executor._cancel_and_flatten`) reads `filled_amount` after every cancel and flattens the filled portion immediately, marking the order `CANCELLED_PARTIAL` and alerting the operator if the flatten itself fails. (5) The post-fill slippage check was symmetric (rejecting price *improvement* — the only deviation a limit order can produce) and fired after both legs filled with no unwind, abandoning executed spreads unrecorded; it is now directional (`_check_slippage(side=…)`) and unwinds both legs before raising. A persistent `RECONCILE MISMATCH` now escalates to a one-shot operator alert after `RECONCILE_ESCALATE_AFTER_CYCLES` cycles. Demonstrated offline by `python -m scratch.scratch_partial_fill_flatten` and `python -m scratch.scratch_roll_resilience`. The orphaned 1750 inventory still predates these fixes and must be cleared manually on test.deribit.com.

---

## Trading modes

The bot supports three operational modes set by `TRADING_MODE` in `config.py`:

| Mode | Data feed | Order execution | Real money? |
| --- | --- | --- | --- |
| `"paper"` *(default)* | test.deribit.com | Dry-run — logged locally, no orders sent | No |
| `"test"` | test.deribit.com | Real orders on test.deribit.com | No |
| `"live"` | <www.deribit.com> | Real orders on <www.deribit.com> | **Yes** |

**Paper mode** connects to the test exchange for live market data and pricing, but the executor never sends an order — all fills are simulated locally. This is the mode used by all scratch / debug scripts and is the recommended starting point.

**Test mode** connects to the same test exchange and actually submits orders. Use this to verify the full order lifecycle (combo submission, fill detection, reconciliation) before risking real capital.

**Live mode** connects to the production exchange and places real orders with real money.

API credentials are stored in `.env` (never committed). Paper and test share the test-exchange key pair (`DERIBIT_TEST_CLIENT_ID` / `SECRET`); live uses a separate production key pair (`DERIBIT_LIVE_CLIENT_ID` / `SECRET`).

On startup the bot prints a prominent banner identifying the active mode. It refuses to start in `"live"` mode if `DAILY_LOSS_LIMIT` is not configured. Scratch scripts abort if `TRADING_MODE` is `"live"`.

**Run in paper mode, then test mode, for at least 4 weeks total before switching to live.** Key things to verify:

- Scanner selects setups that profit at expiry
- Stop-loss and take-profit triggers fire correctly
- Roll logic produces better outcomes than outright close
- Daily loss limit halts the bot as expected
- Notifications arrive for every decision event

---

## Telegram commands

The bot accepts incoming commands from the same Telegram chat it uses for outgoing notifications. Every message is validated against `TELEGRAM_CHAT` — messages from any other chat ID are silently dropped.

| Command | What it returns |
| --- | --- |
| `/positions` | One line per open trade: `#id asset strike type  nearDate→farDate   entry=$X  sv=$X  PnL=$X  ev=X` — PnL is net of entry fees paid |
| `/portfolio` | One block per open trade: asset, strike, expiry range, debit, fees, EV at entry, current spread value and PnL net of entry fees |
| `/new_trades [today\|session]` | New trades (default: today AEST) — per trade: id, asset, debit, ev, strike, type, expiry range |
| `/closed_trades [today\|session]` | Closed trades (default: today AEST) — per trade: id, asset, debit, pnl, close reason — PnL is net of all fees |
| `/status` | Trading mode, drain/drain-and-new mode, paused/running, uptime, open count, today AEST PnL (net of fees), session PnL since bot start, cumulative session fees paid |
| `/pnl` | Equity-curve chart (PNG): cumulative realized PnL across all closed trades as a black line, plus current unrealized PnL from open positions as a dotted green line, captioned with realized/unrealized/total figures and open trade count |
| `/help` | Lists every available command with a one-line description |
| `/stop_bot` | Pauses scan and monitor ticks; the feed and listener remain alive; positions are not closed |
| `/start_bot` | Resumes normal scanning and monitoring |
| `/start_drain` | Activates drain mode — no new entries or rolls; existing positions close at stop/TP/expiry |
| `/start_with_assets BTC,ETH,...` | Override the active asset list at runtime and resume scanning |
| `/drain_and_new [portfolio=N] [assets=A,B]` | Close existing positions outright (no rolls) while still allowing new entries; optionally sets a new portfolio budget and asset list |
| `/info trade_id=N` | Check current position status on Deribit with live bid/ask prices and unrealized P&L |
| `/close trade_id=N` | Retry closing a stuck position (resets the close_stuck flag so bot attempts close on next monitor tick) |
| `/close_manually trade_id=N spread=VALUE` | Manually close a stuck position with a user-provided spread value when automatic close fails |
| `/deribit_positions` | List the positions currently open on Deribit, cross-referenced against the bot DB — flags any instrument not tracked in the DB and hints when stuck trades are still open on the exchange (test/live mode only) |

`/stop_bot` and `/start_bot` pause and resume the decision engine without restarting the process, so the listener stays connected throughout.

`/drain_and_new` differs from `/start_drain` in that new entries are still allowed — existing positions are closed outright at stop/TP/expiry rather than rolled, but the scanner continues to find and enter new setups. An optional `portfolio=N` argument overrides the live account cash used for position sizing.

`/pnl` sends an image rather than text. The black line is the running total of realized PnL for every trade the bot has ever closed, in chronological order. If any positions are currently open, a dotted green line extends from the last realized point to the present, showing where total equity stands including unrealized gains/losses; the caption notes how many positions are open. With no trading history yet, it replies with text instead of an empty chart.

Typing `/` in the Telegram chat will show a suggestion menu listing all commands — this is populated automatically when the bot starts via Telegram's `setMyCommands` API, with no manual BotFather setup required.

The bot can also be started in drain mode from the command line with `python bot.py --drain`, which is equivalent to setting the `DRAIN_MODE` env var.

---

## Requirements

- Python 3.11+
- Deribit account (paper or live) with API key
- Dependencies: `websockets`, `aiohttp`, `apscheduler`, `scipy`, `numpy`, `matplotlib` (chart rendering for `/pnl`)

---

## Relationship to optionsStrat

| optionsStrat | calendar-bot |
| --- | --- |
| Manual paper trading UI | Fully automated |
| Single position at a time | Up to 3 concurrent positions |
| Interactive spot/IV input | Live Deribit WebSocket feed |
| SQLite state | SQLite state (same schema) |
| Black-Scholes pricing | Same (ported) |
| Deribit executor | Same (hardened for unattended use) |
