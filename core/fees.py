"""
core/fees.py
============
Fee calculation for Deribit trading.

Deribit fee structure (options):
  - 0.04% of underlying spot price (i.e. 0.0004 × spot, in USD)
  - Cannot exceed 12.5% of option price

This module provides:
  - calculate_fee()         — fee for a single option leg
  - calculate_spread_fees() — total fees for a two-leg calendar spread
"""


def calculate_fee(spot: float, option_price: float, asset: str = "BTC") -> float:
    """
    Calculate trading fee per Deribit structure.

    Args:
        spot: Underlying spot price in USD
        option_price: Option premium in USD
        asset: Asset ticker ("BTC", "ETH", "SOL", "XRP") — reserved for future
               per-asset overrides; currently all assets use the same 0.04% rate

    Returns:
        Fee amount in USD

    Raises:
        ValueError: If spot or option_price is negative, or asset is empty
    """
    if spot < 0:
        raise ValueError(f"Spot price cannot be negative: {spot}")
    if option_price < 0:
        raise ValueError(f"Option price cannot be negative: {option_price}")
    if not asset or not isinstance(asset, str):
        raise ValueError(f"Asset must be a non-empty string: {asset}")

    # 0.04% of underlying spot price
    fee = spot * 0.0004

    # Cap at 12.5% of option price
    max_fee = option_price * 0.125

    return min(fee, max_fee)


def calculate_spread_fees(
    spot: float,
    near_price: float,
    far_price: float,
    asset: str = "BTC",
) -> dict:
    """
    Calculate total fees for a two-leg calendar spread (entry or exit).

    Args:
        spot: Underlying spot price in USD
        near_price: Near-leg option premium in USD
        far_price: Far-leg option premium in USD
        asset: Asset ticker

    Returns:
        dict with keys: near_fee, far_fee, total_fee (all in USD)
    """
    near_fee = calculate_fee(spot, near_price, asset)
    far_fee = calculate_fee(spot, far_price, asset)
    return {
        "near_fee": near_fee,
        "far_fee": far_fee,
        "total_fee": near_fee + far_fee,
    }
