"""
scratch/scratch_liquidity_gate_qty.py
======================================
Offline demonstration of the Phase 27e liquidity-gate fix.

Symptom (2026-07-27 test-mode run): an entry sized at **qty 29** was approved
into a far-leg book quoting **8** available.  The order could not fill in full,
so it partially filled, timed out waiting for the rest, and had to be cancelled
and flattened — a round trip through the spread for nothing.

Root cause: ``strategy/decision.py::_check_liquidity_gate`` compared every
quoted size against the constants ``MIN_LEG_BID_SIZE`` / ``MIN_LEG_ASK_SIZE``
(both ``1``) and never against the quantity it was about to submit.  Any book
quoting a single contract passed the gate for an order of any size.  Worse, the
check was written as ``if snap.ask_size > 0:`` — a leg quoting **nothing** at all
skipped the check entirely instead of failing it.

The fix sizes the requirement against the order, on the side each leg actually
hits:

    entry:  sell the near leg into the bid   → near.bid_size >= qty
            buy  the far  leg from the ask   → far.ask_size  >= qty
    roll:   only the new near leg is sold    → far leg not size-checked

Sides the trade does not touch keep the plain ``MIN_LEG_*_SIZE`` floor, so a thin
quote on an untraded side cannot block an otherwise good entry — and a roll is
not held to the depth of a leg it never trades (the Phase 26c failure mode where
healthy positions were liquidated over gates that did not apply to the roll).

No network, no live orders — runs entirely against a fake in-memory cache.

Run from the repo root:
    python -m scratch.scratch_liquidity_gate_qty

Aborts if TRADING_MODE == "live".
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from data.deribit_feed import TickerSnapshot
from strategy.decision import DecisionEngine
from strategy.scanner import CalendarCandidate

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


# ── Fixtures modelled on the logged failure ───────────────────────────────────

SPOT = 65_000.0


def _label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _candidate(qty: float) -> CalendarCandidate:
    """An otherwise-clean ATM BTC calendar, sized at *qty*."""
    near = f"BTC-{_label(7)}-65000-P"
    far  = f"BTC-{_label(35)}-65000-P"
    return CalendarCandidate(
        asset="BTC", strike=65_000.0, option_type="Put",
        near_instrument=near, far_instrument=far,
        near_days=7, far_days=35,
        spot=SPOT, near_iv=0.85, far_iv=0.80, iv_contango=0.05,
        near_ask=1020.0, near_bid=1000.0,
        far_ask=1530.0,  far_bid=1500.0,
        net_debit=530.0,
        near_oi=500.0, far_oi=600.0,
        pop=0.55, be_lo=60_000.0, be_hi=70_000.0, ev_score=0.30,
        qty=qty,
    )


def _snap(instrument: str, bid: float, ask: float,
          bid_size: float, ask_size: float) -> TickerSnapshot:
    return TickerSnapshot(
        instrument=instrument, asset="BTC", spot=SPOT,
        mark_price=(bid + ask) / 2, mark_iv=0.82,
        bid=bid, ask=ask, open_interest=500.0,
        bid_size=bid_size, ask_size=ask_size,
    )


class _FakeCache:
    def __init__(self, snaps: dict[str, TickerSnapshot]) -> None:
        self._snaps = snaps

    def get(self, instrument: str):
        return self._snaps.get(instrument)

    def get_spot(self, asset: str) -> float:
        return SPOT

    def get_chain(self, asset: str) -> list:
        return list(self._snaps.values())


def _engine(candidate: CalendarCandidate, tmp: Path,
            near_bid_size: float, near_ask_size: float,
            far_bid_size: float, far_ask_size: float) -> DecisionEngine:
    cache = _FakeCache({
        candidate.near_instrument: _snap(
            candidate.near_instrument, candidate.near_bid, candidate.near_ask,
            near_bid_size, near_ask_size,
        ),
        candidate.far_instrument: _snap(
            candidate.far_instrument, candidate.far_bid, candidate.far_ask,
            far_bid_size, far_ask_size,
        ),
    })
    return DecisionEngine(
        cache=cache,
        portfolio_value=100_000.0,
        db_path=tmp / "scratch_liquidity_gate_qty.db",
    )


def _describe(reason: str | None) -> str:
    return "PASSED the gate" if reason is None else f"REJECTED — {reason}"


# ── 1. The logged failure: qty 29 into a book quoting 8 ───────────────────────

def demo_logged_failure(tmp: Path) -> None:
    print("\n1. The logged failure — entry sized qty 29, far ask quoting 8")

    candidate = _candidate(qty=29.0)
    engine = _engine(candidate, tmp,
                     near_bid_size=100.0, near_ask_size=100.0,
                     far_bid_size=100.0,  far_ask_size=8.0)

    # Old behaviour: qty ignored, only the MIN_LEG_*_SIZE floors applied.
    with patch.object(config, "REQUIRE_LEG_SIZE_FOR_QTY", False):
        old = engine._check_liquidity_gate(candidate)
    new = engine._check_liquidity_gate(candidate)

    print(f"   before 27e (qty ignored): {_describe(old)}")
    print(f"   after  27e (qty checked): {_describe(new)}")
    check("old gate approved qty 29 into a book of 8", old is None)
    check("new gate rejects it", new is not None and "order qty 29.0" in new)
    print("   → this is the order that partially filled, timed out, and had to "
          "be\n     cancelled and flattened.")


# ── 2. Both traded sides are covered ──────────────────────────────────────────

def demo_both_traded_sides(tmp: Path) -> None:
    print("\n2. Each leg is checked on the side the entry actually hits")

    candidate = _candidate(qty=29.0)

    thin_near = _engine(candidate, tmp,
                        near_bid_size=3.0, near_ask_size=100.0,
                        far_bid_size=100.0, far_ask_size=100.0)
    reason = thin_near._check_liquidity_gate(candidate)
    print(f"   near bid 3 (we sell into it) : {_describe(reason)}")
    check("thin near bid rejected — that is the side the entry sells into",
          reason is not None and "near-leg bid_size" in reason)

    deep = _engine(candidate, tmp,
                   near_bid_size=29.0, near_ask_size=100.0,
                   far_bid_size=100.0, far_ask_size=29.0)
    reason = deep._check_liquidity_gate(candidate)
    print(f"   both traded sides exactly 29 : {_describe(reason)}")
    check("a book that exactly covers the order passes", reason is None)


# ── 3. Untraded sides keep the constant floor ─────────────────────────────────

def demo_untraded_sides(tmp: Path) -> None:
    print("\n3. Sides the entry never touches keep the MIN_LEG_*_SIZE floor")

    candidate = _candidate(qty=29.0)
    engine = _engine(candidate, tmp,
                     near_bid_size=50.0, near_ask_size=1.0,   # near ask: not hit
                     far_bid_size=1.0,   far_ask_size=50.0)   # far bid:  not hit
    reason = engine._check_liquidity_gate(candidate)
    print(f"   near ask 1, far bid 1 (neither traded): {_describe(reason)}")
    check("a thin quote on an untraded side does not block the entry",
          reason is None)
    print("   → the gate tightened only where the order is actually filled; it "
          "did\n     not become blanket-stricter.")


# ── 4. A leg quoting nothing no longer skips the check ────────────────────────

def demo_zero_size(tmp: Path) -> None:
    print("\n4. A leg quoting no size at all")

    candidate = _candidate(qty=1.0)
    engine = _engine(candidate, tmp,
                     near_bid_size=0.0, near_ask_size=0.0,
                     far_bid_size=0.0,  far_ask_size=0.0)

    with patch.object(config, "REQUIRE_LEG_SIZE_DATA", False):
        old = engine._check_liquidity_gate(candidate)
    new = engine._check_liquidity_gate(candidate)

    print(f"   before 27e (size 0 skipped the check): {_describe(old)}")
    print(f"   after  27e (size 0 fails the check)  : {_describe(new)}")
    check("size 0 used to pass the gate", old is None)
    check("size 0 is now rejected", new is not None and "no quoted size" in new)
    print("   → REQUIRE_LEG_SIZE_DATA=False restores the old reading for a venue "
          "or\n     feed that genuinely does not report size.")


# ── 5. Rolls are not held to the untouched far leg ────────────────────────────

def demo_roll_relaxation(tmp: Path) -> None:
    print("\n5. Roll candidates — only the new near leg is traded")

    candidate = _candidate(qty=29.0)
    engine = _engine(candidate, tmp,
                     near_bid_size=100.0, near_ask_size=100.0,
                     far_bid_size=100.0,  far_ask_size=1.0)

    as_entry = engine._check_liquidity_gate(candidate)
    as_roll  = engine._check_liquidity_gate(candidate, is_roll=True)
    print(f"   far ask 1, as an entry : {_describe(as_entry)}")
    print(f"   far ask 1, as a roll   : {_describe(as_roll)}")
    check("as an entry the thin far ask rejects (we must buy it)",
          as_entry is not None)
    check("as a roll it passes (the far leg is not traded)", as_roll is None)

    thin_near = _engine(candidate, tmp,
                        near_bid_size=3.0, near_ask_size=100.0,
                        far_bid_size=100.0, far_ask_size=100.0)
    reason = thin_near._check_liquidity_gate(candidate, is_roll=True)
    print(f"   new near bid 3, as a roll: {_describe(reason)}")
    check("a roll is still held to depth on the near leg it does sell",
          reason is not None)
    print("   → a healthy position is not liquidated over a leg the roll never "
          "touches\n     (Phase 26c), but the leg it does trade must still fit.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 72)
    print("  Phase 27e — liquidity gate sized against the order (offline demo)")
    print("=" * 72)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        demo_logged_failure(tmp)
        demo_both_traded_sides(tmp)
        demo_untraded_sides(tmp)
        demo_zero_size(tmp)
        demo_roll_relaxation(tmp)

    print("\n" + "=" * 72)
    failed = [lbl for lbl, r in results if r == FAIL]
    if failed:
        print(f"  {len(failed)} CHECK(S) FAILED:")
        for lbl in failed:
            print(f"    ✗ {lbl}")
        sys.exit(1)
    print(f"  ALL {len(results)} CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
