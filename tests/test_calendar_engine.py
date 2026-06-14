"""
Unit tests for core/calendar_engine.py
"""

import pytest
from core.calendar_engine import (
    spread_value,
    pnl_at_near_expiry,
    find_breakevens,
    check_calendar_status,
    RISK_FREE_RATE,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

SPOT   = 100_000.0  # BTC-like
STRIKE = 100_000.0
IV     = 0.80
R      = RISK_FREE_RATE
QTY    = 0.01
NEAR   = 7    # days
FAR    = 30   # days


# ── spread_value ──────────────────────────────────────────────────────────────

class TestSpreadValue:
    def test_call_far_worth_more_than_near(self):
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        sv = spread_value(SPOT, STRIKE, T_near, T_far, R, IV, QTY, "Call")
        assert sv > 0, "Far leg should be worth more than near leg at same strike"

    def test_put_far_worth_more_than_near(self):
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        sv = spread_value(SPOT, STRIKE, T_near, T_far, R, IV, QTY, "Put")
        assert sv > 0

    def test_zero_when_T_near_equals_T_far(self):
        T = NEAR / 365.0
        sv = spread_value(SPOT, STRIKE, T, T, R, IV, QTY, "Call")
        assert abs(sv) < 1e-9

    def test_spread_increases_with_iv(self):
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        sv_lo = spread_value(SPOT, STRIKE, T_near, T_far, R, 0.50, QTY, "Call")
        sv_hi = spread_value(SPOT, STRIKE, T_near, T_far, R, 1.20, QTY, "Call")
        assert sv_hi > sv_lo


# ── pnl_at_near_expiry ────────────────────────────────────────────────────────

class TestPnlAtNearExpiry:
    def _net_debit(self):
        from core.pricing import bs_call
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        near_prem = bs_call(SPOT, STRIKE, T_near, R, IV) * QTY
        far_prem  = bs_call(SPOT, STRIKE, T_far,  R, IV) * QTY
        return far_prem - near_prem

    def test_atm_at_expiry_is_profitable(self):
        nd  = self._net_debit()
        pnl = pnl_at_near_expiry(SPOT, STRIKE, NEAR, FAR, R, IV, QTY, nd, "Call")
        assert pnl > 0, "ATM at near expiry should profit (far leg retains time value)"

    def test_far_otm_is_loss(self):
        nd  = self._net_debit()
        far_spot = SPOT * 1.50
        pnl = pnl_at_near_expiry(far_spot, STRIKE, NEAR, FAR, R, IV, QTY, nd, "Call")
        assert pnl < 0, "Large move away from strike should result in a loss"

    def test_put_atm_at_expiry_is_profitable(self):
        from core.pricing import bs_put
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        nd = (bs_put(SPOT, STRIKE, T_far, R, IV) - bs_put(SPOT, STRIKE, T_near, R, IV)) * QTY
        pnl = pnl_at_near_expiry(SPOT, STRIKE, NEAR, FAR, R, IV, QTY, nd, "Put")
        assert pnl > 0


# ── find_breakevens ───────────────────────────────────────────────────────────

class TestFindBreakevens:
    def _net_debit(self):
        from core.pricing import bs_call
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        return (bs_call(SPOT, STRIKE, T_far, R, IV) - bs_call(SPOT, STRIKE, T_near, R, IV)) * QTY

    def test_returns_two_floats(self):
        nd = self._net_debit()
        be_lo, be_hi = find_breakevens(SPOT, STRIKE, NEAR, FAR, R, IV, QTY, nd, "Call")
        assert isinstance(be_lo, float)
        assert isinstance(be_hi, float)

    def test_breakevens_straddle_strike(self):
        nd = self._net_debit()
        be_lo, be_hi = find_breakevens(SPOT, STRIKE, NEAR, FAR, R, IV, QTY, nd, "Call")
        if be_lo > 0 and be_hi > 0:
            assert be_lo < STRIKE < be_hi

    def test_high_net_debit_gives_no_breakevens(self):
        # With a huge debit, there may be no profitable zone
        be_lo, be_hi = find_breakevens(SPOT, STRIKE, NEAR, FAR, R, IV, QTY, 1e9, "Call")
        assert be_lo == 0.0 and be_hi == 0.0


# ── check_calendar_status ─────────────────────────────────────────────────────

class TestCheckCalendarStatus:
    def _open_position(self, net_debit):
        return {
            "net_debit":   net_debit,
            "qty":         QTY,
            "strike":      STRIKE,
            "option_type": "Call",
        }

    def test_ok_status_near_debit(self):
        # Spread at roughly debit = fair value: current spread ≈ debit → ~100%
        from core.pricing import bs_call
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        nd = (bs_call(SPOT, STRIKE, T_far, R, IV) - bs_call(SPOT, STRIKE, T_near, R, IV)) * QTY
        op = self._open_position(nd)
        status, sv, pct, msg = check_calendar_status(SPOT, IV, NEAR, FAR, op)
        assert status in ("ok", "warn", "tp")

    def test_stop_when_debit_is_huge(self):
        op = self._open_position(net_debit=1_000_000.0)
        status, sv, pct, msg = check_calendar_status(SPOT, IV, NEAR, FAR, op)
        assert status == "stop"
        assert "STOP" in msg

    def test_tp_when_debit_is_tiny(self):
        op = self._open_position(net_debit=0.001)
        status, sv, pct, msg = check_calendar_status(SPOT, IV, NEAR, FAR, op)
        assert status == "tp"
        assert "TAKE-PROFIT" in msg

    def test_returns_four_tuple(self):
        from core.pricing import bs_call
        T_near = NEAR / 365.0
        T_far  = FAR  / 365.0
        nd = (bs_call(SPOT, STRIKE, T_far, R, IV) - bs_call(SPOT, STRIKE, T_near, R, IV)) * QTY
        result = check_calendar_status(SPOT, IV, NEAR, FAR, self._open_position(nd))
        assert len(result) == 4
        assert result[0] in ("ok", "warn", "stop", "tp")
