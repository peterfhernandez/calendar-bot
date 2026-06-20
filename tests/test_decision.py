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
               bid: float = 0.02, ask: float = 0.03) -> TickerSnapshot:
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

    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    cache.get_chain.return_value = [near_snap, far_snap]
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
        near_ask=0.03,
        near_bid=0.02,
        far_ask=0.04,
        far_bid=0.035,
        net_debit=0.02,
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
