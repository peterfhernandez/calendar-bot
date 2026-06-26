"""
core/fees.py
============
Fee calculation for Deribit options trading.

Deribit fee schedule (options):
  - Taker and maker fee: 0.03% of underlying index price per leg
  - Minimum fee: 0.0003 BTC/ETH/SOL per contract
  - SOL maker fee: 0% (taker only for SOL)
  - Cap: fee per leg cannot exceed 12.5% of option market value
  - Combo taker orders: 100% discount on the cheaper leg

Delivery fees (charged at expiry when option is ITM and cash-settled):
  - Daily (≤1d) and weekly (≤7d) near legs: 0% delivery fee
  - Monthly and longer (>7d): 0.015% of underlying, capped at 12.5% of option value

Public API
----------
leg_fee(asset, spot, qty, is_maker, option_price)
    Per-leg fee in USD.

entry_fees(asset, spot, qty, near_price, far_price, via_combo)
    Total entry cost; applies combo cheap-leg discount when via_combo=True.

exit_fees(asset, spot, qty, near_price, far_price)
    Total exit cost for closing both legs.

roll_fees(asset, spot, qty, near_price, new_near_price)
    Fee to close old near leg and open new near leg.

delivery_fee(asset, spot, qty, option_price, expiry_days)
    Delivery fee at expiry; 0 for daily/weekly near legs.

round_trip_fees(asset, spot, qty, near_price, far_price, via_combo)
    Entry plus exit fees combined; used in EV calculations.
"""

from __future__ import annotations

import config


def _min_fee_usd(asset: str, spot: float, qty: float) -> float:
    """Minimum fee in USD per Deribit fee schedule (0.0003 native/contract × spot)."""
    asset_up = asset.upper()
    if asset_up == "BTC":
        min_native = config.OPTIONS_MIN_FEE_BTC
    elif asset_up == "ETH":
        min_native = config.OPTIONS_MIN_FEE_ETH
    else:
        min_native = config.OPTIONS_MIN_FEE_SOL
    return min_native * spot * qty


def leg_fee(
    asset: str,
    spot: float,
    qty: float,
    is_maker: bool,
    option_price: float,
) -> float:
    """
    Calculate the trading fee for a single option leg in USD.

    Parameters
    ----------
    asset
        Underlying asset ("BTC", "ETH", "SOL").
    spot
        Current spot/index price of the underlying in USD.
    qty
        Number of contracts.
    is_maker
        True for maker orders. SOL maker fee is 0%; BTC/ETH maker = taker = 0.03%.
    option_price
        Option premium in USD per contract.

    Returns
    -------
    float
        Fee amount in USD (>= 0).
    """
    if spot < 0:
        raise ValueError(f"spot cannot be negative: {spot}")
    if option_price < 0:
        raise ValueError(f"option_price cannot be negative: {option_price}")
    if qty < 0:
        raise ValueError(f"qty cannot be negative: {qty}")
    if not asset or not isinstance(asset, str):
        raise ValueError(f"asset must be a non-empty string: {asset}")
    if qty == 0:
        return 0.0

    # SOL maker fee is 0%
    if is_maker and asset.upper() == "SOL":
        return 0.0

    # Raw fee: OPTIONS_FEE_PCT of underlying × qty
    raw_fee = spot * config.OPTIONS_FEE_PCT * qty

    # Apply minimum fee floor
    min_fee = _min_fee_usd(asset, spot, qty)
    fee = max(raw_fee, min_fee)

    # Cap at 12.5% of option value (applies even when cap is 0)
    cap = option_price * qty * config.OPTIONS_DELIVERY_FEE_CAP
    if option_price >= 0:
        fee = min(fee, cap)

    return fee


def entry_fees(
    asset: str,
    spot: float,
    qty: float,
    near_price: float,
    far_price: float,
    via_combo: bool = True,
) -> float:
    """
    Total entry fee for a calendar spread in USD.

    When via_combo is True, the cheaper leg receives a 100% taker discount
    (Deribit combo order discount — only the more expensive leg is charged).

    Parameters
    ----------
    near_price
        Near-leg option premium in USD per contract (collected, short leg).
    far_price
        Far-leg option premium in USD per contract (paid, long leg).
    via_combo
        True if entering via a Deribit combo/spread order.
    """
    near_fee = leg_fee(asset, spot, qty, is_maker=False, option_price=near_price)
    far_fee  = leg_fee(asset, spot, qty, is_maker=False, option_price=far_price)

    if via_combo:
        # 100% taker discount on the cheaper leg — only the expensive leg is charged
        return max(near_fee, far_fee)
    return near_fee + far_fee


