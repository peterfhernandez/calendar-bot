"""
bot.py
======
Entry point for the calendar spread bot.

Starts the Deribit WebSocket feed and the BotLoop scheduler together.
The feed and the loop run as concurrent asyncio tasks; the loop blocks
until SIGINT/SIGTERM, then both are shut down cleanly.

Usage
-----
    python bot.py                        # uses TRADING_MODE from config.py (default: paper)
    python bot.py --portfolio 50000
    python bot.py --collect              # also run the data collector alongside the bot
    python bot.py --drain                # start in drain mode (no new entries or rolls)
    python bot.py --env .env.test        # run test mode alongside paper using a separate env file
    python bot.py --env .env.test --db calendar_bot_test.db --log logs/bot_test.log
"""

from __future__ import annotations

# ── Pre-parse --env / --db / --log before importing config or db.state ─────────
# config.py and db/state.py execute module-level code that reads env vars
# (TRADING_MODE, BOT_DB_PATH, etc.) at import time.  We must set those vars
# before the first import so the right values are captured.
import os as _os
import sys as _sys

def _preparse_argv() -> None:
    """Extract --env / --db / --log from sys.argv and set env vars immediately."""
    _flags = {
        "--env":    "BOT_ENV_FILE",    # which .env file to load
        "--db":     "BOT_DB_PATH",     # SQLite database path override
        "--log":    "BOT_LOG_FILE",    # log file path override
        "--config": "BOT_CONFIG_FILE", # strategy config override file
    }
    _argv = _sys.argv[1:]
    _i = 0
    while _i < len(_argv):
        for _flag, _var in _flags.items():
            if _argv[_i] == _flag and _i + 1 < len(_argv):
                _os.environ[_var] = _argv[_i + 1]
                break
            elif _argv[_i].startswith(_flag + "="):
                _os.environ[_var] = _argv[_i].split("=", 1)[1]
                break
        _i += 1

_preparse_argv()
del _preparse_argv  # keep module namespace tidy
# ── End pre-parse ──────────────────────────────────────────────────────────────

import argparse
import asyncio
import logging

import config
from alerts.notifier import Notifier
from data.chain_cache import ChainCache
from data.deribit_feed import DeribitFeed
from db.state import list_assets_with_open_positions
from execution.executor import CalendarExecutor
from monitor.loop import BotLoop, configure_logging

logger = logging.getLogger("bot")


_BANNERS = {
    "paper": "*** PAPER MODE — data from test.deribit.com, no orders placed ***",
    "test":  "*** TEST MODE — orders will be placed on test.deribit.com ***",
    "live":  "*** LIVE MODE — REAL MONEY on www.deribit.com ***",
}


def _check_startup() -> None:
    """Validate config before starting; print the mode banner."""
    mode = config.TRADING_MODE
    if mode not in ("paper", "test", "live"):
        raise SystemExit(f"Invalid TRADING_MODE={mode!r}. Must be 'paper', 'test', or 'live'.")
    if mode == "live" and not (config.DAILY_LOSS_LIMIT and config.DAILY_LOSS_LIMIT > 0):
        raise SystemExit(
            "TRADING_MODE='live' requires DAILY_LOSS_LIMIT to be set to a positive value in config.py."
        )
    print(_BANNERS.get(mode, f"*** MODE: {mode} ***"), flush=True)
    if config.DRAIN_MODE:
        print("*** DRAIN MODE — no new entries, no rolls; existing positions close at stop/TP/expiry ***", flush=True)




async def _cancel_open_orders(client_id: str, client_secret: str) -> None:
    """Cancel any open option orders left from prior sessions on startup."""
    from execution.executor import _DeribitRPCClient
    try:
        async with _DeribitRPCClient(client_id, client_secret) as client:
            for asset in config.ASSETS:
                try:
                    orders = await client._rpc(
                        "private/cancel_all_by_currency",
                        {"currency": asset, "kind": "option"},
                    )
                    if orders:
                        logger.info("Cancelled %s open %s option order(s) from prior session", orders, asset)
                except Exception as exc:
                    logger.warning("Could not cancel open %s orders on startup: %s", asset, exc)
    except Exception as exc:
        logger.warning("Startup order cleanup failed (non-fatal): %s", exc)


