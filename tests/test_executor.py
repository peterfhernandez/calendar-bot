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
import config
from execution.executor import (
    SlippageError,
    LegRiskError,
    OrderTimeoutError,
    CalendarExecutor,
    AmountBelowMinimumError,
    _check_slippage,
    _clamp_amount_to_step,
    _asset_from_instrument,
    _index_price,
    _usd_price,
    _async_enter_spread,
    _async_enter_spread_combo,
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

    async def place_order(self, instrument, direction, amount, price, label="", validate_amount=True):
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

    async def create_combo(self, legs: list) -> dict:
        raise RuntimeError("create_combo not supported in this mock (forces individual-leg fallback)")


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

    def test_asset_from_instrument(self):
        assert _asset_from_instrument("BTC-07JUN25-100000-C") == "BTC"
        assert _asset_from_instrument("ETH-07JUN25-3000-P") == "ETH"
        assert _asset_from_instrument("") == ""

    def test_clamp_amount_btc_valid(self):
        # 0.3 with min 0.1, step 0.1 → 0.3 unchanged
        assert _clamp_amount_to_step(0.3, 0.1, 0.1) == pytest.approx(0.3)

    def test_clamp_amount_eth_rounds_to_integer_step(self):
        # 9.5 with min 1, step 1 → floored to 9.0
        assert _clamp_amount_to_step(9.5, 1.0, 1.0) == pytest.approx(9.0)

    def test_clamp_amount_below_minimum_returns_none(self):
        # 0.1 with ETH min 1, step 1 → floors to 0 → below minimum → None
        assert _clamp_amount_to_step(0.1, 1.0, 1.0) is None

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

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread(
                    candidate, client_id="", client_secret="",
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

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread(
                    candidate, client_id="", client_secret="",
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

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            with pytest.raises(LegRiskError):
                self._run(
                    _async_enter_spread(
                        candidate, client_id="", client_secret="",
                        order_manager=mgr, portfolio_value=50_000.0,
                    )
                )

    def test_slippage_rejection_near_leg(self):
        """An *adverse* near-leg fill raises SlippageError (Phase 26b: directional).

        The near leg is a SELL at an intended $200. A fill *below* that ($100 =
        0.001 BTC) means we received materially less than intended — adverse — so
        SlippageError is raised.
        """
        candidate = _make_candidate()  # near_bid=200 USD (intended sell price)
        mock_client = self._make_mock_client(near_fill_price=0.001, far_fill_price=0.006)

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            with pytest.raises(SlippageError):
                # _async_enter_spread raises directly; CalendarExecutor catches it
                self._run(
                    _async_enter_spread(
                        candidate, client_id="", client_secret="",
                        order_manager=mgr, portfolio_value=50_000.0, slippage_pct=0.02,
                    )
                )

    def test_slippage_allows_near_leg_price_improvement(self):
        """Phase 26b: a *better* near-leg fill (sold for more) must NOT raise.

        The old symmetric abs() check rejected price improvement — the only
        deviation a limit order can actually produce.  A near SELL filling at $400
        (0.004 BTC) vs the intended $200 is favourable and must pass the near-leg
        slippage check (the trade may still fail later on the far leg mock; we only
        assert no SlippageError escapes for the near-leg improvement).
        """
        candidate = _make_candidate()  # near_bid=200 USD
        mock_client = self._make_mock_client(near_fill_price=0.004, far_fill_price=0.006)

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            # Should not raise SlippageError for the improved near-leg fill.
            self._run(
                _async_enter_spread(
                    candidate, client_id="", client_secret="",
                    order_manager=mgr, portfolio_value=50_000.0, slippage_pct=0.02,
                )
            )

    def test_paper_mode_returns_simulated_fill(self):
        """In paper mode, enter_spread returns a simulated fill without any API calls."""
        candidate = _make_candidate()
        with patch.object(config, "TRADING_MODE", "paper"):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread(
                    candidate, client_id="", client_secret="",
                    order_manager=mgr, portfolio_value=50_000.0,
                )
            )
        assert result is not None
        assert result["net_debit"] == pytest.approx(candidate.net_debit)
        assert result["near_order_id"] == "paper-near"


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
            result = self._run(
                _async_close_spread(
                    position, client_id="", client_secret="",
                    order_manager=mgr,
                )
            )

        assert result is not None
        # Phase 25c: returns (credit, near_close_usd, far_close_usd)
        credit, near_close_usd, far_close_usd = result
        assert isinstance(credit, float)
        assert credit == pytest.approx(far_close_usd - near_close_usd)

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
                    position, client_id="", client_secret="",
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
                    position, candidate, client_id="", client_secret="",
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
                    position, candidate, client_id="", client_secret="",
                    order_manager=mgr, order_timeout=0,
                )
            )

        assert ok is False


