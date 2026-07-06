"""
telegram_cmd/handlers.py
========================
Per-command handler functions for the Telegram command listener.

Each handler receives a python-telegram-bot Update and CallbackContext,
plus references to the engine, cache, and db_path injected by the listener.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import config
from db.state import (
    get_open_trades,
    get_trades_closed_today_aest,
    get_trades_closed_since,
    get_trades_opened_today_aest,
    get_trades_opened_since,
    get_stuck_positions,
    mark_position_manually_closed,
    reset_close_stuck_position,
    get_all_closed_trades,
    DB_PATH,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import CallbackContext
    from data.chain_cache import ChainCache
    from strategy.decision import DecisionEngine

logger = logging.getLogger(__name__)

_AEST_LABEL = "AEST"


def _mid(bid: float, ask: float) -> float | None:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None


def _fmt_ev(ev_score: float) -> str:
    """Return formatted EV string, or 'N/A' for the 0.0 sentinel (pre-tracking trades)."""
    return f"{ev_score:.2f}" if ev_score != 0.0 else "N/A"


def _fmt_expiry(expiry_iso: str) -> str:
    """Convert ISO date string to ddMMMYY format, e.g. '2026-06-27' → '27Jun26'."""
    try:
        return datetime.strptime(expiry_iso, "%Y-%m-%d").strftime("%d%b%y")
    except (ValueError, TypeError):
        return expiry_iso or "?"


def _fmt_type(option_type: str) -> str:
    """Return 'Call' or 'Put' from any option_type string."""
    if option_type and option_type[0].upper() == "C":
        return "Call"
    return "Put"


async def handle_positions(
    update: Update,
    context: CallbackContext,
    cache: ChainCache,
    db_path: Path = DB_PATH,
) -> None:
    """Open trades: ev, strike/type, expiry range, entry cost, current value, PnL (separated by roll)."""
    trades = get_open_trades(db_path)
    if not trades:
        await update.message.reply_text("No open positions.")
        return

    lines: list[str] = []
    for t in trades:
        near_snap = cache.get(t.near_instrument) if t.near_instrument else None
        far_snap  = cache.get(t.far_instrument)  if t.far_instrument  else None

        near_mid = _mid(near_snap.bid, near_snap.ask) if near_snap else None
        far_mid  = _mid(far_snap.bid,  far_snap.ask)  if far_snap  else None

        if near_mid is not None and far_mid is not None:
            spread_val = max(0.0, far_mid - near_mid) * t.qty
            cost_basis = t.net_debit * t.qty + t.open_fees
            unr_pnl    = spread_val - cost_basis
            total_pnl  = unr_pnl + t.roll_pnl  # add roll profit
            pnl_pct    = (total_pnl / cost_basis * 100) if cost_basis else 0.0

            pnl_note = f"PnL=${total_pnl:+.2f} ({pnl_pct:+.1f}%)"
            if t.roll_pnl != 0.0:
                pnl_note += f"  [unr=${unr_pnl:+.2f}  roll=${t.roll_pnl:+.2f}]"
            val_note   = f"sv=${spread_val:.2f}  {pnl_note}"
        elif t.last_spread_value > 0.0:
            # Cache is stale, but we have a last known spread value from the previous monitor tick
            cost_basis = t.net_debit * t.qty + t.open_fees
            unr_pnl    = t.last_spread_value - cost_basis
            total_pnl  = unr_pnl + t.roll_pnl
            pnl_pct    = (total_pnl / cost_basis * 100) if cost_basis else 0.0

            pnl_note = f"PnL=${total_pnl:+.2f} ({pnl_pct:+.1f}%)"
            if t.roll_pnl != 0.0:
                pnl_note += f"  [unr=${unr_pnl:+.2f}  roll=${t.roll_pnl:+.2f}]"
            val_note   = f"sv=${t.last_spread_value:.2f}*  {pnl_note}"  # asterisk indicates cached value
        else:
            val_note = "sv=N/A (stale cache)"

        near_date = _fmt_expiry(t.expiry_near)
        far_date  = _fmt_expiry(t.expiry_far)
        opt_type  = _fmt_type(t.option_type)

        ev_display = f"ev_init={_fmt_ev(t.ev_score_initial)}"
        if t.ev_score_at_roll != 0.0:
            ev_display += f"  ev_roll={_fmt_ev(t.ev_score_at_roll)}"

        lines.append(
            f"#{t.id} {t.asset} {t.strike:.0f} {opt_type}  {near_date}→{far_date}"
            f"   entry=${t.net_debit * t.qty:.2f}  {val_note}   {ev_display}"
        )

    await update.message.reply_text("\n\n".join(lines))


async def handle_closed_trades(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
    db_path: Path = DB_PATH,
) -> None:
    """List of closed trades. Usage: /closed_trades [today|session] (default: today)."""
    args = context.args
    mode = (args[0].lower() if args else "today")

    if mode == "session":
        trades = get_trades_closed_since(engine.start_time, db_path)
        label = "since bot start"
    else:
        trades = get_trades_closed_today_aest(db_path)
        label = f"today ({_AEST_LABEL})"

    if not trades:
        await update.message.reply_text(f"No trades closed {label}.")
        return

    total_pnl = sum(t.pnl for t in trades if t.pnl is not None)
    lines = [f"{len(trades)} trade(s) closed {label}. Total PnL: ${total_pnl:+.2f}\n"]
    for t in trades:
        pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "N/A"
        reason  = t.notes or t.result or "—"
        lines.append(
            f"#{t.id} {t.asset}  debit=${t.net_debit * t.qty:.2f}  pnl={pnl_str}  {reason}"
        )

    await update.message.reply_text("\n".join(lines))


async def handle_new_trades(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
    db_path: Path = DB_PATH,
) -> None:
    """List of new trades. Usage: /new_trades [today|session] (default: today)."""
    args = context.args
    mode = (args[0].lower() if args else "today")

    if mode == "session":
        trades = get_trades_opened_since(engine.start_time, db_path)
        label = "since bot start"
    else:
        trades = get_trades_opened_today_aest(db_path)
        label = f"today ({_AEST_LABEL})"

    if not trades:
        await update.message.reply_text(f"No new trades {label}.")
        return

    lines = [f"{len(trades)} new trade(s) {label}:\n"]
    for t in trades:
        near_date = _fmt_expiry(t.expiry_near)
        far_date  = _fmt_expiry(t.expiry_far)
        opt_type  = _fmt_type(t.option_type)
        lines.append(
            f"#{t.id} {t.asset}  debit=${t.net_debit * t.qty:.2f}  ev={_fmt_ev(t.ev_score)}"
            f"  {t.strike:.0f} {opt_type}  {near_date}→{far_date}"
        )

    await update.message.reply_text("\n".join(lines))


async def handle_status(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
    db_path: Path = DB_PATH,
) -> None:
    """Trading mode, drain mode, paused state, uptime, open count, today AEST PnL, session PnL."""
    open_count = len(get_open_trades(db_path))

    uptime_secs  = int((datetime.now(timezone.utc) - engine.start_time).total_seconds())
    hours, rem   = divmod(uptime_secs, 3600)
    minutes, sec = divmod(rem, 60)
    uptime_str   = f"{hours}h {minutes}m {sec}s"

    # Today AEST: sum closed trades since AEST midnight + current unrealized
    closed_aest  = get_trades_closed_today_aest(db_path)
    today_pnl    = sum(t.pnl for t in closed_aest if t.pnl is not None) + engine._unrealized_pnl
    session_pnl  = engine.session_pnl + engine._unrealized_pnl

    drain_label = (
        "ON (drain+new)" if config.DRAIN_AND_NEW_MODE
        else ("ON" if config.DRAIN_MODE else "off")
    )

    status = (
        f"Mode:         {config.TRADING_MODE.upper()}\n"
        f"Drain:        {drain_label}\n"
        f"Paused:       {'YES' if engine.paused else 'no'}\n"
        f"State:        {engine.state.value}\n"
        f"Uptime:       {uptime_str}\n"
        f"Open:         {open_count} position(s)\n"
        f"PnL today ({_AEST_LABEL}): ${today_pnl:+.2f}\n"
        f"PnL since start: ${session_pnl:+.2f}\n"
        f"Fees (session):  ${engine.fees_paid_today:.2f}"
    )
    await update.message.reply_text(status)


async def handle_portfolio(
    update: Update,
    context: CallbackContext,
    cache: ChainCache,
    db_path: Path = DB_PATH,
) -> None:
    """Open trades with asset, strike, expiry range, debit, fees, EV at entry & roll, total PnL."""
    trades = get_open_trades(db_path)
    if not trades:
        await update.message.reply_text("No open positions.")
        return

    lines: list[str] = []
    for t in trades:
        near_snap = cache.get(t.near_instrument) if t.near_instrument else None
        far_snap  = cache.get(t.far_instrument)  if t.far_instrument  else None

        near_mid = _mid(near_snap.bid, near_snap.ask) if near_snap else None
        far_mid  = _mid(far_snap.bid,  far_snap.ask)  if far_snap  else None

        if near_mid is not None and far_mid is not None:
            curr_val = max(0.0, far_mid - near_mid) * t.qty
            unr_pnl  = curr_val - t.net_debit * t.qty - t.open_fees
            total_pnl = unr_pnl + t.roll_pnl  # total includes roll profit
            val_str  = f"${curr_val:.2f}  PnL=${total_pnl:+.2f}"
        else:
            val_str = "N/A (stale cache)"

        near_date = _fmt_expiry(t.expiry_near)
        far_date  = _fmt_expiry(t.expiry_far)
        opt_type  = _fmt_type(t.option_type)

        ev_line = f"  EV_init: {_fmt_ev(t.ev_score_initial)}"
        if t.ev_score_at_roll != 0.0:
            ev_line += f"  EV_roll: {_fmt_ev(t.ev_score_at_roll)}"

        lines.append(
            f"#{t.id} {t.asset} {opt_type} {t.strike:.0f}  {near_date}→{far_date}\n"
            f"  Debit: ${t.net_debit * t.qty:.2f}  Fees: ${t.open_fees:.2f}  Roll PnL: ${t.roll_pnl:+.2f}\n"
            f"{ev_line}\n"
            f"  Value: {val_str}"
        )

    await update.message.reply_text("\n\n".join(lines))


async def handle_stop_bot(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
) -> None:
    """Pause scan/monitor ticks; feed and listener remain active."""
    engine.pause()
    await update.message.reply_text(
        "Bot paused — scanning and monitoring stopped.\nUse /start_bot to resume."
    )


async def handle_start_bot(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
) -> None:
    """Resume scan/monitor ticks after a pause."""
    engine.resume()
    await update.message.reply_text(
        "Bot resumed — scanning and monitoring restarted."
    )


async def handle_help(
    update: Update,
    context: CallbackContext,
) -> None:
    """List all available commands with descriptions."""
    from telegram_cmd.listener import COMMAND_REGISTRY
    lines = [f"/{cmd} — {desc}" for cmd, desc in COMMAND_REGISTRY]
    await update.message.reply_text("\n".join(lines))


async def handle_start_drain(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
) -> None:
    """Activate drain mode at runtime: no new entries or rolls."""
    config.DRAIN_MODE = True
    config.DRAIN_AND_NEW_MODE = False
    if engine.paused:
        engine.resume()
    await update.message.reply_text(
        "Drain mode activated — no new entries or rolls.\n"
        "Existing positions will close at stop/TP/expiry."
    )


async def handle_start_with_assets(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
) -> None:
    """Override ASSETS list and resume the bot: /start_with_assets BTC,ETH,SOL"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /start_with_assets Asset1,Asset2,...\nExample: /start_with_assets BTC,ETH"
        )
        return

    raw = " ".join(args)
    assets = [a.strip().upper() for a in raw.split(",") if a.strip()]
    if not assets:
        await update.message.reply_text("No valid assets provided.")
        return

    config.ASSETS = assets
    config.DRAIN_MODE = False
    config.DRAIN_AND_NEW_MODE = False
    if engine.paused:
        engine.resume()

    await update.message.reply_text(
        f"Assets updated to: {', '.join(assets)}\n"
        "Bot resumed — scanning and monitoring restarted."
    )


