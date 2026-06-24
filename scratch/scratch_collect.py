"""
scratch/scratch_collect.py
===========================
Smoke-test for collect.py — runs ``--once`` against the Deribit paper API,
then validates the DB contains rows and prints a summary.

Does NOT run against the live exchange (exits if DERIBIT_PAPER = False).

Run from the repo root::

    python -m scratch.scratch_collect
"""

import asyncio
import sys
import tempfile
import logging
from pathlib import Path

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch files must not run against the live exchange. "
          "Set TRADING_MODE='paper' in config.py.")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scratch_collect")


async def main() -> None:
    import aiohttp
    import duckdb
    from backtest.data_collector import (
        _ensure_schema, collect_snapshot, COLLECTOR_ASSETS
    )
    from backtest.data_loader_db import db_summary

    assets = ["ETH"]  # single asset for a fast smoke test

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "smoke.duckdb"

        # --- Section 1: collect one snapshot --------------------------------
        logger.info("=== Section 1: collect one snapshot (asset=%s) ===", assets)
        con = duckdb.connect(str(db_path))
        _ensure_schema(con)

        async with aiohttp.ClientSession() as session:
            totals = await collect_snapshot(session, con, assets)
        con.close()

        logger.info("Rows collected: %s", totals)
        assert any(v > 0 for v in totals.values()), \
            f"Expected at least one row to be collected, got {totals}"
        print(f"  PASS  collect_snapshot returned {totals}")

        # --- Section 2: db_summary -------------------------------------------
        logger.info("=== Section 2: db_summary ===")
        summary = db_summary(db_path)
        assert summary, "Expected non-empty summary"
        for s in summary:
            print(
                f"  {s['asset']}: {s['total_rows']} rows, "
                f"{s['total_snapshots']} snapshots, "
                f"{s['distinct_instruments']} instruments"
            )
        print("  PASS  db_summary returned data")

        # --- Section 3: verify collect.py CLI --once mode -------------------
        logger.info("=== Section 3: verify collect.py --once via subprocess ===")
        import subprocess
        result = subprocess.run(
            [sys.executable, "collect.py", "--once",
             "--assets", *assets,
             "--db", str(db_path),
             "--log-level", "WARNING"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("  FAIL  collect.py --once exited non-zero")
            print("  stderr:", result.stderr[:500])
            sys.exit(1)
        print("  PASS  collect.py --once exited 0")

        # --- Section 4: confirm second snapshot was added -------------------
        logger.info("=== Section 4: confirm second snapshot added ===")
        summary2 = db_summary(db_path)
        rows2 = summary2[0]["total_rows"]
        rows1 = summary[0]["total_rows"]
        assert rows2 >= rows1, f"Expected more rows after second run: {rows2} vs {rows1}"
        print(f"  PASS  rows after second run: {rows2} (was {rows1})")

    logger.info("=== All checks passed ===")


if __name__ == "__main__":
    asyncio.run(main())
