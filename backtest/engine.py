"""
backtest/engine.py
==================
Replay historical option chain snapshots through the scanner and decision
engine, record all trades, and compute performance statistics.

The engine feeds each frame (a list of TickerSnapshots at one point in time)
into a BacktestChainCache, then calls the DecisionEngine's scan_tick and
monitor_tick methods.  A BacktestExecutor sits between the decision engine
and the cache so that closing prices reflect the market at close time rather
than a fixed break-even assumption.

At the end of a run, closed trades are pulled from the temporary SQLite
database and summarised into a BacktestResult.

Public API
----------
BacktestEngine(portfolio_value, scan_every_n_frames, daily_loss_limit)
    Main replay engine.

BacktestResult
    Dataclass holding per-trade records and aggregate statistics.

BacktestChainCache
    ChainCache subclass with TTL disabled (always returns data as fresh).

BacktestExecutor
    ExecutorProtocol implementation that prices closes from the live cache.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from data.chain_cache import ChainCache
from data.deribit_feed import TickerSnapshot
from db.state import get_calendar_stats, init_db
from strategy.decision import DecisionEngine, DryRunExecutor
from strategy.scanner import CalendarCandidate

logger = logging.getLogger(__name__)


# ── Cache ─────────────────────────────────────────────────────────────────────

class BacktestChainCache(ChainCache):
    """
    ChainCache variant for backtesting: TTL staleness check is disabled.

    All snapshots are always returned as fresh regardless of their timestamp.
    This lets the engine inject historical data without the cache discarding
    it for being "old".
    """

    def _is_stale(self, snap: TickerSnapshot) -> bool:  # type: ignore[override]
        return False


# ── Executor ──────────────────────────────────────────────────────────────────

class BacktestExecutor:
    """
    ExecutorProtocol implementation for backtesting.

    Opens use the candidate's current bid/ask.  Closes look up the current
    mark prices in the cache; if the instrument is no longer in the cache
    (e.g. expiry passed), a small loss is assumed.

    Parameters
    ----------
    cache:
        The BacktestChainCache updated by the engine on every frame.
    slippage:
        Fraction of mark price added to buys / subtracted from sells to
        model execution friction (default 0.5%).
    """

    def __init__(self, cache: BacktestChainCache, slippage: float = 0.005) -> None:
        self._cache    = cache
        self._slippage = slippage

    def enter_spread(self, candidate: CalendarCandidate) -> dict | None:
        near_prem = candidate.near_bid * (1 - self._slippage)
        far_prem  = candidate.far_ask  * (1 + self._slippage)
        net_debit = far_prem - near_prem
        if net_debit <= 0:
            return None
        return {
            "near_prem": near_prem,
            "far_prem":  far_prem,
            "net_debit": net_debit,
            "qty":       candidate.qty,
        }

    def close_spread(self, position: dict) -> float | None:
        """
        Return the per-contract closing credit.

        The caller computes: pnl = close_credit - net_debit * qty
        """
        near_snap = self._cache.get(position.get("near_instrument", ""))
        far_snap  = self._cache.get(position.get("far_instrument",  ""))

        if near_snap and far_snap:
            near_ask = (near_snap.ask if near_snap.ask > 0 else near_snap.mark_price) * (1 + self._slippage)
            far_bid  = (far_snap.bid  if far_snap.bid  > 0 else far_snap.mark_price)  * (1 - self._slippage)
            return max(far_bid - near_ask, 0.0)

        # Instruments have left the cache (e.g. near leg expired).
        # Assume the far leg retains 60% of its value (modest loss).
        net_debit = position.get("net_debit", 0.0)
        return net_debit * 0.6

    def roll_near_leg(self, position: dict, new_candidate: CalendarCandidate) -> bool:
        return True  # rolling always succeeds in simulation


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    """
    Summary statistics for a single backtest run.

    Attributes
    ----------
    regime_name:    Human-readable label for the market regime.
    trades:         List of closed trade dicts from the database.
    equity_curve:   Cumulative portfolio value after each trade closes.
    total_trades:   Number of completed (closed) trades.
    win_rate:       Fraction of trades with positive P&L (0–1).
    avg_pnl:        Mean P&L per trade in USD.
    total_pnl:      Sum of all trade P&Ls in USD.
    max_drawdown:   Largest peak-to-trough drop in cumulative P&L.
    sharpe:         Annualised Sharpe ratio estimated from per-trade returns.
    """

    regime_name:   str
    trades:        list[dict]      = field(default_factory=list)
    equity_curve:  list[float]     = field(default_factory=list)

    total_trades:  int   = 0
    win_rate:      float = 0.0
    avg_pnl:       float = 0.0
    total_pnl:     float = 0.0
    max_drawdown:  float = 0.0
    sharpe:        float = 0.0
    total_fees:    float = 0.0   # sum of open_fees + close_fees across all trades

    def print_summary(self) -> None:
        """Print a formatted one-line summary to stdout."""
        wr  = f"{self.win_rate * 100:5.1f}%"
        ap  = f"{self.avg_pnl:+8.2f}"
        mdd = f"{self.max_drawdown:8.2f}"
        sh  = f"{self.sharpe:+6.2f}" if not math.isnan(self.sharpe) else "   N/A"
        tp  = f"{self.total_pnl:+8.2f}"
        tf  = f"{self.total_fees:8.2f}"
        n   = self.total_trades
        print(
            f"  {self.regime_name:<22}  trades={n:>3}  win={wr}  "
            f"avg_pnl={ap}  total={tp}  fees={tf}  mdd={mdd}  sharpe={sh}"
        )


# ── Engine ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Replay option chain frames through the scanner and decision engine.

    Parameters
    ----------
    portfolio_value:
        Starting portfolio size in USD (used for position sizing).
    scan_every_n_frames:
        How often to call scan_tick (default 1 = every frame).
        Monitor tick is always called on every frame.
    daily_loss_limit:
        USD halt threshold passed to DecisionEngine.  Set to a very large
        number to effectively disable the halt during backtesting.
    """

    def __init__(
        self,
        portfolio_value:     float = 10_000.0,
        scan_every_n_frames: int   = 1,
        daily_loss_limit:    float = 1e9,
    ) -> None:
        self._portfolio_value     = portfolio_value
        self._scan_every_n_frames = scan_every_n_frames
        self._daily_loss_limit    = daily_loss_limit

    def run(
        self,
        frames:      list[list[TickerSnapshot]],
        regime_name: str = "unnamed",
    ) -> BacktestResult:
        """
        Replay *frames* and return a BacktestResult.

        Parameters
        ----------
        frames:
            Chronological list of market frames from the loader.
        regime_name:
            Label attached to the result (shown in summaries).
        """
        if not frames:
            logger.warning("BacktestEngine.run: no frames — returning empty result")
            return BacktestResult(regime_name=regime_name)

        # Isolated temporary database for this run
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = Path(tmp.name)
        init_db(db_path)

        cache    = BacktestChainCache(ttl=999_999.0)
        executor = BacktestExecutor(cache)
        engine   = DecisionEngine(
            cache            = cache,
            portfolio_value  = self._portfolio_value,
            executor         = executor,
            db_path          = db_path,
            daily_loss_limit = self._daily_loss_limit,
        )

        logger.info("Backtest '%s': replaying %d frames", regime_name, len(frames))

        for i, frame in enumerate(frames):
            # Inject the frame into the cache
            for snap in frame:
                cache.update(snap)

            # Monitor every frame; scan every N frames
            engine.monitor_tick()
            if i % self._scan_every_n_frames == 0:
                engine.scan_tick()

        result = self._build_result(regime_name, db_path)
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass
        return result

    # ── Result construction ───────────────────────────────────────────────────

    @staticmethod
    def _build_result(regime_name: str, db_path: Path) -> BacktestResult:
        stats = get_calendar_stats(db_path=db_path)

        # Fetch closed trade rows for equity curve
        closed_statuses = (
            "Win", "Loss", "Closed",
            "Win (Auto TP)", "Loss (Auto Stop)", "Loss (Stop)", "Loss (Early)",
        )
        placeholders = ",".join("?" * len(closed_statuses))
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM calendar_trades WHERE result IN ({placeholders}) ORDER BY date_close, id",
                closed_statuses,
            ).fetchall()

        trades: list[dict] = [dict(r) for r in rows]
        pnls   = [t["pnl"] for t in trades if t.get("pnl") is not None]

        equity_curve = list(_cumulative(pnls))
        max_drawdown = _max_drawdown(equity_curve)
        sharpe       = _sharpe(pnls)
        n            = len(pnls)
        total_pnl    = sum(pnls) if pnls else 0.0
        wins         = sum(1 for p in pnls if p > 0)
        total_fees   = sum(
            (t.get("open_fees") or 0.0) + (t.get("close_fees") or 0.0)
            for t in trades
        )

        return BacktestResult(
            regime_name  = regime_name,
            trades       = trades,
            equity_curve = equity_curve,
            total_trades = n,
            win_rate     = wins / n if n else 0.0,
            avg_pnl      = total_pnl / n if n else 0.0,
            total_pnl    = total_pnl,
            max_drawdown = max_drawdown,
            sharpe       = sharpe,
            total_fees   = total_fees,
        )


# ── Statistics helpers ────────────────────────────────────────────────────────

def _cumulative(pnls: list[float]) -> Iterator[float]:
    total = 0.0
    for p in pnls:
        total += p
        yield total


def _max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough decline in the equity curve."""
    if not equity:
        return 0.0
    peak = equity[0]
    mdd  = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > mdd:
            mdd = dd
    return mdd


def _sharpe(pnls: list[float], periods_per_year: float = 252.0) -> float:
    """
    Annualised Sharpe ratio estimated from per-trade P&Ls.

    Uses a 0 risk-free rate.  Returns NaN when fewer than 2 observations.
    """
    n = len(pnls)
    if n < 2:
        return float("nan")
    mean = sum(pnls) / n
    var  = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std  = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return float("nan")
    # Scale from per-trade to annualised assuming `periods_per_year` trades/year
    return (mean / std) * math.sqrt(periods_per_year)
