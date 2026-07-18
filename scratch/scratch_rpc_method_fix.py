"""
scratch/scratch_rpc_method_fix.py
=================================
Offline demonstration of the Deribit RPC-method fix for tick-size lookup.

The Phase 22 close/roll price fix added tick-size lookups to
``execution/executor.py`` but called the wrong Deribit endpoint:
``public/get_instruments`` (plural — the list-all-instruments-for-a-currency
endpoint, which does NOT accept ``instrument_name``) instead of
``public/get_instrument`` (singular — which accepts ``instrument_name`` and
returns a single instrument object).  Because the plural endpoint returns a
list, parsing crashed with ``'list' object has no attribute 'get'``, was
swallowed by a generic ``except``, and the code silently fell back to naive
4-decimal rounding — producing off-tick prices that Deribit rejects with
``-32602 Invalid params`` on every entry/close/roll submission.

This script mocks the two Deribit response shapes and shows that:

  1. ``get_instrument()`` now requests ``public/get_instrument`` (singular).
  2. ``_fetch_tick_info()`` parses the direct instrument object and populates
     the tick-size caches (no list-shape assumption).
  3. Feeding the OLD plural-list shape into the new parser yields no crash and
     no cache poisoning — it just returns ``(None, None)``.

No network, no live orders — everything runs against mocked RPC responses.

Run from the repo root:
    python -m scratch.scratch_rpc_method_fix

Aborts if TRADING_MODE == "live".
"""

import asyncio
import sys
from unittest.mock import AsyncMock, patch

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from execution.executor import (
    _DeribitRPCClient,
    _TICK_SIZE_CACHE,
    _TICK_STEPS_CACHE,
)

INSTR = "BTC-3JAN26-60000-C"
PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


# What test.deribit.com actually returns for public/get_instrument (singular):
SINGULAR_RESPONSE = {
    "instrument_name": INSTR,
    "tick_size": 0.0005,
    "tick_size_steps": [{"above_price": 0.1, "tick_size": 0.001}],
}

# What public/get_instruments (plural) returns — a bare list, the shape that
# broke the old parser.
PLURAL_RESPONSE = [
    {"instrument_name": INSTR, "tick_size": 0.0005},
    {"instrument_name": "BTC-3JAN26-61000-C", "tick_size": 0.0005},
]


async def main() -> None:
    print("\nDeribit RPC-method fix for tick-size lookup\n" + "=" * 60)

    # 1. get_instrument() requests the singular endpoint with instrument_name.
    print("\n[1] get_instrument() targets public/get_instrument (singular)")
    client = _DeribitRPCClient("id", "secret")
    with patch.object(client, "_rpc", new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = SINGULAR_RESPONSE
        instr = await client.get_instrument(INSTR)
        method, params = mock_rpc.call_args.args[0], mock_rpc.call_args.args[1]
        check(f"method == 'public/get_instrument' (got {method!r})",
              method == "public/get_instrument")
        check("params carry instrument_name",
              params == {"instrument_name": INSTR})
        check("returns the instrument object directly (has tick_size)",
              isinstance(instr, dict) and instr.get("tick_size") == 0.0005)

    # 2. _fetch_tick_info() parses the direct object and caches it.
    print("\n[2] _fetch_tick_info() parses the singular object shape")
    _TICK_SIZE_CACHE.pop(INSTR, None)
    _TICK_STEPS_CACHE.pop(INSTR, None)
    client = _DeribitRPCClient("id", "secret")
    with patch.object(client, "_rpc", new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = SINGULAR_RESPONSE
        tick, steps = await client._fetch_tick_info(INSTR)
        check(f"tick_size resolved (got {tick})", tick == 0.0005)
        check("tick_size_steps resolved",
              steps == [{"above_price": 0.1, "tick_size": 0.001}])
        check("tick-size cache populated", _TICK_SIZE_CACHE.get(INSTR) == 0.0005)

    # 3. The old plural-list shape no longer crashes or poisons the cache.
    print("\n[3] Old plural-list shape is handled safely (no crash)")
    _TICK_SIZE_CACHE.pop(INSTR, None)
    _TICK_STEPS_CACHE.pop(INSTR, None)
    client = _DeribitRPCClient("id", "secret")
    with patch.object(client, "_rpc", new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = PLURAL_RESPONSE  # a list — the wrong shape
        tick, steps = await client._fetch_tick_info(INSTR)
        check("returns (None, None) rather than raising", tick is None and steps is None)
        check("cache not poisoned by the list shape", INSTR not in _TICK_SIZE_CACHE)

    print("\n" + "=" * 60)
    failed = [lbl for lbl, r in results if r == FAIL]
    if failed:
        print(f"RESULT: {len(failed)} check(s) FAILED")
        for lbl in failed:
            print(f"  ✗ {lbl}")
        sys.exit(1)
    print(f"RESULT: all {len(results)} checks passed ✓")


if __name__ == "__main__":
    asyncio.run(main())