# ── Tests: CalendarExecutor (sync wrappers) ───────────────────────────────────

class TestCalendarExecutor:
    """Test the public sync interface of CalendarExecutor."""

    def _make_executor(self, **kwargs) -> CalendarExecutor:
        return CalendarExecutor(
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

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            exc = self._make_executor()
            result = exc.enter_spread(candidate)

        assert result is not None
        assert result["net_debit"] == pytest.approx(result["far_prem"] - result["near_prem"])

    def test_enter_spread_paper_mode_returns_simulated_fill(self):
        """In paper mode CalendarExecutor returns a simulated fill without network calls."""
        candidate = _make_candidate()
        with patch.object(config, "TRADING_MODE", "paper"):
            exc = self._make_executor()
            result = exc.enter_spread(candidate)
        assert result is not None
        assert result["near_order_id"] == "paper-near"

    def test_enter_spread_slippage_returns_none(self):
        # near_bid=$200 (intended). Fill at 0.004 BTC = $400 → 100% slippage → rejected.
        candidate = _make_candidate()
        mock_client = _MockRPCClient(
            place_order_results=[_submitted_order_result("n1", 0.004)],
            order_states={"n1": [_filled_order_state("n1", 0.004)]},
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
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


# ── Tests: combo orders ───────────────────────────────────────────────────────

class TestComboOrder:
    """Tests for _async_enter_spread_combo."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_combo_fills_successfully(self):
        """A combo order that fills immediately returns a fill dict with via_combo=True."""
        candidate = _make_candidate()
        combo_order_id = "combo-1"

        class ComboMockClient(_MockRPCClient):
            async def create_combo(self, legs):
                return {"combo_id": "COMBO-BTC-NEAR-FAR", "instrument_name": "COMBO-BTC-NEAR-FAR"}

            async def place_order(self, instrument, direction, amount, price, label="", validate_amount=True):
                self.placed_orders.append({"instrument": instrument, "direction": direction})
                return {"order": {"order_id": combo_order_id, "order_state": "open", "price": price}}

            async def get_order_state(self, order_id):
                return {"order_id": order_id, "order_state": "filled", "average_price": 0.004, "legs": [
                    {"direction": "sell", "price": 0.002},
                    {"direction": "buy",  "price": 0.006},
                ]}

        mock_client = ComboMockClient()
        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread_combo(
                    candidate=candidate,
                    client_id="", client_secret="",
                    order_manager=mgr,
                    amount=0.1,
                    net_debit_limit_index=0.004,
                    combo_timeout=5,
                )
            )

        assert result is not None
        assert result["via_combo"] is True

    def test_combo_timeout_returns_none(self):
        """A combo that never fills within the timeout returns None (triggers fallback)."""
        candidate = _make_candidate()

        class ComboMockClient(_MockRPCClient):
            async def create_combo(self, legs):
                return {"combo_id": "COMBO-BTC"}

            async def place_order(self, instrument, direction, amount, price, label="", validate_amount=True):
                return {"order": {"order_id": "combo-1", "order_state": "open"}}

            async def get_order_state(self, order_id):
                return {"order_id": order_id, "order_state": "open"}  # never fills

        mock_client = ComboMockClient()
        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread_combo(
                    candidate=candidate,
                    client_id="", client_secret="",
                    order_manager=mgr,
                    amount=0.1,
                    net_debit_limit_index=0.004,
                    combo_timeout=0,  # immediate timeout
                )
            )

        assert result is None

    def test_combo_unavailable_returns_none(self):
        """If create_combo raises, the combo path returns None (fallback to individual legs)."""
        candidate = _make_candidate()
        mock_client = _MockRPCClient()  # create_combo raises RuntimeError by default

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread_combo(
                    candidate=candidate,
                    client_id="", client_secret="",
                    order_manager=mgr,
                    amount=0.1,
                    net_debit_limit_index=0.004,
                    combo_timeout=5,
                )
            )

        assert result is None


class TestIndividualLegFallback:
    """Verify the fallback path is used when combo is unavailable."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_falls_back_to_individual_legs_when_combo_unavailable(self):
        """When create_combo raises, enter_spread falls back to individual-leg execution."""
        candidate = _make_candidate()
        # _MockRPCClient.create_combo raises by default → combo returns None → fallback
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("near-1", 0.002),
                _submitted_order_result("far-1",  0.006),
            ],
            order_states={
                "near-1": [_filled_order_state("near-1", 0.002)],
                "far-1":  [_filled_order_state("far-1",  0.006)],
            },
        )

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            result = self._run(
                _async_enter_spread(
                    candidate, client_id="", client_secret="",
                    order_manager=mgr, portfolio_value=50_000.0,
                )
            )

        assert result is not None
        assert result.get("via_combo") is False
        assert result["net_debit"] == pytest.approx(result["far_prem"] - result["near_prem"])


