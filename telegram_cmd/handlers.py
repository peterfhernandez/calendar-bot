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
    get_trades_closed_today,
    get_trades_opened_today,
    DB_PATH,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import CallbackContext
    from data.chain_cache import ChainCache
    from strategy.decision import DecisionEngine

logger = logging.getLogger(__name__)


def _mid(bid: float, ask: float) -> float | None:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None


async def handle_positions(
    update: Update,
    context: CallbackContext,
    cache: ChainCache,
    db_path: Path = DB_PATH,
) -> None:
    """One line per open trade: instrument pair, entry cost, current spread value, unrealized PnL."""
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

        pair = f"{t.near_instrument or t.expiry_near} / {t.far_instrument or t.expiry_far}"
        lines.append(
            f"#{t.id} {t.asset} {t.strike:.0f}{t.option_type[0]}  {pair}\n"
            f"  entry=${t.net_debit * t.qty:.2f}  {val_note}"
        )

    await update.message.reply_text("\n\n".join(lines))


async def handle_closed_today(
    update: Update,
    context: CallbackContext,
    db_path: Path = DB_PATH,
) -> None:
    """Count of trades closed since midnight UTC and their total realized PnL."""
    trades = get_trades_closed_today(db_path)
    total_pnl = sum(t.pnl for t in trades if t.pnl is not None)
    count = len(trades)
    if count == 0:
        await update.message.reply_text("No trades closed today.")
    else:
        await update.message.reply_text(
            f"{count} trade(s) closed today.\nTotal realized PnL: ${total_pnl:+.2f}"
        )


async def handle_new_today(
    update: Update,
    context: CallbackContext,
    db_path: Path = DB_PATH,
) -> None:
    """Count of positions opened since midnight UTC and their instrument names."""
    trades = get_trades_opened_today(db_path)
    count = len(trades)
    if count == 0:
        await update.message.reply_text("No new positions opened today.")
    else:
        pairs = [
            f"#{t.id} {t.asset} {t.strike:.0f}{t.option_type[0]}"
            for t in trades
        ]
        await update.message.reply_text(
            f"{count} new position(s) opened today:\n" + "\n".join(pairs)
        )


async def handle_status(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
) -> None:
    """Trading mode, drain mode, paused state, uptime, open position count, daily PnL."""
    open_count = len(get_open_trades(engine._db_path))
    uptime_secs = int((datetime.now(timezone.utc) - engine.start_time).total_seconds())
    hours, rem   = divmod(uptime_secs, 3600)
    minutes, sec = divmod(rem, 60)
    uptime_str   = f"{hours}h {minutes}m {sec}s"

    status = (
        f"Mode:    {config.TRADING_MODE.upper()}\n"
        f"Drain:   {'ON' if config.DRAIN_MODE else 'off'}\n"
        f"Paused:  {'YES' if engine.paused else 'no'}\n"
        f"State:   {engine.state.value}\n"
        f"Uptime:  {uptime_str}\n"
        f"Open:    {open_count} position(s)\n"
        f"PnL:     ${engine._today_pnl + engine._unrealized_pnl:+.2f} (today)"
    )
    await update.message.reply_text(status)


async def handle_portfolio(
    update: Update,
    context: CallbackContext,
    cache: ChainCache,
    db_path: Path = DB_PATH,
) -> None:
    """One line per open trade with asset, strike, expiries, debit, fees, EV, IV, OI."""
    trades = get_open_trades(db_path)
    if not trades:
        await update.message.reply_text("No open positions.")
        return

    lines: list[str] = []
    for t in trades:
        near_snap = cache.get(t.near_instrument) if t.near_instrument else None
        far_snap  = cache.get(t.far_instrument)  if t.far_instrument  else None

        near_iv  = f"{near_snap.mark_iv:.1%}" if near_snap and near_snap.mark_iv else "N/A"
        far_iv   = f"{far_snap.mark_iv:.1%}"  if far_snap  and far_snap.mark_iv  else "N/A"
        near_oi  = f"{near_snap.open_interest:.0f}" if near_snap else "N/A"
        far_oi   = f"{far_snap.open_interest:.0f}"  if far_snap  else "N/A"

        stale_note = ""
        if not near_snap or not far_snap:
            stale_note = " (cache stale)"

        lines.append(
            f"#{t.id} {t.asset} {t.option_type} {t.strike:.0f}\n"
            f"  Near: {t.expiry_near}  Far: {t.expiry_far}\n"
            f"  Debit: ${t.net_debit * t.qty:.2f}  Fees: ${t.open_fees:.2f}\n"
            f"  Near IV: {near_iv}  Far IV: {far_iv}\n"
            f"  Near OI: {near_oi}  Far OI: {far_oi}{stale_note}"
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


async def handle_start_drain(
    update: Update,
    context: CallbackContext,
    engine: DecisionEngine,
) -> None:
    """Activate drain mode at runtime: no new entries or rolls."""
    config.DRAIN_MODE = True
    if engine.paused:
        engine.resume()
    await update.message.reply_text(
        "Drain mode activated — no new entries or rolls.\n"
        "Existing positions will close at stop/TP/expiry."
    )
