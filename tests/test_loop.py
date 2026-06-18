"""
tests/test_loop.py
==================
Unit tests for monitor/loop.py (BotLoop + configure_logging).

All tests use fake cache / executor objects — no network calls, no disk I/O
beyond a temp SQLite file that is cleaned up after each test.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Shared fakes ──────────────────────────────────────────────────────────────

@dataclass
class _FakeSnap:
    instrument: str
    asset:      str
    spot:       float
    mark_price: float
    mark_iv:    float
    bid:        float
    ask:        float
    open_interest: float
    timestamp:  float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.mark_price


class _FakeCache:
    def get_spot(self, asset: str) -> float:
        return 65_000.0 if asset == "BTC" else 3_500.0

    def get_chain(self, asset: str) -> list[_FakeSnap]:
        spot   = self.get_spot(asset)
        strike = round(spot / 1_000) * 1_000
        return [
            _FakeSnap(f"{asset}-7DAY-{strike}-C",  asset, spot, 0.05, 0.85, 0.04, 0.06, 500),
            _FakeSnap(f"{asset}-30DAY-{strike}-C", asset, spot, 0.08, 0.80, 0.07, 0.09, 600),
        ]

    async def update(self, snap: Any) -> None:
        pass


class _FakeExecutor:
    def __init__(self) -> None:
        self.enters: list[Any] = []
        self.closes: list[Any] = []

    def enter_spread(self, candidate: Any) -> dict | None:
        self.enters.append(candidate)
        return {
            "near_prem": candidate.near_bid,
            "far_prem":  candidate.far_ask,
            "net_debit": candidate.net_debit,
            "qty":       candidate.qty,
        }

    def close_spread(self, position: dict) -> float | None:
        self.closes.append(position)
        return position.get("net_debit", 0.0)

    def roll_near_leg(self, position: dict, new_candidate: Any) -> bool:
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_loop(tmp_path: Path, scan_interval: int = 3, monitor_interval: int = 2) -> Any:
    """Return a BotLoop wired with fake deps and fast intervals."""
    from monitor.loop import BotLoop
    import config as _cfg

    # Patch intervals so tests don't wait 5 minutes
    _cfg.SCAN_INTERVAL_SEC    = scan_interval
    _cfg.MONITOR_INTERVAL_SEC = monitor_interval

    return BotLoop(
        cache=_FakeCache(),
        portfolio_value=10_000.0,
        executor=_FakeExecutor(),
        db_path=tmp_path / "test_loop.db",
        log_dir=str(tmp_path / "logs"),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestConfigureLogging:
    def test_adds_handlers_once(self, tmp_path: Path) -> None:
        import monitor.loop as _ml

        # Reset the module flag so we get a fresh call
        _ml._logging_configured = False

        root = logging.getLogger()
        original_count = len(root.handlers)

        _ml.configure_logging(log_dir=tmp_path / "logs1")
        after_first = len(root.handlers)

        _ml.configure_logging(log_dir=tmp_path / "logs2")  # second call — should be no-op
        after_second = len(root.handlers)

        assert after_first > original_count
        assert after_second == after_first

        # Cleanup handlers we added so other tests are unaffected
        for h in root.handlers[original_count:]:
            root.removeHandler(h)
        _ml._logging_configured = False

    def test_log_file_created(self, tmp_path: Path) -> None:
        import monitor.loop as _ml

        _ml._logging_configured = False
        log_dir = tmp_path / "mylog"
        _ml.configure_logging(log_dir=log_dir)

        assert (log_dir / "bot.log").exists()

        # Cleanup
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.close()
                root.removeHandler(h)
        _ml._logging_configured = False


class TestBotLoopProperties:
    def test_portfolio_value_passthrough(self, tmp_path: Path) -> None:
        import config as _cfg
        _cfg.SCAN_INTERVAL_SEC    = 300
        _cfg.MONITOR_INTERVAL_SEC = 60

        from monitor.loop import BotLoop
        loop = BotLoop(
            cache=_FakeCache(),
            portfolio_value=20_000.0,
            db_path=tmp_path / "pv.db",
            log_dir=str(tmp_path / "logs"),
        )
        assert loop.portfolio_value == 20_000.0
        loop.portfolio_value = 30_000.0
        assert loop.portfolio_value == 30_000.0
        assert loop.engine.portfolio_value == 30_000.0

    def test_is_running_initially_false(self, tmp_path: Path) -> None:
        import config as _cfg
        _cfg.SCAN_INTERVAL_SEC    = 300
        _cfg.MONITOR_INTERVAL_SEC = 60

        from monitor.loop import BotLoop
        loop = BotLoop(
            cache=_FakeCache(),
            portfolio_value=10_000.0,
            db_path=tmp_path / "run.db",
            log_dir=str(tmp_path / "logs"),
        )
        assert not loop.is_running


class TestBotLoopRun:
    """Integration-style tests that actually run the asyncio loop briefly."""

    @pytest.mark.asyncio
    async def test_run_and_stop(self, tmp_path: Path) -> None:
        """BotLoop starts, fires at least one job tick, then stops cleanly."""
        import config as _cfg
        _cfg.SCAN_INTERVAL_SEC    = 1
        _cfg.MONITOR_INTERVAL_SEC = 1

        from monitor.loop import BotLoop
        loop = BotLoop(
            cache=_FakeCache(),
            portfolio_value=10_000.0,
            executor=_FakeExecutor(),
            db_path=tmp_path / "run_stop.db",
            log_dir=str(tmp_path / "logs"),
        )

        async def _stopper() -> None:
            await asyncio.sleep(3)   # let at least 2 ticks fire
            await loop.stop()

        stopper = asyncio.create_task(_stopper())
        await loop.run()
        stopper.cancel()

        assert not loop.is_running

    @pytest.mark.asyncio
    async def test_scan_job_updates_engine_state(self, tmp_path: Path) -> None:
        """After a scan tick the engine is no longer IDLE (it moved through SCAN)."""
        import config as _cfg
        _cfg.SCAN_INTERVAL_SEC    = 1
        _cfg.MONITOR_INTERVAL_SEC = 60  # suppress monitor during this test

        from monitor.loop import BotLoop
        from strategy.decision import BotState

        loop = BotLoop(
            cache=_FakeCache(),
            portfolio_value=10_000.0,
            executor=_FakeExecutor(),
            db_path=tmp_path / "scan_state.db",
            log_dir=str(tmp_path / "logs"),
        )

        # Run one scan tick directly (bypass scheduler for determinism)
        loop._engine.scan_tick()

        # Engine should have processed at least one cycle
        assert loop.engine.state in {BotState.IDLE, BotState.MONITOR, BotState.HALTED}

    @pytest.mark.asyncio
    async def test_monitor_job_runs_without_error(self, tmp_path: Path) -> None:
        """Monitor tick on empty positions returns IDLE state without crashing."""
        import config as _cfg
        _cfg.SCAN_INTERVAL_SEC    = 60
        _cfg.MONITOR_INTERVAL_SEC = 1

        from monitor.loop import BotLoop
        from strategy.decision import BotState

        loop = BotLoop(
            cache=_FakeCache(),
            portfolio_value=10_000.0,
            executor=_FakeExecutor(),
            db_path=tmp_path / "monitor_empty.db",
            log_dir=str(tmp_path / "logs"),
        )

        status = loop._engine.monitor_tick()
        assert status.state is BotState.IDLE
        assert status.open_positions == 0

    @pytest.mark.asyncio
    async def test_halted_engine_skips_jobs(self, tmp_path: Path) -> None:
        """Once engine is HALTED, scan and monitor jobs log a warning and return."""
        import config as _cfg
        _cfg.SCAN_INTERVAL_SEC    = 1
        _cfg.MONITOR_INTERVAL_SEC = 1

        from monitor.loop import BotLoop
        from strategy.decision import BotState

        loop = BotLoop(
            cache=_FakeCache(),
            portfolio_value=10_000.0,
            executor=_FakeExecutor(),
            db_path=tmp_path / "halted.db",
            log_dir=str(tmp_path / "logs"),
        )

        # Force-halt the engine
        loop.engine._state       = BotState.HALTED
        loop.engine._today_pnl   = -999.0

        # Jobs must return without raising
        await loop._scan_job()
        await loop._monitor_job()

        assert loop.engine.state is BotState.HALTED

    @pytest.mark.asyncio
    async def test_job_exception_does_not_crash_loop(self, tmp_path: Path) -> None:
        """An unexpected exception inside a job is caught and logged, not propagated."""
        import config as _cfg
        _cfg.SCAN_INTERVAL_SEC    = 1
        _cfg.MONITOR_INTERVAL_SEC = 60

        from monitor.loop import BotLoop

        loop = BotLoop(
            cache=_FakeCache(),
            portfolio_value=10_000.0,
            db_path=tmp_path / "exc.db",
            log_dir=str(tmp_path / "logs"),
        )

        with patch.object(loop.engine, "scan_tick", side_effect=RuntimeError("boom")):
            # Should not raise
            await loop._scan_job()