class TestFallbackCancelsNearOnFarFailure:
    """Verify the fallback cancels the near leg if the far leg fails."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_near_cancelled_when_far_leg_submit_fails(self):
        """After near fills in individual-leg fallback, far leg failure raises LegRiskError."""
        candidate = _make_candidate()

        async def bad_place_order(instrument, direction, amount, price, label=""):
            if direction == "sell":
                return _submitted_order_result("near-1")
            raise OSError("far leg connection reset")

        mock_client = _MockRPCClient(
            order_states={"near-1": [_filled_order_state("near-1", 0.002)]},
        )
        mock_client.place_order = bad_place_order  # type: ignore[method-assign]

        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            with pytest.raises(LegRiskError):
                self._run(
                    _async_enter_spread(
                        candidate, client_id="", client_secret="",
                        order_manager=mgr, portfolio_value=50_000.0,
                    )
                )


# ── Fee model integration ──────────────────────────────────────────────────────

class TestPaperFeeSimulation:
    """
    In paper mode the executor must return a 'fees_paid' field in the fill dict
    so that callers can track cumulative fee costs without network calls.
    """

    def test_paper_fill_includes_fees_paid(self):
        """Paper mode fill dict must contain fees_paid > 0 for a BTC candidate."""
        candidate = _make_candidate(spot=100_000.0)
        with patch.object(config, "TRADING_MODE", "paper"):
            exc = CalendarExecutor(client_id="", client_secret="",
                                   portfolio_value=50_000.0)
            result = exc.enter_spread(candidate)

        assert result is not None
        assert "fees_paid" in result, "Paper fill must include fees_paid"
        assert result["fees_paid"] > 0.0, "fees_paid must be positive for BTC at $100k"

    def test_paper_fees_match_fees_module(self):
        """fees_paid in the paper fill must equal entry_fees() from core/fees.py."""
        from core.fees import entry_fees
        candidate = _make_candidate(spot=100_000.0)

        with patch.object(config, "TRADING_MODE", "paper"):
            exc = CalendarExecutor(client_id="", client_secret="",
                                   portfolio_value=50_000.0)
            result = exc.enter_spread(candidate)

        assert result is not None
        expected_fees = entry_fees(
            candidate.asset, candidate.spot, result["qty"],
            near_price=candidate.near_bid, far_price=candidate.far_ask,
            via_combo=True,
        )
        assert result["fees_paid"] == pytest.approx(expected_fees, rel=1e-6)

    def test_paper_fees_sol_candidate(self):
        """fees_paid for a SOL candidate uses the taker fee rate."""
        from core.fees import entry_fees
        sol_candidate = _make_candidate(asset="SOL", spot=150.0, near_bid=5.0,
                                        near_ask=6.0, far_bid=10.0, far_ask=12.0)
        with patch.object(config, "TRADING_MODE", "paper"):
            exc = CalendarExecutor(client_id="", client_secret="",
                                   portfolio_value=50_000.0)
            result = exc.enter_spread(sol_candidate)

        assert result is not None
        expected = entry_fees("SOL", 150.0, result["qty"],
                              near_price=sol_candidate.near_bid,
                              far_price=sol_candidate.far_ask, via_combo=True)
        assert result["fees_paid"] == pytest.approx(expected, rel=1e-6)


class TestRunInsideEventLoop:
    """CalendarExecutor._run() must work when called from within a running event loop."""

    def test_run_from_running_loop(self):
        """_run() should not raise when called from an async context (simulates bot runtime)."""
        async def inner():
            async def dummy():
                return 42

            exc = CalendarExecutor(client_id="", client_secret="", portfolio_value=10_000.0)
            return exc._run(dummy())

        result = asyncio.run(inner())
        assert result == 42

    def test_enter_spread_paper_from_running_loop(self):
        """Paper-mode enter_spread() must succeed when called from within a running event loop."""
        async def inner():
            candidate = _make_candidate(spot=100_000.0)
            with patch.object(config, "TRADING_MODE", "paper"):
                exc = CalendarExecutor(client_id="", client_secret="", portfolio_value=50_000.0)
                return exc.enter_spread(candidate)

        result = asyncio.run(inner())
        assert result is not None
        assert "fees_paid" in result


# ── Phase 18: Tick-size rounding for far-leg close order rejection fix ───────

class TestTickSizeRounding:
    """Tests for Phase 18 Bug 1: tick-size aware price rounding to prevent "-32602 Invalid params" errors."""

    def test_tick_size_rounding_basic(self):
        """Basic tick-size rounding: price divided by tick_size and multiplied back."""
        from execution.executor import _round_to_tick

        # Standard 0.0001 tick — rounds to nearest tick
        assert _round_to_tick(1.23456, "BTC-3JAN26-60000-C", tick_size=0.0001) == pytest.approx(1.2346)
        assert _round_to_tick(1.23454, "BTC-3JAN26-60000-C", tick_size=0.0001) == pytest.approx(1.2345)

        # Coarser 0.0005 tick (e.g., higher-priced options)
        # 0.5554 / 0.0005 = 1110.8, rounds to 1111, 1111 * 0.0005 = 0.5555
        assert _round_to_tick(0.5550, "BTC-3JAN26-60000-C", tick_size=0.0005) == pytest.approx(0.5550)
        assert _round_to_tick(0.5554, "BTC-3JAN26-60000-C", tick_size=0.0005) == pytest.approx(0.5555)
        assert _round_to_tick(0.5560, "BTC-3JAN26-60000-C", tick_size=0.0005) == pytest.approx(0.5560)

    def test_tick_size_rounding_none_fallback(self):
        """When tick_size is None, default to 4 decimals."""
        from execution.executor import _round_to_tick

        result = _round_to_tick(1.23456, "BTC-UNKNOWN", tick_size=None)
        assert result == round(1.23456, 4)

    def test_tick_size_cache_populated(self):
        """Tick sizes are cached after fetching from Deribit."""
        from execution.executor import _TICK_SIZE_CACHE

        # Clear cache
        _TICK_SIZE_CACHE.clear()

        async def fetch_and_check():
            from execution.executor import _DeribitRPCClient

            client = _DeribitRPCClient("test_id", "test_secret")
            # Mock the get_instrument call.  public/get_instrument (singular)
            # returns the instrument object directly, not a {"instruments": [...]} list.
            with patch.object(client, "_rpc", new_callable=AsyncMock) as mock_rpc:
                mock_rpc.return_value = {
                    "instrument_name": "BTC-3JAN26-60000-C", "tick_size": 0.0001
                }
                instr = await client.get_instrument("BTC-3JAN26-60000-C")
                # Verify the singular endpoint was requested with the instrument name.
                assert mock_rpc.call_args.args[0] == "public/get_instrument"
                assert mock_rpc.call_args.args[1] == {"instrument_name": "BTC-3JAN26-60000-C"}
                assert instr["tick_size"] == 0.0001

        asyncio.run(fetch_and_check())

    def test_place_order_rounds_to_tick(self):
        """place_order() automatically rounds prices to valid ticks."""
        async def test_rounding():
            from execution.executor import _DeribitRPCClient, _TICK_SIZE_CACHE

            _TICK_SIZE_CACHE["BTC-3JAN26-60000-C"] = 0.0001

            client = _DeribitRPCClient("test_id", "test_secret")
            with patch.object(client, "_rpc", new_callable=AsyncMock) as mock_rpc:
                with patch.object(client, "_ws", MagicMock()):
                    with patch.object(client, "_authenticate", new_callable=AsyncMock):
                        # Simulate order with a price that needs rounding
                        await client.place_order(
                            "BTC-3JAN26-60000-C", "buy", 1.0, 1.23456789
                        )
                        # Verify _rpc was called with rounded price
                        call_args = mock_rpc.call_args
                        assert call_args is not None

        asyncio.run(test_rounding())


# ── Phase 22d: close/roll price derivation + tick_size_steps rounding ─────────

class TestClosePriceDerivation:
    """Close/roll prices must come from live best bid/ask (crossed with a buffer),
    not a synthetic mid × 1.02 / mid × 0.98 that lands off-tick."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_close_prices_derived_from_bid_ask(self):
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
            self._run(
                _async_close_spread(
                    position, client_id="", client_secret="",
                    order_manager=OrderManager(),
                )
            )

        buf = config.CLOSE_PRICE_CROSS_BUFFER_PCT
        near_order = next(o for o in mock_client.placed_orders if o["instrument"].endswith("JUN25-100000-C"))
        far_order  = next(o for o in mock_client.placed_orders if o["instrument"].endswith("JUL25-100000-C"))
        # Buy back near: lift the ask (0.0016) + buffer
        assert near_order["direction"] == "buy"
        assert near_order["price"] == pytest.approx(0.0016 * (1 + buf))
        # Sell far: hit the bid (0.0054) − buffer — NOT far_mid * 0.98
        assert far_order["direction"] == "sell"
        assert far_order["price"] == pytest.approx(0.0054 * (1 - buf))

    def test_roll_close_price_derived_from_ask(self):
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
            ok = self._run(
                _async_roll_near_leg(
                    position, candidate, client_id="", client_secret="",
                    order_manager=OrderManager(),
                )
            )

        assert ok is True
        buf = config.CLOSE_PRICE_CROSS_BUFFER_PCT
        close_order = mock_client.placed_orders[0]
        assert close_order["direction"] == "buy"
        assert close_order["price"] == pytest.approx(0.0016 * (1 + buf))


