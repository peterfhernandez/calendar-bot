"""
scratch/scratch_data_collector.py
==================================
Demonstration script for the historical data pipeline.

What it does
------------
1.  Creates a temporary DuckDB (does NOT touch the real options.duckdb).
2.  Calls ``run_once()`` to collect one real snapshot from Deribit paper API.
3.  Prints a summary of what was stored.
4.  Exercises ``load_frames_from_db`` and prints the first 3 snapshots.
5.  Prints ``db_summary`` output.

Run from the repo root::

    python -m scratch.scratch_data_collector

This script does NOT run when ``DERIBIT_PAPER = False``.
"""

import asyncio
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

if not config.DERIBIT_PAPER:
    print("ERROR: scratch files must not run against the live exchange. "
          "Set DERIBIT_PAPER = True in config.py.")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scratch_data_collector")


async def main() -> None:
    # Use a temp DB so we never touch the real options.duckdb during testing
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "scratch.duckdb"

        logger.info("=== Section 1: collect one snapshot ===")
        # Import here so the module-level DB_PATH override works correctly
        from backtest.data_collector import run_once
        from backtest.data_loader_db import load_frames_from_db, db_summary

        totals = await run_once(assets=["ETH"], db_path=db_path)
        logger.info("Rows collected per asset: %s", totals)

        # ── Summary ──────────────────────────────────────────────────────────
        logger.info("=== Section 2: db_summary ===")
        summary = db_summary(db_path)
        if not summary:
            logger.warning("No data in DB — collection may have failed")
        for s in summary:
            print(
                f"  {s['asset']}: {s['total_rows']} rows, "
                f"{s['total_snapshots']} snapshots, "
                f"{s['distinct_instruments']} instruments"
            )

        # ── Load frames ───────────────────────────────────────────────────────
        logger.info("=== Section 3: load_frames_from_db ===")
        now  = datetime.now(timezone.utc)
        yesterday = now - timedelta(hours=1)

        frames = load_frames_from_db(db_path, "ETH", yesterday, now)
        logger.info("Loaded %d frames", len(frames))

        if frames:
            logger.info("=== Section 4: first 3 snapshots from frame 0 ===")
            for snap in frames[0][:3]:
                print(
                    f"  {snap.instrument:35s} "
                    f"spot={snap.spot:>10,.2f}  "
                    f"iv={snap.mark_iv:.2%}  "
                    f"bid={snap.bid:.4f}  ask={snap.ask:.4f}"
                )
        else:
            logger.warning("No frames loaded — cannot show snapshot sample")

    logger.info("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
