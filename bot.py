"""
bot.py
======
Entry point for the calendar spread bot.

Starts the Deribit WebSocket feed and the BotLoop scheduler together.
The feed and the loop run as concurrent asyncio tasks; the loop blocks
until SIGINT/SIGTERM, then both are shut down cleanly.

Usage
-----
    python bot.py                        # paper trading (DERIBIT_PAPER = True in config)
    python bot.py --live                 # live trading  (sets DERIBIT_PAPER = False)
    python bot.py --portfolio 50000
    python bot.py --collect              # also run the data collector alongside the bot
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import config
from data.chain_cache import ChainCache
from data.deribit_feed import DeribitFeed
from monitor.loop import BotLoop, configure_logging

logger = logging.getLogger("bot")


async def _run(portfolio_value: float, paper: bool, collect: bool) -> None:
    configure_logging()
    logging.getLogger("strategy.decision").setLevel(logging.DEBUG)
    logging.getLogger("strategy.sizer").setLevel(logging.DEBUG)

    cache = ChainCache()

    feed = DeribitFeed(
        assets=config.ASSETS,
        paper=paper,
        client_id=config.DERIBIT_CLIENT_ID,
        client_secret=config.DERIBIT_CLIENT_SECRET,
        on_ticker=cache.update,
    )

    loop = BotLoop(
        cache=cache,
        portfolio_value=portfolio_value,
    )

    logger.info(
        "Starting calendar bot  paper=%s  assets=%s  portfolio=%.2f  collect=%s",
        paper,
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
    parser = argparse.ArgumentParser(description="Calendar Spread Bot")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live Deribit endpoint (default: paper trading)",
    )
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

    paper = not args.live
    asyncio.run(_run(portfolio_value=args.portfolio, paper=paper, collect=args.collect))


if __name__ == "__main__":
    main()
