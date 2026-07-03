#!/usr/bin/env python3
"""
scratch/scratch_pnl_chart.py
=============================
Test script for `/pnl` equity curve chart rendering.

Loads real (or synthetic, if empty) closed trades from the paper DB,
renders the chart, and saves it to scratch/pnl_chart_preview.png for visual inspection.

Run with: python -m scratch.scratch_pnl_chart
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import config

if config.TRADING_MODE == "live":
    print("Aborting: TRADING_MODE is 'live'. Scratch scripts must not run against live trading.")
    sys.exit(1)

from db.state import (
    init_db, get_all_closed_trades, get_open_trades,
    create_calendar_trade, close_calendar_trade, DB_PATH,
)
from data.chain_cache import ChainCache
from telegram_cmd.pnl_chart import render_pnl_chart


def populate_synthetic_trades(db_path: Path) -> None:
    """Create synthetic closed trades for demonstration if DB is empty."""
    closed = get_all_closed_trades(db_path)
    if closed:
        print(f"DB already has {len(closed)} closed trades. Skipping synthetic population.")
        return

    print("DB is empty. Creating synthetic trades for demonstration...")
    # Create 5 trades over 5 days
    for i in range(5):
        t = create_calendar_trade(
            asset="BTC",
            date_open=date(2026, 6, i + 1),
            option_type="Call" if i % 2 == 0 else "Put",
            strike=100_000.0 + i * 1000,
            expiry_near=f"2026-06-{7 + i:02d}",
            expiry_far=f"2026-07-{4 + i:02d}",
            near_days=7,
            far_days=30,
            qty=1.0,
            spot_open=99_000.0,
            near_prem=500.0,
            far_prem=800.0,
            net_debit=300.0,
            near_instrument=f"BTC-{7 + i:02d}JUN26-{100_000 + i * 1000:.0f}-{'C' if i % 2 == 0 else 'P'}",
            far_instrument=f"BTC-{4 + i:02d}JUL26-{100_000 + i * 1000:.0f}-{'C' if i % 2 == 0 else 'P'}",
            db_path=db_path,
        )
        # Alternate wins and losses
        pnl = 100.0 if i % 2 == 0 else -50.0
        close_calendar_trade(
            t.id,
            date_close=date(2026, 6, i + 8),
            spot_close=101_000.0,
            pnl=pnl,
            result="Win" if pnl > 0 else "Loss",
            db_path=db_path,
        )

    print(f"Created 5 synthetic trades.")


def main():
    init_db(DB_PATH)
    populate_synthetic_trades(DB_PATH)

    closed_trades = get_all_closed_trades(DB_PATH)
    open_trades = get_open_trades(DB_PATH)

    print(f"\nRender test:")
    print(f"  Closed trades: {len(closed_trades)}")
    print(f"  Open trades: {len(open_trades)}")

    # Create a minimal cache (for stale fallback testing)
    cache = ChainCache()

    # Render chart
    try:
        buf = render_pnl_chart(closed_trades, open_trades, cache)
        output_path = Path(__file__).parent / "pnl_chart_preview.png"
        with open(output_path, "wb") as f:
            f.write(buf.read())
        print(f"✓ Chart rendered and saved to: {output_path}")
        print(f"  File size: {output_path.stat().st_size} bytes")
    except Exception as exc:
        print(f"✗ Failed to render chart: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