async def _run(portfolio_value: float, collect: bool, drain: bool) -> None:
    if drain:
        config.DRAIN_MODE = True
    configure_logging()
    logging.getLogger("strategy.decision").setLevel(logging.DEBUG)
    logging.getLogger("strategy.sizer").setLevel(logging.DEBUG)

    notifier = Notifier()
    try:
        notifier.notify_startup(config.TRADING_MODE, config.ASSETS, config.DERIBIT_REST_URL)
    except Exception as exc:
        logger.warning("Startup notification failed (non-fatal): %s", exc)

    # Verify Telegram is configured and working; log prominent warning if not
    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT:
        logger.info("Telegram notifications enabled for chat %s", config.TELEGRAM_CHAT)
    else:
        logger.warning(
            "⚠️  Telegram notifications DISABLED: TELEGRAM_TOKEN or TELEGRAM_CHAT not configured. "
            "Position entries/closes will NOT send notifications. Check your .env file."
        )

    cache = ChainCache(ttl=config.CHAIN_CACHE_TTL_SEC)

    # Include any asset with open positions so the feed subscribes to its
    # tickers even if it has since been removed from config.ASSETS.
    open_assets = list_assets_with_open_positions()
    feed_assets = sorted(set(config.ASSETS) | set(open_assets))
    if extra := sorted(set(open_assets) - set(config.ASSETS)):
        logger.info("Feed expanded to cover assets with open positions: %s", extra)

    feed = DeribitFeed(
        assets=feed_assets,
        paper=config.DERIBIT_PAPER,
        client_id=config.DERIBIT_CLIENT_ID,
        client_secret=config.DERIBIT_CLIENT_SECRET,
        on_ticker=cache.update,
    )

    executor = None
    if config.TRADING_MODE != "paper":
        executor = CalendarExecutor(
            client_id=config.DERIBIT_CLIENT_ID,
            client_secret=config.DERIBIT_CLIENT_SECRET,
            portfolio_value=portfolio_value,
        )
        await _cancel_open_orders(config.DERIBIT_CLIENT_ID, config.DERIBIT_CLIENT_SECRET)

    loop = BotLoop(
        cache=cache,
        portfolio_value=portfolio_value,
        executor=executor,
        notifier=notifier,
    )

    logger.info(
        "Starting calendar bot  mode=%s  trade_assets=%s  feed_assets=%s  portfolio=%.2f  collect=%s",
        config.TRADING_MODE,
        config.ASSETS,
        feed_assets,
        portfolio_value,
        collect,
    )

    listener = None
    if config.TELEGRAM_TOKEN:
        from telegram_cmd.listener import TelegramCommandListener
        listener = TelegramCommandListener(
            engine=loop.engine,
            cache=cache,
            db_path=loop.engine._db_path,
        )
    else:
        logger.info("TELEGRAM_TOKEN not set — Telegram command listener disabled.")

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(feed.start(), name="feed"))
    tasks.append(asyncio.create_task(loop.run(),  name="loop"))

    if listener is not None:
        tasks.append(asyncio.create_task(listener.start(), name="telegram_cmd"))

    if collect:
        from backtest.data_collector import run_loop as collector_loop
        tasks.append(asyncio.create_task(collector_loop(), name="collector"))
        logger.info("Data collector running alongside bot.")

    # Wait for the trading loop to finish (triggered by stop() or a signal),
    # then tear everything else down.
    loop_task = next(t for t in tasks if t.get_name() == "loop")
    feed_task = next(t for t in tasks if t.get_name() == "feed")

    try:
        await loop_task
    except Exception as exc:
        logger.exception("Bot loop raised an unexpected error")
        try:
            notifier.notify_error("bot", exc)
        except Exception:
            pass
        raise
    finally:
        if listener is not None:
            await listener.stop()
        await feed.stop()
        for t in tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    logger.info("Bot exited cleanly.")


def main() -> None:
    _check_startup()

    parser = argparse.ArgumentParser(description="Calendar Spread Bot")
    parser.add_argument(
        "--portfolio",
        type=float,
        default=10_000.0,
        help="Portfolio value in USD used for position sizing (default: 10000)",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        default=False,
        help="Also run the data collector to build historical data (default: off)",
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        default=False,
        help="Start in drain mode: no new entries or rolls; existing positions close normally",
    )
    parser.add_argument(
        "--env",
        metavar="FILE",
        default=".env",
        help=(
            "Path to the .env credentials file (default: .env). "
            "Use a separate file (e.g. .env.test) to run a test-mode instance "
            "alongside the paper-mode bot without sharing credentials or state."
        ),
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default="",
        help=(
            "SQLite database path (default: db/calendar_bot.db). "
            "Override to keep test-mode trades in a dedicated database, e.g. "
            "--db calendar_bot_test.db."
        ),
    )
    parser.add_argument(
        "--log",
        metavar="PATH",
        default="",
        help=(
            "Log file path (default: logs/bot.log). "
            "Override to write a separate log per instance, e.g. "
            "--log logs/bot_test.log."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        default="",
        help=(
            "Path to a Python config override file (default: none). "
            "Variables assigned in this file overwrite their counterparts in "
            "config.py — use it to tune strategy parameters (ASSETS, "
            "MAX_POSITIONS, MAX_LOSS_PCT, etc.) per instance without forking "
            "the main config."
        ),
    )
    args = parser.parse_args()

    asyncio.run(_run(portfolio_value=args.portfolio, collect=args.collect, drain=args.drain))


if __name__ == "__main__":
    main()
