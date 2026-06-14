"""Entry point for the calendar spread bot."""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger("bot")


async def scan_job():
    """Placeholder: run scanner and decision engine."""
    log.info("scan_job fired — scanner not yet implemented")


async def monitor_job():
    """Placeholder: check open positions for stop/TP."""
    log.info("monitor_job fired — monitor not yet implemented")


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_job, "interval", seconds=config.SCAN_INTERVAL_SEC, id="scan")
    scheduler.add_job(monitor_job, "interval", seconds=config.MONITOR_INTERVAL_SEC, id="monitor")
    scheduler.start()

    log.info(
        "Calendar bot started (paper=%s). Scan every %ds, monitor every %ds.",
        config.DERIBIT_PAPER,
        config.SCAN_INTERVAL_SEC,
        config.MONITOR_INTERVAL_SEC,
    )

    stop_event = asyncio.Event()

    def _shutdown(sig, frame):
        log.info("Shutdown signal received (%s), stopping…", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    await stop_event.wait()
    scheduler.shutdown(wait=False)
    log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
