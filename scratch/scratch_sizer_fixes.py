"""
scratch/scratch_sizer_fixes.py
===============================
Demonstrates the two bug fixes that prevented the bot from halting due to:
  1. Near-zero net_debit producing an absurd quantity (22k+ contracts).
  2. An inverted market spread (near_mid > far_mid) producing a negative
     spread value that, multiplied by the absurd quantity, created a
     phantom loss of $52M and triggered the daily loss limit.

Run from repo root:
    python -m scratch.scratch_sizer_fixes

This script aborts if TRADING_MODE == "live" (no live exchange contact needed).
"""

import sys
from unittest.mock import MagicMock

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from data.deribit_feed import TickerSnapshot
from datetime import datetime, timedelta, timezone
from strategy.scanner import CalendarCandidate
from strategy.sizer import size_candidate
from strategy.decision import DecisionEngine

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    status = PASS if condition else FAIL
    results.append((label, status))
    mark = "✓" if condition else "✗"
    print(f"  {mark}  {label}")


def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _make_snap(instrument: str, bid: float, ask: float) -> TickerSnapshot:
    asset = instrument.split("-")[0]
    return TickerSnapshot(
        instrument=instrument,
        asset=asset,
        spot=90_000.0,
        mark_price=(bid + ask) / 2,
        bid=bid,
        ask=ask,
        mark_iv=0.80,
        open_interest=500.0,
        timestamp=datetime.now(timezone.utc).timestamp(),
    )


def _dummy_candidate(**kwargs) -> CalendarCandidate:
    defaults = dict(
        asset="BTC", strike=58_000.0, option_type="Put",
        near_instrument="BTC-07JUN25-58000-P",
        far_instrument="BTC-27JUN25-58000-P",
        near_days=7, far_days=30,
        spot=60_000.0,
        near_iv=0.90, far_iv=0.75, iv_contango=0.15,
        near_ask=0.005, near_bid=0.004,
        far_ask=0.0145, far_bid=0.013,
        net_debit=0.0091,  # the offending debit from the incident
        near_oi=600.0, far_oi=600.0,
        pop=0.50, be_lo=52_000.0, be_hi=64_000.0,
        ev_score=313_260.0,
    )
    defaults.update(kwargs)
    return CalendarCandidate(**defaults)


# ─── Section 1: MIN_NET_DEBIT guard ──────────────────────────────────────────
print("\n=== Section 1: MIN_NET_DEBIT guard (sizer) ===")

# The incident: BTC Put 58000 with debit=0.0091 was sized to 22,062 contracts
bad_candidate = _dummy_candidate(net_debit=0.0091)
result = size_candidate(bad_candidate, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
print(f"  net_debit=0.0091 → qty={result.qty}  reason: {result.reason}")
check("near-zero debit (0.0091) is REJECTED", result.qty == 0.0)
check("rejection reason mentions 'minimum'", "minimum" in result.reason.lower())

# A sensible debit should still pass
good_candidate = _dummy_candidate(net_debit=96.25)
result2 = size_candidate(good_candidate, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
print(f"  net_debit=96.25 → qty={result2.qty}  reason: {result2.reason}")
check("sensible debit (96.25) is APPROVED", result2.qty > 0.0)

# ─── Section 2: MAX_QTY cap ───────────────────────────────────────────────────
print("\n=== Section 2: MAX_QTY hard cap (sizer) ===")

# Even if a debit above the floor produces a huge raw qty, MAX_QTY caps it
# E.g. max_loss=200, net_debit=0.50 → raw=400, capped to MAX_QTY=100
tiny_debit = _dummy_candidate(net_debit=0.50)
result3 = size_candidate(tiny_debit, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
max_qty = getattr(config, "MAX_QTY", 100.0)
print(f"  net_debit=0.50, MAX_QTY={max_qty} → qty={result3.qty}")
check(f"qty capped at MAX_QTY ({max_qty})", result3.qty <= max_qty)

# ─── Section 3: Negative spread clamped to zero ───────────────────────────────
print("\n=== Section 3: Negative spread value clamped to zero (decision) ===")

near_instr = "BTC-07JUN25-58000-P"
far_instr  = "BTC-27JUN25-58000-P"

# Inverted: near_mid=505, far_mid=105 — near is more expensive than far
near_snap = _make_snap(near_instr, bid=500.0, ask=510.0)   # mid=505
far_snap  = _make_snap(far_instr,  bid=100.0, ask=110.0)   # mid=105

cache = MagicMock()
cache.get_spot.return_value = 60_000.0
cache.get_chain.return_value = [near_snap, far_snap]

import tempfile
from pathlib import Path
db_path = Path(tempfile.mktemp(suffix=".db"))
engine = DecisionEngine(cache=cache, portfolio_value=10_000.0, db_path=db_path)

pos = {
    "trade_id": 4, "status": "Open", "asset": "BTC",
    "option_type": "Put", "strike": 58_000.0,
    "expiry_near": _future_label(7), "expiry_far": _future_label(30),
    "qty": 22_062.8, "net_debit": 0.0091, "spot_open": 60_000.0,
    "near_days": 7, "far_days": 30,
    "near_instrument": near_instr,
    "far_instrument":  far_instr,
    "open_fees": 0.0, "close_fees": 0.0,
}

market_sv = engine._get_market_spread_value(pos)
unclamped = (105.0 - 505.0) * 22_062.8
print(f"  Without clamp: (105-505) × 22062.8 = {unclamped:,.2f}")
print(f"  With clamp:    market_sv = {market_sv}")
check("inverted spread clamped to 0.0 (not negative)", market_sv == 0.0)
check("would have been catastrophic without clamp", unclamped < -8_000_000)

# Normal (positive) spread passes through unchanged
near_snap2 = _make_snap(near_instr, bid=100.0, ask=110.0)  # mid=105
far_snap2  = _make_snap(far_instr,  bid=200.0, ask=220.0)  # mid=210

cache2 = MagicMock()
cache2.get_spot.return_value = 60_000.0
cache2.get_chain.return_value = [near_snap2, far_snap2]
engine2 = DecisionEngine(cache=cache2, portfolio_value=10_000.0, db_path=db_path)

pos2 = dict(pos)
pos2["qty"] = 2.0
market_sv2 = engine2._get_market_spread_value(pos2)
expected_sv2 = (210.0 - 105.0) * 2.0  # = 210
print(f"  Positive spread: (210-105) × 2 = {expected_sv2}  market_sv={market_sv2}")
check("positive spread passes through unchanged", abs((market_sv2 or 0) - expected_sv2) < 0.01)

# ─── Summary ─────────────────────────────────────────────────────────────────
print("\n=== Summary ===")
passed = sum(1 for _, s in results if s == PASS)
failed = sum(1 for _, s in results if s == FAIL)
print(f"  {passed} passed, {failed} failed")
if failed:
    print("\nFAILED checks:")
    for label, status in results:
        if status == FAIL:
            print(f"  ✗  {label}")
    sys.exit(1)
else:
    print("  All checks passed.")