class TestTickSizeSteps:
    """Rounding must honour Deribit's per-price-band tick_size_steps and stay on-grid."""

    def test_effective_tick_size_uses_band(self):
        from execution.executor import _effective_tick_size
        steps = [{"above_price": 0.1, "tick_size": 0.0005}]
        # Below the band threshold — base tick applies
        assert _effective_tick_size(0.05, 0.0001, steps) == 0.0001
        # At/above the band threshold — the coarser tick applies
        assert _effective_tick_size(0.20, 0.0001, steps) == 0.0005

    def test_round_to_tick_with_steps_stays_on_grid(self):
        from execution.executor import _round_to_tick
        steps = [{"above_price": 0.1, "tick_size": 0.0005}]
        # Price 0.12345 is above 0.1 → 0.0005 tick.  0.12345/0.0005 = 246.9 → 247 → 0.1235
        result = _round_to_tick(0.12345, "BTC-X-60000-C", tick_size=0.0001, tick_size_steps=steps)
        assert result == pytest.approx(0.1235)
        # Must be an exact multiple of the effective tick (on-grid, no float drift)
        assert abs(round(result / 0.0005) * 0.0005 - result) < 1e-9

    def test_round_to_tick_no_float_drift(self):
        from execution.executor import _round_to_tick
        # A value known to produce float drift with naive division rounding.
        result = _round_to_tick(0.0003, "BTC-X-60000-C", tick_size=0.0001)
        assert result == pytest.approx(0.0003)
        # Exactly on-grid.
        assert (result / 0.0001) == pytest.approx(round(result / 0.0001))


