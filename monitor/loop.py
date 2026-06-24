"""
monitor/loop.py
===============
Scheduler and monitoring loop for the calendar spread bot.

Sets up two APScheduler jobs:
  - scan_job  — runs DecisionEngine.scan_tick()  every SCAN_INTERVAL_SEC
  - monitor_job — runs DecisionEngine.monitor_tick() every MONITOR_INTERVAL_SEC

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
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from alerts.notifier import Notifier
from data.chain_cache import ChainCache
from portfolio.tracker import PortfolioTracker
from strategy.decision import BotState, DecisionEngine

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_LOG_DATE   = "%Y-%m-%d %H:%M:%S"

_logging_configured = False  # guard against double-init


# ── Logging setup ─────────────────────────────────────────────────────────────

def configure_logging(
    log_dir: str | Path = "logs",
    level: int = logging.INFO,
) -> None:
    """
    Configure root logger with a console handler and a rotating file handler.

    Safe to call multiple times — extra calls are no-ops.
    """
    global _logging_configured
    if _logging_configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE)

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    root.addHandler(ch)

    # Rotating file
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_path / "bot.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

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

    # ── Scheduler jobs ────────────────────────────────────────────────────────

    async def _scan_job(self) -> None:
        """APScheduler job: run one scan cycle."""
        if self._engine.state is BotState.HALTED:
            logger.warning("scan_job skipped — engine is HALTED")
            return
        try:
            status = self._engine.scan_tick()
            logger.info(
                "scan_job complete  state=%s  open=%d  daily_pnl=%.2f  msg=%s",
                status.state.value,
                status.open_positions,
                status.daily_pnl,
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
            status = self._engine.monitor_tick()
            logger.info(
                "monitor_job complete  state=%s  open=%d  daily_pnl=%.2f  msg=%s",
                status.state.value,
                status.open_positions,
                status.daily_pnl,
                status.message,
            )
        except Exception as exc:
            logger.exception("monitor_job raised an unexpected error")
            if self._notifier:
                try:
                    self._notifier.notify_error("monitor_job", exc)
                except Exception:
                    pass
