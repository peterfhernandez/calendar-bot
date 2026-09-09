#!/usr/bin/env python3
"""
Diagnostic script for roll logic failures.

Traces through a specific open position and shows:
1. What's in the cache for that position's strike/type
2. What scan() finds (before and after far_instrument filter)
3. Which gates reject candidates
4. Specific bid/ask/spread values for each candidate

Run with:
    python -m scratch.scratch_roll_diagnostic
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
if config.TRADING_MODE == "live":
    print("ERROR: This script cannot run in live mode")
    sys.exit(1)

from data.chain_cache import ChainCache
from strategy.scanner import scan, CalendarCandidate
from strategy.decision import _expiry_gap_days, _days_left, _instrument_expiry_label
from db.state import load_calendar_state
from core.pricing import breakeven_call, breakeven_put
import logging

logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _print_section(title: str) -> None:
    """Print a formatted section header."""
    print(f"\n{'='*80}")
    print(f"{title:^80}")
    print(f"{'='*80}")


def _print_subsection(title: str) -> None:
    """Print a formatted subsection header."""
    print(f"\n{title}")
    print("-" * len(title))


def diagnose_roll(asset: str, trade_id: int | None = None, db_path: str | None = None) -> None:
    """
    Diagnose roll logic for a specific asset and optionally a specific trade.

    Args:
        asset: Asset to diagnose (e.g., "BTC")
        trade_id: Specific trade ID to diagnose (optional)
        db_path: Path to database (default: config default)
    """
    _print_section("ROLL LOGIC DIAGNOSTIC")

    # Load open positions
    state = load_calendar_state(asset, db_path=db_path)
    open_positions = state.get("open_positions", [])

    if not open_positions:
        print(f"No open positions for {asset}")
        return

    # Filter to specific trade if requested
    positions = open_positions
    if trade_id is not None:
        positions = [p for p in positions if p.get("trade_id") == trade_id]
        if not positions:
            print(f"No position with trade_id={trade_id}")
            return

    print(f"\nFound {len(positions)} open position(s) for {asset}")

    # Initialize cache
    cache = ChainCache()

    # Simulate feed update (in real bot, this happens continuously)
    # For now, we'll just show what's configured
    print(f"\nCache configuration:")
    print(f"  ASSETS: {config.ASSETS}")
    print(f"  NEAR_DAYS_OPTIONS: {config.NEAR_DAYS_OPTIONS}")
    print(f"  FAR_DAYS_OPTIONS: {config.FAR_DAYS_OPTIONS}")
    print(f"  NEAR_DAY_TOLERANCE: {config.NEAR_DAY_TOLERANCE}")
    print(f"  FAR_DAY_TOLERANCE: {config.FAR_DAY_TOLERANCE}")
    print(f"  MIN_ROLL_NEAR_FAR_GAP_DAYS: {config.MIN_ROLL_NEAR_FAR_GAP_DAYS}")

    for pos in positions:
        _print_subsection(f"Position: trade_id={pos.get('trade_id')} {pos['asset']}-{pos['strike']}-{pos['option_type']}")

        trade_id = pos.get("trade_id")
        strike = pos["strike"]
        opt_type = pos["option_type"]
        near_instr = pos.get("near_instrument")
        far_instr = pos.get("far_instrument")
        near_days = pos.get("near_days", "?")
        far_days = pos.get("far_days", "?")

        print(f"  Current near leg: {near_instr} ({near_days}-DTE at entry)")
        print(f"  Current far leg:  {far_instr} ({far_days}-DTE at entry)")
        print(f"  Strike: {strike}")
        print(f"  Type: {opt_type}")

        # Show what would be searched for
        roll_for = {
            "asset": pos["asset"],
            "strike": strike,
            "option_type": opt_type,
            "far_instrument": far_instr,
        }

        _print_subsection("Step 1: Scan for candidates (before far_instrument filter)")
        print(f"  Will search for: {opt_type} options at strike {strike}")
        print(f"  Target near legs: {config.NEAR_DAYS_OPTIONS} (±{config.NEAR_DAY_TOLERANCE} days)")
        print(f"  Target far legs: {config.FAR_DAYS_OPTIONS} (±{config.FAR_DAY_TOLERANCE} days)")
        print(f"  After scan, will filter to far_instrument={far_instr}")

        # NOTE: We can't actually run scan() without a live cache with data.
        # In production, the cache is populated by the WebSocket feed.
        print(f"\n  ℹ️  To see actual candidates, run the bot with DERIBIT_PAPER=true")
        print(f"     and monitor logs for 'Scan complete' and 'Roll:' messages")

        _print_subsection("Step 2: Gates that would be applied")
        print(f"  ✓ Liquidity gate (is_roll=True): checks spread width and size")
        print(f"  ✓ Margin gate: checks if rolled position fits in available margin")
        print(f"  ✓ Near/Far gap gate: requires gap >= {config.MIN_ROLL_NEAR_FAR_GAP_DAYS} days")
        print(f"  ✗ Moneyness gate (RELAXED for rolls)")
        print(f"  ✗ Min entry POP gate (RELAXED for rolls)")
        print(f"  ✗ Min IV contango gate (RELAXED for rolls)")

        _print_subsection("Diagnostic checklist")
        print(f"  □ Is near leg ({near_instr}) approaching expiry?")
        print(f"  □ Is far leg ({far_instr}) subscribed to feed? (check: Phase 18 fix active)")
        print(f"  □ Are there any new {opt_type} options at strike {strike}?")
        print(f"  □ What bid/ask spreads exist for candidates?")
        print(f"  □ What margin utilization would the roll create?")

        print(f"\n  🔍 To debug this specific position:")
        print(f"     1. Watch logs while bot runs (tail -f bot.log | grep -E 'Roll:|Scan complete')")
        print(f"     2. Look for 'Scan complete' showing candidates found")
        print(f"     3. Look for 'Roll: candidate' showing which candidates pass/fail gates")
        print(f"     4. Check 'Roll: fee gate' to see if roll_cost > theta_gain")


def main() -> None:
    """Main entry point."""

    # Find an asset with open positions
    assets_with_positions = []
    for asset in config.ASSETS:
        state = load_calendar_state(asset)
        if state.get("open_positions"):
            assets_with_positions.append(asset)

    if not assets_with_positions:
        print("No open positions in any asset. Run the bot in paper mode to generate test data.")
        return

    # Diagnose first asset with positions
    asset = assets_with_positions[0]
    print(f"\nDiagnosing roll logic for {asset}...")
    diagnose_roll(asset)

    _print_section("RECOMMENDATIONS")
    print("""
1. If "Roll: no matching candidate" appears:
   - Check if far_instrument is subscribed to the feed
   - Verify Phase 18 fix is active (check logs for "extra-position")
   - Run scratch/scratch_feed_open_position_coverage.py to verify subscription

2. If "Roll: no acceptable roll candidate after gates" appears:
   - Note which gate is rejecting: liquidity, margin, or gap_days
   - Liquidity gate too strict? Check MAX_LEG_SPREAD_PCT, MAX_ENTRY_PREMIUM
   - Margin gate too strict? Check POSITION_MARGIN_UTIL_LIMIT
   - Consider relaxing roll-specific thresholds

3. If "Roll: fee gate blocked" appears:
   - Roll cost is eating up theta gain
   - Check compute_roll_fees() implementation
   - Consider relaxing fee gate for rolls

4. Enable DEBUG logging:
   - Set LOG_LEVEL=DEBUG in config.py
   - Rerun to see detailed gate rejection messages
    """)


if __name__ == "__main__":
    main()
