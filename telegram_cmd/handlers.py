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
    """Open trades: ev, strike/type, expiry range, entry cost, current value, PnL."""
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
            unr_pnl    = spread_val - t.net_debit * t.qty
            pnl_pct    = (unr_pnl / (t.net_debit * t.qty) * 100) if t.net_debit else 0.0
            val_note   = f"sv=${spread_val:.2f}  PnL=${unr_pnl:+.2f} ({pnl_pct:+.1f}%)"
        else:
            val_note = "sv=N/A (stale cache)"

        near_date = _fmt_expiry(t.expiry_near)
        far_date  = _fmt_expiry(t.expiry_far)
        opt_type  = _fmt_type(t.option_type)

        lines.append(
            f"#{t.id} {t.asset} {t.strike:.0f} {opt_type}  {near_date}→{far_date}  ev={_fmt_ev(t.ev_score)}\n"
            f"  entry=${t.net_debit * t.qty:.2f}  {val_note}"
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
        f"PnL since start: ${session_pnl:+.2f}"
    )
    await update.message.reply_text(status)


async def handle_portfolio(
    update: Update,
    context: CallbackContext,
    cache: ChainCache,
    db_path: Path = DB_PATH,
) -> None:
    """Open trades with asset, strike, expiry range, debit, fees, EV at entry, current value."""
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
            pnl      = curr_val - t.net_debit * t.qty
            val_str  = f"${curr_val:.2f}  PnL=${pnl:+.2f}"
        else:
            val_str = "N/A (stale cache)"

        near_date = _fmt_expiry(t.expiry_near)
        far_date  = _fmt_expiry(t.expiry_far)
        opt_type  = _fmt_type(t.option_type)

        lines.append(
            f"#{t.id} {t.asset} {opt_type} {t.strike:.0f}  {near_date}→{far_date}\n"
            f"  Debit: ${t.net_debit * t.qty:.2f}  Fees: ${t.open_fees:.2f}  EV: {_fmt_ev(t.ev_score)}\n"
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
