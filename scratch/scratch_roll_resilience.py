"""
scratch/scratch_roll_resilience.py
==================================
Offline demonstration of the Phase 26c roll-failure resilience fixes for the
2026-07-22/23 test run (trades 14/15/16) and paper trade 208.

Before Phase 26c, a *single* failed roll attempt immediately liquidated the
position (`Roll failed — closing`), so three healthy positions (110%/101%/98%
of debit) were force-closed on one bad scan tick.  Two structural causes:

    * `_try_roll` only ever looked at `matches[0]`, giving up whenever the top
      match happened to be the currently-held near leg (trades 14/15).
    * the roll search inherited entry-grade filters (moneyness/POP/contango), so
      a drifted position past the moneyness cap became structurally unrollable
      (paper trade 208).

The fixes:
    26c  a failed roll HOLDS the position and retries up to POSITION_FAILURE_RETRY_CAP
    26c  `_try_roll` iterates ALL roll candidates, excluding the held near leg
    26c  a roll-specific `scan(roll_for=...)` relaxes moneyness/POP/contango but
         keeps the liquidity / OI / gap / margin gates
    26c  roll-failure and drain closes are labelled honestly

No network, no live orders — runs against in-memory helpers and a temp DB.

Run from the repo root:
    python -m scratch.scratch_roll_resilience

Aborts if TRADING_MODE == "live".
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from data.deribit_feed import TickerSnapshot
from strategy.decision import DecisionEngine
from strategy.scanner import scan

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _snap(instrument: str, mark_iv: float, bid: float, ask: float, oi: float = 600) -> TickerSnapshot:
    return TickerSnapshot(
        instrument=instrument, asset=instrument.split("-")[0], spot=100_000.0,
        mark_price=(bid + ask) / 2, mark_iv=mark_iv, bid=bid, ask=ask,
        open_interest=oi, bid_size=10.0, ask_size=10.0, timestamp=time.time(),
    )


def demo_deep_otm_roll_candidate_survives() -> None:
    print("\n1. A deep-OTM (entry-illegal) strike still yields a ROLL candidate")
    strike = 130_000  # 30% from 100k spot — would fail the entry moneyness cap
    near = f"BTC-{_future_label(6)}-{strike}-C"
    far  = f"BTC-{_future_label(30)}-{strike}-C"
    snaps = [
        _snap(near, mark_iv=0.80, bid=4800, ask=5200),
        _snap(far,  mark_iv=0.82, bid=8200, ask=8800),  # backwardation: fails entry contango
    ]
    cache = MagicMock()
    cache.get_spot.return_value = 100_000.0
    cache.get_chain.return_value = snaps

    entry = scan(cache, assets=["BTC"], near_days_options=[6], far_days_options=[30])
    roll = scan(cache, roll_for={"asset": "BTC", "strike": float(strike),
                                 "option_type": "Call", "far_instrument": far},
                near_days_options=[6], far_days_options=[30])
    check("entry scan rejects the deep-OTM / backwardated strike", len(entry) == 0)
    check("roll scan (relaxed) finds a candidate", len(roll) >= 1)
    check("roll candidate keeps the position's own far leg",
          all(c.far_instrument == far for c in roll))


def demo_single_roll_failure_holds() -> None:
    print("\n2. A single failed roll HOLDS the position (retries next tick)")
    executor = MagicMock()
    executor.roll_near_leg.return_value = False  # roll fails
    executor.close_spread.return_value = 0.02
    executor.last_close_fills = None

    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    cache.get.return_value = None  # skip the bid/ask-size gate (no live snapshot)
    engine = DecisionEngine(cache=cache, portfolio_value=50_000.0, executor=executor)

    pos = {
        "trade_id": 14, "status": "Open", "asset": "BTC", "option_type": "Call",
        "strike": 90_000.0, "expiry_near": _future_label(2), "expiry_far": _future_label(30),
        "qty": 1.0, "net_debit": 0.02, "spot_open": 90_000.0,
        "near_days": 7, "far_days": 30,   # entered at 7d, now 2d left → decayed, roll-eligible
        "near_instrument": f"BTC-{_future_label(2)}-90000-C",
        "far_instrument":  f"BTC-{_future_label(30)}-90000-C",
        "open_fees": 0.0, "close_fees": 0.0,
    }

    from strategy.scanner import CalendarCandidate
    # Tight per-leg spreads so the candidate passes the liquidity gate.
    fake_candidate = CalendarCandidate(
        asset="BTC", strike=90_000.0, option_type="Call",
        near_instrument=f"BTC-{_future_label(6)}-90000-C",
        far_instrument=pos["far_instrument"], near_days=6, far_days=30,
        spot=90_000.0, near_iv=0.85, far_iv=0.75, iv_contango=0.10,
        near_ask=0.0205, near_bid=0.0200, far_ask=0.0505, far_bid=0.0500,
        net_debit=0.0305, near_oi=600, far_oi=600, pop=0.5,
        be_lo=0.0, be_hi=0.0, ev_score=0.3, qty=1.0,
    )

    with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
         patch("strategy.decision.check_calendar_status", return_value=("ok", 0.025, 1.0, "OK")), \
         patch("strategy.decision.scan", return_value=[fake_candidate]), \
         patch("strategy.decision.close_calendar_trade"):
        engine._get_iv = MagicMock(return_value=0.80)
        engine.monitor_tick()

    check("roll was attempted", executor.roll_near_leg.called)
    check("position NOT closed on the first failed roll", not executor.close_spread.called)
    check("failure counter incremented to 1", engine._close_roll_failures.get(14) == 1)


def demo_roll_failure_closes_after_cap() -> None:
    print("\n3. Only after the retry cap is a roll-failed position closed (labelled honestly)")
    executor = MagicMock()
    executor.roll_near_leg.return_value = False
    executor.close_spread.return_value = 0.02
    executor.last_close_fills = None

    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    cache.get.return_value = None
    engine = DecisionEngine(cache=cache, portfolio_value=50_000.0, executor=executor)

    pos = {
        "trade_id": 15, "status": "Open", "asset": "BTC", "option_type": "Call",
        "strike": 90_000.0, "expiry_near": _future_label(2), "expiry_far": _future_label(30),
        "qty": 1.0, "net_debit": 0.02, "spot_open": 90_000.0,
        "near_days": 7, "far_days": 30,
        "near_instrument": f"BTC-{_future_label(2)}-90000-C",
        "far_instrument":  f"BTC-{_future_label(30)}-90000-C",
        "open_fees": 0.0, "close_fees": 0.0,
    }
    engine._close_roll_failures[15] = config.POSITION_FAILURE_RETRY_CAP  # at the cap

    recorded = {}

    def _capture(**kwargs):
        recorded.update(kwargs)

    with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
         patch("strategy.decision.check_calendar_status", return_value=("ok", 0.025, 1.0, "OK")), \
         patch("strategy.decision.scan", return_value=[]), \
         patch("strategy.decision.close_calendar_trade", side_effect=_capture):
        engine._get_iv = MagicMock(return_value=0.80)
        engine.monitor_tick()

    check("position closed once retry cap reached", executor.close_spread.called)
    check("close labelled 'Closed (Roll Failed)' (not a fake Auto Stop)",
          recorded.get("result") == "Closed (Roll Failed)")


def main() -> None:
    print("=" * 68)
    print("  Phase 26c — roll-failure resilience (offline demo)")
    print("=" * 68)
    demo_deep_otm_roll_candidate_survives()
    demo_single_roll_failure_holds()
    demo_roll_failure_closes_after_cap()

    print("\n" + "=" * 68)
    failed = [lbl for lbl, r in results if r == FAIL]
    if failed:
        print(f"  {len(failed)} CHECK(S) FAILED:")
        for lbl in failed:
            print(f"    ✗ {lbl}")
        sys.exit(1)
    print(f"  ALL {len(results)} CHECKS PASSED")
    print("=" * 68)


if __name__ == "__main__":
    main()
