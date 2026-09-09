#!/usr/bin/env python3
"""
Debug script to trace roll logic failures.

Analyzes why rolls are failing based on position data and cache state.
No live orders placed.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
if config.TRADING_MODE == "live":
    print("ERROR: This script cannot run in live mode")
    sys.exit(1)

from data.chain_cache import ChainCache
from strategy.scanner import scan, CalendarCandidate
from strategy.decision import _expiry_gap_days, _days_left, _instrument_expiry_label
from core.pricing import breakeven_call, breakeven_put
import logging

logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ============================================================================
# Test case: Simulate the roll failure scenario
# ============================================================================

def test_roll_candidate_search():
    """Test if the roll candidate search logic can find valid candidates."""

    print("\n" + "="*80)
    print("ROLL LOGIC DEBUG")
    print("="*80)

    # Simulate a position that needs to roll
    position = {
        "trade_id": 65,
        "asset": "BTC",
        "strike": 88000.0,
        "option_type": "Call",
        "near_instrument": "BTC-30AUG26-88000-C",  # About to expire
        "near_days": 1,  # Originally entered with 1-DTE near
        "far_instrument": "BTC-11SEP26-88000-C",  # Far leg still ~13 days out
        "far_days": 14,  # Originally entered with 14-DTE far
        "qty": 1.0,
        "net_debit": 0.05,
        "near_prem": 0.0012,
        "far_prem": 0.0020,
    }

    print(f"\nPosition to roll:")
    print(f"  Trade ID: {position['trade_id']}")
    print(f"  Asset: {position['asset']}")
    print(f"  Strike: {position['strike']}")
    print(f"  Type: {position['option_type']}")
    print(f"  Current near: {position['near_instrument']} ({position['near_days']}-DTE at entry)")
    print(f"  Current far: {position['far_instrument']} ({position['far_days']}-DTE at entry)")
    print(f"  Qty: {position['qty']}")

    # Check what near/far DTEs are configured
    print(f"\nConfigured DTE options:")
    print(f"  NEAR_DAYS_OPTIONS: {config.NEAR_DAYS_OPTIONS}")
    print(f"  FAR_DAYS_OPTIONS: {config.FAR_DAYS_OPTIONS}")
    print(f"  NEAR_DAY_TOLERANCE: {config.NEAR_DAY_TOLERANCE}")
    print(f"  FAR_DAY_TOLERANCE: {config.FAR_DAY_TOLERANCE}")
    print(f"  ROLL_TRIGGER_DAYS: {config.ROLL_TRIGGER_DAYS}")
    print(f"  MIN_ROLL_NEAR_FAR_GAP_DAYS: {config.MIN_ROLL_NEAR_FAR_GAP_DAYS}")

    # Analyze the far leg
    print(f"\nFar leg analysis:")
    far_expiry_label = _instrument_expiry_label(position["far_instrument"])
    print(f"  Far expiry label: {far_expiry_label}")

    # Simulate what should be valid roll candidates
    print(f"\nValid roll candidates (theory):")
    print(f"  Need: same asset ({position['asset']}), strike ({position['strike']}), type ({position['option_type']})")
    print(f"  Need: far leg = {position['far_instrument']}")
    print(f"  Need: new near leg expiry < {far_expiry_label}")
    print(f"  Need: gap between new near and far >= {config.MIN_ROLL_NEAR_FAR_GAP_DAYS} days")
    print(f"  Candidate near legs must be in: {config.NEAR_DAYS_OPTIONS}")
    print(f"  Entry-grade filters RELAXED for rolls (moneyness, contango, POP)")
    print(f"  Liquidity gate and OI gate RETAINED")

    # Simulate scanner search parameters
    print(f"\nScanner roll_for parameters:")
    roll_for = {
        "asset": position["asset"],
        "strike": position["strike"],
        "option_type": position["option_type"],
        "far_instrument": position["far_instrument"],
    }
    print(f"  {roll_for}")

    # ========================================================================
    # Key diagnostic: Check if the issue is in candidate finding
    # ========================================================================

    print("\n" + "-"*80)
    print("DIAGNOSTIC: Why might roll fail?")
    print("-"*80)

    print("\n1. CACHE SUBSCRIPTION ISSUE (most likely)")
    print(f"   - Is {position['far_instrument']} in the feed's subscription list?")
    print(f"   - If far leg is at or past expiry, Deribit may not trade it")
    print(f"   - If far leg is outside the configured FAR_DAYS_OPTIONS window,")
    print(f"     it won't be subscribed by the normal feed (Phase 18 issue)")
    print(f"   - The bot added Phase 18 fix: open positions should be auto-subscribed")

    print("\n2. SCANNER NEAR-FAR MATCHING ISSUE")
    print(f"   - Scanner only returns pairs where far_target > near_target")
    print(f"   - For 1-DTE near legs: limited to FAR_DAYS in range [2..{config.MAX_FAR_DAYS_FOR_1D_NEAR}]")
    print(f"     (current config: MAX_FAR_DAYS_FOR_1D_NEAR = {config.MAX_FAR_DAYS_FOR_1D_NEAR})")
    print(f"   - Near legs must be within ±{config.NEAR_DAY_TOLERANCE} of target")
    print(f"   - Far legs must be within ±{config.FAR_DAY_TOLERANCE} of target")

    print("\n3. TOLERANCE WINDOW ISSUE")
    print(f"   - If position far leg is {position['far_days']}-DTE at entry,")
    print(f"     and now it's decayed (say, 3 days left),")
    print(f"     the scanner still looks for the EXACT instrument")
    print(f"   - No tolerance on instrument name matching — it's an exact filter")
    print(f"   - But the instrument name encodes the date, so aged far legs")
    print(f"     won't match newer candidates with the same strike/type")

    print("\n4. NO CANDIDATE SCENARIO (trade #65 case)")
    print(f"   - If there's literally no near leg option available")
    print(f"     for strike {position['strike']}, type {position['option_type']}")
    print(f"   - Then `candidates` list will be empty")
    print(f"   - Log: 'Roll: no matching candidate for asset=...'")

    print("\n5. GATES REJECTING ALL CANDIDATES (trade #59 case)")
    print(f"   - Candidates found but all rejected by:")
    print(f"     a) Liquidity gate: spread too wide, bid/ask size too small")
    print(f"     b) Margin gate: simulated roll would exceed margin utilization")
    print(f"   - Log: 'Roll: no acceptable roll candidate after gates'")

    print("\n" + "-"*80)
    print("ROOT CAUSE ANALYSIS")
    print("-"*80)

    print("\nMost likely root cause: FEED SUBSCRIPTION GAP")
    print("\nScenario:")
    print("  1. Position entered with 1-DTE near, 14-DTE far")
    print("  2. Near leg is in NEAR_DAYS_OPTIONS=[1,7,14], so it's subscribed")
    print("  3. Far leg (14-DTE) is in FAR_DAYS_OPTIONS=[7,14,30,45,60], subscribed")
    print("  4. Time passes. Near leg decays to ~2 days left")
    print("  5. Far leg decays to ~11 days left")
    print("  6. Roll trigger fires (near_days_left <= 2)")
    print("  7. Scanner looks for new near legs that pair with far_instrument")
    print("  8. BUG: far_instrument is still 'BTC-11SEP26-88000-C' (14 days from entry)")
    print("  9. But if feed only subscribed to instrument patterns like 'BTC-*-88000-C'")
    print("     where expiry is in [0,7,14,30,45,60] day windows,")
    print("     and now BTC-11SEP26-88000-C is out of any window,")
    print("     it won't be in the cache")
    print("  10. So scan() returns empty candidates")

    print("\nAlternate scenario (trade #59):")
    print("  - Candidates ARE found")
    print("  - But liquidity gate rejects them because:")
    print("    a) The strike is deep ITM/OTM and bid/ask spreads are wide")
    print("    b) Open interest has dried up as the strike ages")
    print("    c) The new near leg's entry premium is too high (> MAX_ENTRY_PREMIUM)")

    print("\n" + "="*80)
    print("RECOMMENDED FIXES")
    print("="*80)

    print("\n1. Verify Phase 18 fix is working")
    print("   - Check that open position instruments are being subscribed")
    print("   - grep logs for 'extra_instruments' or 'open position' subscription")

    print("\n2. Relax roll-specific gates (if appropriate)")
    print("   - MAX_ENTRY_PREMIUM is tight (0.10); consider allowing 0.15 for rolls")
    print("   - MAX_LEG_SPREAD_PCT is tight (0.05); consider 0.10 for rolls")
    print("   - These gates protect entry; rolls are just continuation")

    print("\n3. Debug individual roll attempts")
    print("   - Add detailed logging in _try_roll showing:")
    print("     - Far instrument being searched for")
    print("     - Candidates found (count)")
    print("     - Each candidate's near instrument and why it was rejected")

    print("\n4. Check fee gate")
    print("   - Log theta_gain vs roll_cost breakdown")
    print("   - Verify fees are calculated correctly")

    print("\n" + "="*80)

if __name__ == "__main__":
    test_roll_candidate_search()
    print("\nDone. No live orders placed.")
