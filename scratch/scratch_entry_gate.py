"""
scratch/scratch_entry_gate.py
==============================
Demonstrates the liquidity gate checks added to strategy/decision.py:

  1. Per-leg spread gate  — rejects candidates where (ask-bid)/mid > MAX_LEG_SPREAD_PCT (5%)
  2. Entry premium gate   — rejects candidates where net_debit > spread_mid * (1 + MAX_ENTRY_PREMIUM) (10%)

These two checks prevent the bot from entering positions that start deeply
underwater due to wide bid/ask friction on thin crypto option books.

Root cause from 2026-06-23 log: trade_id=5 entered at $60.60/unit but the
market spread mid was ~$40.25/unit — a 51% premium over fair value.  The
position was immediately 34% underwater and stopped out 31 minutes later.

Run with:
    python -m scratch.scratch_entry_gate
"""

import sys
import os

# Abort if running against the live exchange
import config
if getattr(config, "TRADING_MODE", None) == "live" or not getattr(config, "DERIBIT_PAPER", True):
    print("ERROR: scratch scripts must not run against the live exchange.")
    sys.exit(1)

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from strategy.decision import DecisionEngine
from strategy.scanner import CalendarCandidate

PASS = "✓ PASS"
FAIL = "✗ FAIL"


def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _make_candidate(**overrides) -> CalendarCandidate:
    near_label = _future_label(10)
    far_label  = _future_label(35)
    base = dict(
        asset="ETH",
        strike=1400.0,
        option_type="Call",
        near_instrument=f"ETH-{near_label}-1400-C",
        far_instrument=f"ETH-{far_label}-1400-C",
        near_days=10,
        far_days=35,
        spot=1400.0,
        near_iv=0.85,
        far_iv=0.75,
        iv_contango=0.10,
        near_ask=62.0,
        near_bid=60.0,   # near_mid=61, spread=2/61≈3.3% — tight
        far_ask=72.0,
        far_bid=70.0,    # far_mid=71, spread=2/71≈2.8% — tight
        net_debit=12.0,  # far_ask(72) - near_bid(60) = 12; spread_mid=71-61=10; premium=20%
        near_oi=500.0,
        far_oi=500.0,
        pop=0.55,
        be_lo=1200.0,
        be_hi=1600.0,
        ev_score=0.25,
        qty=1.0,
    )
    base.update(overrides)
    return CalendarCandidate(**base)


def _make_engine() -> DecisionEngine:
    cache = MagicMock()
    cache.get_spot.return_value = 1400.0
    cache.get_chain.return_value = []
    return DecisionEngine(cache=cache, portfolio_value=5000.0)


def _check(engine, candidate, label: str, expect_blocked: bool) -> bool:
    reason = engine._check_liquidity_gate(candidate)
    blocked = reason is not None
    ok = blocked == expect_blocked
    status = PASS if ok else FAIL
    verdict = "BLOCKED" if blocked else "ALLOWED"
    print(f"  {status}  [{verdict}]  {label}")
    if reason:
        print(f"         reason: {reason}")
    return ok


def main() -> None:
    engine = _make_engine()
    results = []

    print("=" * 70)
    print("Liquidity gate demo  (MAX_LEG_SPREAD_PCT=5%  MAX_ENTRY_PREMIUM=10%)")
    print("=" * 70)

    # ── Section 1: Per-leg spread gate ────────────────────────────────────────
    print("\n1. Per-leg bid/ask spread gate")

    # Near leg: bid=45, ask=55 → mid=50, spread=20% → blocked
    c = _make_candidate(near_bid=45.0, near_ask=55.0, net_debit=55.0 - 45.0 + 10.0)
    results.append(_check(engine, c, "Near-leg spread 20% (> 5%) — must block", expect_blocked=True))

    # Far leg: bid=55, ask=85 → mid=70, spread=43% → blocked
    c = _make_candidate(far_bid=55.0, far_ask=85.0, net_debit=85.0 - 60.0)
    results.append(_check(engine, c, "Far-leg spread 43% (> 5%) — must block", expect_blocked=True))

    # Both legs tight: 2% spread each → allowed (but premium check may fire)
    c = _make_candidate(
        near_bid=60.0, near_ask=61.2,  # near_mid=60.6, spread=2%
        far_bid=70.0,  far_ask=71.4,   # far_mid=70.7,  spread=2%
        net_debit=71.4 - 60.0,         # =11.4; spread_mid=70.7-60.6=10.1; premium=12.9% — still blocked by premium
    )
    results.append(_check(engine, c, "Both legs 2% spread but premium 12.9% — premium gate fires", expect_blocked=True))

    # ── Section 2: Entry premium gate ─────────────────────────────────────────
    print("\n2. Entry premium gate  (net_debit vs spread_mid)")

    # Mirrors the live incident: debit 51% above mid → blocked
    # near_mid=40.25, far_mid=80.85, spread_mid=40.6, debit=60.6, premium=49%
    c = _make_candidate(
        near_bid=39.25, near_ask=41.25,   # near_mid=40.25, spread=5% (at limit — allow)
        far_bid=79.85,  far_ask=81.85,    # far_mid=80.85,  spread≈2.5%
        net_debit=60.6,                   # far_ask(81.85) - near_bid(39.25) = 42.6 normally
                                          # but setting to 60.6 to simulate the live case
    )
    # Manually compute to confirm: spread_mid=80.85-40.25=40.6; premium=(60.6-40.6)/40.6=49%
    results.append(_check(engine, c, "Entry premium 49% (live trade_id=5 scenario) — must block", expect_blocked=True))

    # Premium just below limit: 9.9% → allowed
    c = _make_candidate(
        near_bid=60.0, near_ask=61.2,   # near_mid=60.6, spread=2%
        far_bid=71.0,  far_ask=72.2,    # far_mid=71.6,  spread≈1.7%
        net_debit=11.0 * 1.099,         # spread_mid=71.6-60.6=11; debit=12.089; premium≈9.9%
    )
    results.append(_check(engine, c, "Entry premium 9.9% (just below 10%) — must allow", expect_blocked=False))

    # Premium 9% — allowed
    c = _make_candidate(
        near_bid=60.0, near_ask=61.2,   # near_mid=60.6
        far_bid=71.0,  far_ask=72.2,    # far_mid=71.6; spread_mid=11
        net_debit=11.0 * 1.09,          # premium=9% → pass
    )
    results.append(_check(engine, c, "Entry premium 9% (< 10%) — must allow", expect_blocked=False))

    # ── Section 3: Clean candidate passes all checks ──────────────────────────
    print("\n3. Clean candidate (passes all checks)")

    c = _make_candidate(
        near_bid=60.0, near_ask=61.2,   # 2% spread
        far_bid=71.0,  far_ask=72.2,    # 1.7% spread
        net_debit=11.5,                 # spread_mid=11; premium=4.5%
    )
    results.append(_check(engine, c, "Tight spreads, low premium — must allow", expect_blocked=False))

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(results)
    total  = len(results)
    print(f"\n{'='*70}")
    print(f"Result: {passed}/{total} checks passed")
    print(f"Config: MAX_LEG_SPREAD_PCT={config.MAX_LEG_SPREAD_PCT:.0%}  "
          f"MAX_ENTRY_PREMIUM={config.MAX_ENTRY_PREMIUM:.0%}")
    if passed == total:
        print("All checks passed — liquidity gate is working correctly.")
    else:
        print("SOME CHECKS FAILED — review the gate logic.")
        sys.exit(1)


if __name__ == "__main__":
    main()
