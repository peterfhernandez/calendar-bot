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
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import config
from alerts.notifier import Notifier
from data.chain_cache import ChainCache
from data.deribit_feed import DeribitFeed
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




async def _run(portfolio_value: float, collect: bool) -> None:
    configure_logging()
    logging.getLogger("strategy.decision").setLevel(logging.DEBUG)
    logging.getLogger("strategy.sizer").setLevel(logging.DEBUG)

    notifier = Notifier()
    try:
        notifier.notify_startup(config.TRADING_MODE, config.ASSETS, config.TRADING_MODE)
    except Exception as exc:
        logger.warning("Startup notification failed (non-fatal): %s", exc)

    cache = ChainCache()

    feed = DeribitFeed(
        assets=config.ASSETS,
        paper=config.DERIBIT_PAPER,
        client_id=config.DERIBIT_CLIENT_ID,
        client_secret=config.DERIBIT_CLIENT_SECRET,
        on_ticker=cache.update,
    )

    loop = BotLoop(
        cache=cache,
        portfolio_value=portfolio_value,
        notifier=notifier,
    )

    logger.info(
        "Starting calendar bot  mode=%s  assets=%s  portfolio=%.2f  collect=%s",
        config.TRADING_MODE,
        config.ASSETS,
        portfolio_value,
        collect,
    )

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(feed.start(), name="feed"))
    tasks.append(asyncio.create_task(loop.run(),  name="loop"))

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
    args = parser.parse_args()

    asyncio.run(_run(portfolio_value=args.portfolio, collect=args.collect))


if __name__ == "__main__":
    main()
