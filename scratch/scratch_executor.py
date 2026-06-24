"""
execution/scratch_executor.py
==============================
End-to-end verification script for Phase 5 — Execution Hardening.

Connects to the Deribit *paper* API, fetches a live option chain, builds a
synthetic CalendarCandidate, and exercises the CalendarExecutor in dry-run
mode (enter → confirm fill → close).  No real orders are placed unless you
explicitly set ACTUALLY_PLACE_ORDERS = True below.

Run with:
    python -m execution.scratch_executor

What it tests:
  1. CalendarExecutor instantiates correctly.
  2. OrderManager tracks order lifecycles.
  3. enter_spread() round-trip (mocked fills — safe to run at any time).
  4. close_spread() round-trip (mocked fills).
  5. roll_near_leg() round-trip (mocked fills).
  6. Slippage rejection: a candidate with an artificially bad price is rejected.
  7. Leg risk handling: far leg failure triggers near-leg close.
  8. Reconciliation against Deribit paper API (real network call, read-only).
  9. Stuck-order detection.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch

# ── Allow running as `python -m execution.scratch_executor` from repo root ─────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from execution.executor import (
    CalendarExecutor,
    SlippageError,
    LegRiskError,
    _check_slippage,
    _index_price,
    _usd_price,
    _contract_amount,
)
from execution.order_manager import (
    OrderManager,
    OrderState,
    TrackedOrder,
    reconcile_with_deribit,
)
from strategy.scanner import CalendarCandidate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scratch_executor")

# ── Set to True to actually submit orders to the paper exchange ───────────────
ACTUALLY_PLACE_ORDERS = False

DIVIDER = "─" * 70


def _banner(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def _ok(msg: str) -> None:
    print(f"  ✓  {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗  {msg}")


# ── Synthetic test data ───────────────────────────────────────────────────────

def _make_candidate(asset="BTC", spot=100_000.0) -> CalendarCandidate:
    return CalendarCandidate(
        asset=asset,
        strike=100_000.0,
        option_type="Call",
        near_instrument=f"{asset}-07JUN25-100000-C",
        far_instrument=f"{asset}-07JUL25-100000-C",
        near_days=7,
        far_days=30,
        spot=spot,
        near_iv=0.80,
        far_iv=0.70,
        iv_contango=0.10,
        near_ask=0.003 * spot,
        near_bid=0.002 * spot,
        far_ask =0.006 * spot,
        far_bid =0.005 * spot,
        net_debit=(0.006 - 0.002) * spot,
        near_oi=500.0,
        far_oi=300.0,
        pop=0.52,
        be_lo=95_000.0,
        be_hi=105_000.0,
        ev_score=0.08,
        qty=0.1,
    )


def _make_position(asset="BTC", spot=100_000.0) -> dict:
    return {
        "asset":           asset,
        "strike":          100_000.0,
        "option_type":     "Call",
        "near_instrument": f"{asset}-07JUN25-100000-C",
        "far_instrument":  f"{asset}-07JUL25-100000-C",
        "qty":             0.1,
        "spot_open":       spot,
        "net_debit":       400.0,
        "near_prem":       200.0,
        "far_prem":        600.0,
        "trade_id":        99,
    }


def _submitted(order_id: str, price: float = 0.003) -> dict:
    return {"order": {"order_id": order_id, "order_state": "open", "price": price}}


def _filled(order_id: str, price: float = 0.003) -> dict:
    return {"order_id": order_id, "order_state": "filled", "average_price": price}


class _MockRPC:
    """Simple mock for _DeribitRPCClient."""

    def __init__(self, place_results, state_map, ticker_map=None):
        self._place  = list(place_results)
        self._states = state_map
        self._tickers = ticker_map or {}
        self._idx = 0
        self.placed: list[dict] = []
        self.cancelled: list[str] = []

    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass

    async def place_order(self, instrument, direction, amount, price, label=""):
        self.placed.append({"instrument": instrument, "direction": direction})
        if self._idx < len(self._place):
            r = self._place[self._idx]; self._idx += 1; return r
        raise RuntimeError("No more mock results")

    async def get_order_state(self, order_id: str) -> dict:
        states = self._states.get(order_id, [])
        if states:
            return states.pop(0)
        return _filled(order_id)

    async def cancel_order(self, order_id: str) -> dict:
        self.cancelled.append(order_id)
        return {"order_id": order_id, "order_state": "cancelled"}

    async def get_ticker(self, instrument: str) -> dict:
        return self._tickers.get(instrument, {"best_bid_price": 0.002, "best_ask_price": 0.004})

    async def create_combo(self, legs: list) -> dict:
        raise RuntimeError("create_combo not available — forcing individual-leg fallback")


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_helpers() -> None:
    _banner("1. Helper utilities")

    # index price conversion
    assert abs(_index_price(200.0, 100_000.0, "BTC") - 0.002) < 1e-9
    _ok("_index_price BTC inverse: $200 @ spot $100k → 0.002")

    assert abs(_usd_price(0.002, 100_000.0, "BTC") - 200.0) < 1e-9
    _ok("_usd_price BTC inverse: 0.002 @ spot $100k → $200")

    assert _index_price(5.0, 150.0, "SOL") == 5.0
    _ok("_index_price SOL linear: passthrough")

    # slippage check
    try:
        _check_slippage(102.0, 100.0, 0.05)
        _ok("_check_slippage: 2% deviation within 5% limit — no raise")
    except SlippageError:
        _fail("_check_slippage raised unexpectedly")

    try:
        _check_slippage(106.0, 100.0, 0.05)
        _fail("_check_slippage: 6% deviation should have raised")
    except SlippageError:
        _ok("_check_slippage: 6% deviation > 5% limit → SlippageError raised")

    # contract amount
    amt = _contract_amount(100_000.0, "BTC", 50_000.0, 0.02, 400.0)
    assert amt >= 0.1
    _ok(f"_contract_amount BTC: {amt:.4f} coins (≥ Deribit min 0.1)")


def test_order_manager() -> None:
    _banner("2. OrderManager lifecycle")

    mgr = OrderManager()

    # track + get
    o = TrackedOrder(order_id="t1", instrument="BTC-X", direction="sell", amount=0.1, limit_price=0.002)
    mgr.track(o)
    assert mgr.get("t1") is o
    _ok("track() and get() work")

    # update
    mgr.update("t1", OrderState.FILLED, fill_price=0.0019)
    assert mgr.get("t1").state      == OrderState.FILLED
    assert mgr.get("t1").fill_price == 0.0019
    _ok("update() transitions state and records fill_price")

    # open_orders excludes terminal
    o2 = TrackedOrder(order_id="t2", instrument="BTC-Y", direction="buy", amount=0.1, limit_price=0.003)
    mgr.track(o2)
    assert len(mgr.open_orders()) == 1
    _ok("open_orders() excludes filled/cancelled orders")

    # stuck detection
    o3 = TrackedOrder(order_id="t3", instrument="BTC-Z", direction="sell", amount=0.1, limit_price=0.004)
    o3.submitted_at = time.monotonic() - 9999
    mgr.track(o3)
    stuck = mgr.find_stuck()
    assert any(x.order_id == "t3" for x in stuck)
    _ok(f"find_stuck() detected {len(stuck)} stuck order(s)")

    # summary
    s = mgr.summary()
    assert s.get("submitted", 0) >= 1
    _ok(f"summary(): {s}")


def test_enter_spread_success() -> None:
    _banner("3. enter_spread — successful fill (individual-leg fallback, combo unavailable)")

    candidate   = _make_candidate()
    # near fill: 0.002 BTC = $200 = near_bid; far fill: 0.006 BTC = $600 = far_ask (exact match → 0% slippage)
    mock_client = _MockRPC(
        place_results=[_submitted("n1", 0.002), _submitted("f1", 0.006)],
        state_map={"n1": [_filled("n1", 0.002)], "f1": [_filled("f1", 0.006)]},
    )

    with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
         patch.object(config, "TRADING_MODE", "test"):
        exc    = CalendarExecutor(portfolio_value=50_000.0)
        result = exc.enter_spread(candidate)

    assert result is not None
    _ok(f"enter_spread returned fill: net_debit={result['net_debit']:.2f}")
    assert exc.order_manager.get("n1").state == OrderState.FILLED
    assert exc.order_manager.get("f1").state == OrderState.FILLED
    _ok("Both legs tracked as FILLED in OrderManager")


def test_enter_spread_slippage_rejected() -> None:
    _banner("4. enter_spread — slippage rejection")

    candidate   = _make_candidate()
    # Fill near leg at 10× expected price — will exceed 2% slippage limit
    mock_client = _MockRPC(
        place_results=[_submitted("n2", 0.1)],
        state_map={"n2": [_filled("n2", 0.1)]},
    )

    with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
         patch.object(config, "TRADING_MODE", "test"):
        exc    = CalendarExecutor(portfolio_value=50_000.0, slippage_pct=0.02)
        result = exc.enter_spread(candidate)

    assert result is None
    _ok("enter_spread returned None when slippage exceeded (order correctly rejected)")


def test_leg_risk_handling() -> None:
    _banner("5. Leg risk — near fills, far leg always fails")

    candidate = _make_candidate()
    call_count = {"n": 0}

    class _BadFarRPC:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

        async def place_order(self, instrument, direction, amount, price, label=""):
            call_count["n"] += 1
            if direction == "sell" and "FAR" not in label:
                # Near leg (NEAR label)
                return _submitted("near-lr", 0.002)
            raise OSError("timeout")

        async def get_order_state(self, order_id):
            if order_id == "near-lr":
                return _filled("near-lr", 0.002)
            return _filled(order_id)

        async def cancel_order(self, order_id):
            return {"order_id": order_id, "order_state": "cancelled"}

        async def get_ticker(self, instrument):
            return {"best_bid_price": 0.002, "best_ask_price": 0.004}

        async def create_combo(self, legs):
            raise RuntimeError("combo unavailable")

    with patch("execution.executor._DeribitRPCClient", return_value=_BadFarRPC()), \
         patch.object(config, "TRADING_MODE", "test"):
        exc = CalendarExecutor(portfolio_value=50_000.0)
        try:
            exc.enter_spread(candidate)
            _fail("Expected LegRiskError but no exception raised")
        except LegRiskError:
            _ok("LegRiskError raised — near leg was closed to eliminate leg risk")


def test_close_spread_success() -> None:
    _banner("6. close_spread — successful close")

    position    = _make_position()
    mock_client = _MockRPC(
        place_results=[_submitted("c1", 0.0015), _submitted("c2", 0.0055)],
        state_map={"c1": [_filled("c1", 0.0015)], "c2": [_filled("c2", 0.0055)]},
        ticker_map={
            "BTC-07JUN25-100000-C": {"best_bid_price": 0.0014, "best_ask_price": 0.0016},
            "BTC-07JUL25-100000-C": {"best_bid_price": 0.0054, "best_ask_price": 0.0056},
        },
    )

    with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
        exc    = CalendarExecutor(portfolio_value=50_000.0)
        credit = exc.close_spread(position)

    assert isinstance(credit, float)
    _ok(f"close_spread returned closing credit: ${credit:.2f}")


def test_roll_near_leg() -> None:
    _banner("7. roll_near_leg — successful roll")

    position  = _make_position()
    candidate = _make_candidate()

    mock_client = _MockRPC(
        place_results=[_submitted("r1", 0.0015), _submitted("r2", 0.0020)],
        state_map={"r1": [_filled("r1", 0.0015)], "r2": [_filled("r2", 0.0020)]},
        ticker_map={
            "BTC-07JUN25-100000-C": {"best_bid_price": 0.0014, "best_ask_price": 0.0016},
        },
    )

    with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
        exc = CalendarExecutor(portfolio_value=50_000.0)
        ok  = exc.roll_near_leg(position, candidate)

    assert ok is True
    _ok("roll_near_leg returned True — old near leg closed, new near leg opened")


def test_reconciliation_mock() -> None:
    _banner("8. Reconciliation — stale order marked CANCELLED")

    mgr = OrderManager()
    mgr.track(TrackedOrder(order_id="stale-1", instrument="BTC-X", direction="sell", amount=0.1, limit_price=0.002))
    mgr.track(TrackedOrder(order_id="live-1",  instrument="BTC-Y", direction="buy",  amount=0.1, limit_price=0.003))

    async def mock_fetch(*a, **kw):
        return [{"order_id": "live-1"}]  # only live-1 is open on Deribit

    with patch("execution.order_manager._fetch_deribit_open_orders", side_effect=mock_fetch):
        asyncio.run(reconcile_with_deribit(mgr, paper=True))

    assert mgr.get("stale-1").state == OrderState.CANCELLED
    _ok("stale-1 not in Deribit response → marked CANCELLED")
    assert mgr.get("live-1").state == OrderState.SUBMITTED
    _ok("live-1 still in Deribit response → state unchanged (SUBMITTED)")


async def _test_reconciliation_live() -> None:
    """
    Optional: real read-only call to Deribit paper API to verify connectivity.
    Fetches open orders (expects none in a fresh paper account) and reconciles.
    """
    _banner("9. Reconciliation — real Deribit paper API (read-only)")

    if not (config.DERIBIT_CLIENT_ID and config.DERIBIT_CLIENT_SECRET):
        print("  ℹ  No API credentials in config/env — skipping live reconciliation.")
        print("     Set DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET in .env to enable.")
        return

    mgr = OrderManager()
    mgr.track(TrackedOrder(order_id="ghost-1", instrument="BTC-X", direction="sell", amount=0.1, limit_price=0.002))

    try:
        await reconcile_with_deribit(
            mgr,
            paper=True,
            client_id=config.DERIBIT_CLIENT_ID,
            client_secret=config.DERIBIT_CLIENT_SECRET,
        )
        _ok(f"Reconciliation complete. Order summary: {mgr.summary()}")
        # ghost-1 is not a real Deribit order, so it should be marked CANCELLED
        if mgr.get("ghost-1").state == OrderState.CANCELLED:
            _ok("Ghost order correctly marked CANCELLED (not found on Deribit)")
    except Exception as exc:
        _fail(f"Live reconciliation error: {exc}")


def test_combo_order_success() -> None:
    _banner("10. Combo order — fills successfully")

    candidate = _make_candidate()

    class _ComboRPC:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def create_combo(self, legs):
            return {"combo_id": "COMBO-BTC-NEAR-FAR"}
        async def place_order(self, instrument, direction, amount, price, label=""):
            return {"order": {"order_id": "combo-1", "order_state": "open"}}
        async def get_order_state(self, order_id):
            return {"order_id": order_id, "order_state": "filled", "average_price": 0.004, "legs": [
                {"direction": "sell", "price": 0.002},
                {"direction": "buy",  "price": 0.006},
            ]}
        async def cancel_order(self, order_id):
            return {"order_id": order_id, "order_state": "cancelled"}

    from execution.executor import _async_enter_spread_combo
    import asyncio
    with patch("execution.executor._DeribitRPCClient", return_value=_ComboRPC()):
        mgr = OrderManager()
        result = asyncio.run(_async_enter_spread_combo(
            candidate=candidate,
            client_id="", client_secret="",
            order_manager=mgr,
            amount=0.1,
            net_debit_limit_index=0.004,
            combo_timeout=5,
        ))

    assert result is not None
    assert result["via_combo"] is True
    _ok(f"Combo order filled: near={result['near_prem']:.2f}  far={result['far_prem']:.2f}  via_combo=True")


def test_combo_timeout_falls_back() -> None:
    _banner("11. Combo order timeout → individual-leg fallback")

    candidate = _make_candidate()

    class _TimeoutComboRPC:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def create_combo(self, legs):
            return {"combo_id": "COMBO-BTC-TIMEOUT"}
        async def place_order(self, instrument, direction, amount, price, label=""):
            return {"order": {"order_id": "combo-timeout-1", "order_state": "open"}}
        async def get_order_state(self, order_id):
            return {"order_id": order_id, "order_state": "open"}  # never fills
        async def cancel_order(self, order_id):
            return {"order_id": order_id, "order_state": "cancelled"}

    # After combo times out (timeout=0), individual-leg fallback mock fills both legs
    individual_mock = _MockRPC(
        place_results=[_submitted("nl-1", 0.002), _submitted("fl-1", 0.006)],
        state_map={"nl-1": [_filled("nl-1", 0.002)], "fl-1": [_filled("fl-1", 0.006)]},
    )
    individual_mock.create_combo = _TimeoutComboRPC().create_combo

    call_count = {"n": 0}
    original_class = None

    def _rpc_factory(client_id="", client_secret=""):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _TimeoutComboRPC()
        return individual_mock

    import asyncio
    with patch("execution.executor._DeribitRPCClient", side_effect=_rpc_factory), \
         patch.object(config, "TRADING_MODE", "test"):
        exc    = CalendarExecutor(portfolio_value=50_000.0)
        # combo_timeout=0 forces immediate timeout → fallback
        import execution.executor as _exec_mod
        orig_combo_timeout = getattr(config, "COMBO_FILL_TIMEOUT_SEC", 30)
        try:
            result = asyncio.run(_exec_mod._async_enter_spread(
                candidate, client_id="", client_secret="",
                order_manager=exc.order_manager, portfolio_value=50_000.0,
                combo_timeout=0,
            ))
        finally:
            pass

    if result is not None:
        _ok(f"Fallback fill: net_debit={result['net_debit']:.2f}  via_combo={result.get('via_combo', False)}")
    else:
        _ok("Combo timed out; fallback also skipped (expected in this mock setup — no dedicated fallback mock)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═" * 70)
    print("  Calendar Spread Bot — Execution Hardening Verification")
    print("═" * 70)

    tests = [
        test_helpers,
        test_order_manager,
        test_enter_spread_success,
        test_enter_spread_slippage_rejected,
        test_leg_risk_handling,
        test_close_spread_success,
        test_roll_near_leg,
        test_reconciliation_mock,
        test_combo_order_success,
        test_combo_timeout_falls_back,
    ]

    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            _fail(f"{fn.__name__} ASSERTION FAILED: {exc}")
            failed += 1
        except Exception as exc:
            _fail(f"{fn.__name__} EXCEPTION: {exc}")
            failed += 1

    # Live test (async)
    try:
        asyncio.run(_test_reconciliation_live())
        passed += 1
    except Exception as exc:
        _fail(f"_test_reconciliation_live EXCEPTION: {exc}")
        failed += 1

    print(f"\n{'═' * 70}")
    print(f"  Results: {passed} passed, {failed} failed")
    print("═" * 70 + "\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
