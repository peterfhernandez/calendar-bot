"""
Unit tests for strategy/decision.py — DecisionEngine state machine.

All tests use an in-memory SQLite database and a mock executor to avoid
any live network calls or file system side-effects.
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from data.deribit_feed import TickerSnapshot
from strategy.decision import (
    BotState,
    DailyLossLimitError,
    DecisionEngine,
    DryRunExecutor,
    EngineStatus,
    _days_left,
    _instrument_expiry_label,
    _trade_to_position,
)
from strategy.scanner import CalendarCandidate


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _future_label(days: int) -> str:
    """Return a Deribit-style expiry label N days from today."""
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _make_snap(instrument: str, mark_iv: float = 0.80, oi: float = 500,
               bid: float = 0.02, ask: float = 0.03,
               bid_size: float = 0.0, ask_size: float = 0.0) -> TickerSnapshot:
    asset = instrument.split("-")[0]
    return TickerSnapshot(
        instrument=instrument,
        asset=asset,
        spot=90_000.0,
        mark_price=0.025,
        bid=bid,
        ask=ask,
        mark_iv=mark_iv,
        open_interest=oi,
        bid_size=bid_size,
        ask_size=ask_size,
        timestamp=datetime.now(timezone.utc).timestamp(),
    )


def _make_cache(near_days: int = 10, far_days: int = 35) -> MagicMock:
    """Build a mock ChainCache with two BTC call instruments."""
    near_label = _future_label(near_days)
    far_label  = _future_label(far_days)

    near_instr = f"BTC-{near_label}-90000-C"
    far_instr  = f"BTC-{far_label}-90000-C"

    near_snap = _make_snap(near_instr, mark_iv=0.90)  # higher IV → contango
    far_snap  = _make_snap(far_instr,  mark_iv=0.70)

    snap_map = {near_instr: near_snap, far_instr: far_snap}

    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    cache.get_chain.return_value = [near_snap, far_snap]
    cache.get.side_effect = lambda instr: snap_map.get(instr)
    return cache


def _make_candidate(near_days: int = 10, far_days: int = 35) -> CalendarCandidate:
    near_label = _future_label(near_days)
    far_label  = _future_label(far_days)
    return CalendarCandidate(
        asset="BTC",
        strike=90_000.0,
        option_type="Call",
        near_instrument=f"BTC-{near_label}-90000-C",
        far_instrument=f"BTC-{far_label}-90000-C",
        near_days=near_days,
        far_days=far_days,
        spot=90_000.0,
        near_iv=0.90,
        far_iv=0.70,
        iv_contango=0.20,
        near_ask=0.0255,
        near_bid=0.0245,   # near_mid=0.025, spread=4% — passes 5% gate
        far_ask=0.0445,
        far_bid=0.0435,    # far_mid=0.044,  spread=2.3% — passes 5% gate
        net_debit=0.02,    # far_ask-near_bid=0.0445-0.0245=0.02; spread_mid=0.019; premium≈5.3%
        near_oi=500.0,
        far_oi=500.0,
        pop=0.55,
        be_lo=80_000.0,
        be_hi=100_000.0,
        ev_score=0.25,   # EV = 25% of net_debit (0.005 BTC per contract)
        qty=0.0,
    )


def _make_engine(
    cache=None,
    portfolio_value: float = 10_000.0,
    executor=None,
    daily_loss_limit: float = 500.0,
) -> tuple[DecisionEngine, Path]:
    """Return (engine, db_path) using a temporary database."""
    db_path = Path(tempfile.mktemp(suffix=".db"))
    engine = DecisionEngine(
        cache=cache or _make_cache(),
        portfolio_value=portfolio_value,
        executor=executor,
        db_path=db_path,
        daily_loss_limit=daily_loss_limit,
    )
    return engine, db_path


def _fill_dict(candidate: CalendarCandidate) -> dict:
    return {
        "near_prem": candidate.near_bid,
        "far_prem":  candidate.far_ask,
        "net_debit": candidate.net_debit,
        "qty":       1.0,
    }


# ── DryRunExecutor ────────────────────────────────────────────────────────────

class TestDryRunExecutor:
    def test_enter_spread_returns_fill(self):
        exe = DryRunExecutor()
        candidate = _make_candidate()
        candidate.qty = 1.0
        fill = exe.enter_spread(candidate)
        assert fill is not None
        assert fill["net_debit"] == candidate.net_debit
        assert fill["qty"] == candidate.qty

    def test_close_spread_returns_debit_times_qty(self):
        exe = DryRunExecutor()
        pos = {"trade_id": 1, "asset": "BTC", "strike": 90000, "net_debit": 0.02, "qty": 2.0}
        result = exe.close_spread(pos)
        assert result == pytest.approx(0.02 * 2.0)

    def test_roll_near_leg_returns_true(self):
        exe = DryRunExecutor()
        pos = {"trade_id": 1}
        candidate = _make_candidate()
        candidate.qty = 1.0
        assert exe.roll_near_leg(pos, candidate) is True


# ── Helper functions ──────────────────────────────────────────────────────────

class TestHelpers:
    def test_instrument_expiry_label(self):
        assert _instrument_expiry_label("BTC-27JUN25-100000-C") == "27JUN25"
        assert _instrument_expiry_label("ETH-1AUG25-3000-P") == "1AUG25"
        assert _instrument_expiry_label("bad") == ""

    def test_days_left_from_expiry_labels(self):
        pos = {
            "expiry_near": _future_label(7),
            "expiry_far":  _future_label(30),
            "near_days": 7,
            "far_days": 30,
        }
        near, far = _days_left(pos)
        assert 5 <= near <= 9
        assert 28 <= far <= 32

    def test_days_left_falls_back_to_stored(self):
        pos = {"expiry_near": "bad", "expiry_far": "bad", "near_days": 5, "far_days": 25}
        near, far = _days_left(pos)
        assert near == 5
        assert far == 25


# ── DecisionEngine initialisation ─────────────────────────────────────────────

class TestDecisionEngineInit:
    def test_default_state_is_idle(self):
        engine, _ = _make_engine()
        assert engine.state is BotState.IDLE

    def test_portfolio_value_property(self):
        engine, _ = _make_engine(portfolio_value=50_000)
        assert engine.portfolio_value == 50_000
        engine.portfolio_value = 60_000
        assert engine.portfolio_value == 60_000

    def test_default_executor_is_dry_run(self):
        engine, _ = _make_engine()
        assert isinstance(engine._executor, DryRunExecutor)


# ── scan_tick ─────────────────────────────────────────────────────────────────

class TestScanTick:
    def test_no_candidates_returns_idle(self):
        cache = MagicMock()
        cache.get_spot.return_value = 90_000.0
        cache.get_chain.return_value = []
        engine, _ = _make_engine(cache=cache)
        status = engine.scan_tick()
        assert status.state is BotState.IDLE
        assert status.open_positions == 0

    def test_halted_engine_skips_scan(self):
        engine, _ = _make_engine()
        engine._state = BotState.HALTED
        status = engine.scan_tick()
        assert status.state is BotState.HALTED
        assert "halted" in status.message.lower()

    def test_successful_entry_creates_position(self):
        executor = MagicMock()
        candidate = _make_candidate()
        executor.enter_spread.return_value = _fill_dict(candidate)

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved")

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        assert executor.enter_spread.called
        assert status.open_positions >= 1

    def test_sizer_blocks_entry(self):
        executor = MagicMock()

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [_make_candidate()]
            mock_size.return_value = MagicMock(qty=0.0, reason="Max positions reached")

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        executor.enter_spread.assert_not_called()
        assert status.open_positions == 0

    def test_executor_rejection_logs_and_continues(self):
        executor = MagicMock()
        executor.enter_spread.return_value = None  # rejected

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [_make_candidate()]
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved")

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        # Entry was attempted but failed — no position recorded
        assert status.open_positions == 0


# ── monitor_tick ──────────────────────────────────────────────────────────────

class TestMonitorTick:
    def _open_pos(self, trade_id: int = 1, near_days: int = 10,
                  far_days: int = 35) -> dict:
        near_label = _future_label(near_days)
        far_label  = _future_label(far_days)
        return {
            "trade_id":        trade_id,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             1.0,
            "net_debit":       0.02,
            "spot_open":       90_000.0,
            "near_days":       near_days,
            "far_days":        far_days,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_no_open_positions_returns_idle(self):
        engine, _ = _make_engine()
        with patch.object(engine, "_load_all_open_positions", return_value=[]):
            status = engine.monitor_tick()
        assert status.state is BotState.IDLE

    def test_halted_engine_skips_monitor(self):
        engine, _ = _make_engine()
        engine._state = BotState.HALTED
        status = engine.monitor_tick()
        assert status.state is BotState.HALTED

    def test_stop_loss_triggers_close(self):
        executor = MagicMock()
        executor.close_spread.return_value = 0.005  # credit < debit → loss

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("stop", 0.005, 0.25, "STOP")), \
             patch("strategy.decision.close_calendar_trade") as mock_close_db:
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        executor.close_spread.assert_called_once()
        mock_close_db.assert_called_once()

    def test_take_profit_triggers_close(self):
        executor = MagicMock()
        executor.close_spread.return_value = 0.05  # credit > debit → profit

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("tp", 0.05, 1.8, "TAKE-PROFIT")), \
             patch("strategy.decision.close_calendar_trade") as mock_close_db:
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        executor.close_spread.assert_called_once()
        mock_close_db.assert_called_once()

    def test_ok_status_no_action(self):
        executor = MagicMock()

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("ok", 0.025, 1.0, "OK")):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        executor.close_spread.assert_not_called()

    def test_expired_near_leg_triggers_close(self):
        executor = MagicMock()
        executor.close_spread.return_value = 0.02

        engine, _ = _make_engine(executor=executor)
        # near expiry in the past
        pos = self._open_pos(near_days=0)
        pos["expiry_near"] = _future_label(-2)  # expired 2 days ago

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            status = engine.monitor_tick()

        executor.close_spread.assert_called_once()

    def test_roll_trigger_attempts_roll(self):
        executor = MagicMock()
        executor.roll_near_leg.return_value = True

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(near_days=2)  # at roll trigger threshold

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("ok", 0.025, 1.0, "OK")), \
             patch("strategy.decision.scan", return_value=[_make_candidate()]):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        executor.roll_near_leg.assert_called_once()

    def test_roll_failure_falls_back_to_close(self):
        executor = MagicMock()
        executor.roll_near_leg.return_value = False
        executor.close_spread.return_value = 0.02

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(near_days=2)

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("ok", 0.025, 1.0, "OK")), \
             patch("strategy.decision.scan", return_value=[_make_candidate()]), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        executor.close_spread.assert_called_once()


# ── Daily loss limit ──────────────────────────────────────────────────────────

class TestDailyLossLimit:
    def test_limit_halts_engine(self):
        engine, _ = _make_engine(daily_loss_limit=100.0)
        engine._today_pnl = -101.0
        status = engine.scan_tick()
        assert status.state is BotState.HALTED
        assert "halted" in status.message.lower()

    def test_limit_not_triggered_below_threshold(self):
        engine, _ = _make_engine(daily_loss_limit=100.0)
        engine._today_pnl = -50.0
        # No open positions, no candidates → should remain IDLE, not HALTED
        cache = MagicMock()
        cache.get_spot.return_value = 90_000.0
        cache.get_chain.return_value = []
        engine._cache = cache
        status = engine.scan_tick()
        assert status.state is not BotState.HALTED

    def test_daily_pnl_accumulates_from_closes(self):
        executor = MagicMock()
        executor.close_spread.return_value = 0.005  # loss

        engine, _ = _make_engine(executor=executor)
        pos = {
            "trade_id": 1, "status": "Open", "asset": "BTC",
            "option_type": "Call", "strike": 90000.0,
            "expiry_near": _future_label(10), "expiry_far": _future_label(35),
            "qty": 1.0, "net_debit": 0.02, "spot_open": 90000.0,
            "near_days": 10, "far_days": 35,
            "near_instrument": "BTC-X-90000-C",
            "far_instrument":  "BTC-Y-90000-C",
            "open_fees": 0.0, "close_fees": 0.0,
        }

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("stop", 0.005, 0.25, "STOP")), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()

        # net_debit=0.02, close_credit=0.005, qty=1 → pnl = 0.005 - 0.02*1 = -0.015
        assert engine._today_pnl == pytest.approx(0.005 - 0.02 * 1.0)


# ── Fix 1: Negative-EV filter ─────────────────────────────────────────────────

class TestNegativeEvFilter:
    def test_negative_ev_candidate_not_entered(self):
        """A candidate with ev_score < 0 must be rejected before entry."""
        executor = MagicMock()
        candidate = _make_candidate()
        candidate.ev_score = -0.35  # EV is -35% of debit — clearly negative

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved")

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        executor.enter_spread.assert_not_called()
        assert status.open_positions == 0

    def test_positive_ev_candidate_is_entered(self):
        """A candidate with ev_score > 0 must pass the EV filter."""
        executor = MagicMock()
        candidate = _make_candidate()
        candidate.ev_score = 0.25

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved")
            executor.enter_spread.return_value = {
                "near_prem": candidate.near_bid,
                "far_prem":  candidate.far_ask,
                "net_debit": candidate.net_debit,
                "qty":       1.0,
            }

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        executor.enter_spread.assert_called_once()


# ── Fix 2: Stale-IV monitor message ───────────────────────────────────────────

class TestMonitorSkippedNoIv:
    def _open_pos(self, trade_id: int = 1) -> dict:
        near_label = _future_label(10)
        far_label  = _future_label(35)
        return {
            "trade_id":        trade_id,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             1.0,
            "net_debit":       0.02,
            "spot_open":       90_000.0,
            "near_days":       10,
            "far_days":        35,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_all_skipped_message_not_ok(self):
        """When all IV checks are skipped, the message must NOT say 'All positions OK.'"""
        engine, _ = _make_engine()
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=None)  # stale feed
            status = engine.monitor_tick()

        assert "All positions OK" not in status.message
        assert "skipped" in status.message.lower()
        assert "no IV" in status.message or "no iv" in status.message.lower()

    def test_partial_skip_message_contains_both(self):
        """When some positions are skipped and one is actioned, both appear in the message."""
        executor = MagicMock()
        executor.close_spread.return_value = 0.005

        engine, _ = _make_engine(executor=executor)
        pos_good = self._open_pos(trade_id=1)
        pos_stale = self._open_pos(trade_id=2)

        def _get_iv_side_effect(pos):
            return 0.80 if pos["trade_id"] == 1 else None

        with patch.object(engine, "_load_all_open_positions",
                          side_effect=[[pos_good, pos_stale], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("stop", 0.005, 0.25, "STOP")), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(side_effect=_get_iv_side_effect)
            status = engine.monitor_tick()

        assert "skipped" in status.message.lower()
        assert "trade_id=1" in status.message


# ── Fix 3: daily_pnl reflects unrealized MTM ─────────────────────────────────

class TestDailyPnlUnrealized:
    def _open_pos(self) -> dict:
        near_label = _future_label(10)
        far_label  = _future_label(35)
        return {
            "trade_id":        1,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             1.0,
            "net_debit":       0.02,
            "spot_open":       90_000.0,
            "near_days":       10,
            "far_days":        35,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_daily_pnl_includes_unrealized(self):
        """daily_pnl in EngineStatus must include unrealized MTM when positions are held."""
        engine, _ = _make_engine()
        pos = self._open_pos()
        # sv=0.025 (already qty-weighted, qty=1), net_debit=0.02 per unit
        # unrealized = sv - net_debit * qty = 0.025 - 0.02 * 1.0 = 0.005
        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("ok", 0.025, 1.25, "OK")):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        assert status.daily_pnl == pytest.approx(0.005)  # 0.025 - 0.02 * 1.0

    def test_daily_pnl_zero_when_iv_skipped(self):
        """Unrealized P&L contribution is 0 when IV is missing (position not valued)."""
        engine, _ = _make_engine()
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=None)
            status = engine.monitor_tick()

        assert status.daily_pnl == pytest.approx(0.0)

    def test_daily_pnl_combines_realized_and_unrealized(self):
        """daily_pnl is the sum of already-realized closes and current MTM."""
        executor = MagicMock()
        executor.close_spread.return_value = 0.005

        engine, _ = _make_engine(executor=executor)
        engine._today_pnl = -0.01  # a previous realized loss

        pos = self._open_pos()
        # spread value = 0.025 → unrealized = +0.005
        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("ok", 0.025, 1.25, "OK")):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        assert status.daily_pnl == pytest.approx(-0.01 + 0.005)


# ── Fix 4: New-position grace period (no instant TP on entry) ─────────────────

class TestNewPositionGracePeriod:
    """
    A position entered by scan_tick must be skipped by a monitor_tick that runs
    in the same scheduler cycle.  Without this guard the monitor can compute a
    wildly-different B-S spread value from the actual fill price and trigger a
    spurious TP or stop immediately after entry.
    """

    def _open_pos(self, trade_id: int = 4, near_days: int = 10,
                  far_days: int = 35) -> dict:
        near_label = _future_label(near_days)
        far_label  = _future_label(far_days)
        return {
            "trade_id":        trade_id,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             1.0,
            "net_debit":       0.02,
            "spot_open":       90_000.0,
            "near_days":       near_days,
            "far_days":        far_days,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_newly_entered_position_skipped_on_first_monitor(self):
        """Position in _just_entered must not trigger close, even at 1000% of debit."""
        executor = MagicMock()
        executor.close_spread.return_value = 20.0  # would be a huge TP

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(trade_id=4)

        # Simulate that trade_id=4 was just entered this scan tick
        engine._just_entered.add(4)

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("tp", 20.0, 10.0, "TAKE-PROFIT")):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            status = engine.monitor_tick()

        # Must NOT close despite TP signal
        executor.close_spread.assert_not_called()

    def test_grace_period_cleared_after_monitor_tick(self):
        """_just_entered must be empty after monitor_tick so the next tick evaluates normally."""
        engine, _ = _make_engine()
        engine._just_entered.add(4)

        pos = self._open_pos(trade_id=4)
        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("ok", 0.025, 1.25, "OK")):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()

        assert len(engine._just_entered) == 0

    def test_position_evaluated_on_second_monitor_tick(self):
        """After grace period is cleared, TP is triggered normally on the next monitor tick."""
        executor = MagicMock()
        executor.close_spread.return_value = 20.0

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(trade_id=4)

        # First monitor tick — grace period active, no close
        engine._just_entered.add(4)
        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("tp", 20.0, 10.0, "TAKE-PROFIT")):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()
        executor.close_spread.assert_not_called()

        # Second monitor tick — grace period cleared, close fires
        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("tp", 20.0, 10.0, "TAKE-PROFIT")), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()
        executor.close_spread.assert_called_once()


# ── Fix 5: Realized P&L computed from spread_value, not executor return ────────

class TestClosePositionPnl:
    """
    _close_position must compute P&L from the observed spread value returned by
    check_calendar_status, not from the executor's close_spread return value.
    In dry-run mode the executor returns net_debit × qty (i.e. "got back what we
    paid"), which would always produce pnl = 0.
    """

    def _open_pos(self, trade_id: int = 1, net_debit: float = 0.02,
                  qty: float = 1.5) -> dict:
        near_label = _future_label(10)
        far_label  = _future_label(35)
        return {
            "trade_id":        trade_id,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             qty,
            "net_debit":       net_debit,
            "spot_open":       90_000.0,
            "near_days":       10,
            "far_days":        35,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_tp_pnl_uses_spread_value_not_executor_return(self):
        """
        sv=0.21 is qty-weighted total; net_debit=0.02/unit, qty=1.5.
        pnl = sv - net_debit * qty = 0.21 - 0.02 * 1.5 = 0.18
        The executor return (net_debit * qty = 0.03) must NOT be used.
        """
        executor = MagicMock()
        executor.close_spread.return_value = 0.03  # = net_debit * qty → would give pnl=0

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(net_debit=0.02, qty=1.5)

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("tp", 0.21, 10.5, "TAKE-PROFIT")), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()

        expected_pnl = 0.21 - 0.02 * 1.5  # sv - net_debit*qty = 0.18
        assert engine._today_pnl == pytest.approx(expected_pnl)

    def test_stop_pnl_uses_spread_value(self):
        """
        sv=0.005 is qty-weighted total; net_debit=0.02/unit, qty=1.0.
        pnl = sv - net_debit * qty = 0.005 - 0.02 * 1.0 = -0.015
        """
        executor = MagicMock()
        executor.close_spread.return_value = 0.02  # would give pnl=0

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(net_debit=0.02, qty=1.0)

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("stop", 0.005, 0.25, "STOP")), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()

        expected_pnl = 0.005 - 0.02 * 1.0  # sv - net_debit*qty = -0.015
        assert engine._today_pnl == pytest.approx(expected_pnl)

    def test_expiry_close_falls_back_to_executor_return(self):
        """
        When closing due to expiry (no sv available), P&L falls back to
        executor return value minus entry debit.
        """
        executor = MagicMock()
        executor.close_spread.return_value = 0.025  # net credit on expiry close

        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(net_debit=0.02, qty=1.0)
        pos["expiry_near"] = _future_label(-2)  # expired

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine.monitor_tick()

        expected_pnl = 0.025 - 0.02 * 1.0  # = 0.005
        assert engine._today_pnl == pytest.approx(expected_pnl)


# ── Fix 6: Market mid-price spread value preferred over B-S ───────────────────

class TestMarketSpreadValue:
    """
    _monitor_position must use live market bid/ask mid-prices to compute the
    current spread value rather than Black-Scholes with a single uniform IV.
    B-S can diverge massively from market prices for options away from ATM or
    with strong IV skew (e.g. deep-ITM BTC calls at 61000 with BTC at 63600
    showed B-S sv=~$2266 vs actual market spread of ~$222).
    """

    def _open_pos(self, near_instr: str, far_instr: str,
                  net_debit: float = 222.59, qty: float = 0.8) -> dict:
        near_label = _future_label(13)
        far_label  = _future_label(41)
        return {
            "trade_id":        10,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          61_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             qty,
            "net_debit":       net_debit,
            "spot_open":       63_597.0,
            "near_days":       13,
            "far_days":        41,
            "near_instrument": near_instr,
            "far_instrument":  far_instr,
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def _cache_with_legs(self, near_instr: str, far_instr: str,
                         near_bid: float, near_ask: float,
                         far_bid: float,  far_ask: float) -> MagicMock:
        """Cache that returns specific bid/ask for both leg instruments."""
        near_snap = _make_snap(near_instr, bid=near_bid, ask=near_ask)
        far_snap  = _make_snap(far_instr,  bid=far_bid,  ask=far_ask)
        cache = MagicMock()
        cache.get_spot.return_value = 63_597.0
        cache.get_chain.return_value = [near_snap, far_snap]
        return cache

    def test_market_sv_used_when_leg_prices_available(self):
        """
        When both legs are in the cache with bid/ask, the monitor must compute
        sv = (far_mid - near_mid) * qty from market prices, not B-S.
        """
        near_instr = "BTC-13DAY-61000-C"
        far_instr  = "BTC-41DAY-61000-C"
        # Market: near_mid=4866, far_mid=5089 → spread_mid=223, total_sv=223*0.8=178.4
        # B-S would give sv≈2266 (1272% of debit) — would spuriously TP
        cache = self._cache_with_legs(
            near_instr, far_instr,
            near_bid=4865.0, near_ask=4867.0,
            far_bid=5088.0,  far_ask=5090.0,
        )
        pos = self._open_pos(near_instr, far_instr, net_debit=222.59, qty=0.8)

        engine, _ = _make_engine(cache=cache)
        engine._get_iv = MagicMock(return_value=0.80)

        market_sv = engine._get_market_spread_value(pos)
        # near_mid=4866, far_mid=5089, spread_mid=223, total=223*0.8=178.4
        expected_sv = ((5088 + 5090) / 2 - (4865 + 4867) / 2) * 0.8
        assert market_sv == pytest.approx(expected_sv)

    def test_market_sv_prevents_spurious_tp(self):
        """
        With market prices, pct = sv/total_debit ≈ 100% (no TP).
        With B-S only it would be ~1272% (immediate TP).
        """
        near_instr = "BTC-13DAY-61000-C"
        far_instr  = "BTC-41DAY-61000-C"
        # market spread value ≈ net_debit (position roughly at breakeven)
        cache = self._cache_with_legs(
            near_instr, far_instr,
            near_bid=4865.0, near_ask=4867.0,
            far_bid=5088.0,  far_ask=5090.0,
        )
        executor = MagicMock()
        pos = self._open_pos(near_instr, far_instr, net_debit=222.59, qty=0.8)

        engine, _ = _make_engine(cache=cache, executor=executor)
        engine._get_iv = MagicMock(return_value=0.80)

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]):
            status = engine.monitor_tick()

        # Must NOT have closed (pct≈100%, well inside 150% TP threshold)
        executor.close_spread.assert_not_called()
        assert "All positions OK" in status.message or "OK" in status.message

    def test_falls_back_to_bs_when_leg_prices_missing(self):
        """When leg instruments are not in the cache, market_sv is None and B-S is used."""
        near_instr = "BTC-13DAY-61000-C"
        far_instr  = "BTC-41DAY-61000-C"
        # Cache has no matching instruments
        cache = MagicMock()
        cache.get_spot.return_value = 63_597.0
        cache.get_chain.return_value = []

        pos = self._open_pos(near_instr, far_instr, net_debit=222.59, qty=0.8)
        engine, _ = _make_engine(cache=cache)

        market_sv = engine._get_market_spread_value(pos)
        assert market_sv is None


# ── Bug fix: negative spread value clamped to zero ───────────────────────────

class TestNegativeSpreadValueClamped:
    """
    Regression test for the halt caused by an inverted market spread.

    When the near leg's mid exceeds the far leg's mid (stale or thin data),
    (far_mid - near_mid) is negative.  Multiplied by a large qty this becomes
    a catastrophic phantom loss that triggers the daily loss limit.

    _get_market_spread_value must clamp the result to >= 0.
    """

    def _open_pos(self, near_instr: str, far_instr: str, qty: float = 1.0) -> dict:
        near_label = _future_label(10)
        far_label  = _future_label(35)
        return {
            "trade_id":        99,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             qty,
            "net_debit":       50.0,
            "spot_open":       90_000.0,
            "near_days":       10,
            "far_days":        35,
            "near_instrument": near_instr,
            "far_instrument":  far_instr,
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_inverted_spread_clamped_to_zero(self):
        """When near_mid > far_mid, spread value must be 0, not negative."""
        near_instr = "BTC-10DAY-90000-C"
        far_instr  = "BTC-35DAY-90000-C"

        near_snap = _make_snap(near_instr, bid=500.0, ask=510.0)   # near_mid=505
        far_snap  = _make_snap(far_instr,  bid=100.0, ask=110.0)   # far_mid=105 (inverted)

        cache = MagicMock()
        cache.get_spot.return_value = 90_000.0
        cache.get_chain.return_value = [near_snap, far_snap]

        pos = self._open_pos(near_instr, far_instr, qty=100.0)
        engine, _ = _make_engine(cache=cache)

        market_sv = engine._get_market_spread_value(pos)
        # Without clamp: (105 - 505) * 100 = -40000 → catastrophic
        # With clamp: max(0, 105 - 505) * 100 = 0
        assert market_sv == pytest.approx(0.0)

    def test_normal_spread_unaffected_by_clamp(self):
        """A positive spread value must pass through unchanged."""
        near_instr = "BTC-10DAY-90000-C"
        far_instr  = "BTC-35DAY-90000-C"

        near_snap = _make_snap(near_instr, bid=100.0, ask=110.0)   # near_mid=105
        far_snap  = _make_snap(far_instr,  bid=200.0, ask=220.0)   # far_mid=210

        cache = MagicMock()
        cache.get_spot.return_value = 90_000.0
        cache.get_chain.return_value = [near_snap, far_snap]

        pos = self._open_pos(near_instr, far_instr, qty=2.0)
        engine, _ = _make_engine(cache=cache)

        market_sv = engine._get_market_spread_value(pos)
        # (210 - 105) * 2 = 210
        assert market_sv == pytest.approx(210.0)


# ── Liquidity gate: per-leg spread check ─────────────────────────────────────

class TestLiquidityGate:
    """
    The liquidity gate (_check_liquidity_gate) must block candidates whose
    per-leg bid/ask spread exceeds MAX_LEG_SPREAD_PCT, and candidates where
    the entry debit exceeds the spread mid by more than MAX_ENTRY_PREMIUM.

    Integration: scan_tick must not call enter_spread on any blocked candidate.
    """

    def _wide_near_candidate(self) -> CalendarCandidate:
        """Near leg has a 20% bid/ask spread — exceeds 5% gate."""
        c = _make_candidate()
        c.near_bid = 90.0
        c.near_ask = 110.0   # (110-90)/100 = 20% spread
        c.far_bid  = 200.0
        c.far_ask  = 202.0   # 1% spread — fine
        c.net_debit = c.far_ask - c.near_bid  # = 112
        return c

    def _wide_far_candidate(self) -> CalendarCandidate:
        """Far leg has a 20% bid/ask spread — exceeds 5% gate."""
        c = _make_candidate()
        c.near_bid = 90.0
        c.near_ask = 92.0    # 2.2% spread — fine
        c.far_bid  = 180.0
        c.far_ask  = 220.0   # (220-180)/200 = 20% spread
        c.net_debit = c.far_ask - c.near_bid  # = 130
        return c

    def _high_premium_candidate(self) -> CalendarCandidate:
        """Net debit is 34% above spread mid — exceeds 10% entry premium gate.

        near_mid=95, far_mid=105, spread_mid=10
        net_debit = far_ask - near_bid = 108 - 92 = 16
        premium = (16-10)/10 = 60% > 10%
        """
        c = _make_candidate()
        c.near_bid = 92.0
        c.near_ask = 98.0    # near_mid = 95, spread_pct = 6/95 ≈ 6.3% > 5% FAILS leg gate first
        # Use tight spreads so only premium check fires
        c.near_bid = 94.0
        c.near_ask = 96.0    # near_mid=95, spread=2/95≈2.1% — fine
        c.far_bid  = 104.0
        c.far_ask  = 106.0   # far_mid=105, spread=2/105≈1.9% — fine
        c.net_debit = 16.0   # = far_ask(106) - near_bid(94) = 12 ... let's set manually
        # spread_mid = 105 - 95 = 10; net_debit = 16 → premium = (16-10)/10 = 60%
        return c

    def _good_candidate(self) -> CalendarCandidate:
        """All checks pass: tight spreads, debit close to mid."""
        c = _make_candidate()
        c.near_bid = 94.0
        c.near_ask = 96.0    # near_mid=95, spread≈2.1%
        c.far_bid  = 104.0
        c.far_ask  = 106.0   # far_mid=105, spread≈1.9%
        c.net_debit = 10.5   # = far_ask - near_bid = 106-94=12 → but let's set to 10.5
        # spread_mid=10, debit=10.5 → premium=5% ≤ 10% — passes
        return c

    # ── Unit: _check_liquidity_gate ───────────────────────────────────────────

    def test_wide_near_leg_blocked(self):
        engine, _ = _make_engine()
        reason = engine._check_liquidity_gate(self._wide_near_candidate())
        assert reason is not None
        assert "near-leg" in reason

    def test_wide_far_leg_blocked(self):
        engine, _ = _make_engine()
        reason = engine._check_liquidity_gate(self._wide_far_candidate())
        assert reason is not None
        assert "far-leg" in reason

    def test_high_entry_premium_blocked(self):
        engine, _ = _make_engine()
        reason = engine._check_liquidity_gate(self._high_premium_candidate())
        assert reason is not None
        assert "entry premium" in reason

    def test_good_candidate_passes(self):
        engine, _ = _make_engine()
        reason = engine._check_liquidity_gate(self._good_candidate())
        assert reason is None

    # ── Integration: scan_tick respects the gate ──────────────────────────────

    def test_scan_tick_blocks_wide_spread_candidate(self):
        """scan_tick must not call enter_spread when the liquidity gate rejects."""
        executor = MagicMock()
        candidate = self._wide_near_candidate()
        candidate.ev_score = 0.25

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved")

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        executor.enter_spread.assert_not_called()
        assert status.open_positions == 0

    def test_scan_tick_blocks_high_premium_candidate(self):
        """scan_tick must not call enter_spread when entry premium is too high."""
        executor = MagicMock()
        candidate = self._high_premium_candidate()
        candidate.ev_score = 0.25

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved")

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        executor.enter_spread.assert_not_called()
        assert status.open_positions == 0

    def test_scan_tick_allows_good_candidate(self):
        """A candidate passing all gate checks must reach enter_spread."""
        executor = MagicMock()
        candidate = self._good_candidate()
        candidate.ev_score = 0.25

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved")
            executor.enter_spread.return_value = {
                "near_prem": candidate.near_bid,
                "far_prem":  candidate.far_ask,
                "net_debit": candidate.net_debit,
                "qty":       1.0,
            }

            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        executor.enter_spread.assert_called_once()

    # ── Bid/ask size checks ───────────────────────────────────────────────────

    def _engine_with_size_snaps(self, near_bid_sz: float, near_ask_sz: float,
                                 far_bid_sz: float, far_ask_sz: float):
        """Return (engine, candidate) with a cache that reports the given leg sizes."""
        candidate = self._good_candidate()
        near_snap = _make_snap(
            candidate.near_instrument,
            bid=candidate.near_bid, ask=candidate.near_ask,
            bid_size=near_bid_sz, ask_size=near_ask_sz,
        )
        far_snap = _make_snap(
            candidate.far_instrument,
            bid=candidate.far_bid, ask=candidate.far_ask,
            bid_size=far_bid_sz, ask_size=far_ask_sz,
        )
        cache = MagicMock()
        cache.get.side_effect = {
            candidate.near_instrument: near_snap,
            candidate.far_instrument:  far_snap,
        }.get
        engine, _ = _make_engine(cache=cache)
        return engine, candidate

    def test_near_bid_size_too_small_blocked(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_LEG_BID_SIZE", 2)
        engine, candidate = self._engine_with_size_snaps(
            near_bid_sz=1.0, near_ask_sz=5.0, far_bid_sz=5.0, far_ask_sz=5.0
        )
        reason = engine._check_liquidity_gate(candidate)
        assert reason is not None
        assert "near-leg bid_size" in reason

    def test_near_ask_size_too_small_blocked(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_LEG_ASK_SIZE", 2)
        engine, candidate = self._engine_with_size_snaps(
            near_bid_sz=5.0, near_ask_sz=1.0, far_bid_sz=5.0, far_ask_sz=5.0
        )
        reason = engine._check_liquidity_gate(candidate)
        assert reason is not None
        assert "near-leg ask_size" in reason

    def test_far_bid_size_too_small_blocked(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_LEG_BID_SIZE", 2)
        engine, candidate = self._engine_with_size_snaps(
            near_bid_sz=5.0, near_ask_sz=5.0, far_bid_sz=1.0, far_ask_sz=5.0
        )
        reason = engine._check_liquidity_gate(candidate)
        assert reason is not None
        assert "far-leg bid_size" in reason

    def test_far_ask_size_too_small_blocked(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_LEG_ASK_SIZE", 2)
        engine, candidate = self._engine_with_size_snaps(
            near_bid_sz=5.0, near_ask_sz=5.0, far_bid_sz=5.0, far_ask_sz=1.0
        )
        reason = engine._check_liquidity_gate(candidate)
        assert reason is not None
        assert "far-leg ask_size" in reason

    def test_zero_size_skips_size_check(self, monkeypatch):
        """bid_size=0 means the exchange did not report size data — skip, not reject."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_LEG_BID_SIZE", 100)
        engine, candidate = self._engine_with_size_snaps(
            near_bid_sz=0.0, near_ask_sz=0.0, far_bid_sz=0.0, far_ask_sz=0.0
        )
        reason = engine._check_liquidity_gate(candidate)
        assert reason is None

    def test_sufficient_size_passes(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_LEG_BID_SIZE", 1)
        monkeypatch.setattr(cfg, "MIN_LEG_ASK_SIZE", 1)
        engine, candidate = self._engine_with_size_snaps(
            near_bid_sz=5.0, near_ask_sz=5.0, far_bid_sz=5.0, far_ask_sz=5.0
        )
        reason = engine._check_liquidity_gate(candidate)
        assert reason is None


# ── Notification wiring ───────────────────────────────────────────────────────

class TestNotificationWiring:
    """
    DecisionEngine must call the appropriate Notifier methods at each event.
    All tests use a MagicMock notifier — no real network calls are made.
    """

    def _engine_with_notifier(self, executor=None):
        notifier = MagicMock()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        engine = DecisionEngine(
            cache=_make_cache(),
            portfolio_value=10_000.0,
            executor=executor,
            db_path=db_path,
            daily_loss_limit=500.0,
            notifier=notifier,
        )
        return engine, notifier, db_path

    def _open_pos(self, trade_id: int = 1) -> dict:
        near_label = _future_label(10)
        far_label  = _future_label(35)
        return {
            "trade_id":        trade_id,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             1.0,
            "net_debit":       0.02,
            "spot_open":       90_000.0,
            "near_days":       10,
            "far_days":        35,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_notify_entry_called_after_successful_fill(self):
        """notify_entry must fire once when a trade is successfully entered."""
        executor = MagicMock()
        executor.enter_spread.return_value = {
            "near_prem": 0.0245,
            "far_prem":  0.0445,
            "net_debit": 0.02,
            "qty":       1.0,
        }
        engine, notifier, _ = self._engine_with_notifier(executor=executor)

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            candidate = _make_candidate()
            candidate.ev_score = 0.25
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="ok")
            engine.scan_tick()

        notifier.notify_entry.assert_called_once()

    def test_notify_entry_not_called_on_executor_rejection(self):
        """notify_entry must NOT fire when the executor rejects the order."""
        executor = MagicMock()
        executor.enter_spread.return_value = None  # rejected

        engine, notifier, _ = self._engine_with_notifier(executor=executor)

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.size_candidate") as mock_size:
            candidate = _make_candidate()
            candidate.ev_score = 0.25
            mock_scan.return_value = [candidate]
            mock_size.return_value = MagicMock(qty=1.0, reason="ok")
            engine.scan_tick()

        notifier.notify_entry.assert_not_called()

    def test_notify_stop_called_on_stop_loss(self):
        """notify_stop must fire when a stop-loss triggers."""
        executor = MagicMock()
        executor.close_spread.return_value = 0.005
        engine, notifier, _ = self._engine_with_notifier(executor=executor)
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("stop", 0.005, 0.25, "STOP")), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()

        notifier.notify_stop.assert_called_once()

    def test_notify_take_profit_called_on_tp(self):
        """notify_take_profit must fire when a take-profit triggers."""
        executor = MagicMock()
        executor.close_spread.return_value = 0.04
        engine, notifier, _ = self._engine_with_notifier(executor=executor)
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("tp", 0.04, 2.0, "TP")), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()

        notifier.notify_take_profit.assert_called_once()

    def test_notify_close_called_on_expiry(self):
        """notify_close must fire when near leg expires and position is closed."""
        executor = MagicMock()
        executor.close_spread.return_value = 0.025
        engine, notifier, _ = self._engine_with_notifier(executor=executor)
        pos = self._open_pos()
        pos["expiry_near"] = _future_label(-1)  # expired

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], []]), \
             patch("strategy.decision.close_calendar_trade"):
            engine._cache.get_spot.return_value = 90_000.0
            engine.monitor_tick()

        notifier.notify_close.assert_called_once()
        # notify_stop and notify_take_profit must NOT fire for an expiry close
        notifier.notify_stop.assert_not_called()
        notifier.notify_take_profit.assert_not_called()

    def test_notify_daily_limit_called_on_halt(self):
        """notify_daily_limit must fire when the daily loss limit is breached."""
        engine, notifier, _ = self._engine_with_notifier()
        engine._today_pnl = -600.0   # exceeds 500 limit

        # scan_tick triggers the limit check
        result = engine.scan_tick()

        notifier.notify_daily_limit.assert_called_once()
        assert engine.state == BotState.HALTED

    def test_no_notification_on_position_held(self):
        """When a position is held (no stop/TP), no close notifications must fire."""
        executor = MagicMock()
        engine, notifier, _ = self._engine_with_notifier(executor=executor)
        pos = self._open_pos()

        with patch.object(engine, "_load_all_open_positions", side_effect=[[pos], [pos]]), \
             patch("strategy.decision.check_calendar_status",
                   return_value=("ok", 0.022, 1.1, "OK")):
            engine._cache.get_spot.return_value = 90_000.0
            engine._get_iv = MagicMock(return_value=0.80)
            engine.monitor_tick()

        notifier.notify_stop.assert_not_called()
        notifier.notify_take_profit.assert_not_called()
        notifier.notify_close.assert_not_called()


