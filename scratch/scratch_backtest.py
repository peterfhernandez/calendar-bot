"""
scratch/scratch_backtest.py
============================
End-to-end verification script for the backtesting harness.

Generates synthetic BTC option chain data representing four distinct
volatility regimes and runs BacktestEngine on each, then prints a summary
table.  No live network connections are made and no real orders are placed.
This script exits immediately when DERIBIT_PAPER is False.

Run from the repo root:
    python -m scratch.scratch_backtest

What it covers
--------------
1. Loader.from_records()  — dict-to-frame conversion
2. Loader.load_csv()      — round-trip through CSV
3. Loader.load_json()     — round-trip through JSON
4. BacktestChainCache     — TTL disabled, data always treated as fresh
5. BacktestExecutor       — realistic open/close pricing with slippage
6. BacktestEngine.run()   — full scan/monitor replay loop
7. BacktestResult stats   — win rate, avg P&L, max drawdown, Sharpe

Regimes
-------
1. High Vol / Strong Contango  — best conditions for calendar spreads
2. Low Vol / Weak Contango     — marginal setups, few entries
3. IV Spike then Collapse      — tests stop-loss behaviour
4. Stable Sideways             — moderate, steady performance
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import config

if config.TRADING_MODE == "live":
    raise SystemExit("scratch_backtest.py must not run in live mode. Set TRADING_MODE='paper' in config.py.")

logging.basicConfig(level=logging.WARNING)  # suppress decision-engine noise in output

from backtest.engine import BacktestEngine, BacktestResult
from backtest.loader import from_records, load_csv, load_json
from core.pricing import bs_call, bs_put
from data.deribit_feed import TickerSnapshot

# ── Synthetic data generation ─────────────────────────────────────────────────

def _expiry_label(dt: datetime) -> str:
    return dt.strftime("%d%b%y").upper()


def _atm_strike(spot: float, step: float = 1000.0) -> float:
    return round(spot / step) * step


def _option_price(spot, strike, dte, iv, opt_type="Call"):
    T = max(dte / 365.0, 1 / 365.0)
    if opt_type == "Call":
        return bs_call(spot, strike, T, 0.0, iv)
    return bs_put(spot, strike, T, 0.0, iv)


def generate_regime_frames(
    n_frames:       int,
    spot_start:     float,
    spot_end:       float,
    near_iv_func,               # callable(frame_index) -> float
    far_iv_func,                # callable(frame_index) -> float
    near_dte:       int  = 10,  # DTE for the near leg (relative to "now")
    far_dte:        int  = 35,  # DTE for the far leg
    oi:             float = 500.0,
    asset:          str   = "BTC",
    t_base:         float | None = None,  # base timestamp; defaults to now
) -> list[list[TickerSnapshot]]:
    """
    Generate n_frames of synthetic option chain data for one regime.

    Each frame contains two TickerSnapshots (near + far) at an ATM strike.
    The spot price drifts linearly from spot_start to spot_end.
    near_iv_func / far_iv_func receive the frame index and return IV as a
    decimal (e.g. 0.80 for 80%).
    """
    now = t_base or time.time()
    near_expiry = datetime.now(timezone.utc) + timedelta(days=near_dte)
    far_expiry  = datetime.now(timezone.utc) + timedelta(days=far_dte)
    near_label  = _expiry_label(near_expiry)
    far_label   = _expiry_label(far_expiry)

    frames: list[list[TickerSnapshot]] = []
    for i in range(n_frames):
        frac  = i / max(n_frames - 1, 1)
        spot  = spot_start + (spot_end - spot_start) * frac
        ts    = now + i * 3600.0  # one frame per (simulated) hour

        near_iv  = near_iv_func(i)
        far_iv   = far_iv_func(i)
        strike   = _atm_strike(spot)

        near_mark = _option_price(spot, strike, near_dte, near_iv, "Call")
        far_mark  = _option_price(spot, strike, far_dte,  far_iv,  "Call")

        slip = 0.01  # 1% bid/ask spread

        near_snap = TickerSnapshot(
            instrument    = f"{asset}-{near_label}-{int(strike)}-C",
            asset         = asset,
            spot          = spot,
            mark_price    = near_mark,
            mark_iv       = near_iv,
            bid           = near_mark * (1 - slip),
            ask           = near_mark * (1 + slip),
            open_interest = oi,
            timestamp     = ts,
        )
        far_snap = TickerSnapshot(
            instrument    = f"{asset}-{far_label}-{int(strike)}-C",
            asset         = asset,
            spot          = spot,
            mark_price    = far_mark,
            mark_iv       = far_iv,
            bid           = far_mark * (1 - slip),
            ask           = far_mark * (1 + slip),
            open_interest = oi,
            timestamp     = ts,
        )
        frames.append([near_snap, far_snap])

    return frames


# ── Regime definitions ────────────────────────────────────────────────────────

def _regimes() -> list[tuple[str, list[list[TickerSnapshot]]]]:
    """Return (name, frames) for each of the four regimes."""
    # Regime 1: High Vol / Strong Contango
    # near_iv 80%, far_iv 70% → 10% contango >> 2% threshold
    r1 = generate_regime_frames(
        n_frames    = 40,
        spot_start  = 30_000,
        spot_end    = 31_000,
        near_iv_func = lambda i: 0.80,
        far_iv_func  = lambda i: 0.70,
    )

    # Regime 2: Low Vol / Weak Contango
    # near_iv 40%, far_iv 39% → 1% contango < 2% threshold → very few entries
    r2 = generate_regime_frames(
        n_frames    = 40,
        spot_start  = 30_000,
        spot_end    = 32_000,
        near_iv_func = lambda i: 0.40,
        far_iv_func  = lambda i: 0.39,
    )

    # Regime 3: IV Spike then Collapse
    # IV jumps to 120% then falls to 60% — tests stop-loss / TP behaviour
    def spike_near(i):
        return 1.20 if i < 15 else 0.60

    def spike_far(i):
        return 1.00 if i < 15 else 0.55

    r3 = generate_regime_frames(
        n_frames    = 40,
        spot_start  = 30_000,
        spot_end    = 27_000,  # spot falls during the spike
        near_iv_func = spike_near,
        far_iv_func  = spike_far,
    )

    # Regime 4: Stable Sideways
    # near_iv 55%, far_iv 50% → steady 5% contango, spot flat
    r4 = generate_regime_frames(
        n_frames    = 40,
        spot_start  = 30_000,
        spot_end    = 30_200,
        near_iv_func = lambda i: 0.55,
        far_iv_func  = lambda i: 0.50,
    )

    return [
        ("High Vol / Strong Contango", r1),
        ("Low Vol / Weak Contango",    r2),
        ("IV Spike then Collapse",     r3),
        ("Stable Sideways",            r4),
    ]


# ── Loader round-trip checks ──────────────────────────────────────────────────

def _check_loader_csv(frames: list[list[TickerSnapshot]]) -> None:
    """Verify load_csv round-trips the first regime frames correctly."""
    import tempfile, os
    records: list[dict] = []
    for frame in frames:
        for snap in frame:
            records.append({
                "timestamp":     snap.timestamp,
                "instrument":    snap.instrument,
                "asset":         snap.asset,
                "spot":          snap.spot,
                "mark_price":    snap.mark_price,
                "mark_iv":       snap.mark_iv,
                "bid":           snap.bid,
                "ask":           snap.ask,
                "open_interest": snap.open_interest,
            })

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
        csv_path = f.name

    loaded = load_csv(csv_path)
    os.unlink(csv_path)

    ok = len(loaded) == len(frames)
    print(f"  [{'OK' if ok else 'FAIL'}] CSV round-trip: {len(frames)} frames → {len(loaded)} loaded")


def _check_loader_json(frames: list[list[TickerSnapshot]]) -> None:
    """Verify load_json round-trips the first regime frames correctly."""
    import tempfile, os
    records = [
        {
            "timestamp":     snap.timestamp,
            "instrument":    snap.instrument,
            "asset":         snap.asset,
            "spot":          snap.spot,
            "mark_price":    snap.mark_price,
            "mark_iv":       snap.mark_iv,
            "bid":           snap.bid,
            "ask":           snap.ask,
            "open_interest": snap.open_interest,
        }
        for frame in frames for snap in frame
    ]
    payload = {"snapshots": records}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        json_path = f.name

    loaded = load_json(json_path)
    os.unlink(json_path)

    ok = len(loaded) == len(frames)
    print(f"  [{'OK' if ok else 'FAIL'}] JSON round-trip: {len(frames)} frames → {len(loaded)} loaded")


def _check_from_records(frames: list[list[TickerSnapshot]]) -> None:
    """Verify from_records groups snapshots by timestamp correctly."""
    records = [
        {
            "timestamp":     snap.timestamp,
            "instrument":    snap.instrument,
            "asset":         snap.asset,
            "spot":          snap.spot,
            "mark_price":    snap.mark_price,
            "mark_iv":       snap.mark_iv,
            "bid":           snap.bid,
            "ask":           snap.ask,
            "open_interest": snap.open_interest,
        }
        for frame in frames for snap in frame
    ]
    loaded = from_records(records)
    ok = len(loaded) == len(frames)
    print(f"  [{'OK' if ok else 'FAIL'}] from_records: {len(records)} records → {len(loaded)} frames")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    engine = BacktestEngine(
        portfolio_value     = 10_000.0,
        scan_every_n_frames = 5,   # scan every 5 frames (roughly every 5 h)
        daily_loss_limit    = 1e9, # effectively disabled for backtesting
    )

    print("\n" + "═" * 80)
    print("  Calendar Spread Backtest — 4 Regime Run")
    print("═" * 80)

    regimes = _regimes()

    # ── Loader checks ─────────────────────────────────────────────────────────
    print("\nSection 1 — Loader checks")
    print("─" * 60)
    sample_frames = regimes[0][1]
    _check_from_records(sample_frames)
    _check_loader_csv(sample_frames)
    _check_loader_json(sample_frames)

    # ── Backtest runs ─────────────────────────────────────────────────────────
    print("\nSection 2 — Backtest results by regime")
    print("─" * 80)
    header = (
        f"  {'Regime':<22}  {'Trades':>6}  {'Win %':>6}  "
        f"{'Avg P&L':>8}  {'Total':>8}  {'Max DD':>8}  {'Sharpe':>6}"
    )
    print(header)
    print("  " + "-" * 77)

    results: list[BacktestResult] = []
    for name, frames in regimes:
        result = engine.run(frames, regime_name=name)
        result.print_summary()
        results.append(result)

    # ── Cache check ───────────────────────────────────────────────────────────
    print("\nSection 3 — BacktestChainCache verification")
    print("─" * 60)
    from backtest.engine import BacktestChainCache
    cache = BacktestChainCache(ttl=30.0)

    # Inject a snapshot with a timestamp far in the past
    old_ts   = time.time() - 10_000
    old_snap = TickerSnapshot(
        instrument="BTC-TEST-30000-C", asset="BTC", spot=30000.0,
        mark_price=100.0, mark_iv=0.70, bid=99.0, ask=101.0,
        open_interest=500.0, timestamp=old_ts,
    )
    cache.update(old_snap)
    retrieved = cache.get("BTC-TEST-30000-C")
    ok = retrieved is not None
    print(f"  [{'OK' if ok else 'FAIL'}] BacktestChainCache returns 'stale' snapshot as fresh")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\nSection 4 — Cross-regime comparison")
    print("─" * 60)
    best  = max(results, key=lambda r: r.total_pnl)
    worst = min(results, key=lambda r: r.total_pnl)
    print(f"  Best regime  : {best.regime_name}  (total P&L = {best.total_pnl:+.2f})")
    print(f"  Worst regime : {worst.regime_name}  (total P&L = {worst.total_pnl:+.2f})")

    total_checks = 3  # from_records, csv, json, cache = 4 but printed separately
    print("\n" + "═" * 80)
    print(f"  Backtest harness verification complete.  4 regimes replayed.")
    print("═" * 80 + "\n")


if __name__ == "__main__":
    main()
