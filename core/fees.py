"""
trading/fee_calculator.py
========================
Fee calculation for Deribit trading.

Deribit fee structure:
  - 0.04% of underlying spot price, OR
  - 0.0004 BTC (if asset is BTC), OR
  - 0.0004 ETH (if asset is ETH), etc.
  - Whichever is LOWER
  - But cannot exceed 12.5% of option price

This module provides calculate_fee() to compute the trading fee for any option
trade, respecting the Deribit fee cap.
"""


def calculate_fee(spot: float, option_price: float, asset: str = "BTC") -> float:
    """
    Calculate trading fee per Deribit structure.

    Args:
        spot: Underlying spot price (USD for BTC/ETH, or coin value)
        option_price: Premium of the option being traded (USD or coin value)
        asset: Asset ticker ("BTC", "ETH", "SOL", "XRP")

    Returns:
        Fee amount in same units as option_price

    Raises:
        ValueError: If spot or option_price is negative, or asset is empty
    """
    if spot < 0:
        raise ValueError(f"Spot price cannot be negative: {spot}")
    if option_price < 0:
        raise ValueError(f"Option price cannot be negative: {option_price}")
    if not asset or not isinstance(asset, str):
        raise ValueError(f"Asset must be a non-empty string: {asset}")

    # 0.04% of spot price
    fee_spot_pct = spot * 0.0004

    # For BTC, also consider the fixed 0.0004 BTC fee (converted to same currency as spot)
    if asset == "BTC":
        fee_btc_fixed = 0.0004 * spot
        fee = min(fee_spot_pct, fee_btc_fixed)
    else:
        # For other assets, just use the spot percentage
        fee = fee_spot_pct

    # Cap at 12.5% of option price
    max_fee = option_price * 0.125

    return min(fee, max_fee)