class TestTickFetchFailureLoud:
    """A tick-size fetch failure is logged (not swallowed) and falls back safely."""

    def test_fetch_tick_info_failure_returns_none_and_warns(self, caplog):
        import logging
        from execution.executor import _DeribitRPCClient

        async def run():
            client = _DeribitRPCClient("id", "secret")
            with patch.object(client, "_rpc", new_callable=AsyncMock) as mock_rpc:
                mock_rpc.side_effect = RuntimeError("boom")
                with caplog.at_level(logging.WARNING, logger="execution.executor"):
                    tick, steps = await client._fetch_tick_info("BTC-3JAN26-60000-C")
            return tick, steps

        tick, steps = asyncio.run(run())
        assert tick is None and steps is None
        assert any("BTC-3JAN26-60000-C" in r.message for r in caplog.records)

    def test_fetch_tick_info_parses_singular_endpoint_object(self):
        """
        Regression: public/get_instrument (singular) returns the instrument
        object directly.  _fetch_tick_info must parse that shape and populate
        the tick caches — not expect a {"instruments": [...]} list (which was
        the wrong plural-endpoint shape that silently broke tick-size lookup).
        """
        from execution.executor import (
            _DeribitRPCClient, _TICK_SIZE_CACHE, _TICK_STEPS_CACHE,
        )

        _TICK_SIZE_CACHE.pop("BTC-3JAN26-60000-C", None)
        _TICK_STEPS_CACHE.pop("BTC-3JAN26-60000-C", None)

        async def run():
            client = _DeribitRPCClient("id", "secret")
            with patch.object(client, "_rpc", new_callable=AsyncMock) as mock_rpc:
                mock_rpc.return_value = {
                    "instrument_name": "BTC-3JAN26-60000-C",
                    "tick_size": 0.0005,
                    "tick_size_steps": [{"above_price": 0.1, "tick_size": 0.001}],
                }
                tick, steps = await client._fetch_tick_info("BTC-3JAN26-60000-C")
                assert mock_rpc.call_args.args[0] == "public/get_instrument"
                return tick, steps

        tick, steps = asyncio.run(run())
        assert tick == 0.0005
        assert steps == [{"above_price": 0.1, "tick_size": 0.001}]
        assert _TICK_SIZE_CACHE["BTC-3JAN26-60000-C"] == 0.0005


