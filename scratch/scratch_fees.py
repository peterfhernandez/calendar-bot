"""
scratch/scratch_fees.py
=======================
Demonstrates the Deribit fee model across all scenarios.

Covers:
  1. Entry fees — with and without combo discount — for BTC, ETH, SOL
  2. Exit fees (no combo discount)
  3. Expiry / delivery fees — near OTM, near ITM with daily/weekly (exempt),
     near ITM with monthly (delivery fee applies)
  4. Roll fees vs theta gain — shows the break-even roll threshold
  5. Early close (stop-loss / take-profit) — gross vs net P&L after fees

Aborts if TRADING_MODE == "live".

Run with:
    python -m scratch.scratch_fees
"""

from __future__ import annotations

import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode. Aborting.")
    sys.exit(1)

from core.fees import (
    leg_fee,
    entry_fees,
    exit_fees,
    roll_fees,
    delivery_fee,
    round_trip_fees,
)

DIVIDER = "─" * 65


def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


# ── 1. Entry fees ─────────────────────────────────────────────────────────────

section("1. Entry fees — with and without combo discount")

BTC_SPOT = 100_000.0
ETH_SPOT = 3_000.0
SOL_SPOT = 150.0

QTY = 1.0

# BTC options — typical near/far premiums
btc_near_price = 500.0    # $500 per contract
btc_far_price  = 1_200.0  # $1,200 per contract

btc_combo_fee     = entry_fees("BTC", BTC_SPOT, QTY, btc_near_price, btc_far_price, via_combo=True)
btc_individ_fee   = entry_fees("BTC", BTC_SPOT, QTY, btc_near_price, btc_far_price, via_combo=False)

print(f"\n  BTC @ ${BTC_SPOT:,.0f}  near=${btc_near_price:.0f}  far=${btc_far_price:.0f}  qty={QTY}")
print(f"    Combo entry fee   : ${btc_combo_fee:.4f}  (expensive leg only)")
print(f"    Individual entry  : ${btc_individ_fee:.4f}  (both legs charged)")
print(f"    Combo saving      : ${btc_individ_fee - btc_combo_fee:.4f}")

# ETH options
eth_near_price = 20.0
eth_far_price  = 50.0
eth_combo_fee   = entry_fees("ETH", ETH_SPOT, QTY, eth_near_price, eth_far_price, via_combo=True)
eth_individ_fee = entry_fees("ETH", ETH_SPOT, QTY, eth_near_price, eth_far_price, via_combo=False)
print(f"\n  ETH @ ${ETH_SPOT:,.0f}  near=${eth_near_price:.1f}  far=${eth_far_price:.1f}  qty={QTY}")
print(f"    Combo entry fee   : ${eth_combo_fee:.4f}")
print(f"    Individual entry  : ${eth_individ_fee:.4f}")

# SOL options — taker fee same, maker would be 0%
sol_near_price = 1.0
sol_far_price  = 2.5
sol_taker_fee = entry_fees("SOL", SOL_SPOT, QTY, sol_near_price, sol_far_price, via_combo=True)
sol_near_maker = leg_fee("SOL", SOL_SPOT, QTY, is_maker=True,  option_price=sol_near_price)
sol_far_maker  = leg_fee("SOL", SOL_SPOT, QTY, is_maker=True,  option_price=sol_far_price)
print(f"\n  SOL @ ${SOL_SPOT:.0f}  near=${sol_near_price:.2f}  far=${sol_far_price:.2f}  qty={QTY}")
print(f"    Combo taker entry : ${sol_taker_fee:.6f}")
print(f"    SOL maker near leg: ${sol_near_maker:.6f}  (SOL maker = 0%)")
print(f"    SOL maker far leg : ${sol_far_maker:.6f}  (SOL maker = 0%)")


# ── 2. Exit fees ──────────────────────────────────────────────────────────────

section("2. Exit fees (no combo discount)")

btc_close_near = 200.0   # current near premium at close
btc_close_far  = 900.0   # current far premium at close

btc_exit = exit_fees("BTC", BTC_SPOT, QTY, btc_close_near, btc_close_far)
print(f"\n  BTC exit  near=${btc_close_near:.0f}  far=${btc_close_far:.0f}")
print(f"    Exit fee total    : ${btc_exit:.4f}  (both legs, no combo discount)")


# ── 3. Delivery fees ──────────────────────────────────────────────────────────

section("3. Delivery fees at expiry")

btc_option_price = 500.0  # current option value in USD

# Daily (1d) near leg — exempt
d1_fee = delivery_fee("BTC", BTC_SPOT, QTY, btc_option_price, expiry_days=1)
print(f"\n  Near leg OTM — no settlement: $0.00  (option expires worthless)")

print(f"\n  Near leg ITM — daily (1d near leg), expiry_days=1:")
print(f"    Delivery fee      : ${d1_fee:.4f}  (daily — EXEMPT)")

