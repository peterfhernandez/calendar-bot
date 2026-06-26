"""
scratch/scratch_telegram_cmd.py
================================
Demonstration script for the Telegram command listener.

Starts TelegramCommandListener with a real token and prints each received
command and its reply to stdout. Useful for verifying that the listener
connects to the Telegram API and that the security middleware is working.

Aborts if TRADING_MODE is "live".

Run with:
    python -m scratch.scratch_telegram_cmd
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Guard against live mode
import config

if config.TRADING_MODE == "live":
    sys.exit("scratch_telegram_cmd.py must not run in live mode. Aborting.")

if not config.TELEGRAM_TOKEN:
    sys.exit(
        "TELEGRAM_TOKEN is not set in .env — cannot start listener.\n"
        "Set TELEGRAM_TOKEN=<your-bot-token> and TELEGRAM_CHAT=<your-chat-id> in .env and retry."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)
logger = logging.getLogger("scratch_telegram_cmd")


def _make_mock_engine():
    """Create a lightweight fake engine for the demo (no real DB required)."""
    from data.chain_cache import ChainCache
    from strategy.decision import DecisionEngine

    cache = ChainCache(ttl=30)

    db_path = Path("db") / "scratch_telegram_cmd.db"
    db_path.parent.mkdir(exist_ok=True)

    engine = DecisionEngine(
        cache=cache,
        portfolio_value=10_000.0,
        db_path=db_path,
        daily_loss_limit=500.0,
    )
    return engine, cache


async def _run() -> None:
    engine, cache = _make_mock_engine()

    from telegram_cmd.listener import TelegramCommandListener

    listener = TelegramCommandListener(engine=engine, cache=cache)

    logger.info(
        "Starting Telegram command listener in %s mode (TELEGRAM_CHAT=%s)",
        config.TRADING_MODE.upper(),
        config.TELEGRAM_CHAT or "(not set)",
    )
    logger.info(
        "Commands available: /positions /closed_today /new_today /status "
        "/portfolio /stop_bot /start_bot /start_drain"
    )
    logger.info("Send commands from chat_id=%s. Press Ctrl+C to stop.", config.TELEGRAM_CHAT)

    try:
        await listener.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await listener.stop()
        logger.info("Listener stopped.")


if __name__ == "__main__":
    asyncio.run(_run())
