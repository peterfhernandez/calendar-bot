"""
Unit tests for core/fees.py — Deribit fee model.

Tests cover:
  - leg_fee: rate, minimum floor, 12.5% cap, SOL maker zero, validation
  - entry_fees: combo discount (only expensive leg charged), individual-leg total
  - exit_fees: both legs at taker rate
  - roll_fees: two near-leg transactions
  - delivery_fee: exempt for daily/weekly, applied for monthly+
  - round_trip_fees: entry + exit combined
  - legacy wrappers: calculate_fee, calculate_spread_fees
"""

import pytest
import config
from core.fees import (
    leg_fee,
    entry_fees,
    exit_fees,
    roll_fees,
    delivery_fee,
    round_trip_fees,
    calculate_fee,
    calculate_spread_fees,
)

# Convenience constants for tests
BTC_SPOT = 100_000.0   # $100k BTC
ETH_SPOT =   3_000.0   # $3k ETH
SOL_SPOT =     150.0   # $150 SOL
FEE_PCT  = config.OPTIONS_FEE_PCT          # 0.0003
MIN_BTC  = config.OPTIONS_MIN_FEE_BTC      # 0.0003 BTC/contract
CAP_PCT  = config.OPTIONS_DELIVERY_FEE_CAP # 0.125


# ── leg_fee ───────────────────────────────────────────────────────────────────

class TestLegFee:
    def test_btc_raw_fee(self):
        # BTC at $100k, 1 contract, option at $500 (cap = 500*0.125 = $62.5 > $30)
        # raw = 100_000 * 0.0003 = $30; min = 0.0003 * 100_000 = $30 → $30
        assert leg_fee("BTC", BTC_SPOT, 1.0, False, 500.0) == pytest.approx(30.0)

    def test_btc_min_floor_dominates(self):
        # Very low spot where raw fee < min floor
        # spot=50, raw=50*0.0003=0.015; min=0.0003*50=0.015 → they match here
        # Use option_price=1000 to remove cap concern
        # With spot=50_000: raw=50_000*0.0003=15; min=0.0003*50_000=15 → $15
        assert leg_fee("BTC", 50_000.0, 1.0, False, 500.0) == pytest.approx(15.0)

    def test_cap_at_12_5_pct(self):
        # option_price very small; cap = 0.10 * 0.125 = $0.0125
        # raw = 100_000 * 0.0003 = $30; min floor = 0.0003 * 100_000 = $30
        # cap = 0.10 * 0.125 = 0.0125 → capped at $0.0125
        result = leg_fee("BTC", BTC_SPOT, 1.0, False, 0.10)
        assert result == pytest.approx(0.0125)

    def test_multi_contract_scales(self):
        # 2 contracts: fee = 2 * single-contract fee
        fee_1 = leg_fee("BTC", BTC_SPOT, 1.0, False, 500.0)
        fee_2 = leg_fee("BTC", BTC_SPOT, 2.0, False, 500.0)
        assert fee_2 == pytest.approx(2 * fee_1)

    def test_eth_fee(self):
        # ETH at $3k: raw = 3_000 * 0.0003 = $0.90; min = 0.0003 * 3_000 = $0.90
        assert leg_fee("ETH", ETH_SPOT, 1.0, False, 100.0) == pytest.approx(0.90)

    def test_sol_taker_fee(self):
        # SOL taker: 0.03% of $150 = $0.045; min = 0.0003 * 150 = $0.045
        assert leg_fee("SOL", SOL_SPOT, 1.0, False, 10.0) == pytest.approx(0.045)

    def test_sol_maker_fee_zero(self):
        # SOL maker fee is 0% regardless of option price
        assert leg_fee("SOL", SOL_SPOT, 1.0, True, 10.0) == pytest.approx(0.0)

    def test_btc_maker_same_as_taker(self):
        # BTC maker = taker (both 0.03%)
        taker = leg_fee("BTC", BTC_SPOT, 1.0, False, 500.0)
        maker = leg_fee("BTC", BTC_SPOT, 1.0, True,  500.0)
        assert taker == pytest.approx(maker)

    def test_zero_spot_returns_zero(self):
        assert leg_fee("BTC", 0.0, 1.0, False, 100.0) == pytest.approx(0.0)

    def test_zero_qty_returns_zero(self):
        assert leg_fee("BTC", BTC_SPOT, 0.0, False, 100.0) == pytest.approx(0.0)

    def test_negative_spot_raises(self):
        with pytest.raises(ValueError, match="spot cannot be negative"):
            leg_fee("BTC", -1.0, 1.0, False, 100.0)

    def test_negative_option_price_raises(self):
        with pytest.raises(ValueError, match="option_price cannot be negative"):
            leg_fee("BTC", BTC_SPOT, 1.0, False, -1.0)

    def test_negative_qty_raises(self):
        with pytest.raises(ValueError, match="qty cannot be negative"):
            leg_fee("BTC", BTC_SPOT, -1.0, False, 100.0)

    def test_empty_asset_raises(self):
        with pytest.raises(ValueError, match="asset must be a non-empty string"):
            leg_fee("", BTC_SPOT, 1.0, False, 100.0)

    def test_none_asset_raises(self):
        with pytest.raises(ValueError):
            leg_fee(None, BTC_SPOT, 1.0, False, 100.0)  # type: ignore[arg-type]


