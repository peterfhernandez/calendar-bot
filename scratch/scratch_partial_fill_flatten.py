"""
scratch/scratch_partial_fill_flatten.py
========================================
Offline demonstration of the Phase 26a partial-fill-aware cancel.

Cancelling a partially-filled Deribit limit order only removes the *unfilled*
remainder — the already-filled contracts stay on the exchange.  The old
timeout-cancel paths ignored this, leaving untracked naked inventory (the
2026-07 test run left −13 naked short puts from three timed-out legged entries).

`execution/executor._cancel_and_flatten` now reads the filled portion after the
cancel and submits an immediate reverse order for exactly that amount, and marks
the order CANCELLED_PARTIAL so the exposure is never invisible.  A flatten
failure is logged CRITICAL and fires a one-shot operator alert.

No network, no live orders — runs against an in-memory async mock.

Run from the repo root:
    python -m scratch.scratch_partial_fill_flatten

Aborts if TRADING_MODE == "live".
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from execution.executor import _cancel_and_flatten
from execution.order_manager import OrderManager, OrderState, TrackedOrder

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


async def demo_partial_sell_flattened() -> None:
    print("\n1. A timed-out near-leg SELL that partially filled is flattened by a BUY")
    client = AsyncMock()
    client.cancel_order.return_value = {"order_id": "n1", "filled_amount": 6.0}
    client.place_order.return_value = {"order": {"order_id": "flat-1"}}
    mgr = OrderManager()
    mgr.track(TrackedOrder(
        order_id="n1", instrument="ETH-24JUL26-1750-P",
        direction="sell", amount=7.0, limit_price=0.002,
    ))

    filled = await _cancel_and_flatten(
        client, mgr, "n1", "ETH-24JUL26-1750-P",
        orig_direction="sell", reverse_price=0.003, asset="ETH",
    )
    args, _ = client.place_order.call_args
    tracked = mgr.get("n1")
    check("6.0 filled contracts detected", filled == 6.0)
    check("reverse order is a BUY (flattens the short)", args[1] == "buy")
    check("reverse order amount == filled amount", args[2] == 6.0)
    check("order marked CANCELLED_PARTIAL", tracked.state == OrderState.CANCELLED_PARTIAL)
    check("filled_amount recorded on the order", tracked.filled_amount == 6.0)


async def demo_no_partial() -> None:
    print("\n2. A clean timeout-cancel (nothing filled) submits no reverse order")
    client = AsyncMock()
    client.cancel_order.return_value = {"order_id": "n2", "filled_amount": 0.0}
    client.get_order_state.return_value = {"filled_amount": 0.0}
    mgr = OrderManager()
    mgr.track(TrackedOrder(
        order_id="n2", instrument="BTC-24JUL26-64000-C",
        direction="sell", amount=0.3, limit_price=0.002,
    ))
    filled = await _cancel_and_flatten(
        client, mgr, "n2", "BTC-24JUL26-64000-C",
        orig_direction="sell", reverse_price=0.003, asset="BTC",
    )
    check("no fill detected", filled == 0.0)
    check("no reverse order placed", not client.place_order.called)
    check("order marked plain CANCELLED", mgr.get("n2").state == OrderState.CANCELLED)


async def demo_flatten_failure_alerts() -> None:
    print("\n3. A failed flatten logs CRITICAL and fires a one-shot operator alert")
    client = AsyncMock()
    client.cancel_order.return_value = {"filled_amount": 5.0}
    client.place_order.side_effect = RuntimeError("exchange unreachable")
    notifier = MagicMock()
    mgr = OrderManager()
    mgr.track(TrackedOrder(
        order_id="f1", instrument="ETH-24JUL26-1750-P",
        direction="buy", amount=5.0, limit_price=0.006,
    ))
    filled = await _cancel_and_flatten(
        client, mgr, "f1", "ETH-24JUL26-1750-P",
        orig_direction="buy", reverse_price=0.005, asset="ETH", notifier=notifier,
    )
    check("5.0 filled contracts detected", filled == 5.0)
    check("operator alert fired exactly once", notifier.notify_warning.call_count == 1)


async def main() -> None:
    print("=" * 68)
    print("  Phase 26a — partial-fill-aware cancel (offline demo)")
    print("=" * 68)
    await demo_partial_sell_flattened()
    await demo_no_partial()
    await demo_flatten_failure_alerts()

    print("\n" + "=" * 68)
    failed = [lbl for lbl, r in results if r == FAIL]
    if failed:
        print(f"  {len(failed)} CHECK(S) FAILED:")
        for lbl in failed:
            print(f"    ✗ {lbl}")
        sys.exit(1)
    print(f"  ALL {len(results)} CHECKS PASSED")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(main())