async def handle_drain_and_new(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
) -> None:
    """
    Activate drain-and-new mode: close existing positions (no rolls) but allow
    new entries with an optional new portfolio value and asset list.

    Usage: /drain_and_new portfolio=50000 assets=BTC,ETH
    Both parameters are optional.
    """
    args = context.args
    portfolio_override: float | None = None
    new_assets: list[str] | None = None

    for arg in args:
        if arg.lower().startswith("portfolio="):
            try:
                portfolio_override = float(arg.split("=", 1)[1])
            except ValueError:
                await update.message.reply_text(
                    f"Invalid portfolio value: '{arg}'. Use portfolio=50000"
                )
                return
        elif arg.lower().startswith("assets="):
            raw = arg.split("=", 1)[1]
            new_assets = [a.strip().upper() for a in raw.split(",") if a.strip()]

    config.DRAIN_AND_NEW_MODE = True
    config.DRAIN_MODE = False

    if portfolio_override is not None:
        config.PORTFOLIO_OVERRIDE = portfolio_override
        engine.portfolio_value = portfolio_override

    if new_assets:
        config.ASSETS = new_assets

    if engine.paused:
        engine.resume()

    parts = ["Drain-and-new mode activated:"]
    parts.append("  • Existing positions: close outright (no rolls)")
    parts.append("  • New entries: allowed")
    if portfolio_override is not None:
        parts.append(f"  • Portfolio override: ${portfolio_override:,.0f}")
    if new_assets:
        parts.append(f"  • Assets: {', '.join(new_assets)}")

    await update.message.reply_text("\n".join(parts))


