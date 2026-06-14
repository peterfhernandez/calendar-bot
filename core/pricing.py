"""
pricing.py
==========
Black-Scholes option pricing and probability helpers for ETH options.

Functions
---------
ncdf(x)                         Standard normal CDF
bs_put(S, K, T, r, v)          Black-Scholes put price
bs_call(S, K, T, r, v)         Black-Scholes call price
prob_otm_put(S, K, T, r, v)    Probability put expires OTM (worthless)
prob_otm_call(S, K, T, r, v)   Probability call expires OTM (worthless)

All prices are in USD. Volatility (v) and risk-free rate (r) are decimals,
e.g. 80% IV → v=0.80, 5% rate → r=0.05. Time (T) is in years.
"""

import math


# ── Normal CDF ────────────────────────────────────────────────────────────────

def ncdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * math.erfc(-x / math.sqrt(2))

# ── d1 / d2 helpers ───────────────────────────────────────────────────────────
 
def _d1(S: float, K: float, T: float, r: float, v: float) -> float:
    """
    Black-Scholes d1 term.
 
    d1 = [ ln(S/K) + (r + v²/2) * T ] / (v * √T)
    """
    return (math.log(S / K) + (r + 0.5 * v ** 2) * T) / (v * math.sqrt(T))
 
 
def _d2(S: float, K: float, T: float, r: float, v: float) -> float:
    """
    Black-Scholes d2 term.
 
    d2 = d1 - v * √T
    """
    return _d1(S, K, T, r, v) - v * math.sqrt(T)


# ── Black-Scholes pricing ─────────────────────────────────────────────────────

def bs_put(S: float, K: float, T: float, r: float, v: float) -> float:
    """
    Black-Scholes price of a European put option.

    Parameters
    ----------
    S : float  Current underlying price (USD)
    K : float  Strike price (USD)
    T : float  Time to expiry in years (e.g. 7/365 for one week)
    r : float  Risk-free rate as a decimal (e.g. 0.05 for 5%)
    v : float  Implied volatility as a decimal (e.g. 0.80 for 80%)

    Returns
    -------
    float  Put option price per unit of underlying
    """
    if T <= 0 or v <= 0:
        return max(K - S, 0)
    d1 = _d1(S, K, T, r, v)
    d2 = _d2(S, K, T, r, v)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1)


def bs_call(S: float, K: float, T: float, r: float, v: float) -> float:
    """
    Black-Scholes price of a European call option.

    Parameters
    ----------
    S : float  Current underlying price (USD)
    K : float  Strike price (USD)
    T : float  Time to expiry in years
    r : float  Risk-free rate as a decimal
    v : float  Implied volatility as a decimal

    Returns
    -------
    float  Call option price per unit of underlying
    """
    if T <= 0 or v <= 0:
        return max(S - K, 0)
    d1 = _d1(S, K, T, r, v)
    d2 = _d2(S, K, T, r, v)
    return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)


# ── Greeks ───────────────────────────────────────────────────────────────────

def delta_call(S: float, K: float, T: float, r: float, v: float) -> float:
    """Delta of a call option: sensitivity to underlying price changes."""
    if T <= 0 or v <= 0:
        return 1.0 if S > K else 0.0
    return ncdf(_d1(S, K, T, r, v))


def delta_put(S: float, K: float, T: float, r: float, v: float) -> float:
    """Delta of a put option: sensitivity to underlying price changes."""
    return delta_call(S, K, T, r, v) - 1.0


def gamma(S: float, K: float, T: float, r: float, v: float) -> float:
    """Gamma: rate of change of delta with respect to underlying price."""
    if T <= 0 or v <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, v)
    return ncdf(d1) / (S * v * math.sqrt(T))


def vega(S: float, K: float, T: float, r: float, v: float) -> float:
    """Vega: sensitivity to implied volatility (per 1% change)."""
    if T <= 0 or v <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, v)
    return S * ncdf(d1) * math.sqrt(T) / 100.0


