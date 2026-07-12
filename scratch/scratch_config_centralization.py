"""
scratch/scratch_config_centralization.py
========================================
Phase 20 demonstration — scattered config values are now centralized in
config.py.

Shows, without any network access or orders:

1. Every module-level constant that used to be hardcoded now equals its
   config.py counterpart (logging, timeouts, retries, thresholds, paths).
2. The two functional config-bypass bugs are fixed:
   a. Order reconciliation iterates config.ASSETS (SOL included) instead of
      a hardcoded BTC/ETH tuple — demonstrated with a fake WebSocket that
      records which currencies are queried.
   b. ChainCache and the debug viewer default their TTL to
      config.CHAIN_CACHE_TTL_SEC instead of private hardcoded values.
3. Changing a config value actually changes behaviour (strike-increment
   table, far-leg spread model, spread warn threshold).

Run with:  python -m scratch.scratch_config_centralization
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from unittest.mock import patch

import config

if config.TRADING_MODE == "live":
    sys.exit("Refusing to run: TRADING_MODE is 'live'. Scratch scripts never touch the live exchange.")

CHECKS_PASSED = 0
CHECKS_FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS_PASSED, CHECKS_FAILED
    mark = "PASS" if ok else "FAIL"
    if ok:
        CHECKS_PASSED += 1
    else:
        CHECKS_FAILED += 1
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'─' * 70}\n {title}\n{'─' * 70}")


# ── 1. Module constants now sourced from config ───────────────────────────────

section("1. Module constants are sourced from config.py")

import alerts.notifier as nt
import backtest.data_collector as dc
import db.state as st
import execution.executor as ex
import execution.order_manager as om
import portfolio.tracker as pt
import strategy.decision as dec
import strategy.sizer as sz
from alerts.notifier import Notifier
from data.chain_cache import ChainCache

check("executor.SLIPPAGE_LIMIT_PCT == config", ex.SLIPPAGE_LIMIT_PCT == config.SLIPPAGE_LIMIT_PCT,
      f"{ex.SLIPPAGE_LIMIT_PCT}")
check("executor.ORDER_TIMEOUT_SEC == config", ex.ORDER_TIMEOUT_SEC == config.ORDER_TIMEOUT_SEC,
      f"{ex.ORDER_TIMEOUT_SEC}s")
check("executor.MAX_RETRIES == config.MAX_ORDER_RETRIES", ex.MAX_RETRIES == config.MAX_ORDER_RETRIES)
check("executor retry delays == config.ORDER_RETRY_DELAYS", ex._RETRY_DELAYS == config.ORDER_RETRY_DELAYS,
      f"{ex._RETRY_DELAYS}")
check("order_manager.STUCK_ORDER_TIMEOUT == config", om.STUCK_ORDER_TIMEOUT == config.STUCK_ORDER_TIMEOUT_SEC,
      f"{om.STUCK_ORDER_TIMEOUT}s")
check("collector interval == config.COLLECTOR_INTERVAL_SEC",
      dc.COLLECTOR_INTERVAL_SEC == config.COLLECTOR_INTERVAL_SEC, f"{dc.COLLECTOR_INTERVAL_SEC}s")
check("collector DB path == config.HISTORIC_DATA_DB_PATH", dc.DB_PATH == config.HISTORIC_DATA_DB_PATH)
check("notifier SMTP host == config.SMTP_HOST", nt._SMTP_HOST == config.SMTP_HOST, nt._SMTP_HOST)
check("Notifier() cooldown == config.ALERT_COOLDOWN_SEC",
      Notifier()._cooldown == config.ALERT_COOLDOWN_SEC, f"{config.ALERT_COOLDOWN_SEC}s")
check("sizer min qty == config.MIN_CONTRACT_SIZE", sz._MIN_QTY == config.MIN_CONTRACT_SIZE)
check("sizer strike correlation == config.STRIKE_CORRELATION_PCT",
      sz._STRIKE_CORRELATION_PCT == config.STRIKE_CORRELATION_PCT)
check("decision roll trigger == config.ROLL_TRIGGER_DAYS", dec._ROLL_TRIGGER_DAYS == config.ROLL_TRIGGER_DAYS)
check("tracker reconcile threshold == config.RECONCILE_THRESHOLD_PCT",
      pt._RECONCILE_THRESHOLD == config.RECONCILE_THRESHOLD_PCT)
check("db.state DB path == config.DB_PATH", st.DB_PATH == config.DB_PATH, str(st.DB_PATH))
check("db.state timezone == config.TIMEZONE", str(st._AEST) == config.TIMEZONE, config.TIMEZONE)

# ── 2a. SOL reconciliation bug fix ────────────────────────────────────────────

section("2a. Order reconciliation now iterates config.ASSETS (SOL fix)")


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, payload: str) -> None:
        msg = json.loads(payload)
        self.sent.append(msg)
        await self._queue.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": []}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()


class _FakeConnect:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    def __call__(self, endpoint, **kwargs):
        self.endpoint = endpoint
        return self

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *args):
        return False


ws = _FakeWS()
fake_connect = _FakeConnect(ws)
with patch("execution.order_manager.websockets.connect", fake_connect), \
     patch.object(config, "ASSETS", ["BTC", "ETH", "SOL"]):
    asyncio.run(om._fetch_deribit_open_orders(paper=True, client_id="", client_secret=""))

queried = [m["params"]["currency"] for m in ws.sent
           if m["method"] == "private/get_open_orders_by_currency"]
print(f"  Currencies queried during reconciliation: {queried}")
check("SOL is reconciled", "SOL" in queried)
check("endpoint comes from config.DERIBIT_WS_URL", fake_connect.endpoint == config.DERIBIT_WS_URL,
      fake_connect.endpoint)

# ── 2b. Cache TTL bug fix ─────────────────────────────────────────────────────

section("2b. ChainCache / debug viewer TTL defaults to config.CHAIN_CACHE_TTL_SEC")

cache = ChainCache()
check("ChainCache() default TTL == config", cache.ttl == float(config.CHAIN_CACHE_TTL_SEC), f"{cache.ttl}s")
import data.debug_viewer as dv
check("debug viewer no longer hardcodes ttl=60", "ttl=60" not in inspect.getsource(dv._run))

# ── 3. Config changes actually change behaviour ───────────────────────────────

section("3. Changing config changes behaviour (late binding)")

from core.calendar_engine import check_calendar_status
from core.pricing import adjust_far_leg_price, strike_increment

inc_before = strike_increment(50_000)
with patch.object(config, "STRIKE_INCREMENT_TABLE", [(1_000_000, 7.0)]):
    inc_after = strike_increment(50_000)
print(f"  strike_increment(50_000): {inc_before} → {inc_after} after table override")
check("strike increment follows config table", inc_before == config.STRIKE_INCREMENT_DEFAULT and inc_after == 7.0)

adj_before = adjust_far_leg_price(1000.0, 7, is_buy=True)
with patch.object(config, "FAR_LEG_SPREAD_TABLE", [(7, 0.10)]):
    adj_after = adjust_far_leg_price(1000.0, 7, is_buy=True)
print(f"  adjust_far_leg_price(1000, 7d): {adj_before:.2f} → {adj_after:.2f} after spread-table override")
check("far-leg spread model follows config table", abs(adj_before - 1005.0) < 1e-6 and abs(adj_after - 1100.0) < 1e-6)

op = {"net_debit": 100.0, "qty": 1.0, "strike": 100.0, "option_type": "Call"}
status_before, *_ = check_calendar_status(100.0, 0.8, 5, 30, op, market_sv=90.0)
with patch.object(config, "SPREAD_WARN_PCT", 0.95):
    status_after, *_ = check_calendar_status(100.0, 0.8, 5, 30, op, market_sv=90.0)
print(f"  spread at 90% of debit: status '{status_before}' → '{status_after}' with SPREAD_WARN_PCT=0.95")
check("warn threshold follows config.SPREAD_WARN_PCT", status_before == "ok" and status_after == "warn")

# ── Summary ───────────────────────────────────────────────────────────────────

section("Summary")
print(f"  {CHECKS_PASSED} passed, {CHECKS_FAILED} failed")
if CHECKS_FAILED:
    sys.exit(1)
print("  All Phase 20 centralization checks passed.")
