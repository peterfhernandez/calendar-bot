"""
scratch_amount_validation.py
============================
Debug script — Phase 25a order-amount validation.

In the 2026-07 test run every ETH entry was rejected by Deribit with "-32602
Invalid params" because the executor floored every order amount to 0.1 — valid
for BTC options (minimum 0.1) but invalid for ETH options (minimum 1, integer
steps).  Phase 25a clamps the sizer-approved qty to each instrument's live
``min_trade_amount`` / ``contract_size`` and skips it if it falls below the
minimum.

This script:

1. Prints the static per-asset fallback minimums from config.
2. Demonstrates the clamp/skip decision (``_clamp_amount_to_step``) for a range
   of requested amounts on BTC and ETH — showing why 0.1 passes for BTC and is
   rejected for ETH.
3. In test mode (with credentials), fetches a real BTC and ETH option
   instrument's live ``min_trade_amount`` / ``contract_size`` to confirm the
   static fallbacks match the exchange.

No orders are placed.  Aborts in live mode.

Run from the repo root:
    python -m scratch.scratch_amount_validation
"""

from __future__ import annotations

import sys

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode. Aborting.")
    sys.exit(1)

from execution.executor import _clamp_amount_to_step


def _demo_asset(asset: str) -> None:
    min_amt, step = config.DEFAULT_MIN_TRADE_AMOUNTS.get(
        asset, config.DEFAULT_MIN_TRADE_AMOUNT
    )
    print(f"\n{asset}: static fallback min={min_amt}, step={step}")
    for req in (0.1, 0.3, 0.9, 1.0, 1.5, 9.5):
        clamped = _clamp_amount_to_step(req, min_amt, step)
        verdict = f"→ submit {clamped}" if clamped is not None else "→ SKIP (below minimum)"
        print(f"    requested {req:>4}  {verdict}")


def main() -> None:
    print("─" * 60)
    print("  Static per-asset amount minimums (config)")
    print("─" * 60)
    for asset, (min_amt, step) in config.DEFAULT_MIN_TRADE_AMOUNTS.items():
        print(f"  {asset}: min_trade_amount={min_amt}, step={step}")

    print()
    print("─" * 60)
    print("  Clamp/skip decisions")
    print("─" * 60)
    for asset in ("BTC", "ETH"):
        _demo_asset(asset)

    print()
    print("─" * 60)
    print("  Live instrument minimums (test mode only)")
    print("─" * 60)
    if config.TRADING_MODE == "paper" or not config.DERIBIT_CLIENT_ID:
        print("  (skipped — paper mode or no credentials)")
        print("\nDone.")
        return

    import asyncio
    from execution.executor import _DeribitRPCClient

    # Representative instruments; adjust the expiry to a currently-listed one if
    # these have expired on the test exchange.
    samples = {
        "BTC": f"BTC-{_nearest_expiry_label()}-100000-C",
        "ETH": f"ETH-{_nearest_expiry_label()}-3000-C",
    }

    async def _probe() -> None:
        async with _DeribitRPCClient(
            config.DERIBIT_CLIENT_ID, config.DERIBIT_CLIENT_SECRET
        ) as client:
            for asset, instr in samples.items():
                info = await client._fetch_amount_info(instr)
                if info is None:
                    print(f"  {asset}: {instr} — could not fetch (may be delisted)")
                else:
                    print(f"  {asset}: {instr} — min={info[0]}, step={info[1]}")

    try:
        asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001 — read-only diagnostic
        print(f"  live probe failed: {exc}")

    print("\nDone.")


def _nearest_expiry_label() -> str:
    """A near-ish Deribit expiry label (best-effort; adjust if delisted)."""
    from datetime import date, timedelta

    d = date.today() + timedelta(days=7)
    return d.strftime("%-d%b%y").upper()


if __name__ == "__main__":
    main()
