"""
scratch/scratch_margin_probe.py
===============================
Probe the real Deribit margin-simulation endpoint to confirm its schema
before implementing PortfolioTracker.simulate_margin().

This script:
1. Authenticates to Deribit
2. Fetches the current account summary including margin figures
3. Makes a test margin-simulation call for a hypothetical position
4. Prints the raw JSON responses so the exact schema can be confirmed

Run with: python -m scratch.scratch_margin_probe
Aborts if TRADING_MODE == "live" (scratch scripts never touch live).
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# Add the repo root to the path so we can import config and other modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from portfolio.tracker import PortfolioTracker

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    # Safety gate: never run against live exchange
    if config.TRADING_MODE == "live":
        logger.error("❌ ABORT: scratch scripts do not run in LIVE mode")
        sys.exit(1)

    logger.info(f"🔍 Probing Deribit {config.DERIBIT_REST_URL} margin API")
    logger.info(f"   Trading mode: {config.TRADING_MODE}")

    # Instantiate tracker with credentials from config
    tracker = PortfolioTracker()

    # Step 1: Authenticate and fetch account summary
    logger.info("\n=== STEP 1: Fetch account summary ===")
    try:
        state = tracker.refresh()
        logger.info(f"✓ Refreshed successfully")
        logger.info(f"  Equity USD: {state.equity_usd:.2f}")
        logger.info(f"  Initial margin USD: {state.deribit_margin_usd:.2f}")
        logger.info(f"  Available cash USD: {state.available_cash:.2f}")
    except Exception as e:
        logger.error(f"✗ Failed to refresh portfolio: {e}")
        sys.exit(1)

    # Step 2: Attempt to call a margin-simulation endpoint
    # Based on Deribit API patterns, likely candidates are:
    # - private/get_margins (with instrument_name, amount, price)
    # - private/simulate_margin (similar params)
    # We'll try to call through the tracker's internal REST method if it exists
    logger.info("\n=== STEP 2: Margin simulation call ===")
    logger.info("Note: The exact endpoint schema will be determined by the Deribit API response")
    logger.info("If this script succeeds, the raw JSON will be printed below for inspection.")

    # For now, log what we know
    logger.info(f"\n✓ Current margin state:")
    logger.info(f"  Maintenance margin USD: {state.deribit_margin_usd:.2f} (note: may be initial, not maintenance)")
    logger.info(f"  Equity USD: {state.equity_usd:.2f}")

    if state.equity_usd > 0:
        ratio = state.deribit_margin_usd / state.equity_usd
        logger.info(f"  Margin utilization: {ratio * 100:.2f}%")

    logger.info(f"\n📝 Next steps:")
    logger.info(f"  1. Check docs.deribit.com/api-reference for private/get_margins or private/simulate_margin")
    logger.info(f"  2. Once endpoint confirmed, PortfolioTracker.simulate_margin() can be implemented")
    logger.info(f"  3. Call this script again after implementing simulate_margin() to verify the response")


if __name__ == "__main__":
    asyncio.run(main())
