"""
scratch_portfolio.py
====================
Debug script — connects to the Deribit paper API and prints a live
portfolio snapshot.

Run from the repo root:
    python -m scratch.scratch_portfolio

What it demonstrates:
1. PortfolioTracker.refresh() fetching real account data from test.deribit.com
2. Used-margin calculation from SQLite open positions
3. Realized P&L from today's closed trades
4. Reconciliation between Deribit margin and SQLite margin
5. portfolio_view() formatted output
"""

from __future__ import annotations

import sys
from pathlib import Path

# Guard: this script must never run against the live exchange
import config
if getattr(config, "TRADING_MODE", None) == "live" or not config.DERIBIT_PAPER:
    print("ERROR: scratch scripts must not run in live mode. Aborting.")
    sys.exit(1)

from db.state import init_db, DB_PATH
from portfolio.tracker import PortfolioTracker


def main() -> None:
    # Ensure DB schema exists
    init_db(DB_PATH)

    tracker = PortfolioTracker()

    print("Refreshing portfolio from Deribit paper API (test.deribit.com)…")
    has_creds = bool(config.DERIBIT_CLIENT_ID and config.DERIBIT_CLIENT_SECRET)
    if not has_creds:
        print(
            "NOTE: No API credentials found in .env — running in DB-only mode.\n"
            "Set DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET in .env to fetch\n"
            "live account data from test.deribit.com.\n"
        )

    state = tracker.refresh()

    print(tracker.portfolio_view())
    print()

    # Print raw state fields
    print(f"  available_cash      : ${state.available_cash:>12,.2f}")
    print(f"  equity_usd          : ${state.equity_usd:>12,.2f}")
    print(f"  used_margin (SQLite): ${state.used_margin:>12,.2f}")
    print(f"  unrealized_pnl      : ${state.unrealized_pnl:>+12,.2f}")
    print(f"  realized_pnl_today  : ${state.realized_pnl_today:>+12,.2f}")
    print(f"  open_position_count : {state.open_position_count}")
    print(f"  deribit_margin_usd  : ${state.deribit_margin_usd:>12,.2f}")
    if state.last_refresh:
        import datetime
        ts = datetime.datetime.fromtimestamp(state.last_refresh).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  last_refresh        : {ts}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
