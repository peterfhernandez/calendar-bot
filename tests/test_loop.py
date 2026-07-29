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


class TestSecretRedactor:
    """Unit tests for _SecretRedactor logging filter."""

    def _make_record(self, msg: str, *args: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        return record

    def test_token_replaced(self) -> None:
        from monitor.loop import _SecretRedactor
        token = "bot123456:ABCDEFsecretTOKEN"
        f = _SecretRedactor([token])
        record = self._make_record("POST https://api.telegram.org/%s/getUpdates", token)
        f.filter(record)
        assert token not in record.getMessage()
        assert "<redacted>" in record.getMessage()

    def test_chat_id_replaced(self) -> None:
        from monitor.loop import _SecretRedactor
        chat_id = "987654321"
        f = _SecretRedactor([chat_id])
        record = self._make_record("Sending message to chat %s", chat_id)
        f.filter(record)
        assert chat_id not in record.getMessage()
        assert "<redacted>" in record.getMessage()

    def test_multiple_secrets_replaced(self) -> None:
        from monitor.loop import _SecretRedactor
        token = "bot999:SECRETtoken"
        chat = "111222333"
        f = _SecretRedactor([token, chat])
        record = self._make_record(f"url=https://api.telegram.org/{token}/send chat={chat}")
        f.filter(record)
        msg = record.getMessage()
        assert token not in msg
        assert chat not in msg
        assert msg.count("<redacted>") == 2

    def test_record_without_secret_unchanged(self) -> None:
        from monitor.loop import _SecretRedactor
        f = _SecretRedactor(["MYTOKEN"])
        original = "Scanner found 3 candidates for BTC"
        record = self._make_record(original)
        f.filter(record)
        assert record.getMessage() == original

    def test_empty_secrets_list_is_noop(self) -> None:
        from monitor.loop import _SecretRedactor
        f = _SecretRedactor([])
        record = self._make_record("some log line with no secret")
        result = f.filter(record)
        assert result is True
        assert record.getMessage() == "some log line with no secret"

    def test_blank_secret_ignored(self) -> None:
        """Empty and whitespace-only strings must not be added — they would redact every log line."""
        from monitor.loop import _SecretRedactor
        f = _SecretRedactor(["", "   ", "\t", "realtoken"])
        assert "" not in f._secrets
        assert "   " not in f._secrets
        assert "\t" not in f._secrets
        assert "realtoken" in f._secrets

    def test_filter_always_returns_true(self) -> None:
        """Filter must never suppress the record — only rewrite it."""
        from monitor.loop import _SecretRedactor
        f = _SecretRedactor(["tok"])
        record = self._make_record("contains tok in message")
        assert f.filter(record) is True

    def test_configure_logging_silences_httpx(self, tmp_path: Path) -> None:
        import monitor.loop as _ml
        _ml._logging_configured = False
        _ml.configure_logging(log_dir=tmp_path / "logs")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        # Cleanup
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            if hasattr(h, "close"):
                h.close()
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


# ── Phase 27d — slow ticks must not block the shared event loop ───────────────

class TestTickOffload:
    """
    scan_tick()/monitor_tick() are synchronous and can block for tens of seconds
    on a failed entry.  Awaited inline from the scheduler job they froze the one
    event loop that also drives the WS feed and the Telegram listener, so the
    chain cache went stale (observed: "106 stale instrument(s) excluded") and a
    monitor tick was missed outright while a position sat past its stop.
    """

    def _loop(self, tmp_path: Path):
        from monitor.loop import BotLoop
        return BotLoop(
            cache=_FakeCache(),
            portfolio_value=10_000.0,
            executor=_FakeExecutor(),
            db_path=tmp_path / "offload.db",
            log_dir=str(tmp_path / "logs"),
        )

    @staticmethod
    def _status():
        """An EngineStatus the job can log, so mocked ticks take the happy path."""
        from strategy.decision import BotState, EngineStatus
        return EngineStatus(
            state=BotState.IDLE, open_positions=0, daily_pnl=0.0, message="ok",
        )

    def _slow_tick(self, seconds: float):
        """A tick that blocks for *seconds* and returns a valid status."""
        def _tick():
            time.sleep(seconds)
            return self._status()
        return _tick

    @pytest.mark.asyncio
    async def test_loop_keeps_running_during_a_slow_tick(self, tmp_path: Path) -> None:
        """A blocking tick must not stop other loop tasks from making progress."""
        import config as _cfg
        loop = self._loop(tmp_path)

        heartbeats = 0

        async def _heartbeat() -> None:
            nonlocal heartbeats
            while True:
                await asyncio.sleep(0.01)
                heartbeats += 1

        beat = asyncio.create_task(_heartbeat())
        try:
            with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
                 patch.object(loop.engine, "scan_tick",
                              side_effect=self._slow_tick(0.3)):
                await loop._scan_job()
        finally:
            beat.cancel()

        # With the tick on a worker thread the loop stayed live throughout the
        # 0.3s block.  Awaited inline, heartbeats would be 0.
        assert heartbeats > 5, f"event loop was starved (heartbeats={heartbeats})"

    @pytest.mark.asyncio
    async def test_tick_runs_off_the_event_loop_thread(self, tmp_path: Path) -> None:
        """The tick body must execute on a worker thread, not the loop thread."""
        import config as _cfg
        import threading

        loop = self._loop(tmp_path)
        loop_thread = threading.get_ident()
        tick_thread: list[int] = []

        def _record():
            tick_thread.append(threading.get_ident())
            return self._status()

        with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
             patch.object(loop.engine, "monitor_tick", side_effect=_record):
            await loop._monitor_job()

        assert tick_thread and tick_thread[0] != loop_thread

    @pytest.mark.asyncio
    async def test_offload_disabled_runs_inline(self, tmp_path: Path) -> None:
        """TICK_OFFLOAD_ENABLED=False restores pre-27d inline execution."""
        import config as _cfg
        import threading

        loop = self._loop(tmp_path)
        loop_thread = threading.get_ident()
        tick_thread: list[int] = []

        with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", False), \
             patch.object(loop.engine, "monitor_tick",
                          side_effect=lambda: (tick_thread.append(threading.get_ident()),
                                               self._status())[1]):
            await loop._monitor_job()

        assert tick_thread == [loop_thread]

    @pytest.mark.asyncio
    async def test_scan_and_monitor_ticks_never_overlap(self, tmp_path: Path) -> None:
        """
        Offloaded ticks must stay serialized — engine state (failure counters,
        _just_entered, pnl accumulators) is not safe for two concurrent ticks.
        """
        import config as _cfg
        loop = self._loop(tmp_path)

        concurrent = 0
        max_concurrent = 0

        def _tick():
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.15)
            concurrent -= 1
            return self._status()

        with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
             patch.object(loop.engine, "scan_tick", side_effect=_tick), \
             patch.object(loop.engine, "monitor_tick", side_effect=_tick):
            await asyncio.gather(loop._scan_job(), loop._monitor_job())

        assert max_concurrent == 1

    @pytest.mark.asyncio
    async def test_slow_tick_logs_a_warning(self, tmp_path: Path, caplog) -> None:
        """A tick slower than TICK_SLOW_WARN_SEC is reported, not silent."""
        import config as _cfg
        loop = self._loop(tmp_path)

        with caplog.at_level(logging.WARNING, logger="monitor.loop"), \
             patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
             patch.object(_cfg, "TICK_SLOW_WARN_SEC", 0.05), \
             patch.object(loop.engine, "scan_tick",
                          side_effect=self._slow_tick(0.2)):
            await loop._scan_job()

        assert any("TICK_SLOW_WARN_SEC" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_fast_tick_logs_no_warning(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="monitor.loop"):
            import config as _cfg
            loop = self._loop(tmp_path)
            with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
                 patch.object(_cfg, "TICK_SLOW_WARN_SEC", 5.0), \
                 patch.object(loop.engine, "monitor_tick",
                              return_value=self._status()):
                await loop._monitor_job()

        assert not any("TICK_SLOW_WARN_SEC" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_exception_in_offloaded_tick_is_still_caught(self, tmp_path: Path) -> None:
        """The job's error handling must survive the tick moving to a thread."""
        import config as _cfg
        loop = self._loop(tmp_path)

        with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
             patch.object(loop.engine, "scan_tick", side_effect=RuntimeError("boom")):
            await loop._scan_job()   # must not raise

    @pytest.mark.asyncio
    async def test_lock_is_released_after_a_failing_tick(self, tmp_path: Path) -> None:
        """A raising tick must not leave the tick lock held (deadlocking the bot)."""
        import config as _cfg
        loop = self._loop(tmp_path)

        with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
             patch.object(loop.engine, "scan_tick", side_effect=RuntimeError("boom")):
            await loop._scan_job()

        assert not loop._tick_lock.locked()

        with patch.object(_cfg, "TICK_OFFLOAD_ENABLED", True), \
             patch.object(loop.engine, "monitor_tick",
                          return_value=self._status()):
            await asyncio.wait_for(loop._monitor_job(), timeout=5)
