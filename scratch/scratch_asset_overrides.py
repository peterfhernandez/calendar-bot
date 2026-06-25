"""
scratch/scratch_asset_overrides.py
====================================
Demonstrates per-asset threshold overrides (ASSET_OVERRIDES in config.py).

Shows how SOL candidates that fail BTC/ETH-level filters can pass with
asset-specific relaxed thresholds, while BTC candidates are unchanged.

Run with:
    python -m scratch.scratch_asset_overrides
"""

from __future__ import annotations

import sys
import os

# Abort on live mode — this script must never touch the real exchange.
os.environ.setdefault("TRADING_MODE", "paper")
import config
if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode.")
    sys.exit(1)

print(f"Mode: {config.TRADING_MODE}\n")

# ── Section 1: Show effective thresholds per asset ────────────────────────────

print("=" * 60)
print("Section 1: Effective thresholds per asset")
print("=" * 60)

keys = ["MIN_OI_NEAR", "MIN_OI_FAR", "MAX_LEG_SPREAD_PCT", "MAX_ENTRY_PREMIUM", "MIN_IV_CONTANGO"]
col_w = 24

header = f"{'Threshold':<{col_w}}" + "".join(f"{'Global':>10}") + "".join(f"{'BTC':>10}") + "".join(f"{'ETH':>10}") + "".join(f"{'SOL':>10}")
print(header)
print("-" * (col_w + 40))

for key in keys:
    global_val = getattr(config, key)
    btc_val    = config.asset_config("BTC", key)
    eth_val    = config.asset_config("ETH", key)
    sol_val    = config.asset_config("SOL", key)
    changed    = "  *" if sol_val != global_val else ""
    print(f"{key:<{col_w}}{global_val:>10}{btc_val:>10}{eth_val:>10}{sol_val:>10}{changed}")

print("\n* = SOL uses a relaxed asset-specific override\n")

# ── Section 2: Scanner — OI filter ────────────────────────────────────────────

print("=" * 60)
print("Section 2: Scanner OI filter — SOL with OI=20")
print("=" * 60)

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from data.deribit_feed import TickerSnapshot
from strategy.scanner import scan


def _future_date(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _make_snap(asset, expiry_days, strike, opt_type, mark_iv, bid, ask, oi, spot):
    expiry_str = _future_date(expiry_days)
    return TickerSnapshot(
        instrument=f"{asset}-{expiry_str}-{strike}-{opt_type}",
        asset=asset,
        spot=spot,
        mark_price=(bid + ask) / 2,
        mark_iv=mark_iv,
        bid=bid,
        ask=ask,
        open_interest=oi,
        bid_size=10.0,
        ask_size=10.0,
        timestamp=time.time(),
    )


def _make_cache(snaps, spot):
    cache = MagicMock()
    cache.get_spot.return_value = spot
    cache.get_chain.return_value = snaps
    cache.get.return_value = None
    return cache


sol_snaps = [
    _make_snap("SOL", 7,  150, "C", mark_iv=0.85, bid=1.0, ask=1.2, oi=20, spot=150.0),
    _make_snap("SOL", 30, 150, "C", mark_iv=0.75, bid=2.0, ask=2.4, oi=20, spot=150.0),
]
cache = _make_cache(sol_snaps, spot=150.0)

# Without overrides (using the global MIN_OI=100)
results_global = scan(
    cache, assets=["SOL"],
    near_days_options=[7], far_days_options=[30],
    min_oi_near=100, min_oi_far=100,   # explicit global values
    min_iv_contango=0.01, min_pop=0.01,
)
print(f"SOL OI=20 with global MIN_OI=100:   {len(results_global)} candidates (expected 0 — rejected)")

# With SOL overrides (ASSET_OVERRIDES sets MIN_OI=10 for SOL)
results_override = scan(
    cache, assets=["SOL"],
    near_days_options=[7], far_days_options=[30],
    # no explicit min_oi — defers to ASSET_OVERRIDES → 10
    min_iv_contango=0.01, min_pop=0.01,
)
print(f"SOL OI=20 with SOL override MIN_OI=10: {len(results_override)} candidates (expected ≥1 — passes)")
print()

# ── Section 3: Liquidity gate — spread filter ──────────────────────────────────

print("=" * 60)
print("Section 3: Liquidity gate — 15% bid/ask spread on each leg")
print("=" * 60)

from strategy.decision import DecisionEngine
from strategy.scanner import CalendarCandidate
from pathlib import Path
import tempfile


def _make_candidate_with_spread(asset, strike, spot, spread_pct, net_debit_ratio=1.05):
    near_mid = spot * 0.01
    far_mid  = spot * 0.02
    spread_mid = far_mid - near_mid
    near_half = near_mid * spread_pct / 2
    far_half  = far_mid  * spread_pct / 2
    near_label = _future_date(10)
    far_label  = _future_date(35)
    return CalendarCandidate(
        asset=asset,
        strike=strike,
        option_type="Call",
        near_instrument=f"{asset}-{near_label}-{int(strike)}-C",
        far_instrument=f"{asset}-{far_label}-{int(strike)}-C",
        near_days=10, far_days=35, spot=spot,
        near_iv=0.90, far_iv=0.70, iv_contango=0.20,
        near_bid=near_mid - near_half,
        near_ask=near_mid + near_half,
        far_bid=far_mid - far_half,
        far_ask=far_mid + far_half,
        net_debit=spread_mid * net_debit_ratio,
        near_oi=500.0, far_oi=500.0,
        pop=0.50, be_lo=spot * 0.80, be_hi=spot * 1.20,
        ev_score=0.20,
    )


cache_mock = MagicMock()
cache_mock.get_spot.return_value = 90_000.0
cache_mock.get_chain.return_value = []
cache_mock.get.return_value = None

db_path = Path(tempfile.mktemp(suffix=".db"))
engine = DecisionEngine(cache=cache_mock, portfolio_value=10_000.0, db_path=db_path)

spread_pct = 0.15  # 15% bid/ask spread on each leg

for asset, spot, strike in [("BTC", 90_000.0, 90_000), ("ETH", 3_000.0, 3_000), ("SOL", 150.0, 150)]:
    candidate = _make_candidate_with_spread(asset, strike, spot, spread_pct)
    reason = engine._check_liquidity_gate(candidate)
    effective_limit = config.asset_config(asset, "MAX_LEG_SPREAD_PCT")
    status = "PASS" if reason is None else "FAIL"
    print(f"  {asset}: 15% spread, limit={effective_limit:.0%}  → {status}")
    if reason:
        print(f"          ({reason})")

print()

# ── Section 4: Liquidity gate — entry premium filter ──────────────────────────

print("=" * 60)
print("Section 4: Liquidity gate — 15% entry premium")
print("=" * 60)

for asset, spot, strike in [("BTC", 90_000.0, 90_000), ("ETH", 3_000.0, 3_000), ("SOL", 150.0, 150)]:
    # tight leg spreads (2%), but 15% entry premium
    candidate = _make_candidate_with_spread(asset, strike, spot, spread_pct=0.02, net_debit_ratio=1.15)
    reason = engine._check_liquidity_gate(candidate)
    effective_limit = config.asset_config(asset, "MAX_ENTRY_PREMIUM")
    status = "PASS" if reason is None else "FAIL"
    print(f"  {asset}: 15% entry premium, limit={effective_limit:.0%}  → {status}")
    if reason:
        print(f"          ({reason})")

print()
print("Done.")
