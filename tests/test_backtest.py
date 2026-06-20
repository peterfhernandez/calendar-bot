"""
tests/test_backtest.py
======================
Unit tests for backtest/loader.py and backtest/engine.py.

Tests are self-contained: all data is generated in-process.  No network
connections or real Deribit credentials are required.
"""

from __future__ import annotations

import csv
import json
import math
import tempfile
import time
from pathlib import Path

import pytest

from backtest.engine import (
    BacktestChainCache,
    BacktestEngine,
    BacktestExecutor,
    BacktestResult,
    _cumulative,
    _max_drawdown,
    _sharpe,
)
from backtest.loader import from_records, load_csv, load_json, load
from data.deribit_feed import TickerSnapshot


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _snap(instrument, asset, spot, mark_iv, ts=None, oi=500.0, mark_price=None):
    mp = mark_price if mark_price is not None else spot * 0.01
    return TickerSnapshot(
        instrument    = instrument,
        asset         = asset,
        spot          = spot,
        mark_price    = mp,
        mark_iv       = mark_iv,
        bid           = mp * 0.99,
        ask           = mp * 1.01,
        open_interest = oi,
        timestamp     = ts or time.time(),
    )


def _record(snap, ts=None):
    t = ts or snap.timestamp
    return {
        "timestamp":     t,
        "instrument":    snap.instrument,
        "asset":         snap.asset,
        "spot":          snap.spot,
        "mark_price":    snap.mark_price,
        "mark_iv":       snap.mark_iv,
        "bid":           snap.bid,
        "ask":           snap.ask,
        "open_interest": snap.open_interest,
    }


def _make_frames(n=3):
    """Return n minimal 2-snapshot frames (near+far BTC ATM call)."""
    from datetime import datetime, timedelta, timezone
    from core.pricing import bs_call
    near_expiry = datetime.now(timezone.utc) + timedelta(days=10)
    far_expiry  = datetime.now(timezone.utc) + timedelta(days=35)
    near_label  = near_expiry.strftime("%d%b%y").upper()
    far_label   = far_expiry.strftime("%d%b%y").upper()

    frames = []
    for i in range(n):
        ts   = time.time() + i * 3600.0
        spot = 30_000.0
        near_iv = 0.80
        far_iv  = 0.70
        strike  = 30_000.0

        near_mark = bs_call(spot, strike, 10 / 365, 0.0, near_iv)
        far_mark  = bs_call(spot, strike, 35 / 365, 0.0, far_iv)

        frames.append([
            TickerSnapshot(
                instrument=f"BTC-{near_label}-30000-C", asset="BTC", spot=spot,
                mark_price=near_mark, mark_iv=near_iv,
                bid=near_mark * 0.99, ask=near_mark * 1.01,
                open_interest=500.0, timestamp=ts,
            ),
            TickerSnapshot(
                instrument=f"BTC-{far_label}-30000-C", asset="BTC", spot=spot,
                mark_price=far_mark, mark_iv=far_iv,
                bid=far_mark * 0.99, ask=far_mark * 1.01,
                open_interest=500.0, timestamp=ts,
            ),
        ])
    return frames


# ── loader.from_records ───────────────────────────────────────────────────────

class TestFromRecords:
    def test_groups_by_timestamp(self):
        t1, t2 = 1000.0, 2000.0
        s1 = _snap("BTC-1-C", "BTC", 30000, 0.7, ts=t1)
        s2 = _snap("BTC-2-C", "BTC", 30000, 0.6, ts=t1)
        s3 = _snap("BTC-3-C", "BTC", 30000, 0.7, ts=t2)
        records = [_record(s1, t1), _record(s2, t1), _record(s3, t2)]
        frames = from_records(records)
        assert len(frames) == 2
        assert len(frames[0]) == 2
        assert len(frames[1]) == 1

    def test_sorted_chronologically(self):
        t_later  = 5000.0
        t_earlier = 1000.0
        s1 = _snap("A", "BTC", 30000, 0.7, ts=t_later)
        s2 = _snap("B", "BTC", 30000, 0.7, ts=t_earlier)
        frames = from_records([_record(s1, t_later), _record(s2, t_earlier)])
        assert frames[0][0].timestamp < frames[1][0].timestamp

    def test_skips_malformed_rows(self):
        good = _record(_snap("A", "BTC", 30000, 0.7, ts=1000.0), 1000.0)
        bad  = {"timestamp": "x", "instrument": "A", "asset": "BTC",
                "spot": "not_a_number", "mark_price": 100, "mark_iv": 0.7,
                "bid": 99, "ask": 101, "open_interest": 500}
        frames = from_records([good, bad])
        assert len(frames) == 1

    def test_empty_input(self):
        assert from_records([]) == []

    def test_asset_uppercased(self):
        rec = _record(_snap("A", "btc", 30000, 0.7, ts=1000.0), 1000.0)
        rec["asset"] = "btc"
        frames = from_records([rec])
        assert frames[0][0].asset == "BTC"


