"""
scratch/scratch_tick_offload.py
================================
Offline demonstration of the Phase 27d event-loop fix.

Symptom (2026-07-27 test-mode run): two failed entries each blocked for the full
order timeout, and because ``scan_tick()`` was awaited inline from the
scheduler's async job, they blocked the **one** event loop that also runs the
WebSocket feed and the Telegram listener:

    Run time of job "BotLoop._monitor_job" was missed by 0:00:46.679841
    106 stale instrument(s) excluded from ETH chain

For 47–57 seconds the feed stopped pumping (so the chain cache aged past its
30s TTL) and a whole monitor tick never ran — while trade_id=22 sat at 63% of
debit against a 50% hard stop.

``scan_tick``/``monitor_tick`` are synchronous by design, so the fix is to
dispatch them to a worker thread (``asyncio.to_thread``) instead of awaiting them
inline.  A lock keeps the two ticks serialized against each other, because engine
state (failure counters, ``_just_entered``, the pnl accumulators) is not safe for
two concurrent ticks.

What this demonstrates, all against a fake cache/executor:

1. A 1.5s blocking tick starves the loop completely when run inline (the old
   behaviour, still available via ``TICK_OFFLOAD_ENABLED = False``), and leaves
   it fully responsive when offloaded.
2. The tick body really does run off the loop thread.
3. Scan and monitor ticks still never overlap.
4. A slow tick now says so at WARNING instead of passing unnoticed.
5. Executor calls made from the loop thread — the pattern that caused this —
   now log a loud warning naming the consequence.

No network, no live orders, no real feed.

Run from the repo root:
    python -m scratch.scratch_tick_offload

Aborts if TRADING_MODE == "live".
"""

import asyncio
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from monitor.loop import BotLoop
from strategy.decision import BotState, EngineStatus

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _FakeCache:
    """Minimal ChainCache stand-in — the engine ticks are mocked out anyway."""

    def get_spot(self, asset: str) -> float:
        return 65_000.0

    def get_chain(self, asset: str) -> list:
        return []

    def get(self, instrument: str):
        return None


class _FakeExecutor:
    def enter_spread(self, candidate):
        return None

    def close_spread(self, position):
        return None

    def roll_near_leg(self, position, new_candidate):
        return True


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _status() -> EngineStatus:
    return EngineStatus(
        state=BotState.IDLE, open_positions=0, daily_pnl=0.0, message="ok",
    )


def _make_loop(tmp: Path) -> BotLoop:
    return BotLoop(
        cache=_FakeCache(),
        portfolio_value=10_000.0,
        executor=_FakeExecutor(),
        db_path=tmp / "scratch_tick_offload.db",
        log_dir=str(tmp / "logs"),
    )


def _slow_tick(seconds: float):
    """A tick that blocks the way a failed entry does, then returns a status."""
    def _tick():
        time.sleep(seconds)
        return _status()
    return _tick


# ── 1. Loop responsiveness during a slow tick ─────────────────────────────────

async def _count_heartbeats_during_scan(loop: BotLoop, offload: bool,
                                        block_sec: float) -> int:
    """
    Run one scan job whose tick blocks for *block_sec*, counting how many times a
    concurrent 10ms coroutine got to run.  That coroutine stands in for the WS
    feed's read loop and the Telegram listener's poll.
    """
    beats = 0

    async def _heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1

    task = asyncio.create_task(_heartbeat())
    try:
        with patch.object(config, "TICK_OFFLOAD_ENABLED", offload), \
             patch.object(loop.engine, "scan_tick", side_effect=_slow_tick(block_sec)):
            await loop._scan_job()
    finally:
        task.cancel()
    return beats


async def demo_loop_stays_responsive(tmp: Path) -> None:
    print("\n1. Event-loop responsiveness during a 1.5s blocking tick")
    print("   (a real failed entry blocked for 47–57s)")

    block = 1.5
    ideal = int(block / 0.01)

    inline = await _count_heartbeats_during_scan(_make_loop(tmp), False, block)
    print(f"\n   TICK_OFFLOAD_ENABLED=False (old behaviour, inline):")
    print(f"     other loop tasks ran {inline:4d} times "
          f"(a live loop would manage ~{ideal})")
    check("inline tick starves the event loop", inline <= 1)

    offloaded = await _count_heartbeats_during_scan(_make_loop(tmp), True, block)
    print(f"\n   TICK_OFFLOAD_ENABLED=True (fixed, worker thread):")
    print(f"     other loop tasks ran {offloaded:4d} times")
    check("offloaded tick leaves the loop responsive", offloaded > ideal * 0.5)
    check("offloading is a large improvement, not a marginal one",
          offloaded > inline * 10)
    print("\n   → the feed keeps pumping, so the chain cache never goes stale "
          "and\n     the monitor tick is not missed.")


# ── 2. The tick genuinely leaves the loop thread ───────────────────────────────

