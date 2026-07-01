#!/usr/bin/env python3
"""
Demonstration of roll P&L tracking and EV recalculation.

This scratch script shows:
1. How roll P&L is calculated when a near leg closes at a better price
2. How roll P&L is included in the final position P&L
3. How EV is tracked at entry and roll time
4. How the formula accounts for all cash flows: entry → roll → close
"""

import sys
from pathlib import Path

# Abort if running against live trading mode
import config
if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must never touch the live exchange", file=sys.stderr)
    sys.exit(1)

print("=" * 80)
print("ROLL P&L TRACKING DEMONSTRATION")
print("=" * 80)

# Example 1: Roll with realized profit
print("\n" + "─" * 80)
print("EXAMPLE 1: Successful roll with near-leg profit")
print("─" * 80)

print("""
Position entered:
  Trade ID: #42
  Asset: BTC
  Strike: 60000 Put
  Near leg: 1-day (sold @ $0.0040)
  Far leg: 7-day (bought @ $0.0100)
  Net debit: $0.0060/contract
  Quantity: 1 contract
  Entry EV: 0.0385 (positive, good entry)
  Entry fees: $0.00012

After 1 day, near leg approaches expiry at 2 days remaining:
  Current time: next day
  Old near leg market price: $0.0035 (down from $0.0040 entry)
  Far leg still healthy: $0.0078

Rolling the near leg:
  Close old near @ $0.0035 (we sold @ $0.0040)
  Roll profit: ($0.0040 - $0.0035) * 1 = +$0.0005 ✓

  Open new near @ $0.0038 (7-day expiry)
  Roll EV: 0.0421 (improved! better setup)
  Roll fees: $0.00015

Decision engine logs:
  ROLL trade_id=42 → new near=BTC-3JAN26-60000-P
       roll_pnl=+0.0005  roll_fees=0.00015  ev_new=0.0421

P&L tracking after roll:
  _today_pnl: +0.0005 (immediately credit the roll profit)
  Position.roll_pnl: +0.0005 (stored in DB)
  Position.ev_score_initial: 0.0385 (unchanged)
  Position.ev_score_at_roll: 0.0421 (new near leg's EV)
""")

print("""
After more time passes, position closes at stop-loss:
  Close at: 2 days later
  Current far: $0.0082
  Current new near: $0.0036
  Current spread: $0.0082 - $0.0036 = $0.0046
  Gross P&L: $0.0046 - $0.0060 (original debit) = -$0.0014

  But we have roll_pnl from before: +$0.0005
  Plus roll fees: -$0.00015
  Plus close fees: -$0.00012

  Net P&L = gross(-$0.0014) + roll(+$0.0005) - fees($0.00027) = -$0.00067

Decision engine logs at close:
  CLOSE trade_id=42  gross_pnl=-0.0014  roll_pnl=+0.0005  close_fees=0.00012
        net_pnl=-0.00067  open_fees=0.00012  total_fees=0.00039
        ev_initial=0.0385  reason=Stop-loss (66% of debit)

Key insight:
  WITHOUT roll tracking, final P&L would show: -$0.0014 - $0.00012 = -$0.00152
  WITH roll tracking, final P&L shows: -$0.00067
  Difference: +$0.00085 (the roll profit minus some fees)

  The roll saved the trade from a worse loss by capturing $0.0005 profit!
""")

# Example 2: Roll P&L visibility in Telegram commands
print("\n" + "─" * 80)
print("EXAMPLE 2: Roll P&L visibility in Telegram commands")
print("─" * 80)

print("""
/positions output:
  #42 BTC 60000 Put  10Jul26→28Aug26  entry=$6.00  sv=$4.60
       PnL=-$0.67 (-11.2%)  [unr=-$1.40  roll=+$0.50]
       ev_init=0.0385  ev_roll=0.0421

/portfolio output:
  #42 BTC Put 60000  10Jul26→28Aug26
    Debit: $6.00  Fees: $0.12  Roll PnL: +$0.50
    EV_init: 0.0385  EV_roll: 0.0421
    Value: $4.60  PnL=-$0.67

/closed_trades (after position closes):
  1 trade(s) closed today. Total PnL: -$0.67
  #42 BTC  debit=$6.00  pnl=-0.67  Stop-loss (66% of debit)

Transparency achieved:
  - Roll P&L separated from unrealized P&L in /positions
  - Both EVs visible so you can see if the roll improved or worsened prospects
  - Total P&L includes roll profit, giving accurate final result
  - Fees broken down separately for clarity
""")

# Example 3: When roll is rejected by new validation gates
print("\n" + "─" * 80)
print("EXAMPLE 3: Roll rejection when new near leg fails gates")
print("─" * 80)

print("""
Position approaching roll:
  Trade ID: #43, same as above

At roll time, scanner finds a new near leg candidate:
  New near instrument: BTC-5JAN26-60000-P
  Bid: $0.0042, Ask: $0.0044
  Bid/ask spread: $0.0002 / $0.0043 mid = 4.7% ✓ passes
  Bid size: 0.5 contracts

  But MIN_LEG_BID_SIZE = 1.0, so:

Decision engine rejects the roll:
  Roll: candidate rejected by liquidity gate —
        near-leg bid_size 0.5 < MIN_LEG_BID_SIZE 1 (trade_id=43)

Result:
  - Roll is skipped
  - Position is NOT updated
  - No roll_pnl is recorded
  - Position continues to hold old near leg
  - Next monitor tick will try rolling again (if gate not passed last time)

This protects against rolling into a thin book where we'd get a worse price.
""")

print("\n" + "─" * 80)
print("KEY FORMULAS")
print("─" * 80)

print("""
Roll P&L calculation:
  roll_pnl = (old_near_sell_price - new_near_bid_price) * qty

  In Example 1:
  roll_pnl = ($0.0040 - $0.0035) * 1 = +$0.0005

Close P&L calculation (NEW):
  gross_pnl = spread_value - net_debit * qty
  net_pnl = gross_pnl + roll_pnl - open_fees - close_fees

  In Example 1:
  gross_pnl = ($0.0046) - ($0.0060 * 1) = -$0.0014
  net_pnl = -$0.0014 + $0.0005 - $0.00012 - $0.00015 = -$0.00067

The roll_pnl term is now included in the final position P&L,
ensuring all cash flows (entry → roll → close) are accounted for.
""")

print("\n" + "=" * 80)
print("This demonstrates the complete roll P&L tracking system.")
print("Run the bot in paper mode with multiple day positions to observe rolls!")
print("=" * 80)
