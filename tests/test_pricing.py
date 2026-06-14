"""Unit tests for core/pricing.py."""

import math
import pytest
from core.pricing import (
    ncdf,
    npdf,
    bs_call,
    bs_put,
    delta_call,
    delta_put,
    gamma,
    vega,
    theta_call,
    theta_put,
    prob_otm_call,
    prob_otm_put,
    strike_increment,
    round_strike,
    adjust_far_leg_price,
)

# Common test parameters: BTC-like at-the-money
S = 50_000.0
K = 50_000.0
T = 30 / 365
r = 0.0
v = 0.80


# ── Normal helpers ────────────────────────────────────────────────────────────

def test_ncdf_known_values():
    assert abs(ncdf(0) - 0.5) < 1e-9
    assert abs(ncdf(1.96) - 0.975) < 0.001
    assert ncdf(-10) < 1e-6
    assert ncdf(10) > 1 - 1e-6


def test_npdf_known_values():
    expected = 1 / math.sqrt(2 * math.pi)
    assert abs(npdf(0) - expected) < 1e-9
    assert npdf(0) > npdf(1) > npdf(3)


# ── Put-call parity ───────────────────────────────────────────────────────────

def test_put_call_parity():
    """C - P = S - K*exp(-rT)"""
    call = bs_call(S, K, T, r, v)
    put = bs_put(S, K, T, r, v)
    parity = S - K * math.exp(-r * T)
    assert abs((call - put) - parity) < 1e-6


def test_put_call_parity_itm_otm():
    call = bs_call(S, K * 0.9, T, r, v)
    put = bs_put(S, K * 0.9, T, r, v)
    parity = S - K * 0.9 * math.exp(-r * T)
    assert abs((call - put) - parity) < 1e-6


# ── Intrinsic value at expiry ─────────────────────────────────────────────────

def test_bs_call_at_expiry():
    assert bs_call(S, K * 0.9, 0, r, v) == pytest.approx(S - K * 0.9, rel=1e-6)
    assert bs_call(S, K * 1.1, 0, r, v) == 0.0


def test_bs_put_at_expiry():
    assert bs_put(S, K * 1.1, 0, r, v) == pytest.approx(K * 1.1 - S, rel=1e-6)
    assert bs_put(S, K * 0.9, 0, r, v) == 0.0


# ── Option prices are positive ────────────────────────────────────────────────

def test_option_prices_positive():
    assert bs_call(S, K, T, r, v) > 0
    assert bs_put(S, K, T, r, v) > 0


# ── ATM call ≈ put for r=0 ────────────────────────────────────────────────────

def test_atm_call_equals_put_zero_rate():
    call = bs_call(S, K, T, 0.0, v)
    put = bs_put(S, K, T, 0.0, v)
    assert abs(call - put) < 1e-6


# ── Greeks sign checks ────────────────────────────────────────────────────────

def test_delta_call_range():
    d = delta_call(S, K, T, r, v)
    assert 0 < d < 1


def test_delta_put_range():
    d = delta_put(S, K, T, r, v)
    assert -1 < d < 0


def test_delta_call_minus_put_equals_one():
    # delta_call - delta_put = 1 by put-call parity
    assert abs(delta_call(S, K, T, r, v) - delta_put(S, K, T, r, v) - 1.0) < 1e-9


def test_gamma_positive():
    assert gamma(S, K, T, r, v) > 0


def test_vega_positive():
    assert vega(S, K, T, r, v) > 0


def test_theta_call_negative():
    assert theta_call(S, K, T, r, v) < 0


def test_theta_put_negative():
    assert theta_put(S, K, T, r, v) < 0


# ── Greeks at expiry ──────────────────────────────────────────────────────────

def test_greeks_at_expiry_are_zero():
    assert gamma(S, K, 0, r, v) == 0.0
    assert vega(S, K, 0, r, v) == 0.0
    assert theta_call(S, K, 0, r, v) == 0.0
    assert theta_put(S, K, 0, r, v) == 0.0


# ── Probability helpers ───────────────────────────────────────────────────────

def test_prob_otm_put_atm():
    p = prob_otm_put(S, K, T, r, v)
    assert 0.4 < p < 0.6


def test_prob_otm_call_atm():
    p = prob_otm_call(S, K, T, r, v)
    assert 0.4 < p < 0.6


def test_prob_otm_call_deep_itm():
    # Very deep ITM call (S >> K) is very unlikely to expire OTM
    p = prob_otm_call(S * 2, K, T, r, v)
    assert p < 0.05


def test_prob_otm_put_deep_otm():
    # Put with S >> K is almost certain to expire OTM
    p = prob_otm_put(S * 2, K, T, r, v)
    assert p > 0.95


def test_prob_otm_sum_not_one():
    # prob_otm_call + prob_otm_put != 1 in general (they're not complementary for the same option)
    # but for the same S, K, T, r, v with r=0 and ATM they should sum close to 1
    p_call = prob_otm_call(S, K, T, 0.0, v)
    p_put = prob_otm_put(S, K, T, 0.0, v)
    assert abs(p_call + p_put - 1.0) < 1e-9


# ── Strike helpers ────────────────────────────────────────────────────────────

def test_strike_increment_btc():
    assert strike_increment(50_000) == 100.0


def test_strike_increment_eth():
    assert strike_increment(2_500) == 100.0


def test_round_strike():
    assert round_strike(50_123, 50_000) == 50_100.0
    assert round_strike(2_481, 2_500) == 2_500.0


def test_round_strike_never_zero():
    assert round_strike(0, 50_000) > 0


# ── adjust_far_leg_price ──────────────────────────────────────────────────────

def test_adjust_far_leg_buy_more_than_mid():
    adjusted = adjust_far_leg_price(1000.0, 45, is_buy=True)
    assert adjusted > 1000.0


def test_adjust_far_leg_sell_less_than_mid():
    adjusted = adjust_far_leg_price(1000.0, 45, is_buy=False)
    assert adjusted < 1000.0


def test_adjust_far_leg_longer_expiry_larger_spread():
    buy_30 = adjust_far_leg_price(1000.0, 30, is_buy=True)
    buy_60 = adjust_far_leg_price(1000.0, 60, is_buy=True)
    assert buy_60 > buy_30
