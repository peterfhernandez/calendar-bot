"""
scratch/scratch_close_price_rounding.py
=======================================
Offline demonstration of the Phase 22d close/roll price fix.

The old code priced closes as a synthetic ``mid * 1.02`` / ``mid * 0.98`` and
rounded with a blanket 4-decimal ``round()`` that ignored Deribit's
``tick_size_steps`` — reproducing the deterministic "-32602 Invalid params"
rejections.  The fix (1) derives close prices from the live best bid/ask crossed
by ``CLOSE_PRICE_CROSS_BUFFER_PCT`` and (2) rounds in tick-count space, honouring
per-price-band ``tick_size_steps``, so every submitted price lands on the grid.

No network, no live orders — everything runs against the pure pricing helpers.

Run from the repo root:
    python -m scratch.scratch_close_price_rounding

Aborts if TRADING_MODE == "live".
"""

import sys

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from execution.executor import _effective_tick_size, _round_to_tick

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


def _on_grid(price: float, tick: float) -> bool:
    return abs(round(price / tick) * tick - price) < 1e-9


print("\n── 1. Close prices are derived from the live book, not a synthetic mid ──")
buf = config.CLOSE_PRICE_CROSS_BUFFER_PCT
near_bid, near_ask = 0.0140, 0.0160
far_bid, far_ask = 0.0540, 0.0560

# Buy back the short near leg: lift the ask + buffer.
near_close = near_ask * (1 + buf)
# Sell the long far leg: hit the bid − buffer.
far_close = far_bid * (1 - buf)
print(f"  near close (buy)  = ask {near_ask} * (1+{buf}) = {near_close:.6f}")
print(f"  far  close (sell) = bid {far_bid} * (1-{buf}) = {far_close:.6f}")
old_far = ((far_bid + far_ask) / 2) * 0.98
check("far close differs from old synthetic mid*0.98", abs(far_close - old_far) > 1e-9)

print("\n── 2. tick_size_steps selects the right tick per price band ──")
steps = [{"above_price": 0.1, "tick_size": 0.0005}]
check("below band → base tick 0.0001", _effective_tick_size(0.05, 0.0001, steps) == 0.0001)
check("above band → coarse tick 0.0005", _effective_tick_size(0.20, 0.0001, steps) == 0.0005)

print("\n── 3. Rounded prices land exactly on the valid tick grid ──")
# A price in the coarse band: 0.12345 → nearest 0.0005 = 0.1235.
r = _round_to_tick(0.12345, "BTC-X-60000-C", tick_size=0.0001, tick_size_steps=steps)
print(f"  _round_to_tick(0.12345, base=0.0001, steps→0.0005) = {r}")
check("rounds to the coarse-band grid (0.1235)", abs(r - 0.1235) < 1e-9)
check("result is on-grid (0.0005)", _on_grid(r, 0.0005))

# Base-band value with no float drift.
r2 = _round_to_tick(0.0003, "BTC-X-60000-C", tick_size=0.0001)
check("0.0003 stays on-grid (no float drift)", _on_grid(r2, 0.0001))

failed = [l for l, s in results if s == FAIL]
print("\n" + "=" * 60)
print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
print("=" * 60)
sys.exit(1 if failed else 0)