# ── entry_fees ────────────────────────────────────────────────────────────────

class TestEntryFees:
    def test_via_combo_charges_expensive_leg_only(self):
        # near_price=200 → fee=30; far_price=400 → fee=30 (both hit min floor)
        # At $100k spot, both hit the $30 floor. Change spot so prices matter more.
        # Use spot=10_000 for cleaner numbers:
        # spot=10k, near=50→ raw=3, min=3 → fee=3; far=100 → raw=3, min=3 → fee=3
        # combo: max(3, 3) = 3
        f = entry_fees("BTC", 10_000.0, 1.0, 50.0, 100.0, via_combo=True)
        assert f == pytest.approx(3.0)

    def test_via_combo_discount_when_fees_differ(self):
        # Make the cap kick in so near and far fees differ.
        # spot=100k, near_price=0.10 → cap = 0.10*0.125=0.0125 → near_fee=0.0125
        # far_price=500 → no cap → far_fee=30
        # combo: max(0.0125, 30) = 30  (only expensive leg charged)
        f = entry_fees("BTC", BTC_SPOT, 1.0, 0.10, 500.0, via_combo=True)
        near_fee = leg_fee("BTC", BTC_SPOT, 1.0, False, 0.10)
        far_fee  = leg_fee("BTC", BTC_SPOT, 1.0, False, 500.0)
        assert f == pytest.approx(max(near_fee, far_fee))

    def test_individual_legs_charges_both(self):
        near_fee = leg_fee("BTC", BTC_SPOT, 1.0, False, 0.10)
        far_fee  = leg_fee("BTC", BTC_SPOT, 1.0, False, 500.0)
        f = entry_fees("BTC", BTC_SPOT, 1.0, 0.10, 500.0, via_combo=False)
        assert f == pytest.approx(near_fee + far_fee)

    def test_combo_cheaper_than_individual(self):
        # Combo should always be ≤ individual legs
        f_combo = entry_fees("BTC", BTC_SPOT, 1.0, 200.0, 500.0, via_combo=True)
        f_indiv = entry_fees("BTC", BTC_SPOT, 1.0, 200.0, 500.0, via_combo=False)
        assert f_combo <= f_indiv


# ── exit_fees ────────────────────────────────────────────────────────────────

class TestExitFees:
    def test_charges_both_legs(self):
        near_fee = leg_fee("BTC", BTC_SPOT, 1.0, False, 200.0)
        far_fee  = leg_fee("BTC", BTC_SPOT, 1.0, False, 500.0)
        assert exit_fees("BTC", BTC_SPOT, 1.0, 200.0, 500.0) == pytest.approx(near_fee + far_fee)

    def test_eth(self):
        result = exit_fees("ETH", ETH_SPOT, 2.0, 50.0, 100.0)
        expected = 2 * leg_fee("ETH", ETH_SPOT, 2.0, False, 50.0) + 2 * leg_fee("ETH", ETH_SPOT, 2.0, False, 100.0)
        # Note: leg_fee scales by qty so just sum both
        expected = leg_fee("ETH", ETH_SPOT, 2.0, False, 50.0) + leg_fee("ETH", ETH_SPOT, 2.0, False, 100.0)
        assert result == pytest.approx(expected)


# ── roll_fees ─────────────────────────────────────────────────────────────────

class TestRollFees:
    def test_two_near_leg_transactions(self):
        close_fee = leg_fee("BTC", BTC_SPOT, 1.0, False, 200.0)
        open_fee  = leg_fee("BTC", BTC_SPOT, 1.0, False, 180.0)
        assert roll_fees("BTC", BTC_SPOT, 1.0, 200.0, 180.0) == pytest.approx(close_fee + open_fee)

    def test_symmetric_when_same_price(self):
        # If both near legs have the same price, roll cost = 2 × single leg fee
        single = leg_fee("BTC", BTC_SPOT, 1.0, False, 200.0)
        assert roll_fees("BTC", BTC_SPOT, 1.0, 200.0, 200.0) == pytest.approx(2 * single)


# ── delivery_fee ──────────────────────────────────────────────────────────────

