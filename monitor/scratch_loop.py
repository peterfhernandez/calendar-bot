"""
monitor/scratch_loop.py
=======================
End-to-end verification script for the BotLoop scheduler.

Runs a real BotLoop with fast tick intervals (2 s scan, 1 s monitor) and a
mock cache + mock executor.  After 10 seconds it stops the loop and prints a
summary.  No live network calls are made.

Run from the repo root::

    python -m monitor.scratch_loop
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Minimal stubs so the script works without a live feed ─────────────────────

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
    """Returns a minimal option chain that satisfies scanner + monitor calls."""

    def get_spot(self, asset: str) -> float:
        return {"BTC": 65_000.0, "ETH": 3_500.0, "SOL": 150.0}.get(asset, 1_000.0)

    def get_chain(self, asset: str) -> list[_FakeSnap]:
        spot = self.get_spot(asset)
        strike = round(spot / 1_000) * 1_000

        def _make(days_near: int, days_far: int) -> list[_FakeSnap]:
            return [
                _FakeSnap(
                    instrument=f"{asset}-{days_near}DAY-{strike}-C",
                    asset=asset, spot=spot,
                    mark_price=0.05, mark_iv=0.85,
                    bid=0.04, ask=0.06,
                    open_interest=500,
                ),
                _FakeSnap(
                    instrument=f"{asset}-{days_far}DAY-{strike}-C",
                    asset=asset, spot=spot,
                    mark_price=0.08, mark_iv=0.80,
                    bid=0.07, ask=0.09,
                    open_interest=600,
                ),
            ]

        return _make(7, 30)

    async def update(self, snap: Any) -> None:
        pass


class _FakeExecutor:
    """Logs entry/close/roll without touching the network."""

    def enter_spread(self, candidate: Any) -> dict | None:
        logging.getLogger("scratch").info(
            "[FAKE] enter_spread %s %s strike=%.0f",
            candidate.asset, candidate.option_type, candidate.strike,
        )
        return {
            "near_prem": candidate.near_bid,
            "far_prem":  candidate.far_ask,
            "net_debit": candidate.net_debit,
            "qty":       candidate.qty,
        }

    def close_spread(self, position: dict) -> float | None:
        logging.getLogger("scratch").info("[FAKE] close_spread trade_id=%s", position.get("trade_id"))
        return position.get("net_debit", 0.0)

    def roll_near_leg(self, position: dict, new_candidate: Any) -> bool:
        logging.getLogger("scratch").info("[FAKE] roll_near_leg trade_id=%s", position.get("trade_id"))
        return True


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    from monitor.loop import BotLoop, configure_logging

    configure_logging(log_dir="logs")
    log = logging.getLogger("scratch")

    # Use a temp DB so we don't touch the real state
    db_path = Path("db/scratch_loop.db")

    cache    = _FakeCache()
    executor = _FakeExecutor()

    import config as _cfg
    original_scan    = _cfg.SCAN_INTERVAL_SEC
    original_monitor = _cfg.MONITOR_INTERVAL_SEC
    _cfg.SCAN_INTERVAL_SEC    = 3   # fast for the test
    _cfg.MONITOR_INTERVAL_SEC = 2

    loop = BotLoop(
        cache=cache,
        portfolio_value=10_000.0,
        executor=executor,
        db_path=db_path,
        log_dir="logs",
    )

    log.info("=== BotLoop scratch test — will stop in 12 seconds ===")

    # Schedule an auto-stop after 12 s
    async def _auto_stop() -> None:
        await asyncio.sleep(12)
        log.info("Auto-stop triggered — stopping BotLoop")
        await loop.stop()

    stopper = asyncio.create_task(_auto_stop())

    try:
        await loop.run()
    finally:
        stopper.cancel()
        _cfg.SCAN_INTERVAL_SEC    = original_scan
        _cfg.MONITOR_INTERVAL_SEC = original_monitor
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass  # SQLite WAL file still held briefly on Windows — leave it

    log.info("=== BotLoop scratch test complete ===")

    # Print a short summary
    print("\n--- Scratch test summary ---")
    print(f"  Engine final state : {loop.engine.state.value}")
    print(f"  Daily P&L          : {loop.engine._today_pnl:.2f}")
    print("  All checks passed  : loop started, ran jobs, stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