# ── Phase 18: Stuck-position retry loop fix ──────────────────────────────────

class TestStuckPositionRetryFix:
    """Tests for Phase 18 Bug 2: stuck positions excluded from re-evaluation to prevent infinite retry loops."""

    def test_get_open_trades_excludes_stuck(self, tmp_path):
        """get_open_trades() must exclude positions with close_status='close_stuck'."""
        from db.state import (
            init_db, create_calendar_trade, get_open_trades, mark_position_close_stuck
        )
        from datetime import date

        db_path = tmp_path / "test_stuck.db"
        init_db(db_path)

        # Create two trades: one normal, one stuck
        trade_normal = create_calendar_trade(
            asset="BTC", date_open=date.today(), option_type="Call",
            strike=60000.0, expiry_near="3JAN26", expiry_far="24JAN26",
            near_days=3, far_days=24,
            near_instrument="BTC-3JAN26-60000-C", far_instrument="BTC-24JAN26-60000-C",
            qty=1.0, spot_open=100_000.0, near_prem=0.02, far_prem=0.07, net_debit=0.05,
            db_path=db_path,
        )

        trade_stuck = create_calendar_trade(
            asset="BTC", date_open=date.today(), option_type="Call",
            strike=55000.0, expiry_near="3JAN26", expiry_far="24JAN26",
            near_days=3, far_days=24,
            near_instrument="BTC-3JAN26-55000-C", far_instrument="BTC-24JAN26-55000-C",
            qty=1.0, spot_open=100_000.0, near_prem=0.025, far_prem=0.085, net_debit=0.06,
            db_path=db_path,
        )

        # Mark the second trade as stuck
        mark_position_close_stuck(
            trade_id=trade_stuck.id,
            error_reason="Test stuck position",
            db_path=db_path,
        )

        # Verify get_open_trades() excludes the stuck position
        open_trades = get_open_trades(db_path)
        assert len(open_trades) == 1
        assert open_trades[0].id == trade_normal.id


# ── Phase 18: Force-close PnL estimation fix ─────────────────────────────────

class TestForceClosePnLFix:
    """Tests for Phase 18 Bug 3: force-closed positions record real P&L, not 0.0."""

    def test_close_position_executor_failure(self):
        """_close_position() handles executor failure gracefully."""
        from strategy.decision import DecisionEngine
        from unittest.mock import patch

        # Create a mock executor that returns None (failure)
        mock_executor = MagicMock()
        mock_executor.close_spread.return_value = None

        # Create a mock notifier
        mock_notifier = MagicMock()

        engine = DecisionEngine(
            cache=MagicMock(),
            portfolio_value=10_000.0,
            executor=mock_executor,
            notifier=mock_notifier,
        )

        # Create a position with last_spread_value set
        pos = {
            "trade_id": 1,
            "asset": "BTC",
            "strike": 60000.0,
            "net_debit": 0.05,
            "qty": 1.0,
            "open_fees": 0.001,
            "roll_pnl": 0.0,
            "last_spread_value": 0.04,  # Last known spread value
            "near_prem": 0.02,
            "far_prem": 0.07,
        }

        # Mock mark_position_close_stuck to avoid DB operations in this unit test
        with patch("strategy.decision.mark_position_close_stuck") as mock_stuck:
            # Close with executor failure but last_spread_value available
            result = engine._close_position(pos, spot=100_000.0, reason="Test close")

            # Phase 19: a single executor failure returns FAILED so the caller's
            # retry counter increments; the position is NOT marked stuck here
            # (only the retry-cap branches in _monitor_position do that), and no
            # fake PnL=0.0 row is ever written.
            assert "FAILED" in result
            mock_stuck.assert_not_called()


# ── Tests: amount validation (Phase 25a/25b) ──────────────────────────────────

