"""
scratch/scratch_premature_roll.py
=================================
Offline demonstration of the Phase 22f fixes for the premature / degenerate roll
observed in paper-mode trades #206 and #207.

    #206  A freshly-entered near_days=1 position was rolled/closed ~1 minute after
          entry because ROLL_TRIGGER_DAYS=2 made near_days_left(1) <= 2 from the
          very first tick, with no regard for real elapsed time.
    #207  _try_roll matched a candidate by strike/type alone across every scanned
          tenor pairing and picked one whose new near leg expired on the SAME day
          as the position's own far leg — a zero-width spread that collapsed to
          $0.00 and tripped a large stop-loss.

The fixes:
    22f  roll trigger requires near_days_left < near_days-at-entry (genuine decay)
    22f  _try_roll rejects any near candidate that doesn't precede the position's
         own far leg by MIN_ROLL_NEAR_FAR_GAP_DAYS

No network, no live orders — runs against in-memory helpers and a temp DB.

Run from the repo root:
    python -m scratch.scratch_premature_roll

Aborts if TRADING_MODE == "live".
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from strategy.decision import DecisionEngine, _expiry_gap_days
from strategy.scanner import CalendarCandidate

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _engine():
    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    cache.get.return_value = None      # skip per-leg bid/ask size gate in this demo
    cache.get_chain.return_value = []  # force the B-S fallback path (no live marks)
    eng = DecisionEngine(
        cache=cache,
        portfolio_value=10_000.0,
        db_path=Path(tempfile.mktemp(suffix=".db")),
        daily_loss_limit=500.0,
    )
    eng._get_iv = MagicMock(return_value=0.80)
    return eng


def _candidate(near_days: int, far_days: int) -> CalendarCandidate:
    return CalendarCandidate(
        asset="BTC", strike=90_000.0, option_type="Call",
        near_instrument=f"BTC-{_future_label(near_days)}-90000-C",
        far_instrument=f"BTC-{_future_label(far_days)}-90000-C",
        near_days=near_days, far_days=far_days, spot=90_000.0,
        near_iv=0.90, far_iv=0.70, iv_contango=0.20,
        near_ask=0.0255, near_bid=0.0245, far_ask=0.0445, far_bid=0.0435,
        net_debit=0.02, near_oi=500.0, far_oi=500.0,
        pop=0.55, be_lo=80_000.0, be_hi=100_000.0, ev_score=0.5,
    )


print("\n── 1. A freshly-entered 1d-near position is NOT roll-eligible (#206) ──")
eng = _engine()
executor = MagicMock()
eng._executor = executor
pos = {
    "trade_id": 206, "asset": "BTC", "option_type": "Call", "strike": 90_000.0,
    "expiry_near": _future_label(1), "expiry_far": _future_label(30),
    "near_days": 1, "far_days": 30, "qty": 1.0, "net_debit": 0.02, "spot_open": 90_000.0,
    "near_instrument": f"BTC-{_future_label(1)}-90000-C",
    "far_instrument": f"BTC-{_future_label(30)}-90000-C",
    "open_fees": 0.0,
}
with patch("strategy.decision.check_calendar_status", return_value=("ok", 0.025, 1.0, "OK")), \
     patch("strategy.decision.scan", return_value=[_candidate(10, 30)]):
    action, _ = eng._monitor_position(pos)
check("no roll on first tick (near_days_left == near_days)", not executor.roll_near_leg.called)
check("no close on first tick", not executor.close_spread.called)
check("position held (action is None)", action is None)

print("\n── 2. A position that has genuinely decayed IS roll-eligible ──")
eng2 = _engine()
executor2 = MagicMock()
executor2.roll_near_leg.return_value = True
eng2._executor = executor2
pos2 = dict(pos)
pos2["near_days"] = 7          # entered as a 7d near leg …
pos2["expiry_near"] = _future_label(2)  # … now decayed to 2 days left
pos2["near_instrument"] = f"BTC-{_future_label(2)}-90000-C"
with patch("strategy.decision.check_calendar_status", return_value=("ok", 0.025, 1.0, "OK")), \
     patch("strategy.decision.scan", return_value=[_candidate(10, 30)]):
    eng2._monitor_position(pos2)
check("roll attempted once genuine decay has occurred", executor2.roll_near_leg.called)

print("\n── 3. _try_roll rejects a same-expiry-as-far degenerate candidate (#207) ──")
eng3 = _engine()
executor3 = MagicMock()
eng3._executor = executor3
far_label = _future_label(30)
pos3 = {
    "trade_id": 207, "asset": "BTC", "option_type": "Call", "strike": 90_000.0,
    "qty": 1.0, "net_debit": 0.02, "spot_open": 90_000.0,
    "near_instrument": f"BTC-{_future_label(2)}-90000-C",
    "far_instrument": f"BTC-{far_label}-90000-C",
}
degenerate = _candidate(near_days=30, far_days=30)  # new near == far expiry
with patch("strategy.decision.scan", return_value=[degenerate]):
    rolled = eng3._try_roll(pos3, 90_000.0)
check("degenerate same-expiry roll rejected", rolled is False)
check("executor.roll_near_leg NOT called", not executor3.roll_near_leg.called)
check("_expiry_gap_days(near==far) == 0", _expiry_gap_days(
    f"BTC-{far_label}-90000-C", f"BTC-{far_label}-90000-C") == 0)

failed = [l for l, s in results if s == FAIL]
print("\n" + "=" * 60)
print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
print("=" * 60)
sys.exit(1 if failed else 0)
