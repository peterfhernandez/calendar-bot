"""
scratch/scratch_wait_for_fill.py
=================================
Offline demonstration of the Phase 27b fill-wait fixes.

`_wait_for_fill` had two defects, both visible in the 2026-07-27 test-mode run:

1. **All-or-nothing fill detection.**  Only ``order_state == "filled"`` counted.
   Order ``ETH-82752446337`` reported ``8.0000`` of 8 filled — a *complete* fill —
   yet was declared a timeout, unwound at a crossed price, and triggered a
   FLATTEN-NEAR against a correctly-filled near leg.

2. **A blind window before the deadline.**  The poll interval backs off
   (1.0s → ×1.5, capped at 5.0s) and the old loop slept the full interval
   regardless of the time remaining, so on a 30s timeout the last state
   observation landed at t≈28.1s while the loop did not give up until t≈33.1s.
   The logged 06:17:58 → 06:18:32 gap (34s, not the 30s the message claimed)
   matches exactly.

Both are fixed: completion is now detected by quantity as well as by state, the
sleep is clamped to the remaining time so a poll always lands *at* the deadline,
and the timeout message reports true elapsed time plus fill progress.

No network, no live orders — runs against an in-memory async mock.

Run from the repo root:
    python -m scratch.scratch_wait_for_fill

Aborts if TRADING_MODE == "live".
"""

import asyncio
import sys
import time
from unittest.mock import AsyncMock

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from execution.executor import OrderTimeoutError, _wait_for_fill

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


def _client(states: list[dict]) -> AsyncMock:
    client = AsyncMock()
    client.get_order_state.side_effect = list(states)
    return client


async def demo_complete_fill_with_lagging_state() -> None:
    print("\n1. The logged ETH-82752446337 case: 8.0000/8 filled, order_state='open'")
    client = _client([
        {"order_id": "ETH-82752446337", "order_state": "open",
         "amount": 8.0, "filled_amount": 8.0, "average_price": 0.0061},
    ])
    try:
        state = await _wait_for_fill(client, "ETH-82752446337", timeout=5, amount=8.0)
        check("a fully-filled order is no longer declared a timeout", True)
        check("the real fill state is returned to the caller",
              state.get("average_price") == 0.0061)
    except OrderTimeoutError as exc:
        check(f"a fully-filled order is no longer declared a timeout ({exc})", False)
        check("the real fill state is returned to the caller", False)

    print("     → old behaviour: OrderTimeoutError → unwind at a crossed price +")
    print("       a FLATTEN-NEAR fired against a correctly-filled near leg")


async def demo_genuine_partial_still_times_out() -> None:
    print("\n2. A genuine partial fill still times out — and now says how much filled")
    client = AsyncMock()
    client.get_order_state.return_value = {
        "order_id": "e2", "order_state": "open", "amount": 8.0, "filled_amount": 3.0,
    }
    try:
        await _wait_for_fill(client, "e2", timeout=1, amount=8.0)
        check("a 3-of-8 partial fill is not mistaken for a complete fill", False)
    except OrderTimeoutError as exc:
        check("a 3-of-8 partial fill is not mistaken for a complete fill", True)
        check("the exception carries the filled amount", exc.filled_amount == 3.0)
        check("the exception carries the ordered amount", exc.amount == 8.0)
        check("the message names the fill progress", "3.0000/8.0000" in str(exc))
        print(f"     → {exc}")


async def demo_final_poll_at_deadline() -> None:
    print("\n3. A fill landing in the old blind window is now seen")
    client = _client([
        {"order_id": "e3", "order_state": "open",   "amount": 8.0, "filled_amount": 0.0},
        {"order_id": "e3", "order_state": "filled", "amount": 8.0, "filled_amount": 8.0},
    ])
    # The fill is only reported on the *second* poll.  The old loop slept the full
    # 1.0s backoff and exited without re-polling, so it never saw it.
    try:
        state = await _wait_for_fill(client, "e3", timeout=0.05, amount=8.0)
        check("a poll happens at the deadline before giving up",
              client.get_order_state.await_count == 2)
        check("the late fill is returned rather than raised as a timeout",
              state.get("order_state") == "filled")
    except OrderTimeoutError:
        check("a poll happens at the deadline before giving up", False)
        check("the late fill is returned rather than raised as a timeout", False)


async def demo_elapsed_is_truthful() -> None:
    print("\n4. Sleeps are clamped to the deadline; elapsed time is reported honestly")
    client = AsyncMock()
    client.get_order_state.return_value = {
        "order_id": "e4", "order_state": "open", "amount": 8.0, "filled_amount": 0.0,
    }
    started = time.monotonic()
    try:
        await _wait_for_fill(client, "e4", timeout=2, amount=8.0)
        check("a never-filling order times out", False)
    except OrderTimeoutError as exc:
        wall = time.monotonic() - started
        check("a never-filling order times out", True)
        # Old schedule: poll t=0, sleep 1.0, poll t=1.0, sleep 1.5 → exit t≈2.5s.
        check(f"wall time {wall:.2f}s does not overshoot the 2s timeout", wall < 2.4)
        check(f"reported elapsed {exc.elapsed_sec:.2f}s matches wall time",
              abs(exc.elapsed_sec - wall) < 0.1)
        print(f"     → {exc}")


async def main() -> None:
    print("=" * 72)
    print("  Phase 27b — partial-fill-aware _wait_for_fill (offline demo)")
    print("=" * 72)
    await demo_complete_fill_with_lagging_state()
    await demo_genuine_partial_still_times_out()
    await demo_final_poll_at_deadline()
    await demo_elapsed_is_truthful()

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
