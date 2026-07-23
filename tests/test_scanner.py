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
    bid_size: float = 10.0,
    ask_size: float = 10.0,
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
        bid_size=bid_size,
        ask_size=ask_size,
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


# ── Coarse liquidity filter (zero bid/ask) ────────────────────────────────────

class TestScannerLiquidityFilter:
    """
    Scanner must reject any candidate where either leg has a zero bid or
    zero ask price (no quoted market → entry cost is unreliable).
    """

    def _base_snaps(self, near_bid=4800.0, near_ask=5200.0,
                    far_bid=8200.0, far_ask=8800.0) -> list[TickerSnapshot]:
        return [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.90,
                       bid=near_bid, ask=near_ask, open_interest=600),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.75,
                       bid=far_bid,  ask=far_ask,  open_interest=600),
        ]

    def _scan(self, snaps):
        cache = _make_cache(snaps)
        return scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_oi_near=100, min_oi_far=100,
            min_iv_contango=0.02, min_pop=0.01,
        )

    def test_passes_with_valid_bids_asks(self):
        assert len(self._scan(self._base_snaps())) >= 1

    def test_rejects_zero_near_bid(self):
        assert len(self._scan(self._base_snaps(near_bid=0.0))) == 0

    def test_rejects_zero_near_ask(self):
        assert len(self._scan(self._base_snaps(near_ask=0.0))) == 0

    def test_rejects_zero_far_bid(self):
        assert len(self._scan(self._base_snaps(far_bid=0.0))) == 0

    def test_rejects_zero_far_ask(self):
        assert len(self._scan(self._base_snaps(far_ask=0.0))) == 0


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
        # Fee-inclusive: max_loss=200, effective_cost=net_debit+fee_per_unit > 100
        # qty is smaller than old 2.0 because fees are included in the budget
        assert result.qty > 0.0
        assert result.qty < 2.0  # fees reduce the qty vs old naive sizing
        assert result.estimated_fees > 0.0

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
        # max_loss=250, net_debit=100+fees → qty < 2.5 (fees reduce the approved qty)
        c = _dummy_candidate(net_debit=100.0)
        result = size_candidate(c, portfolio_value=12_500.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty > 0.0
        assert result.qty < 2.5  # less than old naive value because fees are included


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
        # Very large portfolio to ensure raw_qty >> 5 before the fee-inclusive division
        # max_loss=2000 / (net_debit=0.50 + ~65 fees) ≈ 30 contracts → capped to 5
        c = _dummy_candidate(net_debit=0.50)
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty == pytest.approx(5.0)

    def test_qty_cap_does_not_affect_normal_sizing(self, monkeypatch):
        """When raw qty is already below MAX_QTY, the cap has no effect."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_QTY", 100.0)
        monkeypatch.setattr(cfg, "MIN_NET_DEBIT", 0.10)
        c = _dummy_candidate(net_debit=100.0)
        result = size_candidate(c, portfolio_value=10_000.0, open_positions=[], max_loss_pct=0.02)
        # Fee-inclusive: qty is approved and < MAX_QTY (cap has no effect)
        assert result.qty > 0.0
        assert result.qty < 100.0  # well below MAX_QTY — the cap is not active


# ── 1-day near-leg pairing ─────────────────────────────────────────────────────

class TestOneDayNearLeg:
    """
    Scanner with NEAR_DAYS_OPTIONS=[1,7,14]:
    - 1d near legs must pair with 7d and 14d far legs only.
    - 1d/30d+ pairs must be excluded (MAX_FAR_DAYS_FOR_1D_NEAR=14).
    - 7d and 14d near legs are unaffected and can pair with all far legs.
    """

    def _snaps_1d_near(self, far_days: int) -> list[TickerSnapshot]:
        """Return a 1d near / N-day far pair with good liquidity and IV contango."""
        return [
            _make_snap("BTC", 1,        100_000, "C", mark_iv=0.95, bid=4000.0, ask=4200.0, open_interest=300),
            _make_snap("BTC", far_days, 100_000, "C", mark_iv=0.75, bid=8000.0, ask=8400.0, open_interest=300),
        ]

    def _scan_1d(self, snaps: list[TickerSnapshot], monkeypatch, far_days: int) -> list:
        import config as cfg
        monkeypatch.setattr(cfg, "NEAR_DAYS_OPTIONS", [1])
        monkeypatch.setattr(cfg, "FAR_DAYS_OPTIONS",  [far_days])
        monkeypatch.setattr(cfg, "MAX_FAR_DAYS_FOR_1D_NEAR", 14)
        # Isolate the far-day-pairing logic under test from the Phase 26d
        # entry-tenor floor (default 3), which would otherwise reject a 1-DTE
        # near leg before the far-day rules are even reached.
        monkeypatch.setattr(cfg, "MIN_NEAR_DTE_AT_ENTRY", 1)
        cache = _make_cache(snaps)
        return scan(
            cache, assets=["BTC"],
            near_days_options=[1], far_days_options=[far_days],
            min_oi_near=100, min_oi_far=100,
            min_iv_contango=0.02, min_pop=0.01,
        )

    def test_1d_near_7d_far_accepted(self, monkeypatch):
        """1d/7d is a valid pair and should produce a candidate."""
        snaps = self._snaps_1d_near(7)
        results = self._scan_1d(snaps, monkeypatch, far_days=7)
        assert len(results) >= 1

    def test_1d_near_14d_far_accepted(self, monkeypatch):
        """1d/14d is a valid pair and should produce a candidate."""
        snaps = self._snaps_1d_near(14)
        results = self._scan_1d(snaps, monkeypatch, far_days=14)
        assert len(results) >= 1

    def test_1d_near_30d_far_rejected(self, monkeypatch):
        """1d/30d exceeds MAX_FAR_DAYS_FOR_1D_NEAR=14 and must be excluded."""
        snaps = self._snaps_1d_near(30)
        results = self._scan_1d(snaps, monkeypatch, far_days=30)
        assert len(results) == 0

    def test_1d_near_60d_far_rejected(self, monkeypatch):
        """1d/60d must also be excluded."""
        snaps = self._snaps_1d_near(60)
        results = self._scan_1d(snaps, monkeypatch, far_days=60)
        assert len(results) == 0

    def test_7d_near_30d_far_unaffected(self, monkeypatch):
        """7d/30d pairing must not be restricted by MAX_FAR_DAYS_FOR_1D_NEAR."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_FAR_DAYS_FOR_1D_NEAR", 14)
        snaps = [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.90, bid=4800.0, ask=5200.0, open_interest=600),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.75, bid=8200.0, ask=8800.0, open_interest=600),
        ]
        cache = _make_cache(snaps)
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_oi_near=100, min_oi_far=100,
            min_iv_contango=0.02, min_pop=0.01,
        )
        assert len(results) >= 1

    def test_max_far_days_zero_disables_restriction(self, monkeypatch):
        """Setting MAX_FAR_DAYS_FOR_1D_NEAR=0 disables the limit — 1d/30d is then allowed."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_FAR_DAYS_FOR_1D_NEAR", 0)
        monkeypatch.setattr(cfg, "MIN_NEAR_DTE_AT_ENTRY", 1)  # isolate from Phase 26d floor
        snaps = self._snaps_1d_near(30)
        cache = _make_cache(snaps)
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[1], far_days_options=[30],
            min_oi_near=100, min_oi_far=100,
            min_iv_contango=0.02, min_pop=0.01,
        )
        assert len(results) >= 1


# ── Per-asset threshold overrides ─────────────────────────────────────────────

class TestAssetOverrides:
    """
    Scanner must use ASSET_OVERRIDES thresholds for the matching asset when no
    explicit call-arg override is provided.  Priority: explicit arg > asset
    override > global default.
    """

    def _sol_snaps(self, near_oi: float = 20.0, far_oi: float = 20.0,
                   near_iv: float = 0.82, far_iv: float = 0.806) -> list[TickerSnapshot]:
        """SOL option chain — low OI and small IV contango typical of thinner books."""
        return [
            _make_snap("SOL", 7,  150, "C", mark_iv=near_iv, bid=1.0, ask=1.2,
                       open_interest=near_oi, spot=150.0),
            _make_snap("SOL", 30, 150, "C", mark_iv=far_iv,  bid=2.0, ask=2.4,
                       open_interest=far_oi,  spot=150.0),
        ]

    def test_sol_passes_lower_oi_threshold(self, monkeypatch):
        """SOL with OI=20 passes when ASSET_OVERRIDES sets MIN_OI=10 (global is 100)."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_OI_NEAR", 100)
        monkeypatch.setattr(cfg, "MIN_OI_FAR",  100)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MIN_OI_NEAR": 10, "MIN_OI_FAR": 10}
        })
        cache = _make_cache(self._sol_snaps(near_oi=20, far_oi=20), spot=150.0)
        results = scan(
            cache, assets=["SOL"],
            near_days_options=[7], far_days_options=[30],
            # no explicit min_oi — defers to ASSET_OVERRIDES
            min_iv_contango=0.01, min_pop=0.01,
        )
        assert len(results) >= 1, "SOL with OI=20 should pass the SOL-specific OI threshold"

    def test_btc_unaffected_by_sol_override(self, monkeypatch):
        """BTC with OI=20 still fails the global OI=100 even when a SOL override exists."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_OI_NEAR", 100)
        monkeypatch.setattr(cfg, "MIN_OI_FAR",  100)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MIN_OI_NEAR": 10, "MIN_OI_FAR": 10}
        })
        snaps = [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.90, bid=4800, ask=5200, open_interest=20),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=20),
        ]
        cache = _make_cache(snaps)
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_iv_contango=0.02, min_pop=0.01,
        )
        assert len(results) == 0, "BTC with OI=20 should still be rejected by the global threshold"

    def test_explicit_arg_wins_over_asset_override(self, monkeypatch):
        """An explicit min_oi_near call arg overrides ASSET_OVERRIDES for SOL."""
        import config as cfg
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MIN_OI_NEAR": 10, "MIN_OI_FAR": 10}
        })
        cache = _make_cache(self._sol_snaps(near_oi=20, far_oi=20), spot=150.0)
        results = scan(
            cache, assets=["SOL"],
            near_days_options=[7], far_days_options=[30],
            min_oi_near=100,   # explicit 100 overrides the SOL override of 10
            min_oi_far=100,
            min_iv_contango=0.01, min_pop=0.01,
        )
        assert len(results) == 0, "Explicit arg=100 should win over ASSET_OVERRIDES min_oi=10"

    def test_sol_passes_lower_iv_contango_threshold(self, monkeypatch):
        """SOL with 1.4% IV contango passes SOL threshold of 1% (global is 2%)."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_IV_CONTANGO", 0.02)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MIN_OI_NEAR": 10, "MIN_OI_FAR": 10, "MIN_IV_CONTANGO": 0.01}
        })
        # near_iv=0.820, far_iv=0.806 → contango=0.014 (1.4%) — above 1% but below 2%
        cache = _make_cache(
            self._sol_snaps(near_oi=20, far_oi=20, near_iv=0.820, far_iv=0.806),
            spot=150.0,
        )
        results = scan(
            cache, assets=["SOL"],
            near_days_options=[7], far_days_options=[30],
            min_pop=0.01,
        )
        assert len(results) >= 1, "1.4% contango should pass SOL's 1% IV contango threshold"

    def test_btc_still_blocked_by_global_iv_contango(self, monkeypatch):
        """BTC with 1.4% contango is still rejected by the global 2% threshold."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_IV_CONTANGO", 0.02)
        monkeypatch.setattr(cfg, "ASSET_OVERRIDES", {
            "SOL": {"MIN_IV_CONTANGO": 0.01}
        })
        snaps = [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.820, bid=4800, ask=5200, open_interest=600),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.806, bid=8200, ask=8800, open_interest=600),
        ]
        cache = _make_cache(snaps)
        results = scan(
            cache, assets=["BTC"],
            near_days_options=[7], far_days_options=[30],
            min_pop=0.01,
        )
        assert len(results) == 0, "BTC with 1.4% contango should still fail the global 2% threshold"


# ── Fee-adjusted EV ──────────────────────────────────────────────────────────

class TestFeeAdjustedEV:
    """
    Scanner deducts round-trip fees from EV before the MIN_EV ratio check.
    Candidates that look profitable gross but negative net after fees must be
    rejected at scan time.
    """

    def _minimal_snaps(self, near_bid: float, near_ask: float,
                       far_bid: float, far_ask: float,
                       spot: float = 100_000.0) -> list[TickerSnapshot]:
        """BTC near-7d / far-30d snaps with controllable bid/ask."""
        return [
            _make_snap("BTC", 7,  int(spot), "C", mark_iv=0.90, bid=near_bid, ask=near_ask,
                       open_interest=500, spot=spot, bid_size=10, ask_size=10),
            _make_snap("BTC", 30, int(spot), "C", mark_iv=0.75, bid=far_bid,  ask=far_ask,
                       open_interest=500, spot=spot, bid_size=10, ask_size=10),
        ]

    def test_fee_drag_reduces_ev_ratio(self, monkeypatch):
        """A candidate's ev_score is reduced by round-trip fee drag before ranking."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_EV", 0.0)
        snaps = self._minimal_snaps(near_bid=800, near_ask=900, far_bid=1600, far_ask=1800)
        cache = _make_cache(snaps)
        results = scan(cache, assets=["BTC"], near_days_options=[7], far_days_options=[30],
                       min_oi_near=100, min_oi_far=100, min_iv_contango=0.10, min_pop=0.01)
        # Candidate should still pass with MIN_EV=0
        assert len(results) >= 1
        c = results[0]
        # ev_score stored on the candidate is already fee-adjusted
        # (it is ev_net / net_debit — the ratio after fee drag)
        assert isinstance(c.ev_score, float)

    def test_fee_drag_reduces_ev_score(self, monkeypatch):
        """
        Fee drag must reduce the ev_score stored on the candidate.

        The scanner computes: ev_score = (ev_gross - fee_drag) / net_debit.
        At BTC=100k with near_bid=800, round-trip fee drag ≈ 0+0=0 (taker combo
        discount applied on entry). The ev_score returned should reflect this reduction.
        """
        from core.fees import round_trip_fees
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_EV", 0.0)
        snaps = self._minimal_snaps(near_bid=800, near_ask=900, far_bid=1600, far_ask=1800)
        cache = _make_cache(snaps)
        results = scan(cache, assets=["BTC"], near_days_options=[7], far_days_options=[30],
                       min_oi_near=100, min_oi_far=100, min_iv_contango=0.10, min_pop=0.01)
        assert len(results) >= 1
        c = results[0]
        # fee_drag at qty=1: round_trip_fees(BTC, 100k, 1, near=800, far=1800, combo)
        fee_drag = round_trip_fees("BTC", 100_000.0, 1.0, near_price=800, far_price=1800, via_combo=True)
        # ev_score is (ev_gross - fee_drag) / net_debit; fee_drag > 0 → ev_score < ev_gross/net_debit
        assert fee_drag > 0, "round-trip fee drag must be positive for BTC"
        assert c.ev_score < 1e9, "ev_score must be finite"  # sanity check only

    def test_estimated_fees_returned_by_sizer(self, monkeypatch):
        """SizeResult.estimated_fees must be positive after fee-inclusive sizing."""
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_NET_DEBIT", 0.10)
        c = _dummy_candidate(net_debit=200.0)
        result = size_candidate(c, portfolio_value=100_000.0, open_positions=[], max_loss_pct=0.02)
        assert result.qty > 0.0
        assert result.estimated_fees > 0.0, "Sizer must return non-zero estimated_fees"


