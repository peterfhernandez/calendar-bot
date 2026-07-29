"""
monitor/loop.py
===============
Scheduler and monitoring loop for the calendar spread bot.

Sets up two APScheduler jobs:
  - scan_job  — runs DecisionEngine.scan_tick()  every SCAN_INTERVAL_SEC
  - monitor_job — runs DecisionEngine.monitor_tick() every MONITOR_INTERVAL_SEC

Both ticks are synchronous and can block for tens of seconds (a failed entry
waits out the full order timeout), so they are dispatched to a worker thread via
_run_tick() rather than awaited inline — otherwise they freeze the single event
loop that also drives the WebSocket feed and the Telegram listener (Phase 27d).
A lock keeps the two ticks serialized against each other.

Logging goes to both the console and a rotating file (logs/bot.log, max 10 MB,
5 backups).

Public API
----------
BotLoop(cache, portfolio_value, executor=None, db_path=None)
    Main loop class.  Call await run() to start; it blocks until SIGINT/SIGTERM.
    Call stop() from another coroutine to shut down cleanly.

configure_logging(log_dir="logs", level=logging.INFO)
    Wire up the rotating-file + console handlers.  Called automatically by
    BotLoop.run() before the scheduler starts.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import time
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from alerts.notifier import Notifier
from data.chain_cache import ChainCache
from portfolio.tracker import PortfolioTracker
from strategy.decision import BotState, DecisionEngine

logger = logging.getLogger(__name__)

_logging_configured = False  # guard against double-init


# ── Logging setup ─────────────────────────────────────────────────────────────

# Backwards-compatible alias — the redactor implementation now lives in
# core/logging_setup.py so every entry point shares it (Phase 20a).
from core.logging_setup import SecretRedactor as _SecretRedactor  # noqa: E402
from core.logging_setup import setup_logging as _setup_logging    # noqa: E402


def configure_logging(
    log_dir: str | Path = config.LOG_DIR,
    level: int | str | None = None,
) -> None:
    """
    Configure root logger with a console handler and a rotating file handler.

    Thin wrapper around core.logging_setup.setup_logging(); format, rotation
    size, backup count, noisy-logger suppression, and secret redaction all
    come from config.py (Phase 20a).

    Safe to call multiple times — extra calls are no-ops.
    """
    global _logging_configured
    if _logging_configured:
        return
    _setup_logging(level=level, log_dir=log_dir, force=True)
    _logging_configured = True


# ── BotLoop ───────────────────────────────────────────────────────────────────

class BotLoop:
    """
    Wraps the DecisionEngine in an APScheduler event loop.

    Parameters
    ----------
    cache
        Populated ChainCache (caller must start the feed before calling run()).
    portfolio_value
        Initial portfolio value in USD.  When a PortfolioTracker is supplied
        this is updated on every scan cycle from the live account balance.
    executor
        ExecutorProtocol implementation.  Defaults to DryRunExecutor.
    db_path
        SQLite database path (defaults to db/calendar_bot.db).
    log_dir
        Directory for the rotating log file.
    portfolio
        Optional PortfolioTracker.  When supplied, a portfolio snapshot is
        logged after every scan cycle.
    """

    def __init__(
        self,
        cache: ChainCache,
        portfolio_value: float,
        executor: Any | None = None,
        db_path: Path | None = None,
        log_dir: str | Path = "logs",
        portfolio: PortfolioTracker | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._cache = cache
        self._log_dir = log_dir
        self._notifier = notifier
        self._engine = DecisionEngine(
            cache=cache,
            portfolio_value=portfolio_value,
            executor=executor,
            db_path=db_path,
            portfolio=portfolio,
            notifier=notifier,
        )
        self._scheduler = AsyncIOScheduler()
        self._stop_event = asyncio.Event()
        self._running = False
        # Serializes scan and monitor ticks against each other (Phase 27d).
        # Offloading the ticks to worker threads means the two jobs could
        # otherwise overlap and mutate engine state concurrently; today's
        # behaviour is one tick at a time, and this preserves it.
        self._tick_lock = asyncio.Lock()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def engine(self) -> DecisionEngine:
        """Direct access to the underlying DecisionEngine (useful for tests)."""
        return self._engine

    @property
    def portfolio_value(self) -> float:
        return self._engine.portfolio_value

    @portfolio_value.setter
    def portfolio_value(self, value: float) -> None:
        self._engine.portfolio_value = value

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Start the scheduler and block until SIGINT/SIGTERM or stop() is called.

        Sets up logging, wires signal handlers, starts APScheduler, then waits.
        """
        configure_logging(log_dir=self._log_dir)

        self._stop_event.clear()
        self._running = True

        # Wire OS signals (SIGINT = Ctrl-C, SIGTERM = service manager)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal, sig)
            except (NotImplementedError, OSError):
                # Windows doesn't support add_signal_handler for all signals
                signal.signal(sig, lambda s, f: asyncio.create_task(self._async_stop()))

        # Register scheduler jobs
        self._scheduler.add_job(
            self._scan_job,
            "interval",
            seconds=config.SCAN_INTERVAL_SEC,
            id="scan",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.add_job(
            self._monitor_job,
            "interval",
            seconds=config.MONITOR_INTERVAL_SEC,
            id="monitor",
            max_instances=1,
            coalesce=True,
        )

        self._scheduler.start()

        logger.info(
            "BotLoop started  mode=%s  scan_interval=%ds  monitor_interval=%ds",
            config.TRADING_MODE,
            config.SCAN_INTERVAL_SEC,
            config.MONITOR_INTERVAL_SEC,
        )

        await self._stop_event.wait()
        await self._shutdown()

    async def stop(self) -> None:
        """Signal the loop to stop gracefully."""
        self._stop_event.set()

    # ── Signal handling ───────────────────────────────────────────────────────

    def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info("Signal %s received — shutting down", sig.name)
        self._stop_event.set()

    async def _async_stop(self) -> None:
        self._stop_event.set()

    async def _shutdown(self) -> None:
        logger.info("Stopping scheduler…")
        self._scheduler.shutdown(wait=False)
        self._running = False
        logger.info("BotLoop stopped.")

    # ── Tick dispatch ─────────────────────────────────────────────────────────

    async def _run_tick(self, name: str, tick):
        """
        Run one synchronous engine tick without blocking the event loop.

        ``scan_tick``/``monitor_tick`` are synchronous and can take tens of
        seconds — a failed entry blocks for the full order timeout.  Awaiting
        them inline froze the single event loop shared with the WebSocket feed
        and the Telegram listener, so the chain cache aged past its TTL and the
        next monitor tick was missed (Phase 27d).  Running the tick in a worker
        thread keeps the loop free; ``_tick_lock`` keeps the two ticks serialized
        so engine state is still only ever touched by one of them at a time.

        Set ``config.TICK_OFFLOAD_ENABLED = False`` to restore inline execution.

        Returns whatever the tick returns.
        """
        if not config.TICK_OFFLOAD_ENABLED:
            return tick()

        waited_at = time.monotonic()
        async with self._tick_lock:
            waited = time.monotonic() - waited_at
            if waited > 1.0:
                logger.info(
                    "%s waited %.1fs for the previous tick to finish", name, waited,
                )
            started = time.monotonic()
            try:
                return await asyncio.to_thread(tick)
            finally:
                elapsed = time.monotonic() - started
                warn_after = config.TICK_SLOW_WARN_SEC
                if warn_after > 0 and elapsed > warn_after:
                    logger.warning(
                        "%s took %.1fs (> TICK_SLOW_WARN_SEC %gs) — the event loop "
                        "stayed responsive, but this tick delayed the next one",
                        name, elapsed, warn_after,
                    )
                else:
                    logger.debug("%s took %.1fs", name, elapsed)

    # ── Scheduler jobs ────────────────────────────────────────────────────────

    async def _scan_job(self) -> None:
        """APScheduler job: run one scan cycle."""
        if self._engine.state is BotState.HALTED:
            logger.warning("scan_job skipped — engine is HALTED")
            return
        try:
            status = await self._run_tick("scan_tick", self._engine.scan_tick)
            logger.info(
                "scan_job complete  state=%s  open=%d  daily_pnl=%.2f  "
                "fees_paid_today=%.2f  msg=%s",
                status.state.value,
                status.open_positions,
                status.daily_pnl,
                self._engine.fees_paid_today,
                status.message,
            )
            if self._engine.portfolio is not None:
                logger.info(self._engine.portfolio.portfolio_view())
        except Exception as exc:
            logger.exception("scan_job raised an unexpected error")
            if self._notifier:
                try:
                    self._notifier.notify_error("scan_job", exc)
                except Exception:
                    pass

    async def _monitor_job(self) -> None:
        """APScheduler job: run one monitor cycle."""
        if self._engine.state is BotState.HALTED:
            logger.warning("monitor_job skipped — engine is HALTED")
            return
        try:
            status = await self._run_tick("monitor_tick", self._engine.monitor_tick)
            logger.info(
                "monitor_job complete  state=%s  open=%d  daily_pnl=%.2f  "
                "fees_paid_today=%.2f  msg=%s",
                status.state.value,
                status.open_positions,
                status.daily_pnl,
                self._engine.fees_paid_today,
                status.message,
            )
        except Exception as exc:
            logger.exception("monitor_job raised an unexpected error")
            if self._notifier:
                try:
                    self._notifier.notify_error("monitor_job", exc)
                except Exception:
                    pass