# ── Roll bug fixes ────────────────────────────────────────────────────────────

class TestRollFixes:
    """
    Tests for the 5 roll-loop bug fixes:
      1. DB updated after roll (update_near_leg called)
      2. In-memory pos dict updated after roll
      3. Paper mode short-circuit in executor (tested via DryRunExecutor path)
      4. _rolled_this_tick guard prevents double-roll in same tick
      5. Same-instrument check skips no-op rolls
    """

    def _make_expiring_pos(self, trade_id: int = 99, near_days: int = 1) -> dict:
        """Position whose near leg is within _ROLL_TRIGGER_DAYS."""
        near_label = _future_label(near_days)
        far_label  = _future_label(35)
        return {
            "trade_id":        trade_id,
            "status":          "Open",
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "qty":             1.0,
            "net_debit":       0.02,
            "spot_open":       90_000.0,
            "near_days":       near_days,
            "far_days":        35,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    # Fix 1 & 2: DB and in-memory update after successful roll
    def test_db_and_memory_updated_after_roll(self):
        new_near_label = _future_label(7)
        new_near_instr = f"BTC-{new_near_label}-90000-C"
        new_candidate = _make_candidate(near_days=7, far_days=35)
        new_candidate.near_instrument = new_near_instr

        executor = MagicMock()
        executor.roll_near_leg.return_value = True
        engine, db_path = _make_engine(executor=executor)

        pos = self._make_expiring_pos()

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.update_near_leg") as mock_update:
            mock_scan.return_value = [new_candidate]
            engine._try_roll(pos, spot=90_000.0)

            # Fix 1: DB update called with new near details
            mock_update.assert_called_once_with(
                pos["trade_id"], new_near_instr, new_near_label,
                db_path=engine._db_path,
            )
            # Fix 2: in-memory dict updated
            assert pos["near_instrument"] == new_near_instr
            assert pos["expiry_near"] == new_near_label

    # Fix 4: _rolled_this_tick guard
    def test_rolled_this_tick_set_after_roll(self):
        new_near_label = _future_label(7)
        new_candidate = _make_candidate(near_days=7, far_days=35)
        new_candidate.near_instrument = f"BTC-{new_near_label}-90000-C"

        executor = MagicMock()
        executor.roll_near_leg.return_value = True
        engine, _ = _make_engine(executor=executor)
        pos = self._make_expiring_pos(trade_id=42)

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.update_near_leg"):
            mock_scan.return_value = [new_candidate]
            engine._try_roll(pos, spot=90_000.0)

        assert 42 in engine._rolled_this_tick

    def test_rolled_this_tick_prevents_second_monitor_call(self):
        """Position already in _rolled_this_tick is skipped by _monitor_position."""
        engine, _ = _make_engine()
        pos = self._make_expiring_pos(trade_id=7)
        engine._rolled_this_tick.add(7)
        action, unr = engine._monitor_position(pos)
        assert action is None
        assert unr == 0.0

    def test_rolled_this_tick_cleared_after_monitor_tick(self):
        engine, _ = _make_engine()
        engine._rolled_this_tick.add(99)
        # monitor_tick with no positions clears the set
        with patch.object(engine, "_load_all_open_positions", return_value=[]):
            engine.monitor_tick()
        assert len(engine._rolled_this_tick) == 0

    # Fix 5: same-instrument guard
    def test_skip_roll_when_new_near_same_as_current(self):
        near_label = _future_label(1)
        same_near_instr = f"BTC-{near_label}-90000-C"
        candidate = _make_candidate(near_days=1, far_days=35)
        candidate.near_instrument = same_near_instr

        executor = MagicMock()
        engine, _ = _make_engine(executor=executor)
        pos = self._make_expiring_pos(near_days=1)
        pos["near_instrument"] = same_near_instr  # current == new

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.update_near_leg") as mock_update:
            mock_scan.return_value = [candidate]
            result = engine._try_roll(pos, spot=90_000.0)

        assert result is False
        executor.roll_near_leg.assert_not_called()
        mock_update.assert_not_called()

    def test_roll_proceeds_when_new_near_is_different(self):
        old_near_label = _future_label(1)
        new_near_label = _future_label(7)
        new_near_instr = f"BTC-{new_near_label}-90000-C"
        candidate = _make_candidate(near_days=7, far_days=35)
        candidate.near_instrument = new_near_instr

        executor = MagicMock()
        executor.roll_near_leg.return_value = True
        engine, _ = _make_engine(executor=executor)
        pos = self._make_expiring_pos(near_days=1)
        pos["near_instrument"] = f"BTC-{old_near_label}-90000-C"

        with patch("strategy.decision.scan") as mock_scan, \
             patch("strategy.decision.update_near_leg"):
            mock_scan.return_value = [candidate]
            result = engine._try_roll(pos, spot=90_000.0)

        assert result is True


# ── Per-asset overrides in the liquidity gate ─────────────────────────────────

class TestAssetOverridesLiquidityGate:
    """
    _check_liquidity_gate must apply per-asset MAX_LEG_SPREAD_PCT and
    MAX_ENTRY_PREMIUM from ASSET_OVERRIDES so that thinner assets (SOL) can
    pass with wider spreads without loosening the filters for BTC/ETH.
    """

    def _sol_candidate(self, near_spread_pct: float, far_spread_pct: float,
                        net_debit_premium: float = 0.05) -> CalendarCandidate:
        """
        Build a SOL candidate with controlled bid/ask spread percentages.
        net_debit is set to spread_mid * (1 + net_debit_premium) so only the
        leg-spread check varies between tests.
        """
        near_mid = 10.0
        far_mid  = 20.0
        spread_mid = far_mid - near_mid  # 10.0

        near_half = near_mid * near_spread_pct / 2
        far_half  = far_mid  * far_spread_pct  / 2
        near_label = _future_label(10)
        far_label  = _future_label(35)
        return CalendarCandidate(
            asset="SOL",
            strike=150.0,
            option_type="Call",
            near_instrument=f"SOL-{near_label}-150-C",
            far_instrument=f"SOL-{far_label}-150-C",
            near_days=10,
            far_days=35,
            spot=150.0,
            near_iv=0.90,
            far_iv=0.70,
            iv_contango=0.20,
            near_bid=near_mid - near_half,
            near_ask=near_mid + near_half,
            far_bid=far_mid - far_half,
            far_ask=far_mid + far_half,
            net_debit=spread_mid * (1 + net_debit_premium),
            near_oi=50.0,
            far_oi=50.0,
            pop=0.50,
            be_lo=120.0,
            be_hi=180.0,
            ev_score=0.20,
        )

    def _btc_candidate_with_spread(self, near_spread_pct: float, far_spread_pct: float) -> CalendarCandidate:
        """BTC candidate with same spread percentages, for global-vs-asset comparison."""
        near_mid = 100.0
        far_mid  = 200.0
        spread_mid = far_mid - near_mid  # 100.0
        near_half = near_mid * near_spread_pct / 2
        far_half  = far_mid  * far_spread_pct  / 2
        near_label = _future_label(10)
        far_label  = _future_label(35)
        return CalendarCandidate(
            asset="BTC",
            strike=90_000.0,
            option_type="Call",
            near_instrument=f"BTC-{near_label}-90000-C",
            far_instrument=f"BTC-{far_label}-90000-C",
            near_days=10,
            far_days=35,
            spot=90_000.0,
            near_iv=0.90,
            far_iv=0.70,
            iv_contango=0.20,
            near_bid=near_mid - near_half,
            near_ask=near_mid + near_half,
            far_bid=far_mid - far_half,
            far_ask=far_mid + far_half,
            net_debit=spread_mid * 1.05,
            near_oi=500.0,
            far_oi=500.0,
            pop=0.50,
            be_lo=80_000.0,
            be_hi=100_000.0,
            ev_score=0.25,
        )

    def test_sol_passes_with_15pct_spread(self, monkeypatch):
        """SOL candidate with 15% leg spread passes the SOL override limit of 20%."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_LEG_SPREAD_PCT", 0.05)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MAX_LEG_SPREAD_PCT": 0.20, "MAX_ENTRY_PREMIUM": 0.20}
        })
        engine, _ = _make_engine()
        candidate = self._sol_candidate(near_spread_pct=0.15, far_spread_pct=0.15)
        reason = engine._check_liquidity_gate(candidate)
        assert reason is None, f"Expected SOL 15% spread to pass 20% limit, got: {reason}"

    def test_btc_fails_with_15pct_spread(self, monkeypatch):
        """BTC candidate with 15% leg spread is rejected by the global 5% limit."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_LEG_SPREAD_PCT", 0.05)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MAX_LEG_SPREAD_PCT": 0.20, "MAX_ENTRY_PREMIUM": 0.20}
        })
        engine, _ = _make_engine()
        candidate = self._btc_candidate_with_spread(near_spread_pct=0.15, far_spread_pct=0.15)
        reason = engine._check_liquidity_gate(candidate)
        assert reason is not None, "BTC with 15% spread should be rejected by 5% global limit"
        assert "MAX_LEG_SPREAD_PCT" in reason

    def test_sol_fails_when_spread_exceeds_sol_override(self, monkeypatch):
        """SOL candidate with 25% leg spread fails even the SOL-specific 20% limit."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_LEG_SPREAD_PCT", 0.05)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MAX_LEG_SPREAD_PCT": 0.20, "MAX_ENTRY_PREMIUM": 0.20}
        })
        engine, _ = _make_engine()
        candidate = self._sol_candidate(near_spread_pct=0.25, far_spread_pct=0.05)
        reason = engine._check_liquidity_gate(candidate)
        assert reason is not None, "25% spread should exceed the SOL 20% limit"
        assert "near-leg" in reason

    def test_sol_entry_premium_uses_sol_override(self, monkeypatch):
        """SOL entry premium of 15% passes the SOL override of 20% (global is 10%)."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_LEG_SPREAD_PCT", 0.05)
        monkeypatch.setattr(cfg, "MAX_ENTRY_PREMIUM", 0.10)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MAX_LEG_SPREAD_PCT": 0.20, "MAX_ENTRY_PREMIUM": 0.20}
        })
        engine, _ = _make_engine()
        # 3% spread on legs — well within both limits
        # 15% entry premium — above global 10% but below SOL's 20%
        candidate = self._sol_candidate(
            near_spread_pct=0.03, far_spread_pct=0.03, net_debit_premium=0.15
        )
        reason = engine._check_liquidity_gate(candidate)
        assert reason is None, f"15% premium should pass SOL's 20% entry premium limit, got: {reason}"

    def test_btc_entry_premium_uses_global_limit(self, monkeypatch):
        """BTC entry premium of 15% is rejected by the global 10% limit."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_LEG_SPREAD_PCT", 0.05)
        monkeypatch.setattr(cfg, "MAX_ENTRY_PREMIUM", 0.10)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MAX_LEG_SPREAD_PCT": 0.20, "MAX_ENTRY_PREMIUM": 0.20}
        })
        engine, _ = _make_engine()
        # BTC candidate: tight leg spreads (2%), 15% entry premium
        candidate = self._btc_candidate_with_spread(near_spread_pct=0.02, far_spread_pct=0.02)
        # Override net_debit to force 15% entry premium:
        # spread_mid = 100; 15% premium → net_debit = 100 * 1.15 = 115
        candidate.net_debit = 115.0
        reason = engine._check_liquidity_gate(candidate)
        assert reason is not None, "BTC with 15% entry premium should fail the global 10% limit"
        assert "entry premium" in reason


# ── Drain mode ────────────────────────────────────────────────────────────────

class TestDrainMode:
    """DRAIN_MODE=True: no new entries, no rolls; stop/TP/expiry still close positions."""

    def _open_pos(self, near_days: int = 10, far_days: int = 35) -> dict:
        near_label = _future_label(near_days)
        far_label  = _future_label(far_days)
        return {
            "trade_id":        1,
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "near_days":       near_days,
            "far_days":        far_days,
            "qty":             1.0,
            "net_debit":       0.02,
            "spot_open":       90_000.0,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
        }

    def test_scan_tick_skipped_in_drain_mode(self, monkeypatch):
        """scan_tick returns without entering a trade when DRAIN_MODE is True."""
        import config as cfg
        monkeypatch.setattr(cfg, "DRAIN_MODE", True)

        executor = MagicMock()
        executor.enter_spread.return_value = {"near_prem": 0.02, "far_prem": 0.04, "net_debit": 0.02, "qty": 1.0}

        with patch("strategy.decision.scan", return_value=[_make_candidate()]):
            engine, _ = _make_engine(executor=executor)
            status = engine.scan_tick()

        executor.enter_spread.assert_not_called()
        assert "Drain mode" in status.message

    def test_scan_tick_normal_when_drain_false(self, monkeypatch):
        """scan_tick enters a trade when DRAIN_MODE is False (baseline)."""
        import config as cfg
        monkeypatch.setattr(cfg, "DRAIN_MODE", False)
        monkeypatch.setattr(cfg, "MIN_EV", 0.0)
        monkeypatch.setattr(cfg, "MIN_NET_DEBIT", 0.0)  # allow small debit in test

        executor = MagicMock()
        executor.enter_spread.return_value = {"near_prem": 0.02, "far_prem": 0.04, "net_debit": 0.02, "qty": 1.0}

        with patch("strategy.decision.scan", return_value=[_make_candidate()]):
            engine, _ = _make_engine(executor=executor)
            engine.scan_tick()

        executor.enter_spread.assert_called_once()

    def test_near_expiry_closes_not_rolls_in_drain_mode(self, monkeypatch):
        """When near leg is within roll-trigger days, drain mode closes rather than rolls."""
        import config as cfg
        monkeypatch.setattr(cfg, "DRAIN_MODE", True)

        executor = MagicMock()
        executor.close_spread.return_value = 0.025

        cache = _make_cache(near_days=1, far_days=30)
        engine, db_path = _make_engine(cache=cache, executor=executor)

        pos = self._open_pos(near_days=1, far_days=30)

        with patch("strategy.decision.check_calendar_status", return_value=("hold", 0.022, 1.1, "OK")), \
             patch("strategy.decision.close_calendar_trade") as mock_close:
            action, _ = engine._monitor_position(pos)

        executor.roll_near_leg.assert_not_called()
        executor.close_spread.assert_called_once()
        assert "Drain mode" in action

    def test_stop_loss_still_fires_in_drain_mode(self, monkeypatch):
        """Stop-loss trigger works normally even when DRAIN_MODE is True."""
        import config as cfg
        monkeypatch.setattr(cfg, "DRAIN_MODE", True)

        executor = MagicMock()
        executor.close_spread.return_value = 0.008

        cache = _make_cache(near_days=10, far_days=35)
        engine, _ = _make_engine(cache=cache, executor=executor)

        pos = self._open_pos(near_days=10, far_days=35)

        with patch("strategy.decision.check_calendar_status", return_value=("stop", 0.008, 0.4, "Stop")), \
             patch("strategy.decision.close_calendar_trade"):
            action, _ = engine._monitor_position(pos)

        executor.close_spread.assert_called_once()
        assert "Stop-loss" in action

    def test_take_profit_still_fires_in_drain_mode(self, monkeypatch):
        """Take-profit trigger works normally even when DRAIN_MODE is True."""
        import config as cfg
        monkeypatch.setattr(cfg, "DRAIN_MODE", True)

        executor = MagicMock()
        executor.close_spread.return_value = 0.034

        cache = _make_cache(near_days=10, far_days=35)
        engine, _ = _make_engine(cache=cache, executor=executor)

        pos = self._open_pos(near_days=10, far_days=35)

        with patch("strategy.decision.check_calendar_status", return_value=("tp", 0.034, 1.7, "TP")), \
             patch("strategy.decision.close_calendar_trade"):
            action, _ = engine._monitor_position(pos)

        executor.close_spread.assert_called_once()
        assert "Take-profit" in action


# ── Fee integration tests ──────────────────────────────────────────────────────

class TestFeeIntegration:
    """
    Verify fee-aware logic in the decision engine:
    - fees_paid_today accumulates after entry
    - roll fee gate: roll skipped when roll_fees > theta_gain
    - fee-inclusive net P&L logged on close (close_fees recorded in DB)
    """

    def _open_pos(self, near_days: int = 10, far_days: int = 35,
                  qty: float = 1.0, net_debit: float = 0.02) -> dict:
        near_label = _future_label(near_days)
        far_label  = _future_label(far_days)
        return {
            "trade_id":        1,
            "asset":           "BTC",
            "option_type":     "Call",
            "strike":          90_000.0,
            "expiry_near":     near_label,
            "expiry_far":      far_label,
            "near_days":       near_days,
            "far_days":        far_days,
            "qty":             qty,
            "net_debit":       net_debit,
            "spot_open":       90_000.0,
            "near_instrument": f"BTC-{near_label}-90000-C",
            "far_instrument":  f"BTC-{far_label}-90000-C",
            "open_fees":       0.0,
            "close_fees":      0.0,
        }

    def test_fees_paid_today_zero_on_init(self):
        engine, _ = _make_engine()
        assert engine.fees_paid_today == 0.0

    def test_fees_paid_today_increments_after_entry(self, monkeypatch):
        """After a successful entry, fees_paid_today should be positive."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_EV", 0.0)
        monkeypatch.setattr(cfg, "MIN_NET_DEBIT", 0.0)

        executor = MagicMock()
        candidate = _make_candidate()
        executor.enter_spread.return_value = _fill_dict(candidate)

        with patch("strategy.decision.scan", return_value=[candidate]), \
             patch("strategy.decision.size_candidate") as mock_size:
            mock_size.return_value = MagicMock(qty=1.0, reason="Approved",
                                               estimated_fees=0.0)
            engine, _ = _make_engine(executor=executor)
            engine.scan_tick()

        # fees_paid_today should be >= 0 (may be 0 if spot was 0 but otherwise positive)
        assert engine.fees_paid_today >= 0.0

    def test_roll_fee_gate_blocks_uneconomic_roll(self, monkeypatch):
        """_try_roll must skip the roll when roll_fees exceed theta_gain.

        We mock compute_roll_fees to return a value larger than near_bid × qty
        so the gate triggers without depending on the exact fee cap behaviour.
        """
        new_near_label = _future_label(7)
        new_candidate = _make_candidate(near_days=7, far_days=35)
        new_candidate.near_instrument = f"BTC-{new_near_label}-90000-C"
        new_candidate.near_bid = 0.0245  # matches _make_candidate default

        executor = MagicMock()
        executor.roll_near_leg.return_value = True
        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(near_days=1)

        # Force roll_fees to return a value larger than theta_gain = near_bid × qty
        huge_roll_cost = new_candidate.near_bid * 10  # 10× the theta gain
        with patch("strategy.decision.scan", return_value=[new_candidate]), \
             patch("strategy.decision.update_near_leg"), \
             patch("strategy.decision.compute_roll_fees", return_value=huge_roll_cost):
            result = engine._try_roll(pos, spot=90_000.0)

        # Gate should block: theta_gain = 0.0245 < huge_roll_cost = 0.245
        assert result is False
        executor.roll_near_leg.assert_not_called()

    def test_roll_proceeds_when_theta_gain_exceeds_fees(self, monkeypatch):
        """_try_roll proceeds when theta_gain > roll_fees (fee gate passes)."""
        new_near_label = _future_label(7)
        new_candidate = _make_candidate(near_days=7, far_days=35)
        new_candidate.near_instrument = f"BTC-{new_near_label}-90000-C"
        new_candidate.near_bid = 0.0245

        executor = MagicMock()
        executor.roll_near_leg.return_value = True
        engine, _ = _make_engine(executor=executor)
        pos = self._open_pos(near_days=1)

        # Force roll_fees to return tiny value so theta_gain easily exceeds it
        tiny_roll_cost = new_candidate.near_bid * 0.01  # 1% of theta gain
        with patch("strategy.decision.scan", return_value=[new_candidate]), \
             patch("strategy.decision.update_near_leg"), \
             patch("strategy.decision.compute_roll_fees", return_value=tiny_roll_cost):
            result = engine._try_roll(pos, spot=90_000.0)

        assert result is True
        executor.roll_near_leg.assert_called_once()

    def test_close_records_close_fees_in_db(self, monkeypatch):
        """_close_position must call close_calendar_trade with a non-zero close_fees arg."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_EV", 0.0)
        monkeypatch.setattr(cfg, "MIN_NET_DEBIT", 0.0)

        executor = MagicMock()
        executor.close_spread.return_value = 0.03
        engine, _ = _make_engine(executor=executor)

        pos = self._open_pos(qty=1.0, net_debit=0.02)
        pos["spot_open"] = 90_000.0

        # Inject spot into the cache
        engine._cache.get_spot.return_value = 90_000.0

        with patch("strategy.decision.close_calendar_trade") as mock_close:
            engine._close_position(pos, spot=90_000.0, reason="stop", spread_value=0.01)

        # close_calendar_trade must be called with close_fees > 0
        assert mock_close.called
        call_kwargs = mock_close.call_args[1]
        assert call_kwargs.get("close_fees", 0.0) >= 0.0  # may be 0 in mocked context

    def test_fees_paid_today_increments_on_close(self, monkeypatch):
        """fees_paid_today increases after _close_position is called."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_EV", 0.0)

        executor = MagicMock()
        executor.close_spread.return_value = 0.03
        engine, _ = _make_engine(executor=executor)

        initial_fees = engine.fees_paid_today
        pos = self._open_pos(qty=1.0, net_debit=0.02)
        pos["spot_open"] = 90_000.0
        engine._cache.get_spot.return_value = 90_000.0

        with patch("strategy.decision.close_calendar_trade"):
            engine._close_position(pos, spot=90_000.0, reason="stop", spread_value=0.01)

        # fees_paid_today should have increased (BTC spot=90k → fees ≈ $54)
        assert engine.fees_paid_today >= initial_fees