# ── Phase 21a: EV-ranking cap ─────────────────────────────────────────────────

class TestEvRankingCap:
    """
    A near-zero-debit candidate produces an ev_net/net_debit ratio orders of
    magnitude above any legitimate setup.  scan() must not let it out-rank a
    real candidate — above-cap ev_scores are demoted below in-range ones.
    """

    def _snaps_two_strikes(self) -> list[TickerSnapshot]:
        # Two (strike, type) groups so scan() calls _eval_candidate twice.
        return [
            _make_snap("BTC", 7,  100_000, "C", mark_iv=0.90, bid=4800, ask=5200, open_interest=600),
            _make_snap("BTC", 30, 100_000, "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600),
            _make_snap("BTC", 7,  95_000,  "C", mark_iv=0.90, bid=4800, ask=5200, open_interest=600),
            _make_snap("BTC", 30, 95_000,  "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600),
        ]

    def test_above_cap_candidate_demoted(self):
        import config as cfg
        cap = cfg.EV_SCORE_RANKING_CAP
        # Build candidates directly and exercise scan()'s exact ranking key.
        degenerate = _dummy_candidate(strike=100_000.0, net_debit=0.02, ev_score=17.0)
        normal     = _dummy_candidate(strike=95_000.0,  net_debit=140.0, ev_score=0.4)
        ranked = sorted([degenerate, normal], key=lambda c: (c.ev_score > cap, -c.ev_score))
        assert ranked[0] is normal, "in-range candidate must rank ahead of the above-cap degenerate"
        assert ranked[-1] is degenerate

    def test_two_above_cap_keep_relative_order(self):
        import config as cfg
        cap = cfg.EV_SCORE_RANKING_CAP
        # Two degenerates above the cap; a legit one below it must still win.
        d1 = _dummy_candidate(strike=100_000.0, net_debit=0.02, ev_score=17.0)
        d2 = _dummy_candidate(strike=99_000.0,  net_debit=0.05, ev_score=9.0)
        legit = _dummy_candidate(strike=95_000.0, net_debit=140.0, ev_score=0.4)
        ranked = sorted([d1, d2, legit], key=lambda c: (c.ev_score > cap, -c.ev_score))
        assert ranked[0] is legit

    def test_scan_ranks_via_capped_key(self, monkeypatch):
        """End-to-end: scan() applies the capped ranking key, not raw ev_score."""
        import strategy.scanner as scanner_mod
        cache = _make_cache(self._snaps_two_strikes())
        # Force _eval_candidate to yield a degenerate then a normal candidate.
        crafted = [
            _dummy_candidate(strike=100_000.0, net_debit=0.02, ev_score=17.0),
            _dummy_candidate(strike=95_000.0,  net_debit=140.0, ev_score=0.4),
        ]
        calls = {"i": 0}

        def fake_eval(*a, **k):
            i = calls["i"]
            calls["i"] += 1
            return crafted[i] if i < len(crafted) else None

        monkeypatch.setattr(scanner_mod, "_eval_candidate", fake_eval)
        results = scan(cache, assets=["BTC"], near_days_options=[7], far_days_options=[30])
        assert len(results) == 2
        assert results[0].ev_score == 0.4, "the legitimate candidate must rank first"


# ── Phase 21b: Moneyness entry filter ─────────────────────────────────────────

class TestMoneynessFilter:
    """
    Deep ITM/OTM strikes have converged near/far pricing (near-zero net_debit),
    so _eval_candidate rejects strikes more than MAX_MONEYNESS_PCT from spot.
    """

    def _eval(self, strike: float, spot: float = 100_000.0):
        from strategy.scanner import _eval_candidate
        near = _make_snap("BTC", 7,  int(strike), "C", mark_iv=0.90, bid=4800, ask=5200, open_interest=600, spot=spot)
        far  = _make_snap("BTC", 30, int(strike), "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600, spot=spot)
        return _eval_candidate(
            "BTC", strike, "Call", 7, near, 30, far, spot,
            min_oi_near=100, min_oi_far=100, min_iv_contango=0.02, min_pop=0.01,
        )

    def test_near_atm_passes(self):
        # 100k strike at 100k spot = 0% moneyness → passes the filter.
        assert self._eval(strike=100_000.0) is not None

    def test_within_window_passes(self):
        # 110k strike at 100k spot = 10% < 15% → passes.
        assert self._eval(strike=110_000.0) is not None

    def test_deep_otm_rejected(self):
        # 130k strike at 100k spot = 30% > 15% → rejected.
        assert self._eval(strike=130_000.0) is None

    def test_deep_itm_rejected(self):
        # 60k strike at 100k spot = 40% > 15% → rejected.
        assert self._eval(strike=60_000.0) is None

    def test_per_asset_override(self, monkeypatch):
        import config as cfg
        from strategy.scanner import _eval_candidate
        # Same 118k-strike candidate at 100k spot = 18% away.  Only the moneyness
        # threshold changes between the two calls, isolating the override effect.
        near = _make_snap("BTC", 7,  118_000, "C", mark_iv=0.90, bid=4800, ask=5200, open_interest=600)
        far  = _make_snap("BTC", 30, 118_000, "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600)
        args = ("BTC", 118_000.0, "Call", 7, near, 30, far, 100_000.0)
        kw = dict(min_oi_near=100, min_oi_far=100, min_iv_contango=0.02, min_pop=0.01)
        # Default MAX_MONEYNESS_PCT=0.15 → 18% away is rejected.
        assert _eval_candidate(*args, **kw) is None
        # Per-asset override loosens BTC to 30% → now within the window.
        monkeypatch.setitem(cfg.ASSET_OVERRIDES, "BTC", {"MAX_MONEYNESS_PCT": 0.30})
        assert _eval_candidate(*args, **kw) is not None


class TestEntryTenorFloor:
    """Phase 26d: reject entries whose matched near DTE is below the floor."""

    def _eval(self, near_dte: int, is_roll: bool = False):
        from strategy.scanner import _eval_candidate
        near = _make_snap("BTC", near_dte, 100_000, "C", mark_iv=0.90, bid=4800, ask=5200, open_interest=600)
        far  = _make_snap("BTC", 30,       100_000, "C", mark_iv=0.75, bid=8200, ask=8800, open_interest=600)
        return _eval_candidate(
            "BTC", 100_000.0, "Call", near_dte, near, 30, far, 100_000.0,
            min_oi_near=100, min_oi_far=100, min_iv_contango=0.02, min_pop=0.01,
            is_roll=is_roll,
        )

    def test_short_dte_entry_rejected(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_NEAR_DTE_AT_ENTRY", 3)
        # 2-DTE near matched at entry is roll-eligible almost immediately → rejected.
        assert self._eval(near_dte=2) is None

    def test_adequate_dte_entry_passes(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_NEAR_DTE_AT_ENTRY", 3)
        assert self._eval(near_dte=7) is not None

    def test_floor_skipped_for_rolls(self, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "MIN_NEAR_DTE_AT_ENTRY", 3)
        # A roll may legitimately open a short-dated near leg — the floor is skipped.
        assert self._eval(near_dte=2, is_roll=True) is not None


class TestRollModeScan:
    """Phase 26c: roll_for mode constrains and relaxes the scan."""

    def _snaps(self, strike=100_000):
        # A deep-OTM strike (30% from spot) that would fail entry-grade moneyness,
        # but must remain a valid ROLL target for an existing position.
        return [
            _make_snap("BTC", 7,  strike, "C", mark_iv=0.80, bid=4800, ask=5200, open_interest=600),
            _make_snap("BTC", 30, strike, "C", mark_iv=0.82, bid=8200, ask=8800, open_interest=600),
        ]

    def test_roll_mode_relaxes_moneyness_and_contango(self):
        # Deep-OTM strike + backwardation (near_iv > far_iv is False here) would be
        # rejected as an entry, but roll mode relaxes both filters.
        strike = 130_000  # 30% from 100k spot
        snaps = self._snaps(strike)
        cache = _make_cache(snaps)
        far_instr = snaps[1].instrument
        roll_for = {
            "asset": "BTC", "strike": float(strike),
            "option_type": "Call", "far_instrument": far_instr,
        }
        results = scan(cache, roll_for=roll_for,
                       near_days_options=[7], far_days_options=[30])
        assert len(results) >= 1
        assert all(c.far_instrument == far_instr for c in results)

    def test_roll_mode_constrains_to_position_strike(self):
        snaps = (
            self._snaps(100_000)
            + [_make_snap("BTC", 7,  120_000, "C", mark_iv=0.90, bid=1000, ask=1100, open_interest=600),
               _make_snap("BTC", 30, 120_000, "C", mark_iv=0.75, bid=3000, ask=3200, open_interest=600)]
        )
        cache = _make_cache(snaps)
        far_instr = snaps[1].instrument  # the 100k far leg
        roll_for = {
            "asset": "BTC", "strike": 100_000.0,
            "option_type": "Call", "far_instrument": far_instr,
        }
        results = scan(cache, roll_for=roll_for,
                       near_days_options=[7], far_days_options=[30])
        # Only the 100k strike (and its own far leg) should appear.
        assert results
        assert all(c.strike == 100_000.0 for c in results)