class TestAmountValidation:
    """Per-instrument order-amount clamping and the AMOUNT GATE abort."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _client_with_meta(self, meta: dict | Exception):
        from execution.executor import _DeribitRPCClient, _AMOUNT_INFO_CACHE
        _AMOUNT_INFO_CACHE.clear()
        client = _DeribitRPCClient()

        async def fake_rpc(method, params):
            if method == "public/get_instrument":
                if isinstance(meta, Exception):
                    raise meta
                return meta
            # order placement echo
            return {"order": {"order_id": "x-1", "order_state": "open"}}

        client._rpc = fake_rpc  # type: ignore[assignment]
        return client

    def test_clamp_amount_uses_live_min(self):
        client = self._client_with_meta({"min_trade_amount": 1.0, "contract_size": 1.0})
        out = self._run(client.clamp_amount("ETH-07JUN25-3000-C", 9.5))
        assert out == pytest.approx(9.0)  # ETH integer step

    def test_clamp_amount_below_min_returns_none(self):
        client = self._client_with_meta({"min_trade_amount": 1.0, "contract_size": 1.0})
        out = self._run(client.clamp_amount("ETH-07JUN25-3000-C", 0.1))
        assert out is None  # floors to 0 → below ETH minimum

    def test_clamp_amount_static_fallback_on_fetch_failure(self):
        # Fetch raises → fall back to config.DEFAULT_MIN_TRADE_AMOUNTS["ETH"] = (1, 1)
        client = self._client_with_meta(RuntimeError("network"))
        out = self._run(client.clamp_amount("ETH-07JUN25-3000-C", 5.7))
        assert out == pytest.approx(5.0)

    def test_place_order_raises_amount_gate_below_min(self):
        client = self._client_with_meta({"min_trade_amount": 1.0, "contract_size": 1.0})
        with pytest.raises(AmountBelowMinimumError):
            self._run(client.place_order("ETH-07JUN25-3000-C", "buy", 0.1, 0.01))

    def test_place_order_skips_validation_for_combo(self):
        client = self._client_with_meta({"min_trade_amount": 1.0, "contract_size": 1.0})
        # validate_amount=False must not raise even though 0.1 < ETH min
        result = self._run(
            client.place_order("COMBO-X", "buy", 0.1, 0.01, validate_amount=False)
        )
        assert result["order"]["order_id"] == "x-1"


# ── Tests: close_spread stashes fill prices (Phase 25c) ───────────────────────

class TestCloseFillTracking:
    def test_close_spread_stores_last_close_fills(self):
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
            ex = CalendarExecutor(client_id="", client_secret="")
            credit = ex.close_spread(position)
        assert credit is not None
        assert ex.last_close_fills is not None
        assert "near_close_usd" in ex.last_close_fills
        assert "far_close_usd" in ex.last_close_fills

    def test_close_spread_clears_fills_on_failure(self):
        position = _make_position()
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
            ex = CalendarExecutor(client_id="", client_secret="", order_timeout=0)
            credit = ex.close_spread(position)
        assert credit is None
        assert ex.last_close_fills is None


# ── Tests: Phase 26 — partial-fill flatten, directional slippage, unwind ──────

class TestPartialFillFlatten:
    """Phase 26a: a timeout-cancel that left a partial fill flattens it."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_flatten_reverses_partial_sell(self):
        from execution.executor import _cancel_and_flatten
        client = AsyncMock()
        client.cancel_order.return_value = {"order_id": "n1", "filled_amount": 3.0}
        client.place_order.return_value = {"order": {"order_id": "flat-1"}}
        mgr = OrderManager()
        mgr.track(TrackedOrder(
            order_id="n1", instrument="BTC-25JUL26-1750-P",
            direction="sell", amount=7.0, limit_price=0.002,
        ))
        filled = self._run(_cancel_and_flatten(
            client, mgr, "n1", "BTC-25JUL26-1750-P",
            orig_direction="sell", reverse_price=0.003, asset="BTC",
        ))
        assert filled == 3.0
        client.place_order.assert_awaited_once()
        args, _ = client.place_order.call_args
        assert args[1] == "buy"    # a partial SELL is flattened by buying it back
        assert args[2] == 3.0      # exactly the filled amount
        tracked = mgr.get("n1")
        assert tracked.state == OrderState.CANCELLED_PARTIAL
        assert tracked.filled_amount == 3.0

    def test_no_partial_marks_cancelled(self):
        from execution.executor import _cancel_and_flatten
        client = AsyncMock()
        client.cancel_order.return_value = {"order_id": "n1", "filled_amount": 0.0}
        client.get_order_state.return_value = {"filled_amount": 0.0}
        mgr = OrderManager()
        mgr.track(TrackedOrder(
            order_id="n1", instrument="X", direction="sell", amount=7.0, limit_price=0.002,
        ))
        filled = self._run(_cancel_and_flatten(
            client, mgr, "n1", "X", orig_direction="sell", reverse_price=0.003, asset="BTC",
        ))
        assert filled == 0.0
        client.place_order.assert_not_called()
        assert mgr.get("n1").state == OrderState.CANCELLED

    def test_flatten_failure_raises_operator_alert(self):
        from execution.executor import _cancel_and_flatten
        client = AsyncMock()
        client.cancel_order.return_value = {"filled_amount": 5.0}
        client.place_order.side_effect = RuntimeError("exchange down")
        notifier = MagicMock()
        mgr = OrderManager()
        mgr.track(TrackedOrder(
            order_id="f1", instrument="ETH-24JUL26-1750-P",
            direction="buy", amount=5.0, limit_price=0.006,
        ))
        filled = self._run(_cancel_and_flatten(
            client, mgr, "f1", "ETH-24JUL26-1750-P",
            orig_direction="buy", reverse_price=0.005, asset="ETH", notifier=notifier,
        ))
        assert filled == 5.0
        notifier.notify_warning.assert_called_once()

    def test_filled_amount_from_order_state_fallback(self):
        """When the cancel response omits filled_amount, get_order_state supplies it."""
        from execution.executor import _cancel_and_flatten
        client = AsyncMock()
        client.cancel_order.return_value = {"order_id": "n1"}  # no filled_amount
        client.get_order_state.return_value = {"filled_amount": 2.0}
        client.place_order.return_value = {"order": {"order_id": "flat-1"}}
        mgr = OrderManager()
        mgr.track(TrackedOrder(
            order_id="n1", instrument="X", direction="sell", amount=7.0, limit_price=0.002,
        ))
        filled = self._run(_cancel_and_flatten(
            client, mgr, "n1", "X", orig_direction="sell", reverse_price=0.003, asset="BTC",
        ))
        assert filled == 2.0
        client.place_order.assert_awaited_once()