# ── loader.load_csv ───────────────────────────────────────────────────────────

class TestLoadCsv:
    def test_round_trip(self, tmp_path):
        frames_in = _make_frames(3)
        rows = [_record(snap) for frame in frames_in for snap in frame]

        csv_file = tmp_path / "chain.csv"
        with csv_file.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        frames_out = load_csv(csv_file)
        assert len(frames_out) == len(frames_in)
        assert frames_out[0][0].instrument == frames_in[0][0].instrument

    def test_extra_columns_ignored(self, tmp_path):
        frames_in = _make_frames(1)
        rows = [_record(snap) for frame in frames_in for snap in frame]
        for r in rows:
            r["extra_column"] = "ignored"

        csv_file = tmp_path / "chain.csv"
        with csv_file.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        frames_out = load_csv(csv_file)
        assert len(frames_out) == 1


# ── loader.load_json ──────────────────────────────────────────────────────────

class TestLoadJson:
    def test_list_format(self, tmp_path):
        frames_in = _make_frames(2)
        records = [_record(snap) for frame in frames_in for snap in frame]
        json_file = tmp_path / "chain.json"
        json_file.write_text(json.dumps(records))
        frames_out = load_json(json_file)
        assert len(frames_out) == 2

    def test_dict_format_with_snapshots_key(self, tmp_path):
        frames_in = _make_frames(2)
        records = [_record(snap) for frame in frames_in for snap in frame]
        json_file = tmp_path / "chain.json"
        json_file.write_text(json.dumps({"snapshots": records, "meta": "ignored"}))
        frames_out = load_json(json_file)
        assert len(frames_out) == 2

    def test_bad_structure_raises(self, tmp_path):
        json_file = tmp_path / "chain.json"
        json_file.write_text('"just_a_string"')
        with pytest.raises(ValueError):
            load_json(json_file)


# ── loader.load (auto-detect) ─────────────────────────────────────────────────

class TestLoad:
    def test_detects_csv(self, tmp_path):
        frames_in = _make_frames(1)
        rows = [_record(snap) for frame in frames_in for snap in frame]
        p = tmp_path / "data.csv"
        with p.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        assert len(load(p)) == 1

    def test_detects_json(self, tmp_path):
        frames_in = _make_frames(1)
        records = [_record(snap) for frame in frames_in for snap in frame]
        p = tmp_path / "data.json"
        p.write_text(json.dumps(records))
        assert len(load(p)) == 1

    def test_unknown_extension_raises(self, tmp_path):
        p = tmp_path / "data.parquet"
        p.write_text("")
        with pytest.raises(ValueError, match="Unsupported"):
            load(p)


# ── BacktestChainCache ────────────────────────────────────────────────────────

class TestBacktestChainCache:
    def test_stale_data_returned(self):
        cache = BacktestChainCache(ttl=0.001)  # very short TTL
        snap  = _snap("BTC-OLD-C", "BTC", 30000, 0.7, ts=time.time() - 10_000)
        cache.update(snap)
        time.sleep(0.01)
        result = cache.get("BTC-OLD-C")
        assert result is not None, "BacktestChainCache should ignore TTL"

    def test_normal_retrieval(self):
        cache = BacktestChainCache()
        snap  = _snap("BTC-TEST-C", "BTC", 30000, 0.7)
        cache.update(snap)
        assert cache.get("BTC-TEST-C") is not None

    def test_get_chain_no_stale_exclusion(self):
        cache = BacktestChainCache(ttl=0.001)
        old_ts = time.time() - 10_000
        cache.update(_snap("BTC-A-C", "BTC", 30000, 0.7, ts=old_ts))
        cache.update(_snap("BTC-B-C", "BTC", 30000, 0.6, ts=old_ts))
        time.sleep(0.01)
        chain = cache.get_chain("BTC")
        assert len(chain) == 2


# ── BacktestExecutor ──────────────────────────────────────────────────────────

