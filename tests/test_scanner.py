"""
Unit tests for strategy/scanner.py and strategy/sizer.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from data.deribit_feed import TickerSnapshot
from strategy.scanner import (
    CalendarCandidate,
    _group_chain,
    days_to_expiry,
    parse_instrument,
    scan,
)
from strategy.sizer import SizeResult, size_candidate


# ── Helpers ───────────────────────────────────────────────────────────────────

def _future_date(days: int) -> str:
    """Return a Deribit-style expiry string N days from today."""
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    # strftime("%d") gives zero-padded day; Deribit uses no leading zero
    return f"{dt.day}{dt.strftime('%b%y').upper()}"   # e.g. "27JUN25"


def _make_snap(
    asset: str,
    expiry_days: int,
    strike: int,
    opt_type: str,          # "C" or "P"
    mark_iv: float = 0.80,
    bid: float = 100.0,
    ask: float = 110.0,
    open_interest: float = 500.0,
    spot: float = 100_000.0,
) -> TickerSnapshot:
    expiry_str = _future_date(expiry_days)
    instrument = f"{asset}-{expiry_str}-{strike}-{opt_type}"
    return TickerSnapshot(
        instrument=instrument,
        asset=asset,
        spot=spot,
        mark_price=(bid + ask) / 2,
        mark_iv=mark_iv,
        bid=bid,
        ask=ask,
        open_interest=open_interest,
        timestamp=time.time(),
    )


def _make_cache(snaps: list[TickerSnapshot], spot: float = 100_000.0) -> MagicMock:
    """Return a mock ChainCache populated with *snaps*."""
    cache = MagicMock()
    cache.get_spot.return_value = spot
    cache.get_chain.return_value = snaps
    return cache


# ── parse_instrument ──────────────────────────────────────────────────────────

class TestParseInstrument:
    def test_valid_call(self):
        result = parse_instrument("BTC-27JUN25-100000-C")
        assert result is not None
        asset, expiry_dt, strike, opt_type = result
        assert asset == "BTC"
        assert strike == 100_000.0
        assert opt_type == "Call"

    def test_valid_put(self):
        result = parse_instrument("ETH-15JAN26-3000-P")
        assert result is not None
        _, _, strike, opt_type = result
        assert strike == 3000.0
        assert opt_type == "Put"

    def test_invalid_format(self):
        assert parse_instrument("BTC-INVALID") is None
        assert parse_instrument("") is None
        assert parse_instrument("BTC-27JUN25-100000-X") is None


# ── days_to_expiry ────────────────────────────────────────────────────────────

class TestDaysToExpiry:
    def test_future_date_positive(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        dte = days_to_expiry(future)
        assert 9 <= dte <= 11

    def test_past_date_zero(self):
        past = datetime.now(timezone.utc) - timedelta(days=5)
        assert days_to_expiry(past) == 0


# ── _group_chain ─────────────────────────────────────────────────────────────

class TestGroupChain:
    def test_groups_by_strike_and_type(self):
        snaps = [
            _make_snap("BTC", 7,  100_000, "C"),
            _make_snap("BTC", 30, 100_000, "C"),
            _make_snap("BTC", 7,  100_000, "P"),
        ]
        groups = _group_chain(snaps)
        assert (100_000.0, "Call") in groups
        assert (100_000.0, "Put")  in groups
        assert len(groups[(100_000.0, "Call")]) == 2

    def test_skips_unparseable(self):
        bad = MagicMock()
        bad.instrument = "NOT-A-VALID-INSTRUMENT"
        groups = _group_chain([bad])
        assert len(groups) == 0


# ── scan ──────────────────────────────────────────────────────────────────────

class TestScan:
    def _contango_snaps(self, near_days=7, far_days=30) -> list[TickerSnapshot]:
        """Two legs with realistic BTC ATM option prices and clear IV contango."""
        # Approximate BS ATM prices: BTC=100k, near 7d IV=90% → ~$5000; far 30d IV=75% → ~$8600
        return [
            _make_snap("BTC", near_days, 100_000, "C", mark_iv=0.90, bid=4800, ask=5200,  open_interest=600),
            _make_snap("BTC", far_days,  100_000, "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600),
        ]

    def test_finds_candidate_with_contango(self):
        cache = _make_cache(self._contango_snaps())
        results = scan(
            cache,
            assets=["BTC"],
            near_days_options=[7],
            far_days_options=[30],
            min_oi_near=100,
            min_oi_far=100,
            min_iv_contango=0.02,
            min_pop=0.01,   # relaxed for unit test
        )
        assert len(results) >= 1
        top = results[0]
        assert top.asset == "BTC"
        assert top.strike == 100_000.0
        assert top.iv_contango > 0.02

    def test_sorted_by_ev_score(self):
        # Two strikes with different contango; higher contango → higher EV.
        snaps = [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.95, bid=5100, ask=5300,  open_interest=600),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600),
            _make_snap("BTC", 7,  95_000,  "C", mark_iv=0.82, bid=4600, ask=5000,  open_interest=600),
            _make_snap("BTC", 30, 95_000,  "C", mark_iv=0.78, bid=8000, ask=8600, open_interest=600),
        ]
        cache = _make_cache(snaps)
        results = scan(
            cache,
            assets=["BTC"],
            near_days_options=[7],
            far_days_options=[30],
            min_oi_near=100,
            min_oi_far=100,
            min_iv_contango=0.01,
            min_pop=0.01,
        )
        if len(results) >= 2:
            assert results[0].ev_score >= results[1].ev_score

    def test_filters_low_oi(self):
        snaps = [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.90, bid=4800, ask=5200,  open_interest=50),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600),
        ]
        cache = _make_cache(snaps)
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_oi_near=100, min_oi_far=100,
            min_iv_contango=0.02, min_pop=0.01,
        )
        assert len(results) == 0

    def test_filters_no_contango(self):
        snaps = [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.70, bid=4000, ask=4400,  open_interest=600),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.80, bid=8200, ask=8800, open_interest=600),
        ]
        cache = _make_cache(snaps)
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_oi_near=100, min_oi_far=100,
            min_iv_contango=0.02, min_pop=0.01,
        )
        assert len(results) == 0

    def test_no_spot_skips_asset(self):
        cache = MagicMock()
        cache.get_spot.return_value = None
        cache.get_chain.return_value = []
        results = scan(cache, assets=["BTC"])
        assert results == []

    def test_empty_chain(self):
        cache = _make_cache([])
        results = scan(cache, assets=["BTC"])
        assert results == []

    def test_net_debit_positive(self):
        """net_debit must be positive (far_ask > near_bid)."""
        cache = _make_cache(self._contango_snaps())
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_oi_near=0, min_oi_far=0,
            min_iv_contango=0.0, min_pop=0.0,
        )
        for c in results:
            assert c.net_debit > 0

    def test_candidate_fields_populated(self):
        cache = _make_cache(self._contango_snaps())
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_oi_near=0, min_oi_far=0,
            min_iv_contango=0.0, min_pop=0.0,
        )
        assert results, "Expected at least one candidate"
        c = results[0]
        assert c.be_lo > 0
        assert c.be_hi > c.be_lo
        assert 0.0 <= c.pop <= 1.0
        assert c.near_days < c.far_days


# ── size_candidate ────────────────────────────────────────────────────────────

def _dummy_candidate(**kwargs) -> CalendarCandidate:
    defaults = dict(
        asset="BTC", strike=100_000.0, option_type="Call",
        near_instrument="BTC-07JUN25-100000-C",
        far_instrument="BTC-27JUN25-100000-C",
        near_days=7, far_days=30,
        spot=100_000.0,
        near_iv=0.90, far_iv=0.75, iv_contango=0.15,
        near_ask=90.0, near_bid=80.0,
        far_ask=220.0, far_bid=200.0,
        net_debit=140.0,
        near_oi=600.0, far_oi=600.0,
        pop=0.50, be_lo=92_000.0, be_hi=108_000.0,
        ev_score=0.36,   # EV = 36% of net_debit
    )
    defaults.update(kwargs)
    return CalendarCandidate(**defaults)


class TestSizeCandidate:
    def test_basic_sizing(self):
        c = _dummy_candidate(net_debit=100.0)
        result = size_candidate(c, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
        # max_loss = 200 USD; qty = floor(200/100 * 10) / 10 = 2.0
        assert result.qty == 2.0

    def test_blocks_when_max_positions_reached(self):
        c = _dummy_candidate()
        open_pos = [{"asset": "BTC", "strike": 90_000.0, "option_type": "Call", "net_debit": 100, "qty": 1}] * 3
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=open_pos, max_positions=3)
        assert result.qty == 0.0
        assert "Max positions" in result.reason

    def test_blocks_correlated_position(self):
        c = _dummy_candidate(asset="BTC", strike=100_000.0, option_type="Call")
        open_pos = [{"asset": "BTC", "strike": 101_000.0, "option_type": "Call", "net_debit": 100, "qty": 1}]
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=open_pos, max_positions=3)
        assert result.qty == 0.0
        assert "Correlated" in result.reason

    def test_different_asset_not_correlated(self):
        c = _dummy_candidate(asset="ETH", strike=3_000.0, option_type="Call")
        open_pos = [{"asset": "BTC", "strike": 3_000.0, "option_type": "Call", "net_debit": 100, "qty": 1}]
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=open_pos, max_positions=3)
        assert result.qty > 0

    def test_different_option_type_not_correlated(self):
        c = _dummy_candidate(asset="BTC", strike=100_000.0, option_type="Put")
        open_pos = [{"asset": "BTC", "strike": 100_000.0, "option_type": "Call", "net_debit": 100, "qty": 1}]
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=open_pos, max_positions=3)
        assert result.qty > 0

    def test_blocks_when_qty_below_minimum(self):
        # Very large net_debit relative to portfolio → qty rounds to 0
        c = _dummy_candidate(net_debit=1_000_000.0)
        result = size_candidate(c, portfolio_value=1_000.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty == 0.0

    def test_zero_net_debit_blocked(self):
        c = _dummy_candidate(net_debit=0.0)
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=[])
        assert result.qty == 0.0

    def test_qty_rounded_down(self):
        # max_loss=250, net_debit=100 → raw=2.5 → floor to 2.5 (already .1 precision)
        c = _dummy_candidate(net_debit=100.0)
        result = size_candidate(c, portfolio_value=12_500.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty == pytest.approx(2.5)


# ── Bug fix: near-zero debit guard and MAX_QTY cap ───────────────────────────

class TestSizerSafetyGuards:
    """
    Regression tests for two bugs that caused the bot to halt:
    1. A near-zero net_debit produced an absurd quantity (22k+ contracts).
    2. No hard cap on quantity existed, so the outsized position compounded
       a small negative spread value into a catastrophic phantom loss.
    """

    def test_near_zero_debit_rejected(self):
        """net_debit below MIN_NET_DEBIT must be blocked, not sized to 20k+ contracts."""
        c = _dummy_candidate(net_debit=0.0091)
        result = size_candidate(c, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty == 0.0
        assert "minimum" in result.reason.lower()

    def test_normal_debit_still_passes(self):
        """A sensible debit above MIN_NET_DEBIT must still be approved."""
        c = _dummy_candidate(net_debit=100.0)
        result = size_candidate(c, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty > 0.0

    def test_qty_capped_at_max_qty(self, monkeypatch):
        """Even if debit passes the floor, qty must never exceed MAX_QTY."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_QTY", 5.0)
        monkeypatch.setattr(cfg, "MIN_NET_DEBIT", 0.10)
        # max_loss=200 / net_debit=0.50 = 400 contracts → should be capped to 5
        c = _dummy_candidate(net_debit=0.50)
        result = size_candidate(c, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty == pytest.approx(5.0)

    def test_qty_cap_does_not_affect_normal_sizing(self, monkeypatch):
        """When raw qty is already below MAX_QTY, the cap has no effect."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_QTY", 100.0)
        monkeypatch.setattr(cfg, "MIN_NET_DEBIT", 0.10)
        c = _dummy_candidate(net_debit=100.0)
        result = size_candidate(c, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty == pytest.approx(2.0)  # unchanged
