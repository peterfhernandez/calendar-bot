import pytest
from core.fees import calculate_fee, calculate_spread_fees


class TestCalculateFee:
    def test_basic_fee(self):
        # 0.04% of 50000 = 20 USD; option price 500 → cap = 62.5 → fee = 20
        assert calculate_fee(50_000, 500) == pytest.approx(20.0)

    def test_cap_at_12_5_pct(self):
        # fee = 0.0004 * 100 = 0.04; cap = 0.125 * 0.10 = 0.0125 → capped
        assert calculate_fee(100, 0.10) == pytest.approx(0.0125)

    def test_zero_spot(self):
        assert calculate_fee(0, 100) == pytest.approx(0.0)

    def test_zero_option_price(self):
        # cap is 0, fee is also 0
        assert calculate_fee(50_000, 0) == pytest.approx(0.0)

    def test_negative_spot_raises(self):
        with pytest.raises(ValueError, match="Spot price cannot be negative"):
            calculate_fee(-1, 100)

    def test_negative_option_price_raises(self):
        with pytest.raises(ValueError, match="Option price cannot be negative"):
            calculate_fee(50_000, -1)

    def test_empty_asset_raises(self):
        with pytest.raises(ValueError, match="Asset must be a non-empty string"):
            calculate_fee(50_000, 100, "")

    def test_non_string_asset_raises(self):
        with pytest.raises(ValueError):
            calculate_fee(50_000, 100, None)

    def test_eth_asset(self):
        # same formula, just confirming the asset param is accepted
        assert calculate_fee(3_000, 200, "ETH") == pytest.approx(1.2)


class TestCalculateSpreadFees:
    def test_returns_correct_keys(self):
        result = calculate_spread_fees(50_000, 300, 500)
        assert set(result.keys()) == {"near_fee", "far_fee", "total_fee"}

    def test_total_is_sum(self):
        result = calculate_spread_fees(50_000, 300, 500)
        assert result["total_fee"] == pytest.approx(result["near_fee"] + result["far_fee"])

    def test_values(self):
        # fee per leg = 0.0004 * 50000 = 20; neither price triggers the cap
        result = calculate_spread_fees(50_000, 300, 500)
        assert result["near_fee"] == pytest.approx(20.0)
        assert result["far_fee"] == pytest.approx(20.0)
        assert result["total_fee"] == pytest.approx(40.0)