class TestBacktestExecutor:
    def _make_candidate(self):
        from strategy.scanner import CalendarCandidate
        return CalendarCandidate(
            asset="BTC", strike=30000.0, option_type="Call",
            near_instrument="BTC-NEAR-30000-C",
            far_instrument="BTC-FAR-30000-C",
            near_days=10, far_days=35, spot=30000.0,
            near_iv=0.80, far_iv=0.70, iv_contango=0.10,
            near_ask=200.0, near_bid=180.0, far_ask=600.0, far_bid=570.0,
            net_debit=420.0, near_oi=500, far_oi=500,
            pop=0.55, be_lo=25000.0, be_hi=35000.0, ev_score=50.0, qty=1.0,
        )

    def test_enter_spread_returns_fill(self):
        cache = BacktestChainCache()
        ex    = BacktestExecutor(cache)
        fill  = ex.enter_spread(self._make_candidate())
        assert fill is not None
        assert fill["net_debit"] > 0
        assert fill["qty"] == 1.0

    def test_enter_slippage_applied(self):
        cache    = BacktestChainCache()
        ex       = BacktestExecutor(cache, slippage=0.01)
        cand     = self._make_candidate()
        fill     = ex.enter_spread(cand)
        # far_ask inflated, near_bid deflated → net_debit > raw gap
        assert fill["net_debit"] > cand.far_ask - cand.near_bid

    def test_close_spread_uses_cache_prices(self):
        cache = BacktestChainCache()
        cache.update(_snap("BTC-NEAR-30000-C", "BTC", 30000, 0.80,
                           mark_price=220.0))
        cache.update(_snap("BTC-FAR-30000-C",  "BTC", 30000, 0.70,
                           mark_price=620.0))
        ex  = BacktestExecutor(cache, slippage=0.0)
        pos = {
            "near_instrument": "BTC-NEAR-30000-C",
            "far_instrument":  "BTC-FAR-30000-C",
            "net_debit": 420.0, "qty": 1.0,
        }
        credit = ex.close_spread(pos)
        assert credit is not None
        assert credit >= 0

    def test_close_spread_fallback_when_no_cache(self):
        cache = BacktestChainCache()
        ex    = BacktestExecutor(cache)
        pos   = {"near_instrument": "MISSING-1", "far_instrument": "MISSING-2",
                 "net_debit": 400.0, "qty": 1.0}
        credit = ex.close_spread(pos)
        # Should return 60% of net_debit
        assert abs(credit - 400.0 * 0.6) < 1e-9

    def test_roll_near_leg_always_succeeds(self):
        cache = BacktestChainCache()
        ex    = BacktestExecutor(cache)
        ok    = ex.roll_near_leg({}, self._make_candidate())
        assert ok is True


# ── Statistics helpers ────────────────────────────────────────────────────────

class TestStatHelpers:
    def test_cumulative(self):
        pnls = [10.0, -5.0, 20.0]
        curve = list(_cumulative(pnls))
        assert curve == [10.0, 5.0, 25.0]

    def test_cumulative_empty(self):
        assert list(_cumulative([])) == []

    def test_max_drawdown_simple(self):
        equity = [0, 10, 20, 5, 15]
        assert _max_drawdown(equity) == pytest.approx(15.0)

    def test_max_drawdown_no_drawdown(self):
        equity = [0, 5, 10, 15]
        assert _max_drawdown(equity) == 0.0

    def test_max_drawdown_empty(self):
        assert _max_drawdown([]) == 0.0

    def test_sharpe_positive_mean(self):
        pnls = [10.0, 12.0, 8.0, 11.0, 9.0]
        s    = _sharpe(pnls)
        assert s > 0

    def test_sharpe_nan_when_too_few(self):
        assert math.isnan(_sharpe([10.0]))

    def test_sharpe_nan_when_zero_std(self):
        assert math.isnan(_sharpe([5.0, 5.0, 5.0]))


# ── BacktestEngine.run ────────────────────────────────────────────────────────

class TestBacktestEngineRun:
    def test_returns_result_with_correct_type(self):
        frames = _make_frames(5)
        engine = BacktestEngine(portfolio_value=10_000, scan_every_n_frames=1)
        result = engine.run(frames, regime_name="test")
        assert isinstance(result, BacktestResult)
        assert result.regime_name == "test"

    def test_empty_frames_returns_empty_result(self):
        engine = BacktestEngine()
        result = engine.run([], regime_name="empty")
        assert result.total_trades == 0
        assert result.total_pnl == 0.0

    def test_result_stats_are_consistent(self):
        frames = _make_frames(20)
        engine = BacktestEngine(portfolio_value=10_000, scan_every_n_frames=2)
        result = engine.run(frames)
        n = result.total_trades
        if n > 0:
            assert 0.0 <= result.win_rate <= 1.0
            assert result.max_drawdown >= 0.0

    def test_equity_curve_length_matches_trades(self):
        frames = _make_frames(20)
        engine = BacktestEngine(portfolio_value=10_000)
        result = engine.run(frames)
        assert len(result.equity_curve) == result.total_trades

    def test_scan_every_n_frames_respected(self):
        """scan_every_n_frames=5 scans less often than scan_every_n_frames=1."""
        frames = _make_frames(10)
        engine_frequent = BacktestEngine(portfolio_value=10_000, scan_every_n_frames=1)
        engine_sparse   = BacktestEngine(portfolio_value=10_000, scan_every_n_frames=5)
        result_frequent = engine_frequent.run(frames, regime_name="frequent")
        result_sparse   = engine_sparse.run(frames, regime_name="sparse")
        # Frequent scanning should open at least as many trades as sparse scanning
        assert result_frequent.total_trades >= result_sparse.total_trades
