"""
Unit tests for the Phase 21d re-entry cooldown in strategy/sizer.py.

Other sizer behaviour (basic sizing, correlation, MAX_QTY/MIN_NET_DEBIT guards)
is covered in tests/test_scanner.py; this file focuses on the cooldown that
prevents a just-auto-closed instrument from being immediately re-entered.
"""

from __future__ import annotations

from datetime import datetime, timezone

from strategy.scanner import CalendarCandidate
from strategy.sizer import size_candidate


def _candidate(asset="BTC", strike=100_000.0, option_type="Call", net_debit=140.0) -> CalendarCandidate:
    return CalendarCandidate(
        asset=asset, strike=strike, option_type=option_type,
        near_instrument=f"{asset}-07JUN25-{int(strike)}-C",
        far_instrument=f"{asset}-27JUN25-{int(strike)}-C",
        near_days=7, far_days=30,
        spot=100_000.0,
        near_iv=0.90, far_iv=0.75, iv_contango=0.15,
        near_ask=90.0, near_bid=80.0, far_ask=220.0, far_bid=200.0,
        net_debit=net_debit,
        near_oi=600.0, far_oi=600.0,
        pop=0.50, be_lo=92_000.0, be_hi=108_000.0,
        ev_score=0.36,
    )


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


class TestReentryCooldown:
    def test_recently_closed_instrument_blocked(self):
        c = _candidate(asset="BTC", strike=100_000.0, option_type="Call")
        recent = {("BTC", 100_000.0, "Call"): _now() - 60}  # closed 60s ago
        result = size_candidate(
            c, portfolio_value=100_000.0, open_positions=[],
            recent_auto_closes=recent, reentry_cooldown_sec=1800,
        )
        assert result.qty == 0.0
        assert "cooldown" in result.reason.lower()

    def test_cooldown_elapsed_allows_entry(self):
        c = _candidate(asset="BTC", strike=100_000.0, option_type="Call")
        recent = {("BTC", 100_000.0, "Call"): _now() - 1801}  # closed just over 30 min ago
        result = size_candidate(
            c, portfolio_value=100_000.0, open_positions=[],
            recent_auto_closes=recent, reentry_cooldown_sec=1800,
        )
        assert result.qty > 0.0

    def test_different_strike_not_blocked(self):
        c = _candidate(asset="BTC", strike=100_000.0, option_type="Call")
        recent = {("BTC", 95_000.0, "Call"): _now() - 60}  # different strike
        result = size_candidate(
            c, portfolio_value=100_000.0, open_positions=[],
            recent_auto_closes=recent, reentry_cooldown_sec=1800,
        )
        assert result.qty > 0.0

    def test_different_option_type_not_blocked(self):
        c = _candidate(asset="BTC", strike=100_000.0, option_type="Put")
        recent = {("BTC", 100_000.0, "Call"): _now() - 60}  # Call closed, candidate is a Put
        result = size_candidate(
            c, portfolio_value=100_000.0, open_positions=[],
            recent_auto_closes=recent, reentry_cooldown_sec=1800,
        )
        assert result.qty > 0.0

    def test_no_recent_closes_map_is_noop(self):
        c = _candidate()
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=[])
        assert result.qty > 0.0

    def test_zero_cooldown_disables_check(self):
        c = _candidate(asset="BTC", strike=100_000.0, option_type="Call")
        recent = {("BTC", 100_000.0, "Call"): _now()}  # closed right now
        result = size_candidate(
            c, portfolio_value=100_000.0, open_positions=[],
            recent_auto_closes=recent, reentry_cooldown_sec=0,
        )
        assert result.qty > 0.0