async def demo_runs_off_loop_thread(tmp: Path) -> None:
    print("\n2. Where the tick body actually executes")

    loop = _make_loop(tmp)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _record():
        seen.append(threading.get_ident())
        return _status()

    with patch.object(config, "TICK_OFFLOAD_ENABLED", True), \
         patch.object(loop.engine, "monitor_tick", side_effect=_record):
        await loop._monitor_job()

    print(f"   event loop thread : {loop_thread}")
    print(f"   tick ran on thread: {seen[0] if seen else 'never ran'}")
    check("offloaded tick runs on a worker thread", bool(seen) and seen[0] != loop_thread)

    seen.clear()
    with patch.object(config, "TICK_OFFLOAD_ENABLED", False), \
         patch.object(loop.engine, "monitor_tick", side_effect=_record):
        await loop._monitor_job()
    check("TICK_OFFLOAD_ENABLED=False still runs inline (escape hatch works)",
          seen == [loop_thread])


# ── 3. Ticks stay serialized ───────────────────────────────────────────────────

async def demo_ticks_do_not_overlap(tmp: Path) -> None:
    print("\n3. Scan and monitor ticks fired together")

    loop = _make_loop(tmp)
    live = 0
    peak = 0

    def _tick():
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        time.sleep(0.2)
        live -= 1
        return _status()

    started = time.monotonic()
    with patch.object(config, "TICK_OFFLOAD_ENABLED", True), \
         patch.object(loop.engine, "scan_tick", side_effect=_tick), \
         patch.object(loop.engine, "monitor_tick", side_effect=_tick):
        await asyncio.gather(loop._scan_job(), loop._monitor_job())
    elapsed = time.monotonic() - started

    print(f"   peak concurrent ticks: {peak}   total elapsed: {elapsed:.2f}s "
          f"(2 × 0.2s serialized)")
    check("the two ticks never run concurrently", peak == 1)
    check("both ticks did run (serialized, not dropped)", elapsed >= 0.4)
    print("   → engine state is still only ever touched by one tick at a time.")


# ── 4. A slow tick is visible in the log ───────────────────────────────────────

async def demo_slow_tick_is_reported(tmp: Path, collector: _Collector) -> None:
    print("\n4. Slow-tick visibility")

    loop = _make_loop(tmp)
    collector.records.clear()

    with patch.object(config, "TICK_OFFLOAD_ENABLED", True), \
         patch.object(config, "TICK_SLOW_WARN_SEC", 0.2), \
         patch.object(loop.engine, "scan_tick", side_effect=_slow_tick(0.5)):
        await loop._scan_job()

    warnings = [r for r in collector.records if r.levelno >= logging.WARNING]
    for r in warnings:
        print(f"   WARNING  {r.getMessage()}")
    check("a tick over TICK_SLOW_WARN_SEC logs a WARNING",
          any("TICK_SLOW_WARN_SEC" in r.getMessage() for r in warnings))

    collector.records.clear()
    with patch.object(config, "TICK_OFFLOAD_ENABLED", True), \
         patch.object(config, "TICK_SLOW_WARN_SEC", 5.0), \
         patch.object(loop.engine, "scan_tick", return_value=_status()):
        await loop._scan_job()
    check("a normal-speed tick stays quiet",
          not any("TICK_SLOW_WARN_SEC" in r.getMessage()
                  for r in collector.records if r.levelno >= logging.WARNING))


# ── 5. Executor calls from the loop thread are no longer silent ────────────────

async def demo_executor_warns_when_called_on_the_loop() -> None:
    print("\n5. Executor invoked from the loop thread (the pattern that caused this)")

    from execution.executor import CalendarExecutor

    collector = _Collector()
    exec_logger = logging.getLogger("execution.executor")
    exec_logger.addHandler(collector)
    previous = exec_logger.level
    exec_logger.setLevel(logging.DEBUG)

    executor = CalendarExecutor(client_id="x", client_secret="y")

    async def _noop():
        return "done"

    try:
        # Called from inside this coroutine — i.e. on the event loop thread.
        result = executor._run(_noop())
    finally:
        exec_logger.removeHandler(collector)
        exec_logger.setLevel(previous)

    warnings = [r for r in collector.records if r.levelno >= logging.WARNING]
    for r in warnings:
        print(f"   WARNING  {r.getMessage().splitlines()[0]}")
    check("the call still completes (behaviour unchanged)", result == "done")
    check("blocking the loop from the executor now logs a WARNING",
          any("running event loop" in r.getMessage() for r in warnings))
    print("   → with ticks offloaded this branch should never be reached; if it "
          "is,\n     the log now names it instead of stalling silently.")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 72)
    print("  Phase 27d — engine ticks off the shared event loop (offline demo)")
    print("=" * 72)

    collector = _Collector()
    loop_logger = logging.getLogger("monitor.loop")
    loop_logger.addHandler(collector)
    loop_logger.setLevel(logging.DEBUG)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        await demo_loop_stays_responsive(tmp)
        await demo_runs_off_loop_thread(tmp)
        await demo_ticks_do_not_overlap(tmp)
        await demo_slow_tick_is_reported(tmp, collector)
        await demo_executor_warns_when_called_on_the_loop()

    loop_logger.removeHandler(collector)

    print("\n" + "=" * 72)
    failed = [lbl for lbl, r in results if r == FAIL]
    if failed:
        print(f"  {len(failed)} CHECK(S) FAILED:")
        for lbl in failed:
            print(f"    ✗ {lbl}")
        sys.exit(1)
    print(f"  ALL {len(results)} CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