def exit_fees(
    asset: str,
    spot: float,
    qty: float,
    near_price: float,
    far_price: float,
) -> float:
    """
    Total exit fee for closing both legs of a calendar spread in USD.

    Both legs charged at taker rate (no combo discount on exit).
    """
    near_fee = leg_fee(asset, spot, qty, is_maker=False, option_price=near_price)
    far_fee  = leg_fee(asset, spot, qty, is_maker=False, option_price=far_price)
    return near_fee + far_fee


def roll_fees(
    asset: str,
    spot: float,
    qty: float,
    near_price: float,
    new_near_price: float,
) -> float:
    """
    Total fee for rolling the near leg in USD.

    A roll closes the existing near leg (buy to close) and opens a new near
    leg (sell to open) — two taker transactions on the near leg only.
    The far leg is untouched.
    """
    close_fee = leg_fee(asset, spot, qty, is_maker=False, option_price=near_price)
    open_fee  = leg_fee(asset, spot, qty, is_maker=False, option_price=new_near_price)
    return close_fee + open_fee


def delivery_fee(
    asset: str,
    spot: float,
    qty: float,
    option_price: float,
    expiry_days: int,
) -> float:
    """
    Delivery fee charged at expiry when an option is ITM and cash-settled.

    Daily (≤1d) and weekly (≤7d) options are exempt.
    Monthly and longer (>7d): 0.015% of underlying, capped at 12.5% of option value.

    Parameters
    ----------
    expiry_days
        Days to expiry of the leg being settled.
    option_price
        Current market value of the option in USD per contract.
    """
    if expiry_days <= 7:
        return 0.0

    raw_fee = spot * config.OPTIONS_DELIVERY_FEE_PCT * qty
    cap     = option_price * qty * config.OPTIONS_DELIVERY_FEE_CAP
    if cap > 0:
        return min(raw_fee, cap)
    return raw_fee


def round_trip_fees(
    asset: str,
    spot: float,
    qty: float,
    near_price: float,
    far_price: float,
    via_combo: bool = True,
) -> float:
    """
    Total round-trip fee (entry + exit) for a calendar spread in USD.

    Used in EV calculations to determine the fee drag on a candidate before
    comparing to MIN_EV. Assumes individual-leg exit (no combo discount on exit).
    """
    return (
        entry_fees(asset, spot, qty, near_price, far_price, via_combo)
        + exit_fees(asset, spot, qty, near_price, far_price)
    )


# ── Legacy API (backward compatibility) ──────────────────────────────────────

def calculate_fee(spot: float, option_price: float, asset: str = "BTC") -> float:
    """
    Legacy single-leg fee. Use leg_fee() for new code.

    Retained for backward compatibility. Uses current Deribit schedule (0.03%,
    12.5% cap, min floor). Unlike the old 0.04% implementation, this now
    reflects the correct live fee rate.
    """
    if spot < 0:
        raise ValueError(f"Spot price cannot be negative: {spot}")
    if option_price < 0:
        raise ValueError(f"Option price cannot be negative: {option_price}")
    if not asset or not isinstance(asset, str):
        raise ValueError(f"Asset must be a non-empty string: {asset}")
    return leg_fee(asset, spot, qty=1.0, is_maker=False, option_price=option_price)


def calculate_spread_fees(
    spot: float,
    near_price: float,
    far_price: float,
    asset: str = "BTC",
) -> dict:
    """
    Legacy two-leg spread fee. Use entry_fees() / exit_fees() for new code.
    """
    near_fee = calculate_fee(spot, near_price, asset)
    far_fee  = calculate_fee(spot, far_price,  asset)
    return {
        "near_fee":  near_fee,
        "far_fee":   far_fee,
        "total_fee": near_fee + far_fee,
    }
