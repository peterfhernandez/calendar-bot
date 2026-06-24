"""
scratch/scratch_two_fixes.py
============================
Demonstrates two bug fixes in strategy/decision.py:

  Fix 4 — New-position grace period
    A position entered by scan_tick is skipped by the monitor_tick that fires in
    the same scheduler second.  Before the fix, the monitor could immediately
    evaluate the new position using a B-S spread value that differed wildly from
    the actual fill price, triggering a spurious TP or stop.

  Fix 5 — Realized P&L computed from spread_value, not executor return
    _close_position now computes pnl = (spread_value - net_debit) * qty using
    the observed mark-to-market value from check_calendar_status.  Before the
    fix, DryRunExecutor.close_spread returned net_debit * qty, making pnl always
    equal 0 on every close.

Run with:
    python -m scratch.scratch_two_fixes

Does NOT run when DERIBIT_PAPER = False.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import config

if config.TRADING_MODE == "live":
    print("scratch_two_fixes.py: TRADING_MODE is 'live' — refusing to run.")
    sys.exit(0)

from data.deribit_feed import TickerSnapshot
from strategy.decision import DecisionEngine, DryRunExecutor
from strategy.scanner import CalendarCandidate


# ── Helpers ────────────────────────────────────────────────────────────────────

def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _make_cache(near_days: int = 10, far_days: int = 35) -> MagicMock:
    near_label = _future_label(near_days)
    far_label  = _future_label(far_days)
    near_instr = f"BTC-{near_label}-90000-C"
    far_instr  = f"BTC-{far_label}-90000-C"

    def _snap(instr, iv):
        return TickerSnapshot(
            instrument=instr, asset="BTC", spot=90_000.0, mark_price=0.025,
            bid=0.02, ask=0.03, mark_iv=iv, open_interest=500,
            timestamp=datetime.now(timezone.utc).timestamp(),
        )

    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    cache.get_chain.return_value = [_snap(near_instr, 0.90), _snap(far_instr, 0.70)]
    return cache


def _make_pos(trade_id: int = 4, net_debit: float = 0.02, qty: float = 1.5,
              near_days: int = 10, far_days: int = 35) -> dict:
    near_label = _future_label(near_days)
    far_label  = _future_label(far_days)
    return {
        "trade_id": trade_id, "status": "Open", "asset": "BTC",
        "option_type": "Call", "strike": 90_000.0,
        "expiry_near": near_label, "expiry_far": far_label,
        "qty": qty, "net_debit": net_debit, "spot_open": 90_000.0,
        "near_days": near_days, "far_days": far_days,
        "near_instrument": f"BTC-{near_label}-90000-C",
        "far_instrument":  f"BTC-{far_label}-90000-C",
        "open_fees": 0.0, "close_fees": 0.0,
    }


def _engine(executor=None) -> DecisionEngine:
    db = Path(tempfile.mktemp(suffix=".db"))
    return DecisionEngine(
        cache=_make_cache(),
        portfolio_value=10_000.0,
        executor=executor or DryRunExecutor(),
        db_path=db,
        daily_loss_limit=500.0,
    )


# ── Section 1: Grace period ────────────────────────────────────────────────────

print("=" * 60)
print("Fix 4 — New-position grace period")
print("=" * 60)

executor = MagicMock()
executor.close_spread.return_value = 20.0  # extreme TP signal

engine = _engine(executor)
pos = _make_pos(trade_id=4, net_debit=0.02, qty=1.0)

# Mark trade_id=4 as just-entered (as scan_tick would do)
engine._just_entered.add(4)

print("\nScenario: trade_id=4 just entered; monitor fires with sv=20.0 (1000% of debit)")
with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
     patch("strategy.decision.check_calendar_status", return_value=("tp", 20.0, 10.0, "TAKE-PROFIT")):
    engine._cache.get_spot.return_value = 90_000.0
    engine._get_iv = MagicMock(return_value=0.80)
    status = engine.monitor_tick()

close_called = executor.close_spread.called
grace_cleared = len(engine._just_entered) == 0
print(f"  close_spread called:  {close_called}   (expected: False)")
print(f"  _just_entered cleared: {grace_cleared}  (expected: True)")
print(f"  status.message: {status.message}")

assert not close_called, "FAIL: close should NOT have been called during grace period"
assert grace_cleared,    "FAIL: _just_entered should be cleared after monitor tick"
print("  PASS")

print("\nScenario: second monitor tick — grace period gone, TP fires normally")
with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
     patch("strategy.decision.check_calendar_status", return_value=("tp", 20.0, 10.0, "TAKE-PROFIT")), \
     patch("strategy.decision.close_calendar_trade"):
    engine._cache.get_spot.return_value = 90_000.0
    engine._get_iv = MagicMock(return_value=0.80)
    engine.monitor_tick()

print(f"  close_spread called:  {executor.close_spread.called}   (expected: True)")
assert executor.close_spread.called, "FAIL: close should fire on second tick"
print("  PASS")


# ── Section 2: Realized P&L from spread_value ─────────────────────────────────

print()
print("=" * 60)
print("Fix 5 — Realized P&L from spread_value, not executor return")
print("=" * 60)

# Scenario: TP with sv=0.21, net_debit=0.02, qty=1.5
# Expected pnl = (0.21 - 0.02) * 1.5 = 0.285
# DryRunExecutor would return net_debit * qty = 0.03 → old pnl = 0.03 - 0.03 = 0.00

executor_tp = MagicMock()
executor_tp.close_spread.return_value = 0.03  # = net_debit * qty (old buggy behaviour)

engine_tp = _engine(executor_tp)
pos_tp = _make_pos(trade_id=1, net_debit=0.02, qty=1.5)

print(f"\nScenario: TP  sv=0.21  net_debit=0.02  qty=1.5")
print(f"  Expected pnl = (0.21 - 0.02) * 1.5 = {(0.21 - 0.02) * 1.5:.4f}")
print(f"  Old (buggy)  = executor_return - net_debit*qty = 0.03 - 0.03 = 0.0000")

with patch.object(engine_tp, "_load_all_open_positions", side_effect=[[pos_tp], []]), \
     patch("strategy.decision.check_calendar_status", return_value=("tp", 0.21, 10.5, "TAKE-PROFIT")), \
     patch("strategy.decision.close_calendar_trade"):
    engine_tp._cache.get_spot.return_value = 90_000.0
    engine_tp._get_iv = MagicMock(return_value=0.80)
    engine_tp.monitor_tick()

actual_pnl = engine_tp._today_pnl
expected_pnl = (0.21 - 0.02) * 1.5
print(f"  Actual pnl   = {actual_pnl:.4f}  (expected: {expected_pnl:.4f})")
assert abs(actual_pnl - expected_pnl) < 1e-9, f"FAIL: pnl mismatch ({actual_pnl} != {expected_pnl})"
print("  PASS")

# Scenario: Stop with sv=0.005, net_debit=0.02, qty=1.0
# Expected pnl = (0.005 - 0.02) * 1.0 = -0.015

executor_sl = MagicMock()
executor_sl.close_spread.return_value = 0.02  # = net_debit * qty (old buggy behaviour)

engine_sl = _engine(executor_sl)
pos_sl = _make_pos(trade_id=2, net_debit=0.02, qty=1.0)

print(f"\nScenario: Stop sv=0.005  net_debit=0.02  qty=1.0")
print(f"  Expected pnl = (0.005 - 0.02) * 1.0 = {(0.005 - 0.02) * 1.0:.4f}")

with patch.object(engine_sl, "_load_all_open_positions", side_effect=[[pos_sl], []]), \
     patch("strategy.decision.check_calendar_status", return_value=("stop", 0.005, 0.25, "STOP")), \
     patch("strategy.decision.close_calendar_trade"):
    engine_sl._cache.get_spot.return_value = 90_000.0
    engine_sl._get_iv = MagicMock(return_value=0.80)
    engine_sl.monitor_tick()

actual_pnl = engine_sl._today_pnl
expected_pnl = (0.005 - 0.02) * 1.0
print(f"  Actual pnl   = {actual_pnl:.4f}  (expected: {expected_pnl:.4f})")
assert abs(actual_pnl - expected_pnl) < 1e-9, f"FAIL: pnl mismatch ({actual_pnl} != {expected_pnl})"
print("  PASS")


print()
print("=" * 60)
print("All checks passed.")
print("=" * 60)
