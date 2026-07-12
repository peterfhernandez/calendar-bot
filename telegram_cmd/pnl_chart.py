"""
telegram_cmd/pnl_chart.py
=========================
Equity curve chart renderer for the `/pnl` Telegram command.

Renders cumulative realized P&L (all-time closed trades) as a black line,
with current unrealized P&L from open positions as a dotted green segment.
Delivered as a PNG via Telegram.
"""

import io
from datetime import datetime

# Set Agg backend BEFORE any pyplot imports
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
import matplotlib.dates as mdates

import config
from data.chain_cache import ChainCache
from db.state import CalendarTrade


def build_cumulative_series(closed_trades: list[CalendarTrade]) -> list[tuple[datetime, float]]:
    """
    Build a running cumulative P&L series from closed trades.

    Each trade's `pnl` is already net of fees (Phase 13) and includes roll P&L (Phase 14).

    Returns list of (date_close as datetime, cumulative_pnl) tuples, ordered chronologically.
    """
    if not closed_trades:
        return []

    cumulative = 0.0
    series = []
    for t in closed_trades:
        if t.date_close:
            pnl = t.pnl or 0.0
            cumulative += pnl
            try:
                date_close = datetime.strptime(t.date_close, config.DATE_FORMAT).replace(tzinfo=None)
            except (ValueError, TypeError):
                continue
            series.append((date_close, cumulative))
    return series


def compute_unrealized(open_trades: list[CalendarTrade], cache: ChainCache) -> tuple[float, int]:
    """
    Compute total unrealized P&L across all open positions.

    Returns (total_unrealized_pnl, open_count).

    Formula per position: (far_mid - near_mid) * qty - net_debit*qty - open_fees + roll_pnl
    Uses live cache mid prices with `last_spread_value` fallback when cache is stale.
    """
    open_count = len(open_trades)
    if not open_trades:
        return 0.0, 0

    total_unrealized = 0.0
    for t in open_trades:
        near_snap = cache.get(t.near_instrument) if t.near_instrument else None
        far_snap = cache.get(t.far_instrument) if t.far_instrument else None

        near_mid = (near_snap.bid + near_snap.ask) / 2.0 if near_snap and near_snap.bid > 0 and near_snap.ask > 0 else None
        far_mid = (far_snap.bid + far_snap.ask) / 2.0 if far_snap and far_snap.bid > 0 and far_snap.ask > 0 else None

        if near_mid is not None and far_mid is not None:
            spread_val = max(0.0, far_mid - near_mid) * t.qty
            unrealized = spread_val - t.net_debit * t.qty - t.open_fees + t.roll_pnl
            total_unrealized += unrealized
        elif t.last_spread_value > 0.0:
            # Fallback to last known spread value
            unrealized = t.last_spread_value - t.net_debit * t.qty - t.open_fees + t.roll_pnl
            total_unrealized += unrealized

    return total_unrealized, open_count


def render_pnl_chart(
    closed_trades: list[CalendarTrade],
    open_trades: list[CalendarTrade],
    cache: ChainCache,
) -> io.BytesIO:
    """
    Render an equity curve chart as a PNG.

    - x-axis: date (chronological)
    - y-axis: cumulative net P&L in USD
    - Black solid line: running sum of realized P&L from closed trades
    - Dotted green line (if open positions exist): unrealized P&L segment from last realized point to now
    - Reference line at y=0

    Returns a seeked-to-0 BytesIO containing PNG bytes.
    """
    fig, ax = plt.subplots(figsize=(10, 6))

    # Build realized series
    realized_series = build_cumulative_series(closed_trades)

    if realized_series:
        dates = [dt for dt, _ in realized_series]
        cumulative_pnls = [pnl for _, pnl in realized_series]

        # Plot realized P&L as black solid line
        ax.plot(dates, cumulative_pnls, color="black", linewidth=2, label="Realized P&L")

        # If there are open positions, extend with unrealized P&L as dotted green line
        total_unrealized, open_count = compute_unrealized(open_trades, cache)
        if open_count > 0:
            last_date = dates[-1]
            now = datetime.now()
            last_cumulative = cumulative_pnls[-1]
            total_cumulative = last_cumulative + total_unrealized

            ax.plot(
                [last_date, now],
                [last_cumulative, total_cumulative],
                color="green",
                linestyle="dotted",
                linewidth=2,
                label=f"Unrealized P&L ({open_count} open)"
            )
    else:
        # No closed trades yet
        now = datetime.now()
        total_unrealized, open_count = compute_unrealized(open_trades, cache)

        if open_count > 0:
            # Plot a dotted green segment from zero to unrealized
            ax.plot(
                [now, now],
                [0, total_unrealized],
                color="green",
                linestyle="dotted",
                linewidth=2,
                label=f"Unrealized P&L ({open_count} open)"
            )
        else:
            # No trades at all
            ax.plot([now], [0], "ko")  # single point at zero

    # Reference line at y=0
    ax.axhline(y=0, color="gray", linestyle="--", alpha=0.5, linewidth=1)

    # Format axes
    ax.set_xlabel("Date")
    ax.set_ylabel("P&L (USD)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x:.0f}"))

    # Rotate and thin x-axis labels for readability with many trades
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter(config.DATE_FORMAT))
    fig.autofmt_xdate(rotation=45, ha="right")

    # Grid and legend
    ax.grid(True, alpha=0.3)
    if realized_series or open_trades:
        ax.legend(loc="upper left")

    plt.tight_layout()

    # Render to BytesIO
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    buf.seek(0)
    plt.close(fig)

    return buf
