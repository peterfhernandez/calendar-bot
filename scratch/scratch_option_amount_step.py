"""
scratch_option_amount_step.py
=============================
Debug script — Phase 27: an option's amount step is ``min_trade_amount``, not
``contract_size``.

In the 2026-07-26/27 test run every BTC entry died before reaching the exchange:

    RANK approved: BTC Put strike=64000  qty=0.2  ev=-0.0713
    AMOUNT GATE: entry aborted — amount 0.2 below exchange minimum for
                 BTC-31JUL26-64000-P

0.2 is twice the real BTC option minimum of 0.1, so the message was wrong.  The
cause was in ``_DeribitRPCClient._fetch_amount_info``:

    step = meta.get("contract_size") or min_amt        # ← wrong for options

A Deribit option reports ``contract_size = 1`` (the quantity of underlying per
contract) and ``min_trade_amount = 0.1`` for BTC.  Reading ``contract_size`` as
the amount step quantised BTC orders to whole BTC, so 0.2 floored to 0.0 and was
rejected.  ETH masked the bug entirely: there both fields are 1, so the two
readings coincide and ETH orders were unaffected — which is why every observed
LegRiskError was ETH and no BTC trade ever reached the book.

This script:

1. Replays the exact log scenario (BTC 0.2) through the old and new step
   derivations side by side, showing the old one flooring to zero.
2. Shows ETH is unchanged — demonstrating why the bug stayed hidden.
3. Confirms futures still use ``contract_size`` as the step.
4. In test mode (with credentials), fetches real BTC and ETH option metadata
   from test.deribit.com to confirm ``contract_size != min_trade_amount`` for
   BTC options on the live exchange.

No orders are placed.  Aborts in live mode.

Run from the repo root:
    python -m scratch.scratch_option_amount_step
"""

from __future__ import annotations

import asyncio
import sys

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode. Aborting.")
    sys.exit(1)

from execution.executor import (  # noqa: E402
    _clamp_amount_to_step,
    _is_option_instrument,
)

PASS = "  [OK]  "
FAIL = "  [FAIL]"

_failures = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"{PASS} {label}{('  ' + detail) if detail else ''}")
    else:
        _failures += 1
        print(f"{FAIL} {label}{('  ' + detail) if detail else ''}")


def _old_step(meta: dict) -> float:
    """The pre-fix derivation: contract_size wins over min_trade_amount."""
    return meta.get("contract_size") or meta.get("min_trade_amount")


def _new_step(instrument: str, meta: dict) -> float:
    """The fixed derivation: options step on min_trade_amount."""
    min_amt = meta.get("min_trade_amount")
    if _is_option_instrument(instrument, meta.get("kind")):
        return min_amt
    return meta.get("contract_size") or min_amt


def _replay(instrument: str, meta: dict, requested: float) -> None:
    min_amt = meta["min_trade_amount"]
    old = _clamp_amount_to_step(requested, min_amt, _old_step(meta))
    new = _clamp_amount_to_step(requested, min_amt, _new_step(instrument, meta))
    print(f"\n  {instrument}")
    print(f"    exchange metadata : min_trade_amount={min_amt}  contract_size={meta.get('contract_size')}")
    print(f"    requested qty     : {requested}")
    print(f"    old step={_old_step(meta)!s:<5} → {old if old is not None else 'REJECTED (floored to 0)'}")
    print(f"    new step={_new_step(instrument, meta)!s:<5} → {new if new is not None else 'REJECTED'}")


def main() -> None:
    print("─" * 68)
    print("  1. The logged failure: BTC option, sizer-approved qty = 0.2")
    print("─" * 68)

    btc = {"min_trade_amount": 0.1, "contract_size": 1.0, "kind": "option"}
    _replay("BTC-31JUL26-64000-P", btc, 0.2)
    _check(
        "old derivation rejected the order",
        _clamp_amount_to_step(0.2, 0.1, _old_step(btc)) is None,
    )
    _check(
        "new derivation submits 0.2",
        _clamp_amount_to_step(0.2, 0.1, _new_step("BTC-31JUL26-64000-P", btc)) == 0.2,
    )

    print("\n" + "─" * 68)
    print("  2. Why ETH hid the bug (min_trade_amount == contract_size == 1)")
    print("─" * 68)

    eth = {"min_trade_amount": 1.0, "contract_size": 1.0, "kind": "option"}
    _replay("ETH-31JUL26-2050-C", eth, 8.0)
    _check(
        "ETH behaviour is identical old vs new",
        _clamp_amount_to_step(8.0, 1.0, _old_step(eth))
        == _clamp_amount_to_step(8.0, 1.0, _new_step("ETH-31JUL26-2050-C", eth)),
    )

    print("\n" + "─" * 68)
    print("  3. Futures still step on contract_size")
    print("─" * 68)

    fut = {"min_trade_amount": 10.0, "contract_size": 10.0, "kind": "future"}
    _check(
        "BTC-PERPETUAL step = contract_size (10.0)",
        _new_step("BTC-PERPETUAL", fut) == 10.0,
    )
    _check(
        "option detected from name when 'kind' is absent",
        _is_option_instrument("BTC-31JUL26-64000-C", None) is True,
    )
    _check(
        "perpetual not misdetected as an option",
        _is_option_instrument("BTC-PERPETUAL", None) is False,
    )

    print("\n" + "─" * 68)
    print("  4. Live exchange metadata (test.deribit.com)")
    print("─" * 68)
    asyncio.run(_live_check())

    print()
    if _failures:
        print(f"RESULT: {_failures} check(s) FAILED")
        sys.exit(1)
    print("RESULT: all checks passed")


async def _live_check() -> None:
    """Fetch real option metadata to confirm the two fields differ for BTC."""
    from execution.executor import _DeribitRPCClient

    if not (config.DERIBIT_CLIENT_ID and config.DERIBIT_CLIENT_SECRET):
        print("  (skipped — no credentials configured)")
        return

    try:
        async with _DeribitRPCClient() as client:
            for currency in ("BTC", "ETH"):
                instruments = await client._rpc(
                    "public/get_instruments",
                    {"currency": currency, "kind": "option", "expired": False},
                )
                if not instruments:
                    print(f"  {currency}: no live option instruments returned")
                    continue
                name = instruments[0]["instrument_name"]
                info = await client._fetch_amount_info(name)
                meta = await client._rpc(
                    "public/get_instrument", {"instrument_name": name}
                )
                print(
                    f"  {name}: min_trade_amount={meta.get('min_trade_amount')} "
                    f"contract_size={meta.get('contract_size')} → resolved step={info[1]}"
                )
                _check(
                    f"{currency} resolved step equals min_trade_amount",
                    info is not None
                    and info[1] == float(meta.get("min_trade_amount")),
                )
    except Exception as exc:  # network/auth issues must not fail the offline demo
        print(f"  (skipped — live fetch unavailable: {exc})")


if __name__ == "__main__":
    main()