async def handle_info(
    update: Update,
    context: CallbackContext,
    cache: ChainCache,
    db_path: Path = DB_PATH,
) -> None:
    """
    Check current position status on Deribit.

    Shows live bid/ask, mark price, and current position P&L from cache.
    Usage: /info trade_id=42
    """
    if not context.args:
        await update.message.reply_text("Usage: /info trade_id=N")
        return

    trade_id = None
    for arg in context.args:
        if arg.lower().startswith("trade_id="):
            try:
                trade_id = int(arg.split("=", 1)[1])
            except ValueError:
                await update.message.reply_text(f"Invalid trade_id: {arg}")
                return

    if trade_id is None:
        await update.message.reply_text("Usage: /info trade_id=42")
        return

    # Fetch trade from DB
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM calendar_trades WHERE id = ?", (trade_id,)
    ).fetchone()
    conn.close()

    if not row:
        await update.message.reply_text(f"Trade #{trade_id} not found in database")
        return

    # Fetch live prices from cache
    near_snap = cache.get(row["near_instrument"])
    far_snap = cache.get(row["far_instrument"])

    parts = [f"*Trade #{trade_id} Status*"]
    parts.append(f"{row['asset']} {row['option_type']} strike={row['strike']:.0f}")
    parts.append(f"Expiry: {row['expiry_near']} → {row['expiry_far']}")
    parts.append(f"Qty: {row['qty']}")
    parts.append(f"Entry debit: ${row['net_debit'] * row['qty']:.4f}")
    parts.append("")
    parts.append("*Current Market Prices (from Deribit):*")

    if near_snap:
        near_mid = _mid(near_snap.bid, near_snap.ask)
        mid_str = f"{near_mid:.4f}" if near_mid is not None else "N/A"
        parts.append(f"Near leg: bid={near_snap.bid:.4f} ask={near_snap.ask:.4f} mid={mid_str}")
    else:
        parts.append(f"Near leg: NOT IN CACHE (stale or not subscribed)")

    if far_snap:
        far_mid = _mid(far_snap.bid, far_snap.ask)
        far_mid_str = f"{far_mid:.4f}" if far_mid is not None else "N/A"
        parts.append(f"Far leg:  bid={far_snap.bid:.4f} ask={far_snap.ask:.4f} mid={far_mid_str}")
    else:
        parts.append(f"Far leg: NOT IN CACHE (stale or not subscribed)")

    if near_snap and far_snap and _mid(near_snap.bid, near_snap.ask) and _mid(far_snap.bid, far_snap.ask):
        sv = (_mid(far_snap.bid, far_snap.ask) - _mid(near_snap.bid, near_snap.ask)) * row['qty']
        cost_basis = row['net_debit'] * row['qty'] + row['open_fees']
        unrealized = sv - cost_basis
        parts.append("")
        parts.append(f"*Unrealized P&L:* ${unrealized:+.4f} ({unrealized/cost_basis*100:+.1f}%)")
    else:
        parts.append("")
        parts.append("⚠️  Cannot calculate current P&L — cache data incomplete")

    parts.append(f"\nDB Status: {row['close_status']}")
    if row['close_error_reason']:
        parts.append(f"Error: {row['close_error_reason']}")

    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def handle_close(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
    db_path: Path = DB_PATH,
) -> None:
    """
    Retry closing a stuck position (tell bot to try again).

    Resets the close_stuck flag so the bot will attempt close on next monitor tick.
    Usage: /close trade_id=42
    """
    if not context.args:
        await update.message.reply_text("Usage: /close trade_id=N")
        return

    trade_id = None
    for arg in context.args:
        if arg.lower().startswith("trade_id="):
            try:
                trade_id = int(arg.split("=", 1)[1])
            except ValueError:
                await update.message.reply_text(f"Invalid trade_id: {arg}")
                return

    if trade_id is None:
        await update.message.reply_text("Usage: /close trade_id=42")
        return

    try:
        reset_close_stuck_position(trade_id, db_path)
        # Clear the notification flag so user gets notified again if it gets stuck again
        engine._notified_stuck.discard(trade_id)
        await update.message.reply_text(
            f"✓ Trade #{trade_id} reset for retry.\n"
            f"Bot will attempt to close on next monitor tick (~1 minute)."
        )
    except Exception as exc:
        await update.message.reply_text(f"Error resetting trade: {exc}")
        logger.error("Failed to reset close_stuck for trade_id=%d: %s", trade_id, exc)


