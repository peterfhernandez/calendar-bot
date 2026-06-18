"""
bot.py
======
Entry point for the calendar spread bot.

Starts the Deribit WebSocket feed and the BotLoop scheduler together.
The feed and the loop run as concurrent asyncio tasks; the loop blocks
until SIGINT/SIGTERM, then both are shut down cleanly.

Usage
-----
    python bot.py              # paper trading (DERIBIT_PAPER = True in config)
    python bot.py --live       # live trading  (sets DERIBIT_PAPER = False)
    python bot.py --portfolio 50000
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


async def _run(portfolio_value: float, paper: bool) -> None:
    configure_logging()

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
        "Starting calendar bot  paper=%s  assets=%s  portfolio=%.2f",
        paper,
        config.ASSETS,
        portfolio_value,
    )

    # Run the feed and the scheduler loop concurrently
    feed_task = asyncio.create_task(feed.start(), name="feed")
    loop_task = asyncio.create_task(loop.run(),  name="loop")

    # Wait for the loop to finish (triggered by stop() or a signal).
    # Cancel the feed afterwards so we don't leave a dangling WS connection.
    try:
        await loop_task
    finally:
        await feed.stop()
        feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
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
    args = parser.parse_args()

    paper = not args.live
    asyncio.run(_run(portfolio_value=args.portfolio, paper=paper))


if __name__ == "__main__":
    main()
