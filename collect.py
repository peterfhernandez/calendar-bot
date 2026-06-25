"""
collect.py
==========
Standalone entry point for the historical data collector.

Polls the Deribit public REST API on a fixed cadence and writes option-chain
snapshots to ``backtest/historic_data/options.duckdb``.

Usage
-----
Start continuous collection (default 5-minute cadence)::

    python collect.py

Collect a single snapshot and exit (useful for smoke-testing)::

    python collect.py --once

Override assets and interval::

    python collect.py --assets BTC ETH --interval 300

Write to a custom database path::

    python collect.py --db /path/to/my.duckdb

Options
-------
--once              Collect one snapshot then exit.
--assets ASSET …    Assets to collect (default: from config.ASSETS).
--interval SECS     Poll cadence in seconds (default: config.COLLECTOR_INTERVAL_SEC or 300).
--db PATH           DuckDB file path (default: backtest/historic_data/options.duckdb).
--log-level LEVEL   Logging level: DEBUG, INFO, WARNING, ERROR (default: INFO).
--log-file PATH     Write logs to this file in addition to stdout (rotating, 10 MB × 5).

Notes
-----
- The script reads ``DERIBIT_PAPER`` from ``config.py``; market data is
  identical on paper (test.deribit.com) and live (www.deribit.com) endpoints,
  so the flag only controls which hostname is used.
- ``COLLECTOR_ASSETS`` in ``config.py`` sets which assets are collected by
  default (independent of ``ASSETS``, which controls what the bot trades).
- SIGINT / SIGTERM trigger a clean shutdown after the current collection
  cycle finishes.
- A daily-row-count summary is logged every hour for easy health monitoring.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

import config
from backtest.data_collector import (
    DB_PATH,
    COLLECTOR_INTERVAL_SEC,
    COLLECTOR_ASSETS,
    _ensure_schema,
    collect_snapshot,
)


# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(level: str, log_file: str | None) -> None:
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root.addHandler(handler)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


logger = logging.getLogger("collect")


# ── Health summary ────────────────────────────────────────────────────────────

def _log_db_summary(db_path: Path) -> None:
    """Log a one-line row-count summary for each asset."""
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        rows = con.execute(
            """
            SELECT asset, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last
            FROM   option_chain
            GROUP  BY asset ORDER BY asset
            """
        ).fetchall()
        con.close()
        if not rows:
            logger.info("[summary] DB is empty — no snapshots collected yet")
            return
        for asset, n, first, last in rows:
            logger.info("[summary] %s: %d rows  first=%s  last=%s", asset, n, first, last)
    except Exception as exc:
        logger.warning("[summary] Could not read DB summary: %s", exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

_shutdown = asyncio.Event()


def _handle_signal(sig, _frame):
    logger.info("Received signal %s — shutting down after current cycle", sig)
    # asyncio.Event.set() is not signal-safe in all Python versions;
    # set a module-level flag and let the loop check it.
    global _stop_requested
    _stop_requested = True


_stop_requested = False


async def _run_loop(
    assets: list[str],
    interval_sec: int,
    db_path: Path,
) -> None:
    import aiohttp

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    _ensure_schema(con)

    logger.info(
        "Collector started — assets=%s  interval=%ds  db=%s  mode=%s",
        assets, interval_sec, db_path, config.TRADING_MODE,
    )
    _log_db_summary(db_path)

    last_summary_time = time.monotonic()
    SUMMARY_INTERVAL = 3600  # log a summary every hour

    async with aiohttp.ClientSession() as session:
        while not _stop_requested:
            t0 = time.monotonic()
            try:
                totals = await collect_snapshot(session, con, assets)
                logger.info("Snapshot done: %s", totals)
            except Exception as exc:
                logger.error("Unexpected error in collection loop: %s", exc, exc_info=True)

            # Periodic health summary
            if time.monotonic() - last_summary_time >= SUMMARY_INTERVAL:
                _log_db_summary(db_path)
                last_summary_time = time.monotonic()

            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, interval_sec - elapsed)

            # Sleep in short chunks so SIGINT wakes us promptly
            slept = 0.0
            while slept < sleep_for and not _stop_requested:
                chunk = min(1.0, sleep_for - slept)
                await asyncio.sleep(chunk)
                slept += chunk

    con.close()
    logger.info("Collector stopped cleanly.")
    _log_db_summary(db_path)


async def _run_once(assets: list[str], db_path: Path) -> None:
    import aiohttp

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    _ensure_schema(con)

    logger.info(
        "Single collection — assets=%s  db=%s  mode=%s",
        assets, db_path, config.TRADING_MODE,
    )

    async with aiohttp.ClientSession() as session:
        totals = await collect_snapshot(session, con, assets)

    con.close()
    logger.info("Done: %s", totals)
    _log_db_summary(db_path)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect Deribit option-chain snapshots into DuckDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Collect one snapshot then exit (smoke-test mode).",
    )
    p.add_argument(
        "--assets",
        nargs="+",
        metavar="ASSET",
        default=None,
        help=f"Assets to collect (default: config.COLLECTOR_ASSETS = {COLLECTOR_ASSETS}).",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="SECS",
        help=f"Poll cadence in seconds (default: {COLLECTOR_INTERVAL_SEC}).",
    )
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help=f"DuckDB file path (default: {DB_PATH}).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log level (default: INFO).",
    )
    p.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Optional rotating log file (10 MB × 5 backups).",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_level, args.log_file)

    assets      = [a.upper() for a in args.assets] if args.assets else COLLECTOR_ASSETS
    interval    = args.interval if args.interval is not None else COLLECTOR_INTERVAL_SEC
    db_path     = Path(args.db) if args.db else DB_PATH

    # Register signal handlers for graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    if args.once:
        asyncio.run(_run_once(assets, db_path))
    else:
        asyncio.run(_run_loop(assets, interval, db_path))


if __name__ == "__main__":
    main()