async def handle_close_manually(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
    db_path: Path = DB_PATH,
) -> None:
    """
    Manually close a stuck position with a user-provided spread value.

    Marks the position as closed in the database with the given spread value.
    P&L is calculated as: spread_value - net_debit*qty - open_fees

    Usage: /close_manually trade_id=42 spread=0.0050
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /close_manually trade_id=N spread=VALUE\n"
            "Example: /close_manually trade_id=42 spread=0.0050"
        )
        return

    trade_id = None
    spread_value = None

    for arg in context.args:
        if arg.lower().startswith("trade_id="):
            try:
                trade_id = int(arg.split("=", 1)[1])
            except ValueError:
                await update.message.reply_text(f"Invalid trade_id: {arg}")
                return
        elif arg.lower().startswith("spread="):
            try:
                spread_value = float(arg.split("=", 1)[1])
            except ValueError:
                await update.message.reply_text(f"Invalid spread value: {arg}")
                return

    if trade_id is None or spread_value is None:
        await update.message.reply_text(
            "Usage: /close_manually trade_id=N spread=VALUE\n"
            "Example: /close_manually trade_id=42 spread=0.0050"
        )
        return

    try:
        trade = mark_position_manually_closed(trade_id, spread_value, "manual", db_path)
        pnl = trade.pnl or 0.0
        # Clear the notification flag since position is now resolved
        engine._notified_stuck.discard(trade_id)

        await update.message.reply_text(
            f"✓ Trade #{trade_id} manually closed\n"
            f"{trade.asset} {trade.option_type} strike={trade.strike:.0f}\n"
            f"Spread value: ${spread_value:.4f}\n"
            f"Realised P&L: ${pnl:+.4f}"
        )
        logger.info("Trade #%d manually closed by user: spread=%.4f, pnl=%.4f", trade_id, spread_value, pnl)
    except ValueError as exc:
        await update.message.reply_text(f"Trade not found: {exc}")
    except Exception as exc:
        await update.message.reply_text(f"Error closing trade: {exc}")
        logger.error("Failed to manually close trade_id=%d: %s", trade_id, exc)


async def handle_pnl(
    update: Update,
    context: CallbackContext,
    cache: ChainCache,
    db_path: Path = DB_PATH,
) -> None:
    """
    Send an equity curve chart: realized P&L (black line) + unrealized P&L (dotted green).

    Returns a PNG image with cumulative realized P&L from all closed trades, plus the
    projected total including unrealized P&L from open positions.

    If no trading history exists, replies with text instead.
    """
    from telegram_cmd.pnl_chart import render_pnl_chart

    try:
        closed_trades = get_all_closed_trades(db_path)
        open_trades = get_open_trades(db_path)

        # If no history at all, reply with text
        if not closed_trades and not open_trades:
            await update.message.reply_text("No trading history yet.")
            return

        # If only open trades (no closed trades), still render chart
        if not closed_trades:
            buf = render_pnl_chart([], open_trades, cache)
            caption = "No closed trades yet. Showing unrealized P&L from open positions."
            await update.message.reply_photo(photo=buf, caption=caption)
            return

        # Normal case: render chart with history
        buf = render_pnl_chart(closed_trades, open_trades, cache)

        # Compute realized and unrealized totals for caption
        from telegram_cmd.pnl_chart import build_cumulative_series, compute_unrealized
        realized_series = build_cumulative_series(closed_trades)
        total_realized = realized_series[-1][1] if realized_series else 0.0
        total_unrealized, open_count = compute_unrealized(open_trades, cache)
        total_combined = total_realized + total_unrealized

        caption_parts = [
            f"Realized: ${total_realized:+.2f}",
            f"Unrealized: ${total_unrealized:+.2f} ({open_count} open)",
            f"Total: ${total_combined:+.2f}",
        ]
        caption = "\n".join(caption_parts)

        await update.message.reply_photo(photo=buf, caption=caption)
        logger.info("Sent /pnl chart to Telegram (closed=%d, open=%d)", len(closed_trades), open_count)

    except Exception as exc:
        await update.message.reply_text(f"Error rendering chart: {exc}")
        logger.error("Failed to render /pnl chart: %s", exc)
