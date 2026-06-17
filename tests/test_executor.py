"""
tests/test_executor.py
======================
Unit tests for execution/executor.py and execution/order_manager.py.

All Deribit network calls are mocked via unittest.mock — no live connection
is required.  Tests cover:

- Successful enter/close/roll round-trips
- Slippage rejection
- Near-leg-filled / far-leg-failed leg risk handling
- Order timeout handling
- OrderManager lifecycle tracking
- Reconciliation logic
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.order_manager import (
    OrderManager,
    OrderState,
    TrackedOrder,
    reconcile_with_deribit,
)
from execution.executor import (
    SlippageError,
    LegRiskError,
    OrderTimeoutError,
    CalendarExecutor,
    _check_slippage,
    _contract_amount,
    _index_price,
    _usd_price,
    _async_enter_spread,
    _async_close_spread,
    _async_roll_near_leg,
)
from strategy.scanner import CalendarCandidate


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_candidate(
    asset="BTC",
    strike=100_000.0,
    spot=100_000.0,
    near_bid=200.0,   # USD — 0.002 BTC @ $100k spot
    near_ask=300.0,   # USD — 0.003 BTC @ $100k spot
    far_bid=500.0,    # USD — 0.005 BTC @ $100k spot
    far_ask=600.0,    # USD — 0.006 BTC @ $100k spot
    near_iv=0.80,
    far_iv=0.70,
    near_days=7,
    far_days=30,
) -> CalendarCandidate:
    return CalendarCandidate(
        asset=asset,
        strike=strike,
        option_type="Call",
        near_instrument=f"{asset}-07JUN25-{int(strike)}-C",
        far_instrument=f"{asset}-07JUL25-{int(strike)}-C",
        near_days=near_days,
        far_days=far_days,
        spot=spot,
        near_iv=near_iv,
        far_iv=far_iv,
        iv_contango=near_iv - far_iv,
        near_ask=near_ask,
        near_bid=near_bid,
        far_ask=far_ask,
        far_bid=far_bid,
        net_debit=far_ask - near_bid,
        near_oi=500.0,
        far_oi=300.0,
        pop=0.52,
        be_lo=95_000.0,
        be_hi=105_000.0,
        ev_score=0.08,
        qty=0.1,
    )


def _make_position(
    asset="BTC",
    strike=100_000.0,
    spot=100_000.0,
    near_instrument="BTC-07JUN25-100000-C",
    far_instrument="BTC-07JUL25-100000-C",
    qty=0.1,
    net_debit=400.0,
) -> dict:
    return {
        "asset":           asset,
        "strike":          strike,
        "option_type":     "Call",
        "near_instrument": near_instrument,
        "far_instrument":  far_instrument,
        "qty":             qty,
        "spot_open":       spot,
        "net_debit":       net_debit,
        "near_prem":       200.0,
        "far_prem":        600.0,
        "trade_id":        42,
    }


def _filled_order_state(order_id: str, avg_price: float = 0.003) -> dict:
    return {
        "order_id":     order_id,
        "order_state":  "filled",
        "average_price": avg_price,
        "amount":       0.1,
    }


def _submitted_order_result(order_id: str, price: float = 0.003) -> dict:
    return {"order": {"order_id": order_id, "order_state": "open", "price": price}}


# ── Helper: build a mock _DeribitRPCClient async context manager ──────────────

class _MockRPCClient:
    """
    Async context manager mock for _DeribitRPCClient.

    Provides place_order, get_order_state, cancel_order, and get_ticker stubs.
    """

    def __init__(
        self,
        place_order_results: list[dict] | None = None,
        order_states:        dict[str, list[dict]] | None = None,
        ticker_results:      dict[str, dict] | None = None,
    ):
        self._place_results  = place_order_results or []
        self._place_idx      = 0
        self._order_states   = order_states or {}
        self._state_calls:   dict[str, int] = {}
        self._ticker_results = ticker_results or {}
        self.cancelled_orders: list[str] = []
        self.placed_orders:    list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def place_order(self, instrument, direction, amount, price, label=""):
        self.placed_orders.append(
            {"instrument": instrument, "direction": direction, "amount": amount, "price": price}
        )
        if self._place_idx < len(self._place_results):
            result = self._place_results[self._place_idx]
            self._place_idx += 1
            return result
        raise RuntimeError("No more place_order results configured")

    async def get_order_state(self, order_id: str) -> dict:
        states = self._order_states.get(order_id, [])
        idx    = self._state_calls.get(order_id, 0)
        self._state_calls[order_id] = idx + 1
        if idx < len(states):
            return states[idx]
        # Default: filled
        return {"order_id": order_id, "order_state": "filled", "average_price": 0.003}

    async def cancel_order(self, order_id: str) -> dict:
        self.cancelled_orders.append(order_id)
        return {"order_id": order_id, "order_state": "cancelled"}

    async def get_ticker(self, instrument: str) -> dict:
        if instrument in self._ticker_results:
            return self._ticker_results[instrument]
        return {"best_bid_price": 0.002, "best_ask_price": 0.004, "index_price": 100_000.0}


# ── Tests: utility functions ──────────────────────────────────────────────────

class TestHelpers:

    def test_index_price_btc(self):
        # BTC is inverse: USD price / spot
        assert _index_price(200.0, 100_000.0, "BTC") == pytest.approx(0.002, rel=1e-6)

    def test_index_price_linear(self):
        # SOL is linear: price stays in USD
        assert _index_price(5.0, 150.0, "SOL") == pytest.approx(5.0, rel=1e-6)

    def test_usd_price_btc(self):
        assert _usd_price(0.002, 100_000.0, "BTC") == pytest.approx(200.0, rel=1e-6)

    def test_usd_price_linear(self):
        assert _usd_price(5.0, 150.0, "SOL") == pytest.approx(5.0, rel=1e-6)

    def test_contract_amount_btc(self):
        # portfolio=$10k, max_loss_pct=2%, net_debit=$400 → $200 budget
        # $200 / ($400 * (0.002 * 100000/100000)) ... actually let's just check >=0.1
        amt = _contract_amount(100_000.0, "BTC", 10_000.0, 0.02, 0.004 * 100_000.0)
        assert amt >= 0.1  # Deribit minimum

    def test_check_slippage_within_bounds(self):
        _check_slippage(102.0, 100.0, 0.05)  # 2% deviation, 5% limit — should not raise

    def test_check_slippage_exceeds_bounds(self):
        with pytest.raises(SlippageError):
            _check_slippage(106.0, 100.0, 0.05)  # 6% deviation, 5% limit

    def test_check_slippage_zero_mid(self):
        _check_slippage(100.0, 0.0, 0.02)  # no mid price — should not raise


# ── Tests: OrderManager ───────────────────────────────────────────────────────

class TestOrderManager:

    def test_track_and_get(self):
        mgr = OrderManager()
        order = TrackedOrder(
            order_id="ord-1", instrument="BTC-07JUN25-100000-C",
            direction="sell", amount=0.1, limit_price=0.002,
        )
        mgr.track(order)
        assert mgr.get("ord-1") is order

    def test_update_state(self):
        mgr = OrderManager()
        order = TrackedOrder(
            order_id="ord-2", instrument="BTC-07JUN25-100000-C",
            direction="buy", amount=0.1, limit_price=0.003,
        )
        mgr.track(order)
        mgr.update("ord-2", OrderState.FILLED, fill_price=0.0029)
        assert mgr.get("ord-2").state       == OrderState.FILLED
        assert mgr.get("ord-2").fill_price  == pytest.approx(0.0029)

    def test_update_unknown_order(self):
        mgr = OrderManager()
        mgr.update("nonexistent", OrderState.CANCELLED)  # should not raise

    def test_open_orders(self):
        mgr = OrderManager()
        for i, state in enumerate([OrderState.SUBMITTED, OrderState.FILLED, OrderState.CANCELLED]):
            o = TrackedOrder(
                order_id=f"ord-{i}", instrument="X", direction="buy", amount=0.1, limit_price=0.001,
            )
            mgr.track(o)
            if state != OrderState.SUBMITTED:
                mgr.update(f"ord-{i}", state)
        assert len(mgr.open_orders()) == 1

    def test_find_stuck(self):
        mgr = OrderManager()
        order = TrackedOrder(
            order_id="old-ord", instrument="BTC-X", direction="sell", amount=0.1, limit_price=0.002,
        )
        # Backdate submitted_at
        order.submitted_at = time.monotonic() - 999
        mgr.track(order)
        stuck = mgr.find_stuck()
        assert any(o.order_id == "old-ord" for o in stuck)

    def test_summary(self):
        mgr = OrderManager()
        for i in range(3):
            o = TrackedOrder(
                order_id=f"s-{i}", instrument="X", direction="buy", amount=0.1, limit_price=0.001,
            )
            mgr.track(o)
        mgr.update("s-0", OrderState.FILLED)
        summary = mgr.summary()
        assert summary["submitted"] == 2
        assert summary["filled"]    == 1

    def test_all_orders(self):
        mgr = OrderManager()
        for i in range(5):
            mgr.track(TrackedOrder(
                order_id=f"a-{i}", instrument="X", direction="buy", amount=0.1, limit_price=0.001,
            ))
        assert len(mgr.all_orders()) == 5


# ── Tests: async_enter_spread ─────────────────────────────────────────────────

class TestAsyncEnterSpread:

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_mock_client(self, near_fill_price=0.002, far_fill_price=0.006):
        """
        near_fill_price / far_fill_price are index fractions (BTC units).
        Default 0.002 BTC = $200 (matches near_bid=200 USD) and 0.006 BTC = $600 (far_ask=600 USD).
        """
        return _MockRPCClient(
            place_order_results=[
                _submitted_order_result("near-1", near_fill_price),
                _submitted_order_result("far-1",  far_fill_price),
            ],
            order_states={
                "near-1": [_filled_order_state("near-1", near_fill_price)],
                "far-1":  [_filled_order_state("far-1",  far_fill_price)],
            },
        )

    def test_successful_entry(self):
        candidate = _make_candidate()
        mock_client = self._make_mock_client()

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread(
                    candidate, paper=True, client_id="", client_secret="",
                    order_manager=mgr, portfolio_value=50_000.0,
                )
            )

        assert result is not None
        assert "near_prem" in result
        assert "far_prem"  in result
        assert "net_debit" in result
        assert result["net_debit"] == pytest.approx(result["far_prem"] - result["near_prem"])
        # Both legs should be tracked as FILLED
        assert mgr.get("near-1").state == OrderState.FILLED
        assert mgr.get("far-1").state  == OrderState.FILLED

    def test_near_leg_timeout_returns_none(self):
        candidate = _make_candidate()
        mock_client = _MockRPCClient(
            place_order_results=[_submitted_order_result("near-1")],
            order_states={"near-1": [{"order_id": "near-1", "order_state": "open"}]},  # never fills
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread(
                    candidate, paper=True, client_id="", client_secret="",
                    order_manager=mgr, portfolio_value=50_000.0, order_timeout=0,
                )
            )

        assert result is None

    def test_far_leg_failure_raises_leg_risk_error(self):
        """Near fills, far leg submit raises, must raise LegRiskError."""
        candidate = _make_candidate()

        async def bad_place_order(instrument, direction, amount, price, label=""):
            if direction == "sell" and "far" not in label.lower():
                # Near leg (sell) succeeds
                return _submitted_order_result("near-1")
            # Far leg fails every time
            raise OSError("connection reset")

        mock_client = _MockRPCClient(
            order_states={"near-1": [_filled_order_state("near-1", 0.002)]},  # 0.002 BTC = $200 = near_bid
        )
        mock_client.place_order = bad_place_order  # type: ignore[method-assign]

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            with pytest.raises(LegRiskError):
                self._run(
                    _async_enter_spread(
                        candidate, paper=True, client_id="", client_secret="",
                        order_manager=mgr, portfolio_value=50_000.0,
                    )
                )

    def test_slippage_rejection_near_leg(self):
        """Fill price deviating too far from intended raises SlippageError."""
        # near_bid=$200 (intended sell price). Fill at 0.004 BTC = $400 → 100% deviation → SlippageError.
        candidate = _make_candidate()  # near_bid=200 USD
        # Fill in index fraction: 0.004 BTC = $400, intended was $200 → 100% deviation
        mock_client = self._make_mock_client(near_fill_price=0.004, far_fill_price=0.006)

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            with pytest.raises(SlippageError):
                # _async_enter_spread raises directly; CalendarExecutor catches it
                self._run(
                    _async_enter_spread(
                        candidate, paper=True, client_id="", client_secret="",
                        order_manager=mgr, portfolio_value=50_000.0, slippage_pct=0.02,
                    )
                )


# ── Tests: async_close_spread ─────────────────────────────────────────────────

class TestAsyncCloseSpread:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_successful_close(self):
        position = _make_position()
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("close-near-1", 0.0015),
                _submitted_order_result("close-far-1",  0.0055),
            ],
            order_states={
                "close-near-1": [_filled_order_state("close-near-1", 0.0015)],
                "close-far-1":  [_filled_order_state("close-far-1",  0.0055)],
            },
            ticker_results={
                "BTC-07JUN25-100000-C": {"best_bid_price": 0.0014, "best_ask_price": 0.0016},
                "BTC-07JUL25-100000-C": {"best_bid_price": 0.0054, "best_ask_price": 0.0056},
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            credit = self._run(
                _async_close_spread(
                    position, paper=True, client_id="", client_secret="",
                    order_manager=mgr,
                )
            )

        assert credit is not None
        # far_close - near_close > 0 means we collected more than we paid
        assert isinstance(credit, float)

    def test_close_failure_returns_none(self):
        position = _make_position()
        # Provide place results for both legs but configure them to never fill (timeout=0)
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("close-near-1", 0.0015),
                _submitted_order_result("close-far-1",  0.0055),
            ],
            order_states={
                "close-near-1": [{"order_id": "close-near-1", "order_state": "open"}],
                "close-far-1":  [{"order_id": "close-far-1",  "order_state": "open"}],
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            result = self._run(
                _async_close_spread(
                    position, paper=True, client_id="", client_secret="",
                    order_manager=mgr, order_timeout=0,
                )
            )

        assert result is None


# ── Tests: async_roll_near_leg ────────────────────────────────────────────────

class TestAsyncRollNearLeg:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_successful_roll(self):
        position  = _make_position()
        candidate = _make_candidate()
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("roll-close-1", 0.0015),
                _submitted_order_result("roll-open-1",  0.0020),
            ],
            order_states={
                "roll-close-1": [_filled_order_state("roll-close-1", 0.0015)],
                "roll-open-1":  [_filled_order_state("roll-open-1",  0.0020)],
            },
            ticker_results={
                "BTC-07JUN25-100000-C": {"best_bid_price": 0.0014, "best_ask_price": 0.0016},
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            ok = self._run(
                _async_roll_near_leg(
                    position, candidate, paper=True, client_id="", client_secret="",
                    order_manager=mgr,
                )
            )

        assert ok is True

    def test_roll_fails_if_open_fails(self):
        position  = _make_position()
        candidate = _make_candidate()
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("roll-close-1", 0.0015),
                _submitted_order_result("roll-open-1",  0.0020),
            ],
            order_states={
                "roll-close-1": [_filled_order_state("roll-close-1", 0.0015)],
                "roll-open-1":  [{"order_id": "roll-open-1", "order_state": "open"}],  # never fills
            },
            ticker_results={
                "BTC-07JUN25-100000-C": {"best_bid_price": 0.0014, "best_ask_price": 0.0016},
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            ok = self._run(
                _async_roll_near_leg(
                    position, candidate, paper=True, client_id="", client_secret="",
                    order_manager=mgr, order_timeout=0,
                )
            )

        assert ok is False


# ── Tests: CalendarExecutor (sync wrappers) ───────────────────────────────────

class TestCalendarExecutor:
    """Test the public sync interface of CalendarExecutor."""

    def _make_executor(self, **kwargs) -> CalendarExecutor:
        return CalendarExecutor(
            paper=True,
            client_id="",
            client_secret="",
            portfolio_value=50_000.0,
            **kwargs,
        )

    def test_enter_spread_returns_fill(self):
        # near fill: 0.002 BTC = $200 = near_bid; far fill: 0.006 BTC = $600 = far_ask
        candidate = _make_candidate()
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("n1", 0.002),
                _submitted_order_result("f1", 0.006),
            ],
            order_states={
                "n1": [_filled_order_state("n1", 0.002)],
                "f1": [_filled_order_state("f1", 0.006)],
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            exc = self._make_executor()
            result = exc.enter_spread(candidate)

        assert result is not None
        assert result["net_debit"] == pytest.approx(result["far_prem"] - result["near_prem"])

    def test_enter_spread_slippage_returns_none(self):
        # near_bid=$200 (intended). Fill at 0.004 BTC = $400 → 100% slippage → rejected.
        candidate = _make_candidate()
        mock_client = _MockRPCClient(
            place_order_results=[_submitted_order_result("n1", 0.004)],
            order_states={"n1": [_filled_order_state("n1", 0.004)]},
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            exc = self._make_executor(slippage_pct=0.02)
            result = exc.enter_spread(candidate)

        assert result is None

    def test_close_spread_returns_float(self):
        position = _make_position()
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("c1", 0.0015),
                _submitted_order_result("c2", 0.0055),
            ],
            order_states={
                "c1": [_filled_order_state("c1", 0.0015)],
                "c2": [_filled_order_state("c2", 0.0055)],
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            exc = self._make_executor()
            result = exc.close_spread(position)

        assert isinstance(result, float)

    def test_roll_near_leg_returns_true(self):
        position  = _make_position()
        candidate = _make_candidate()
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("r1", 0.0015),
                _submitted_order_result("r2", 0.0020),
            ],
            order_states={
                "r1": [_filled_order_state("r1", 0.0015)],
                "r2": [_filled_order_state("r2", 0.0020)],
            },
            ticker_results={
                "BTC-07JUN25-100000-C": {"best_bid_price": 0.001, "best_ask_price": 0.002},
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            exc = self._make_executor()
            result = exc.roll_near_leg(position, candidate)

        assert result is True


# ── Tests: reconcile_with_deribit ─────────────────────────────────────────────

class TestReconciliation:

    def test_missing_order_marked_cancelled(self):
        mgr = OrderManager()
        mgr.track(TrackedOrder(
            order_id="stale-1", instrument="BTC-X", direction="sell", amount=0.1, limit_price=0.002,
        ))

        async def mock_fetch(*args, **kwargs):
            return []  # Deribit reports no open orders

        with patch("execution.order_manager._fetch_deribit_open_orders", side_effect=mock_fetch):
            asyncio.run(reconcile_with_deribit(mgr, paper=True))

        assert mgr.get("stale-1").state == OrderState.CANCELLED

    def test_present_order_left_open(self):
        mgr = OrderManager()
        mgr.track(TrackedOrder(
            order_id="live-1", instrument="BTC-X", direction="buy", amount=0.1, limit_price=0.003,
        ))

        async def mock_fetch(*args, **kwargs):
            return [{"order_id": "live-1"}]  # Deribit still has it open

        with patch("execution.order_manager._fetch_deribit_open_orders", side_effect=mock_fetch):
            asyncio.run(reconcile_with_deribit(mgr, paper=True))

        assert mgr.get("live-1").state == OrderState.SUBMITTED  # unchanged

    def test_reconcile_handles_fetch_error(self):
        """reconcile_with_deribit should not raise if Deribit is unreachable."""
        mgr = OrderManager()

        async def mock_fetch(*args, **kwargs):
            raise ConnectionError("timeout")

        with patch("execution.order_manager._fetch_deribit_open_orders", side_effect=mock_fetch):
            asyncio.run(reconcile_with_deribit(mgr, paper=True))  # must not raise