class TestDirectionalSlippage:
    """Phase 26b: _check_slippage rejects only adverse fills, not improvement."""

    def test_sell_below_intended_is_adverse(self):
        with pytest.raises(SlippageError):
            _check_slippage(90.0, 100.0, 0.02, side="sell")  # received less

    def test_sell_above_intended_passes(self):
        _check_slippage(110.0, 100.0, 0.02, side="sell")  # sold for more — favourable

    def test_buy_above_intended_is_adverse(self):
        with pytest.raises(SlippageError):
            _check_slippage(110.0, 100.0, 0.02, side="buy")  # paid more

    def test_buy_below_intended_passes(self):
        _check_slippage(90.0, 100.0, 0.02, side="buy")  # bought cheaper — favourable

    def test_legacy_symmetric_still_rejects_both(self):
        with pytest.raises(SlippageError):
            _check_slippage(110.0, 100.0, 0.02)  # no side → symmetric
        with pytest.raises(SlippageError):
            _check_slippage(90.0, 100.0, 0.02)


class TestBothLegsFilledSlippageUnwind:
    """Phase 26b: an adverse far-leg fill after both legs filled unwinds both."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_far_slippage_after_fill_unwinds_both_legs(self):
        candidate = _make_candidate()  # far_ask=600 USD intended (0.006)
        # near fills at intended 0.002; far fills adverse at 0.010 = $1000 (>2%).
        mock_client = _MockRPCClient(
            place_order_results=[
                _submitted_order_result("near-1", 0.002),
                _submitted_order_result("far-1",  0.010),
                _submitted_order_result("unwind-near", 0.002),
                _submitted_order_result("unwind-far",  0.006),
            ],
            order_states={
                "near-1": [_filled_order_state("near-1", 0.002)],
                "far-1":  [_filled_order_state("far-1",  0.010)],
            },
        )
        with patch("execution.executor._DeribitRPCClient", return_value=mock_client), \
             patch.object(config, "TRADING_MODE", "test"):
            mgr = OrderManager()
            with pytest.raises(SlippageError):
                self._run(
                    _async_enter_spread(
                        candidate, client_id="", client_secret="",
                        order_manager=mgr, portfolio_value=50_000.0, slippage_pct=0.02,
                    )
                )
        # near(sell) + far(buy) + unwind-near(buy) + unwind-far(sell)
        assert len(mock_client.placed_orders) == 4
        assert mock_client.placed_orders[2]["direction"] == "buy"   # unwind near
        assert mock_client.placed_orders[3]["direction"] == "sell"  # unwind far


class TestCancelledPartialState:
    """Phase 26a: order_manager exposes the CANCELLED_PARTIAL terminal state."""

    def test_cancelled_partial_is_terminal_and_records_amount(self):
        mgr = OrderManager()
        mgr.track(TrackedOrder(
            order_id="p1", instrument="X", direction="sell", amount=7.0, limit_price=0.002,
        ))
        mgr.update("p1", OrderState.CANCELLED_PARTIAL, filled_amount=3.0)
        o = mgr.get("p1")
        assert o.state == OrderState.CANCELLED_PARTIAL
        assert o.is_terminal
        assert o.filled_amount == 3.0
        assert o not in mgr.open_orders()
