"""
scratch_reconcile_mismatch.py
=============================
Debug script — Phase 24 reconcile-mismatch remediation.

Connects to the paper/test Deribit account, fetches the live option-position
list, and cross-references it against any ``close_stuck`` trades in the bot DB.
Prints a reconciliation report showing:

1. What the DB believes is stuck (``close_status='close_stuck'``)
2. What Deribit actually still holds (``private/get_positions``)
3. Which stuck trades would be auto-reconciled (both legs gone from Deribit)
4. Which live Deribit instruments are not tracked in the bot DB

Read-only: it does not close anything or mutate the DB (``sync_stuck_positions``
is exercised only in a dry-run print, not called).  Aborts in live mode.

Run from the repo root:
    python -m scratch.scratch_reconcile_mismatch
"""

from __future__ import annotations

import sys

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode. Aborting.")
    sys.exit(1)

from db.state import DB_PATH, get_stuck_positions, get_visible_positions, init_db
from portfolio.tracker import PortfolioTracker, _assets_to_currencies


def main() -> None:
    init_db(DB_PATH)

    if config.TRADING_MODE == "paper":
        print(
            "NOTE: TRADING_MODE is 'paper' — Deribit position queries are "
            "disabled (get_deribit_open_positions returns []). Run in 'test' "
            "mode with credentials to see live positions.\n"
        )

    tracker = PortfolioTracker(
        client_id=config.DERIBIT_CLIENT_ID,
        client_secret=config.DERIBIT_CLIENT_SECRET,
        cache=None,
    )

    print("─" * 60)
    print("  DB close_stuck trades")
    print("─" * 60)
    stuck = get_stuck_positions(DB_PATH)
    if not stuck:
        print("  (none)")
    for t in stuck:
        print(
            f"  #{t.id} {t.asset} {t.option_type} {t.strike:.0f}  "
            f"near={t.near_instrument} far={t.far_instrument}"
        )
        if t.close_error_reason:
            print(f"      reason: {t.close_error_reason}")

    print()
    print("─" * 60)
    print("  Live Deribit option positions")
    print("─" * 60)
    live_names: set[str] = set()
    for currency in _assets_to_currencies(config.ASSETS):
        positions = tracker.get_deribit_open_positions(currency)
        if not positions:
            continue
        print(f"  {currency}:")
        for p in positions:
            live_names.add(p["instrument_name"])
            print(
                f"    {p['instrument_name']}  size={p['size']}  "
                f"index=${p['index_price']:,.2f}  mark=${p['mark_value']:.4f}"
            )
    if not live_names:
        print("  (none / unavailable)")

    print()
    print("─" * 60)
    print("  Reconciliation preview (dry-run — nothing is modified)")
    print("─" * 60)
    for t in stuck:
        near_gone = (not t.near_instrument) or (t.near_instrument not in live_names)
        far_gone = (not t.far_instrument) or (t.far_instrument not in live_names)
        if near_gone and far_gone:
            print(f"  #{t.id} → WOULD auto-reconcile (both legs gone from Deribit)")
        else:
            print(f"  #{t.id} → left stuck (at least one leg still open on Deribit)")

    # Instruments live on Deribit but not tracked in the DB.
    visible = get_visible_positions(DB_PATH)
    db_names: set[str] = set()
    for t in visible:
        if t.near_instrument:
            db_names.add(t.near_instrument)
        if t.far_instrument:
            db_names.add(t.far_instrument)
    untracked = sorted(n for n in live_names if n not in db_names)
    if untracked:
        print()
        print("  ⚠️  Live on Deribit but NOT tracked in bot DB:")
        for n in untracked:
            print(f"      {n}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
