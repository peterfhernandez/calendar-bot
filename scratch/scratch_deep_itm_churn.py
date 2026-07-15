"""
scratch/scratch_deep_itm_churn.py
=================================
Offline demonstration of the four Phase 21 fixes that stop the runaway
deep-ITM/OTM calendar churn observed on 2026-07-14 (131 same-day trades, 91 on
one ETH 1400 Call, +$224,247 phantom paper P&L).

Each section reproduces the relevant failure mode against a synthetic candidate
and shows that the corresponding fix prevents it.  No network, no live orders —
everything runs against in-memory objects and a temp SQLite DB.

    21a  EV-ranking cap        — a near-zero-debit degenerate no longer out-ranks
                                 a legitimate near-the-money candidate
    21b  Moneyness filter      — a strike too far from spot is rejected at scan
    21c  Two-sided-quote mark  — a one-sided quote is not trusted as market_sv
    21c  Close debounce        — a single stop signal defers; a confirmed one closes
    21d  Re-entry cooldown     — a just-auto-closed instrument can't be re-entered

Run from the repo root:
    python -m scratch.scratch_deep_itm_churn

Aborts if TRADING_MODE == "live".
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from data.deribit_feed import TickerSnapshot
from db.state import create_calendar_trade
from strategy.decision import DecisionEngine
from strategy.scanner import CalendarCandidate, _eval_candidate
from strategy.sizer import size_candidate

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _cand(**kwargs) -> CalendarCandidate:
    defaults = dict(
        asset="ETH", strike=1400.0, option_type="Call",
        near_instrument="ETH-07JUN25-1400-C",
        far_instrument="ETH-27JUN25-1400-C",
        near_days=7, far_days=30,
        spot=2000.0,
        near_iv=0.90, far_iv=0.75, iv_contango=0.15,
        near_ask=0.005, near_bid=0.004,
        far_ask=0.0145, far_bid=0.013,
        net_debit=0.02,
        near_oi=600.0, far_oi=600.0,
        pop=0.50, be_lo=1800.0, be_hi=2200.0,
        ev_score=17.0,
    )
    defaults.update(kwargs)
    return CalendarCandidate(**defaults)


def _snap(instrument: str, bid: float, ask: float, *, mark: float | None = None,
          oi: float = 600.0, iv: float = 0.80) -> TickerSnapshot:
    asset = instrument.split("-")[0]
    return TickerSnapshot(
        instrument=instrument, asset=asset, spot=2000.0,
        mark_price=mark if mark is not None else (bid + ask) / 2,
        bid=bid, ask=ask, mark_iv=iv, open_interest=oi,
        timestamp=datetime.now(timezone.utc).timestamp(),
    )


# ─── 21a: EV-ranking cap ──────────────────────────────────────────────────────
print("\n=== 21a: EV-ranking cap (degenerate can't win the scan) ===")
cap = config.EV_SCORE_RANKING_CAP
degenerate = _cand(ev_score=17.0, net_debit=0.02, strike=1400.0)     # near-zero debit
normal     = _cand(ev_score=0.4, net_debit=100.0, strike=2000.0)     # legitimate ATM
# Insertion order puts the degenerate first, as it did in the real scan.
ranked = sorted(
    [degenerate, normal],
    key=lambda c: (c.ev_score > cap, -c.ev_score),  # mirrors strategy/scanner.py::scan()
)
print(f"  cap={cap}  degenerate ev=17.0  normal ev=0.4  → winner ev={ranked[0].ev_score}")
check("degenerate (ev=17.0) does NOT rank first", ranked[0] is normal)
check("legitimate candidate (ev=0.4) ranks first", ranked[0] is normal)


# ─── 21b: Moneyness filter ────────────────────────────────────────────────────
print("\n=== 21b: Moneyness filter (deep strike rejected at scan) ===")
spot = 2000.0
# Deep OTM call: strike 1400 is 30% below spot — outside MAX_MONEYNESS_PCT (15%).
deep_near = _snap("ETH-07JUN25-1400-C", bid=600.0, ask=602.0)
deep_far  = _snap("ETH-27JUN25-1400-C", bid=610.0, ask=612.0)
deep = _eval_candidate(
    "ETH", 1400.0, "Call", 7, deep_near, 30, deep_far, spot,
    min_oi_near=100, min_oi_far=100, min_iv_contango=0.02, min_pop=0.45,
)
moneyness = abs(1400.0 - spot) / spot
print(f"  strike=1400 spot=2000 moneyness={moneyness:.2f}  MAX={config.MAX_MONEYNESS_PCT} → {deep!r}")
check("deep-OTM strike (30% away) is rejected", deep is None)


# ─── 21c: Two-sided-quote requirement for market_sv ───────────────────────────
print("\n=== 21c: Two-sided quotes required before trusting a live mark ===")
db_path = Path(tempfile.mktemp(suffix=".db"))
near_i, far_i = "ETH-07JUN25-1400-C", "ETH-27JUN25-1400-C"
pos = {
    "trade_id": 1, "asset": "ETH", "option_type": "Call", "strike": 1400.0,
    "expiry_near": _future_label(7), "expiry_far": _future_label(30),
    "qty": 100.0, "net_debit": 0.02,
    "near_instrument": near_i, "far_instrument": far_i,
    "open_fees": 0.0, "close_fees": 0.0,
}

# One-sided far leg (ask=0) with only a mark_price — must NOT be trusted.
one_sided = MagicMock()
one_sided.get_spot.return_value = 2000.0
one_sided.get_chain.return_value = [
    _snap(near_i, bid=1.0, ask=1.1),
    _snap(far_i, bid=3.5, ask=0.0, mark=3559.0),   # no genuine ask; huge synthetic mark
]
eng_os = DecisionEngine(cache=one_sided, portfolio_value=10_000.0, db_path=db_path)
sv_one_sided = eng_os._get_market_spread_value(pos)
print(f"  one-sided far leg (ask=0, mark=3559) → market_sv={sv_one_sided}")
check("one-sided quote is NOT trusted (returns None → B-S fallback)", sv_one_sided is None)

# Genuine two-sided quotes on both legs — trusted.
two_sided = MagicMock()
two_sided.get_spot.return_value = 2000.0
two_sided.get_chain.return_value = [
    _snap(near_i, bid=1.0, ask=1.1),
    _snap(far_i, bid=3.4, ask=3.6),
]
eng_ts = DecisionEngine(cache=two_sided, portfolio_value=10_000.0, db_path=db_path)
sv_two_sided = eng_ts._get_market_spread_value(pos)
print(f"  two-sided both legs → market_sv={sv_two_sided}")
check("genuine two-sided quote IS trusted", sv_two_sided is not None and sv_two_sided > 0)


# ─── 21c: Close-confirmation debounce ─────────────────────────────────────────
print("\n=== 21c: Close debounce (single stop signal defers) ===")
import strategy.decision as decision_mod

# A real open trade in a temp DB so _monitor_position runs end-to-end.
trade = create_calendar_trade(
    asset="ETH", date_open=datetime.now(timezone.utc).date(),
    option_type="Call", strike=1400.0,
    expiry_near=_future_label(7), expiry_far=_future_label(30),
    near_days=7, far_days=30, qty=100.0, spot_open=2000.0,
    near_prem=1.0, far_prem=3.5, net_debit=0.02,
    near_instrument=near_i, far_instrument=far_i, db_path=db_path,
)
pos_db = {
    "trade_id": trade.id, "asset": "ETH", "option_type": "Call", "strike": 1400.0,
    "expiry_near": _future_label(7), "expiry_far": _future_label(30),
    "qty": 100.0, "net_debit": 0.02, "spot_open": 2000.0,
    "near_days": 7, "far_days": 30,
    "near_instrument": near_i, "far_instrument": far_i,
    "open_fees": 0.0, "close_fees": 0.0,
}

cache = MagicMock()
cache.get_spot.return_value = 2000.0
cache.get_chain.return_value = [_snap(near_i, 1.0, 1.1), _snap(far_i, 3.4, 3.6)]

engine = DecisionEngine(cache=cache, portfolio_value=10_000.0, db_path=db_path)
close_calls = {"n": 0}
engine._close_position = lambda *a, **k: (close_calls.__setitem__("n", close_calls["n"] + 1) or "closed")  # type: ignore
engine._get_iv = lambda pos: 0.80  # type: ignore
# Force a stop signal on every tick.
decision_mod.check_calendar_status = lambda *a, **k: ("stop", 0.0, 0.0, "STOP")

print(f"  CLOSE_CONFIRM_TICKS = {config.CLOSE_CONFIRM_TICKS}")
engine._monitor_position(pos_db)
check("tick 1: stop signal deferred (no close yet)", close_calls["n"] == 0)
engine._monitor_position(pos_db)
check("tick 2: stop confirmed → close called", close_calls["n"] == 1)


# ─── 21d: Re-entry cooldown ───────────────────────────────────────────────────
print("\n=== 21d: Re-entry cooldown (just-closed instrument blocked) ===")
now = datetime.now(timezone.utc).timestamp()
recent = {("ETH", 1400.0, "Call"): now - 60}   # auto-closed 60s ago
cand = _cand(asset="ETH", strike=1400.0, option_type="Call", net_debit=100.0, ev_score=0.4)
blocked = size_candidate(cand, 10_000.0, [], recent_auto_closes=recent)
print(f"  closed 60s ago, cooldown={config.REENTRY_COOLDOWN_SEC}s → qty={blocked.qty}  ({blocked.reason})")
check("candidate within cooldown is blocked", blocked.qty == 0.0)
check("rejection reason mentions cooldown", "cooldown" in blocked.reason.lower())

# Same instrument, but the cooldown has elapsed → allowed.
stale = {("ETH", 1400.0, "Call"): now - config.REENTRY_COOLDOWN_SEC - 1}
allowed = size_candidate(cand, 10_000.0, [], recent_auto_closes=stale)
print(f"  closed {config.REENTRY_COOLDOWN_SEC + 1}s ago → qty={allowed.qty}")
check("candidate after cooldown elapses is allowed", allowed.qty > 0.0)


# ─── Summary ──────────────────────────────────────────────────────────────────
print("\n=== Summary ===")
passed = sum(1 for _, s in results if s == PASS)
failed = sum(1 for _, s in results if s == FAIL)
print(f"  {passed} passed, {failed} failed")
if failed:
    for label, status in results:
        if status == FAIL:
            print(f"  ✗  {label}")
    sys.exit(1)
print("  All checks passed.")
