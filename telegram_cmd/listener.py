"""
telegram_cmd/listener.py
========================
TelegramCommandListener — incoming command handling for the calendar spread bot.

Long-polls the Telegram Bot API using python-telegram-bot v21. Runs as a
fourth asyncio task in bot.py alongside the feed, loop, and (optionally)
the data collector.

Security: every incoming update is validated against config.TELEGRAM_CHAT.
Messages from any other chat ID are silently dropped — no reply is sent.

Telegram imports are lazy (inside methods) so the bot starts normally
even when python-telegram-bot is not installed and TELEGRAM_TOKEN is unset.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import config
from data.chain_cache import ChainCache
from db.state import DB_PATH
from strategy.decision import DecisionEngine
from telegram_cmd import handlers

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, CallbackContext

logger = logging.getLogger(__name__)

# Single source of truth for commands — drives both set_my_commands() and /help.
COMMAND_REGISTRY: list[tuple[str, str]] = [
    ("positions",         "Open trades: ev, strike/type, expiry range, entry cost, current value, PnL"),
    ("portfolio",         "Open trades: asset, strike, expiry range, debit, fees, EV, current value"),
    ("new_trades",        "New trades: /new_trades [today|session] — defaults to today AEST"),
    ("closed_trades",     "Closed trades: /closed_trades [today|session] — defaults to today AEST"),
    ("status",            "Trading mode, drain mode, paused state, uptime, open count, today/session PnL"),
    ("stop_bot",          "Pause scanning and monitoring (feed and listener remain alive)"),
    ("start_bot",         "Resume scanning and monitoring after a pause"),
    ("start_drain",       "Drain mode — no new entries or rolls; existing positions close at stop/TP/expiry"),
    ("start_with_assets", "Override asset list and resume: /start_with_assets BTC,ETH,SOL"),
    ("drain_and_new",     "Close existing positions (no rolls) but allow new entries: /drain_and_new portfolio=N assets=BTC,ETH"),
    ("info",              "Check position status on Deribit: /info trade_id=N"),
    ("close",             "Retry closing a stuck position: /close trade_id=N"),
    ("close_manually",    "Manually close position with known spread: /close_manually trade_id=N spread=VALUE"),
    ("pnl",               "Equity curve: realized P&L (black) + unrealized PnL (dotted green)"),
    ("help",              "List all available commands with descriptions"),
]


def _require_authorized_chat(handler_fn):
    """Decorator: silently drop updates from chats other than TELEGRAM_CHAT."""
    @functools.wraps(handler_fn)
    async def wrapper(update, context, **kwargs):
        if not config.TELEGRAM_CHAT:
            return
        try:
            allowed_id = int(config.TELEGRAM_CHAT)
        except (ValueError, TypeError):
            logger.warning("TELEGRAM_CHAT is not a valid integer — dropping update")
            return
        if update.effective_chat and update.effective_chat.id != allowed_id:
            logger.debug(
                "Dropped update from unauthorized chat_id=%s (allowed=%s)",
                update.effective_chat.id,
                allowed_id,
            )
            return
        return await handler_fn(update, context, **kwargs)
    return wrapper


class TelegramCommandListener:
    """
    Listens for incoming Telegram commands and dispatches them to handlers.

    Parameters
    ----------
    engine
        The running DecisionEngine (for pause/resume and status queries).
    cache
        The live ChainCache (for IV/OI lookups in /positions and /portfolio).
    db_path
        Path to the SQLite database used to load open/closed trade records.
    """

    def __init__(
        self,
        engine: DecisionEngine,
        cache: ChainCache,
        db_path: Path = DB_PATH,
    ) -> None:
        self._engine   = engine
        self._cache    = cache
        self._db_path  = db_path
        self._app: Application | None = None
        self._stopped  = asyncio.Event()

    def _build_app(self) -> Application:
        from telegram.ext import Application, CommandHandler

        # Short get_updates timeouts prevent a harmless ConnectTimeout error
        # in the library's shutdown cleanup pass (_get_updates_cleanup).
        app = (
            Application.builder()
            .token(config.TELEGRAM_TOKEN)
            .get_updates_connect_timeout(5.0)
            .get_updates_read_timeout(5.0)
            .build()
        )

        engine  = self._engine
        cache   = self._cache
        db_path = self._db_path

        # Wrap each handler to inject dependencies and enforce chat security.
        @_require_authorized_chat
        async def cmd_positions(update, context):
            await handlers.handle_positions(update, context, cache, db_path)

        @_require_authorized_chat
        async def cmd_closed_trades(update, context):
            await handlers.handle_closed_trades(update, context, engine, db_path)

        @_require_authorized_chat
        async def cmd_new_trades(update, context):
            await handlers.handle_new_trades(update, context, engine, db_path)

        @_require_authorized_chat
        async def cmd_status(update, context):
            await handlers.handle_status(update, context, engine, db_path)

        @_require_authorized_chat
        async def cmd_portfolio(update, context):
            await handlers.handle_portfolio(update, context, cache, db_path)

        @_require_authorized_chat
        async def cmd_stop_bot(update, context):
            await handlers.handle_stop_bot(update, context, engine)

        @_require_authorized_chat
        async def cmd_start_bot(update, context):
            await handlers.handle_start_bot(update, context, engine)

        @_require_authorized_chat
        async def cmd_start_drain(update, context):
            await handlers.handle_start_drain(update, context, engine)

        @_require_authorized_chat
        async def cmd_start_with_assets(update, context):
            await handlers.handle_start_with_assets(update, context, engine)

        @_require_authorized_chat
        async def cmd_drain_and_new(update, context):
            await handlers.handle_drain_and_new(update, context, engine)

        @_require_authorized_chat
        async def cmd_info(update, context):
            await handlers.handle_info(update, context, cache, db_path)

        @_require_authorized_chat
        async def cmd_close(update, context):
            await handlers.handle_close(update, context, engine, db_path)

        @_require_authorized_chat
        async def cmd_close_manually(update, context):
            await handlers.handle_close_manually(update, context, engine, db_path)

        @_require_authorized_chat
        async def cmd_pnl(update, context):
            await handlers.handle_pnl(update, context, cache, db_path)

        @_require_authorized_chat
        async def cmd_help(update, context):
            await handlers.handle_help(update, context)

        app.add_handler(CommandHandler("positions",         cmd_positions))
        app.add_handler(CommandHandler("closed_trades",     cmd_closed_trades))
        app.add_handler(CommandHandler("new_trades",        cmd_new_trades))
        app.add_handler(CommandHandler("status",            cmd_status))
        app.add_handler(CommandHandler("portfolio",         cmd_portfolio))
        app.add_handler(CommandHandler("stop_bot",          cmd_stop_bot))
        app.add_handler(CommandHandler("start_bot",         cmd_start_bot))
        app.add_handler(CommandHandler("start_drain",       cmd_start_drain))
        app.add_handler(CommandHandler("start_with_assets", cmd_start_with_assets))
        app.add_handler(CommandHandler("drain_and_new",     cmd_drain_and_new))
        app.add_handler(CommandHandler("info",              cmd_info))
        app.add_handler(CommandHandler("close",             cmd_close))
        app.add_handler(CommandHandler("close_manually",    cmd_close_manually))
        app.add_handler(CommandHandler("pnl",               cmd_pnl))
        app.add_handler(CommandHandler("help",              cmd_help))

        return app

    async def start(self) -> None:
        """Initialise the Application and start long-polling. Blocks until stop() is called."""
        if not config.TELEGRAM_TOKEN:
            logger.info("TELEGRAM_TOKEN not set — Telegram command listener disabled.")
            return

        logger.info("Telegram command listener starting…")
        self._app = self._build_app()

        await self._app.initialize()
        await self._app.start()

        # Register the command menu with Telegram so typing "/" shows suggestions.
        try:
            from telegram import BotCommand
            commands = [BotCommand(cmd, desc) for cmd, desc in COMMAND_REGISTRY]
            await self._app.bot.set_my_commands(commands)
            logger.info("Telegram command menu registered (%d commands).", len(commands))
        except Exception as exc:
            logger.warning("Failed to register Telegram command menu: %s", exc)

        self._stopped.clear()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram command listener active.")

        # Block until stop() signals shutdown.
        await self._stopped.wait()

    async def stop(self) -> None:
        """Cleanly shut down the polling loop."""
        if self._app is None:
            return
        try:
            if self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        except Exception as exc:
            logger.warning("Error stopping Telegram listener: %s", exc)
        finally:
            self._app = None
            self._stopped.set()
        logger.info("Telegram command listener stopped.")