# Weekly (7d) near leg — exempt
d7_fee = delivery_fee("BTC", BTC_SPOT, QTY, btc_option_price, expiry_days=7)
print(f"\n  Near leg ITM — weekly (7d near leg), expiry_days=7:")
print(f"    Delivery fee      : ${d7_fee:.4f}  (weekly — EXEMPT)")

# Monthly (30d) — charged
d30_fee = delivery_fee("BTC", BTC_SPOT, QTY, btc_option_price, expiry_days=30)
raw_d30  = BTC_SPOT * config.OPTIONS_DELIVERY_FEE_PCT * QTY
cap_d30  = btc_option_price * QTY * config.OPTIONS_DELIVERY_FEE_CAP
print(f"\n  Near leg ITM — monthly (30d), expiry_days=30, option_price=${btc_option_price:.0f}:")
print(f"    Raw delivery fee  : ${raw_d30:.4f}  (0.015% × ${BTC_SPOT:,.0f})")
print(f"    Cap (12.5%)       : ${cap_d30:.4f}  (12.5% × ${btc_option_price:.0f})")
print(f"    Applied fee       : ${d30_fee:.4f}")

# Monthly with small option value — cap kicks in
small_option  = 20.0
d30_small_fee = delivery_fee("BTC", BTC_SPOT, QTY, small_option, expiry_days=30)
cap_small     = small_option * QTY * config.OPTIONS_DELIVERY_FEE_CAP
print(f"\n  Monthly, cheap option (option_price=${small_option:.0f} — cap applies):")
print(f"    Raw delivery fee  : ${raw_d30:.4f}")
print(f"    Cap (12.5%)       : ${cap_small:.4f}")
print(f"    Applied fee       : ${d30_small_fee:.4f}  (capped)")


# ── 4. Roll fees vs theta gain ────────────────────────────────────────────────

section("4. Roll fees vs theta gain — break-even analysis")

print("\n  Rolling the near leg (close old near, open new near) incurs two leg fees.")
print("  The roll is only economically sensible if the new near premium > roll fees.")
print()

for near_prem, new_near_prem in [(300.0, 400.0), (50.0, 80.0), (15.0, 20.0)]:
    rf     = roll_fees("BTC", BTC_SPOT, QTY, near_prem, new_near_prem)
    gain   = new_near_prem * QTY
    net    = gain - rf
    viable = "✓ ROLL" if net > 0 else "✗ SKIP"
    print(f"  old_near=${near_prem:.0f}  new_near=${new_near_prem:.0f}  "
          f"roll_fee=${rf:.2f}  theta_gain=${gain:.2f}  net=${net:.2f}  {viable}")


# ── 5. Early close — gross vs net P&L ────────────────────────────────────────

section("5. Early close (stop-loss / take-profit) — gross vs net P&L")

entry_debit   = 1_000.0    # net debit paid at entry
entry_fee_val = entry_fees("BTC", BTC_SPOT, QTY, btc_near_price, btc_far_price, via_combo=True)

print(f"\n  Entry: net_debit=${entry_debit:.2f}  entry_fee=${entry_fee_val:.2f}")

for label, close_sv in [("Stop-loss (50%)", 0.50 * entry_debit),
                         ("Take-profit (150%)", 1.50 * entry_debit)]:
    # At close, near leg has declined and far leg has grown
    close_near = btc_near_price * 0.3   # near decayed
    close_far  = btc_far_price  * 1.2   # far grown
    close_fee  = exit_fees("BTC", BTC_SPOT, QTY, close_near, close_far)
    gross_pnl  = close_sv - entry_debit
    net_pnl    = gross_pnl - entry_fee_val - close_fee
    print(f"\n  {label}:  spread_value=${close_sv:.2f}")
    print(f"    Gross P&L         : ${gross_pnl:+.2f}")
    print(f"    Exit fee          : ${close_fee:.2f}")
    print(f"    Net P&L (after fees): ${net_pnl:+.2f}  "
          f"(entry_fee=${entry_fee_val:.2f} + close_fee=${close_fee:.2f})")


# ── 6. Round-trip summary ─────────────────────────────────────────────────────

section("6. Round-trip fee summary")

for asset, spot, near_p, far_p in [
    ("BTC", 100_000.0, 500.0,  1_200.0),
    ("ETH",   3_000.0,  20.0,     50.0),
    ("SOL",     150.0,   1.0,      2.5),
]:
    rt  = round_trip_fees(asset, spot, 1.0, near_price=near_p, far_price=far_p, via_combo=True)
    nd  = far_p - near_p   # approximate net debit
    pct = rt / nd * 100 if nd > 0 else 0
    print(f"  {asset:<4}  spot=${spot:>8,.0f}  near=${near_p:.2f}  far=${far_p:.2f}  "
          f"round_trip=${rt:.4f}  ({pct:.1f}% of debit)")

print(f"\n{DIVIDER}")
print("  scratch_fees.py complete")
print(DIVIDER)
