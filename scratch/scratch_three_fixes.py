"""
scratch/scratch_three_fixes.py
==============================
Demonstration script for three bug fixes applied to the decision engine:

  1. Negative-EV candidates are rejected before entry.
  2. Monitor message correctly reflects "N position(s) skipped — no IV data"
     when the Deribit feed is stale, instead of the misleading "All positions OK."
  3. daily_pnl in EngineStatus reflects the current mark-to-market value of
     open positions (unrealized P&L), not just realised closed-trade P&L.

Run from the repo root:
    python -m scratch.scratch_three_fixes

No live network calls, no database side-effects (in-memory only).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import config

# Guard: never run against live trading
if config.TRADING_MODE == "live":
    print("ERROR: TRADING_MODE is 'live' — refusing to run scratch script on live account.")
    sys.exit(1)

from strategy.decision import DecisionEngine, BotState
from strategy.scanner import CalendarCandidate


# ── Helpers ───────────────────────────────────────────────────────────────────

def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _make_candidate(ev_score: float = 0.005) -> CalendarCandidate:
    near_label = _future_label(10)
    far_label  = _future_label(35)
    return CalendarCandidate(
        asset="BTC", strike=90_000.0, option_type="Call",
        near_instrument=f"BTC-{near_label}-90000-C",
        far_instrument=f"BTC-{far_label}-90000-C",
        near_days=10, far_days=35,
        spot=90_000.0, near_iv=0.90, far_iv=0.70, iv_contango=0.20,
        near_ask=0.03, near_bid=0.02, far_ask=0.04, far_bid=0.035,
        net_debit=0.02, near_oi=500.0, far_oi=500.0,
        pop=0.55, be_lo=80_000.0, be_hi=100_000.0,
        ev_score=ev_score, qty=0.0,
    )


def _open_pos(trade_id: int = 1, net_debit: float = 0.02) -> dict:
    near_label = _future_label(10)
    far_label  = _future_label(35)
    return {
        "trade_id": trade_id, "status": "Open", "asset": "BTC",
        "option_type": "Call", "strike": 90_000.0,
        "expiry_near": near_label, "expiry_far": far_label,
        "qty": 1.0, "net_debit": net_debit, "spot_open": 90_000.0,
        "near_days": 10, "far_days": 35,
        "near_instrument": f"BTC-{near_label}-90000-C",
        "far_instrument":  f"BTC-{far_label}-90000-C",
        "open_fees": 0.0, "close_fees": 0.0,
    }


def _make_engine(executor=None) -> DecisionEngine:
    db_path = Path(tempfile.mktemp(suffix=".db"))
    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    cache.get_chain.return_value = []
    return DecisionEngine(
        cache=cache,
        portfolio_value=10_000.0,
        executor=executor,
        db_path=db_path,
        daily_loss_limit=500.0,
    )


PASS = "✓ PASS"
FAIL = "✗ FAIL"


# ── Fix 1: Negative-EV filter ─────────────────────────────────────────────────

print("=" * 65)
print("FIX 1 — Negative-EV candidates rejected before entry")
print("=" * 65)

executor = MagicMock()
negative_candidate = _make_candidate(ev_score=-0.35)  # EV = -35% of debit

with patch("strategy.decision.scan") as mock_scan, \
     patch("strategy.decision.size_candidate") as mock_size:
    mock_scan.return_value = [negative_candidate]
    mock_size.return_value = MagicMock(qty=1.0, reason="Approved")
    engine = _make_engine(executor=executor)
    status = engine.scan_tick()

entered = executor.enter_spread.call_count
ok = entered == 0
print(f"  Candidate ev_score : -0.35  (-35% of debit)")
print(f"  enter_spread calls : {entered}  (expected 0)")
print(f"  Result             : {PASS if ok else FAIL}")

# Also check positive EV is still allowed
executor2 = MagicMock()
positive_candidate = _make_candidate(ev_score=0.25)  # EV = 25% of debit
executor2.enter_spread.return_value = {
    "near_prem": 0.02, "far_prem": 0.04, "net_debit": 0.02, "qty": 1.0,
}
with patch("strategy.decision.scan") as mock_scan, \
     patch("strategy.decision.size_candidate") as mock_size:
    mock_scan.return_value = [positive_candidate]
    mock_size.return_value = MagicMock(qty=1.0, reason="Approved")
    engine2 = _make_engine(executor=executor2)
    status2 = engine2.scan_tick()

entered2 = executor2.enter_spread.call_count
ok2 = entered2 == 1
print(f"\n  Candidate ev_score : +0.25  (25% of debit)")
print(f"  enter_spread calls : {entered2}  (expected 1)")
print(f"  Result             : {PASS if ok2 else FAIL}")


# ── Fix 2: Stale-IV monitor message ───────────────────────────────────────────

print()
print("=" * 65)
print("FIX 2 — Monitor message when IV data is stale")
print("=" * 65)

engine3 = _make_engine()
pos = _open_pos()

with patch.object(engine3, "_load_all_open_positions", side_effect=[[pos], [pos]]):
    engine3._cache.get_spot.return_value = 90_000.0
    engine3._get_iv = MagicMock(return_value=None)  # simulate stale feed
    status3 = engine3.monitor_tick()

msg = status3.message
has_skipped = "skipped" in msg.lower()
has_no_iv   = "no iv" in msg.lower() or "no IV" in msg
not_ok_msg  = "All positions OK" not in msg
ok3 = has_skipped and has_no_iv and not_ok_msg

print(f"  Monitor message    : '{msg}'")
print(f"  Contains 'skipped' : {has_skipped}")
print(f"  Contains 'no IV'   : {has_no_iv}")
print(f"  No 'All positions OK' : {not_ok_msg}")
print(f"  Result             : {PASS if ok3 else FAIL}")

# Also verify that when IV IS available the old message still appears
engine4 = _make_engine()
with patch.object(engine4, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
     patch("strategy.decision.check_calendar_status",
           return_value=("ok", 0.025, 1.25, "OK")):
    engine4._cache.get_spot.return_value = 90_000.0
    engine4._get_iv = MagicMock(return_value=0.80)
    status4 = engine4.monitor_tick()

ok4 = "All positions OK" in status4.message
print(f"\n  When IV available  : '{status4.message}'")
print(f"  Still says OK      : {PASS if ok4 else FAIL}")


# ── Fix 3: daily_pnl includes unrealized MTM ──────────────────────────────────

print()
print("=" * 65)
print("FIX 3 — daily_pnl reflects unrealized mark-to-market P&L")
print("=" * 65)

engine5 = _make_engine()
pos5 = _open_pos(net_debit=0.02)  # bought spread at 0.02
# current spread value = 0.025 → unrealized = (0.025 - 0.02) * 1 = +0.005

with patch.object(engine5, "_load_all_open_positions", side_effect=[[pos5], [pos5]]), \
     patch("strategy.decision.check_calendar_status",
           return_value=("ok", 0.025, 1.25, "OK")):
    engine5._cache.get_spot.return_value = 90_000.0
    engine5._get_iv = MagicMock(return_value=0.80)
    status5 = engine5.monitor_tick()

expected_pnl = (0.025 - 0.02) * 1.0  # = 0.005
ok5 = abs(status5.daily_pnl - expected_pnl) < 1e-9

print(f"  Entry debit        : 0.0200")
print(f"  Current spread val : 0.0250")
print(f"  Expected daily_pnl : {expected_pnl:+.4f}")
print(f"  Actual daily_pnl   : {status5.daily_pnl:+.4f}")
print(f"  Result             : {PASS if ok5 else FAIL}")

# Verify 0.00 is NOT returned when position has unrealized gain
old_bug = status5.daily_pnl == 0.0
print(f"  Old bug (== 0.00)  : {old_bug}  (must be False)")

# Combined: realized + unrealized
engine6 = _make_engine()
engine6._today_pnl = -0.01  # previous realized loss
pos6 = _open_pos(net_debit=0.02)

with patch.object(engine6, "_load_all_open_positions", side_effect=[[pos6], [pos6]]), \
     patch("strategy.decision.check_calendar_status",
           return_value=("ok", 0.025, 1.25, "OK")):
    engine6._cache.get_spot.return_value = 90_000.0
    engine6._get_iv = MagicMock(return_value=0.80)
    status6 = engine6.monitor_tick()

expected_combined = -0.01 + 0.005
ok6 = abs(status6.daily_pnl - expected_combined) < 1e-9
print(f"\n  Realized P&L       : -0.0100")
print(f"  Unrealized P&L     : +0.0050")
print(f"  Combined expected  : {expected_combined:+.4f}")
print(f"  Actual daily_pnl   : {status6.daily_pnl:+.4f}")
print(f"  Result             : {PASS if ok6 else FAIL}")


# ── Summary ───────────────────────────────────────────────────────────────────

print()
print("=" * 65)
all_ok = ok and ok2 and ok3 and ok4 and ok5 and ok6
print(f"Overall: {'ALL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'}")
print("=" * 65)
sys.exit(0 if all_ok else 1)
