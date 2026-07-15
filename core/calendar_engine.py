"""
core/calendar_engine.py
=======================
Calendar spread valuation, stop/take-profit evaluation, and P&L helpers.

Ported from optionsStrat/strategies/calendar.py.
Interactive paper-trading menus have been removed; this module is pure logic
intended to be called by the bot's scanner, decision engine, and monitor.

Public API
----------
spread_value(spot, strike, T_near, T_far, r, iv, qty, option_type)
    Current mark-to-market value of an open calendar spread.

pnl_at_near_expiry(spot_close, strike, near_days, far_days, r, iv, qty, net_debit, option_type)
    Estimated P&L when the near leg expires at a given closing spot price.

find_breakevens(spot, strike, near_days, far_days, r, iv, qty, net_debit, option_type)
    Numerically locate lower and upper breakeven prices at near-leg expiry.

check_calendar_status(spot, iv, near_days_left, far_days_left, op)
    Evaluate stop / take-profit / warn conditions for an open position.
"""

from config import BREAKEVEN_SCAN_STEPS, BREAKEVEN_SCAN_RANGE, SPREAD_WARN_PCT, RISK_FREE_RATE, STOP_PCT, TAKE_PROFIT_PCT 
 
from core.pricing import bs_call, bs_put


# ── P&L helpers ───────────────────────────────────────────────────────────────

def spread_value(
    spot: float,
    strike: float,
    T_near: float,
    T_far: float,
    r: float,
    iv: float,
    qty: float,
    option_type: str,
) -> float:
    """
    Current mark-to-market value of an open calendar spread.

    Returns (far_leg_value - near_leg_value) in USD.
    Positive when the far leg is worth more than the short near leg (normal).
    """
    if option_type == "Call":
        far_val  = bs_call(spot, strike, T_far,  r, iv) * qty
        near_val = bs_call(spot, strike, T_near, r, iv) * qty
    else:
        far_val  = bs_put(spot, strike, T_far,  r, iv) * qty
        near_val = bs_put(spot, strike, T_near, r, iv) * qty
    return far_val - near_val


def pnl_at_near_expiry(
    spot_close: float,
    strike: float,
    near_days: int,
    far_days: int,
    r: float,
    iv: float,
    qty: float,
    net_debit: float,
    option_type: str,
) -> float:
    """
    Estimated P&L at near-leg expiry for a given closing spot price.

    Near leg is settled at intrinsic value (ITM) or zero (OTM).
    Far leg is valued with Black-Scholes at the remaining time (far - near days).
    """
    T_remaining = max(far_days - near_days, 1) / 365.0

    if option_type == "Call":
        near_cost = max(spot_close - strike, 0) * qty
        far_val   = bs_call(spot_close, strike, T_remaining, r, iv) * qty
    else:
        near_cost = max(strike - spot_close, 0) * qty
        far_val   = bs_put(spot_close, strike, T_remaining, r, iv) * qty

    return far_val - near_cost - net_debit


def find_breakevens(
    spot: float,
    strike: float,
    near_days: int,
    far_days: int,
    r: float,
    iv: float,
    qty: float,
    net_debit: float,
    option_type: str,
    n_steps: int = BREAKEVEN_SCAN_STEPS,
) -> tuple[float, float]:
    """
    Numerically locate lower and upper breakeven prices at near-leg expiry.

    Scans spot * config.BREAKEVEN_SCAN_RANGE for sign changes in P&L.
    Returns (be_lo, be_hi); both are 0.0 if no crossings are found.
    """
    lo = spot * BREAKEVEN_SCAN_RANGE[0]
    hi = spot * BREAKEVEN_SCAN_RANGE[1]
    step = (hi - lo) / n_steps
    prices = [lo + i * step for i in range(n_steps + 1)]
    pnls = [
        pnl_at_near_expiry(p, strike, near_days, far_days, r, iv, qty, net_debit, option_type)
        for p in prices
    ]

    be_lo = be_hi = 0.0
    for i in range(len(pnls) - 1):
        if pnls[i] < 0 <= pnls[i + 1]:
            be_lo = prices[i]
        if pnls[i] >= 0 > pnls[i + 1]:
            be_hi = prices[i + 1]
    return be_lo, be_hi


# ── Status checker ────────────────────────────────────────────────────────────

def check_calendar_status(
    spot: float,
    iv: float,
    near_days_left: int,
    far_days_left: int,
    op: dict,
    market_sv: float | None = None,
) -> tuple[str, float, float, str]:
    """
    Evaluate the current status of an open calendar spread position.

    Parameters
    ----------
    spot           : float  Current underlying price
    iv             : float  Current implied volatility (decimal)
    near_days_left : int    Days remaining until near-leg expiry
    far_days_left  : int    Days remaining until far-leg expiry
    op             : dict   Open position dict with keys:
                            net_debit, qty, strike, option_type
    market_sv      : float | None
                    If provided, use this as the current spread value instead of
                    computing it via Black-Scholes.  Pass the market mid-price
                    spread (far_mid - near_mid) * qty when live bid/ask data is
                    available — B-S with a single uniform IV can be wildly wrong
                    for options far from ATM or with strong IV skew.

    Returns
    -------
    tuple of (status, spread_val, pct_of_debit, message)
        status       : "ok" | "warn" | "stop" | "tp"
        spread_val   : float  Current spread mark value (USD)
        pct_of_debit : float  spread_val / net_debit paid at entry
        message      : str    Human-readable status description
    """
    T_near = max(near_days_left / 365.0, 1 / 365.0)
    T_far  = max(far_days_left  / 365.0, 1 / 365.0)
    net_debit   = op["net_debit"]
    qty         = op["qty"]
    strike      = op["strike"]
    option_type = op["option_type"]

    if market_sv is not None:
        sv = market_sv
    else:
        sv = spread_value(spot, strike, T_near, T_far, RISK_FREE_RATE, iv, qty, option_type)
    total_debit = net_debit * qty
    pct = sv / total_debit if total_debit > 0 else 0.0
    pnl = sv - total_debit

    if pct <= STOP_PCT:
        msg = (
            f"STOP  spread worth ${sv:.2f} ({pct*100:.0f}% of debit paid).  "
            f"Est. loss: ${abs(pnl):.2f}"
        )
        return "stop", sv, pct, msg

    if pct >= TAKE_PROFIT_PCT:
        msg = (
            f"TAKE-PROFIT  spread worth ${sv:.2f} ({pct*100:.0f}% of debit paid).  "
            f"Est. gain: ${pnl:.2f}"
        )
        return "tp", sv, pct, msg

    if pct <= SPREAD_WARN_PCT:
        msg = (
            f"WARN  spread worth ${sv:.2f} ({pct*100:.0f}% of debit).  "
            f"Hard stop at {STOP_PCT*100:.0f}%."
        )
        return "warn", sv, pct, msg

    msg = f"OK  {pct*100:.0f}% of debit  (stop at {STOP_PCT*100:.0f}%)"
    return "ok", sv, pct, msg