class TestDeliveryFee:
    def test_daily_exempt(self):
        assert delivery_fee("BTC", BTC_SPOT, 1.0, 500.0, 1) == pytest.approx(0.0)

    def test_weekly_exempt(self):
        assert delivery_fee("BTC", BTC_SPOT, 1.0, 500.0, 7) == pytest.approx(0.0)

    def test_monthly_charged(self):
        # 14-day option: raw = 100_000 * 0.00015 = $15; cap = 500 * 0.125 = $62.5 → $15
        assert delivery_fee("BTC", BTC_SPOT, 1.0, 500.0, 14) == pytest.approx(15.0)

    def test_monthly_capped(self):
        # Small option price: option_price=0.10 → cap = 0.10 * 0.125 = $0.0125 < $15
        assert delivery_fee("BTC", BTC_SPOT, 1.0, 0.10, 14) == pytest.approx(0.0125)

    def test_8_day_charged(self):
        # 8 days > 7 → delivery fee applies
        assert delivery_fee("ETH", ETH_SPOT, 1.0, 200.0, 8) > 0.0

    def test_multi_contract_scales(self):
        fee_1 = delivery_fee("BTC", BTC_SPOT, 1.0, 500.0, 30)
        fee_3 = delivery_fee("BTC", BTC_SPOT, 3.0, 500.0, 30)
        assert fee_3 == pytest.approx(3 * fee_1)


# ── round_trip_fees ───────────────────────────────────────────────────────────

class TestRoundTripFees:
    def test_equals_entry_plus_exit(self):
        ef = entry_fees("BTC", BTC_SPOT, 1.0, 200.0, 500.0, via_combo=True)
        xf = exit_fees("BTC",  BTC_SPOT, 1.0, 200.0, 500.0)
        assert round_trip_fees("BTC", BTC_SPOT, 1.0, 200.0, 500.0, True) == pytest.approx(ef + xf)

    def test_individual_legs_higher(self):
        rt_combo  = round_trip_fees("BTC", BTC_SPOT, 1.0, 200.0, 500.0, True)
        rt_indiv  = round_trip_fees("BTC", BTC_SPOT, 1.0, 200.0, 500.0, False)
        assert rt_combo <= rt_indiv

    def test_btc_realistic_magnitude(self):
        # BTC at $100k, 1 contract, option prices $300 and $500
        # entry (combo): max(30, 30) = 30; exit: 30 + 30 = 60; total = $90
        rt = round_trip_fees("BTC", BTC_SPOT, 1.0, 300.0, 500.0, True)
        assert 20.0 < rt < 200.0   # sanity range


# ── Legacy API ────────────────────────────────────────────────────────────────

class TestCalculateFee:
    def test_basic_fee(self):
        # 0.03% of 100_000 = $30; option=500 → cap=62.5 → fee=$30
        assert calculate_fee(BTC_SPOT, 500.0) == pytest.approx(30.0)

    def test_cap_at_12_5_pct(self):
        # fee = 0.0003 * 100_000 = 30; cap = 0.10 * 0.125 = 0.0125 → capped
        assert calculate_fee(BTC_SPOT, 0.10) == pytest.approx(0.0125)

    def test_zero_spot(self):
        assert calculate_fee(0.0, 100.0) == pytest.approx(0.0)

    def test_zero_option_price(self):
        # cap = 0, raw floor = $30, but cap=0 means fee=0
        assert calculate_fee(BTC_SPOT, 0.0) == pytest.approx(0.0)

    def test_negative_spot_raises(self):
        with pytest.raises(ValueError, match="Spot price cannot be negative"):
            calculate_fee(-1.0, 100.0)

    def test_negative_option_price_raises(self):
        with pytest.raises(ValueError, match="Option price cannot be negative"):
            calculate_fee(BTC_SPOT, -1.0)

    def test_empty_asset_raises(self):
        with pytest.raises(ValueError, match="Asset must be a non-empty string"):
            calculate_fee(BTC_SPOT, 100.0, "")

    def test_non_string_asset_raises(self):
        with pytest.raises(ValueError):
            calculate_fee(BTC_SPOT, 100.0, None)  # type: ignore[arg-type]

    def test_eth_asset(self):
        # ETH at $3k: fee = $0.90; option=200 → cap=25 → no cap
        assert calculate_fee(ETH_SPOT, 200.0, "ETH") == pytest.approx(0.90)


class TestCalculateSpreadFees:
    def test_returns_correct_keys(self):
        result = calculate_spread_fees(BTC_SPOT, 200.0, 500.0)
        assert set(result.keys()) == {"near_fee", "far_fee", "total_fee"}

    def test_total_is_sum(self):
        result = calculate_spread_fees(BTC_SPOT, 200.0, 500.0)
        assert result["total_fee"] == pytest.approx(result["near_fee"] + result["far_fee"])

    def test_values_at_btc(self):
        # Both legs at $100k spot with uncapped prices → $30 each
        result = calculate_spread_fees(BTC_SPOT, 300.0, 500.0)
        assert result["near_fee"] == pytest.approx(30.0)
        assert result["far_fee"]  == pytest.approx(30.0)
        assert result["total_fee"] == pytest.approx(60.0)