def theta_call(S: float, K: float, T: float, r: float, v: float) -> float:
    """Theta of a call: time decay (per day)."""
    if T <= 0 or v <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, v)
    d2 = _d2(S, K, T, r, v)
    term1 = -S * ncdf(d1) * v / (2 * math.sqrt(T))
    term2 = r * K * math.exp(-r * T) * ncdf(d2)
    return (term1 - term2) / 365.0


def theta_put(S: float, K: float, T: float, r: float, v: float) -> float:
    """Theta of a put: time decay (per day)."""
    if T <= 0 or v <= 0:
        return 0.0
    d1 = _d1(S, K, T, r, v)
    d2 = _d2(S, K, T, r, v)
    term1 = -S * ncdf(d1) * v / (2 * math.sqrt(T))
    term2 = r * K * math.exp(-r * T) * ncdf(-d2)
    return (term1 + term2) / 365.0


# ── Probability helpers ───────────────────────────────────────────────────────

def prob_otm_put(S: float, K: float, T: float, r: float, v: float) -> float:
    """
    Probability that a put expires out-of-the-money (i.e. spot > strike).

    Uses the risk-neutral probability N(d2) from Black-Scholes.
    Returns a value in [0, 1].
    """
    if T <= 0 or v <= 0:
        return 1.0 if S > K else 0.0
    return ncdf(_d2(S, K, T, r, v))


def prob_otm_call(S: float, K: float, T: float, r: float, v: float) -> float:
    """
    Probability that a call expires out-of-the-money (i.e. spot < strike).

    Uses the risk-neutral probability N(-d2) from Black-Scholes.
    Returns a value in [0, 1].
    """
    if T <= 0 or v <= 0:
        return 1.0 if S < K else 0.0
    return ncdf(-_d2(S, K, T, r, v))


# ── Strike rounding ───────────────────────────────────────────────────────────

def strike_increment(spot: float) -> float:
    """Return a sensible strike increment for a given spot price."""
    if spot < 5:
        return 0.50
    if spot < 20:
        return 1.0
    if spot < 100:
        return 5.0
    if spot < 500:
        return 10.0
    if spot < 2_000:
        return 50.0
    return 100.0


def round_strike(price: float, spot: float) -> float:
    """Round *price* to the nearest strike increment appropriate for *spot*."""
    inc = strike_increment(spot)
    rounded = round(price / inc) * inc
    return max(rounded, inc)  # ensure strike is never zero


# ── Liquidity adjustments ─────────────────────────────────────────────────────

def adjust_far_leg_price(
    mid_price: float,
    days_to_expiry: int,
    is_buy: bool = True,
) -> float:
    """
    Adjust option price for a far-leg purchase in a calendar spread.

    Far-dated options have wider bid/ask spreads and lower liquidity.
    When buying (is_buy=True), we move away from mid toward the ask.
    When selling (is_buy=False), we move away from mid toward the bid.

    Parameters
    ----------
    mid_price : float
        Black-Scholes mid price
    days_to_expiry : int
        Days until option expiry
    is_buy : bool
        True if we're buying (pay the ask), False if selling (take the bid)

    Returns
    -------
    float
        Adjusted price accounting for bid/ask spread and liquidity
    """
    # Base bid/ask spread as % of mid price
    # Near-dated (< 14 days): ~0.5% spread
    # Medium (14-30 days): ~1% spread
    # Far-dated (> 30 days): ~2% spread
    if days_to_expiry <= 7:
        spread_pct = 0.005
    elif days_to_expiry <= 14:
        spread_pct = 0.010
    elif days_to_expiry <= 30:
        spread_pct = 0.015
    else:
        spread_pct = 0.025

    # Additional liquidity premium for very far dates (penalty for poor liquidity)
    # Increases with time to expiry
    liquidity_penalty = 0.0
    if days_to_expiry > 30:
        # Scale penalty: +0.5% per 30 days beyond 30d
        excess_days = days_to_expiry - 30
        liquidity_penalty = (excess_days / 30.0) * 0.005

    total_adjustment_pct = spread_pct + liquidity_penalty

    if is_buy:
        # Buying: move toward ask (pay more)
        return mid_price * (1 + total_adjustment_pct)
    else:
        # Selling: move toward bid (receive less)
        return mid_price * (1 - total_adjustment_pct)
