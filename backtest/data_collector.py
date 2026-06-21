"""
backtest/data_collector.py
==========================
Standalone collector that polls the Deribit public REST API every N minutes
and writes a full option-chain snapshot to the DuckDB historical database.

Usage
-----
Run directly::

    python -m backtest.data_collector

Or import and call :func:`run_once` / :func:`run_loop` from another script.

The script writes only to ``backtest/historic_data/options.duckdb`` and never
places orders.  It works against both the paper (``test.deribit.com``) and
live (``www.deribit.com``) REST endpoints — market data is identical on both.

Environment / config
--------------------
- ``DERIBIT_PAPER`` (config.py) controls which REST hostname is used.
- ``COLLECTOR_INTERVAL_SEC`` (config.py, default 300) sets the poll cadence.
- ``COLLECTOR_ASSETS`` (config.py, default same as ``ASSETS``) restricts which
  assets are collected.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import asyncio
import duckdb

import config

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_PAPER_HOST = "test.deribit.com"
_LIVE_HOST  = "www.deribit.com"

DB_PATH = Path(__file__).parent / "historic_data" / "options.duckdb"
SCHEMA_PATH = Path(__file__).parent / "sql" / "schema.sql"

COLLECTOR_INTERVAL_SEC: int = getattr(config, "COLLECTOR_INTERVAL_SEC", 300)
COLLECTOR_ASSETS: list[str] = getattr(config, "COLLECTOR_ASSETS", config.ASSETS)


# ── Database helpers ─────────────────────────────────────────────────────────

def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create tables and indexes if they don't exist yet."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # DuckDB does not have executescript(); split on ";" and run each statement
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)


def _open_db() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    _ensure_schema(con)
    return con


# ── REST API helpers ─────────────────────────────────────────────────────────

def _rest_base() -> str:
    host = _PAPER_HOST if config.DERIBIT_PAPER else _LIVE_HOST
    return f"https://{host}/api/v2/public"


async def _get(session: aiohttp.ClientSession, endpoint: str, **params) -> dict:
    url = f"{_rest_base()}/{endpoint}"
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json()
    result = data.get("result")
    if result is None:
        raise ValueError(f"No 'result' in response from {endpoint}: {data}")
    return result


async def _fetch_spot(session: aiohttp.ClientSession, asset: str) -> float:
    """Return the current index price for *asset* (e.g. 'BTC')."""
    result = await _get(session, "get_index_price", index_name=f"{asset.lower()}_usd")
    return float(result["index_price"])


async def _fetch_chain(
    session: aiohttp.ClientSession,
    asset: str,
    spot: float,
) -> list[dict]:
    """
    Return a list of normalised option_chain rows for *asset*.

    Each row is a dict with keys matching the DuckDB table columns
    (excluding ``id``).
    """
    result = await _get(
        session,
        "get_book_summary_by_currency",
        currency=asset,
        kind="option",
    )

    # Store as naive UTC — DuckDB TIMESTAMP has no TZ; inserting a tz-aware
    # datetime causes it to be shifted to local time before storage.
    ts = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    rows: list[dict] = []

    for item in result:
        instrument: str = item.get("instrument_name", "")
        if not instrument:
            continue

        mark_price    = float(item.get("mark_price") or 0.0)
        mark_iv_raw   = item.get("mark_iv")
        mark_iv       = float(mark_iv_raw) / 100.0 if mark_iv_raw is not None else 0.0
        bid           = float(item.get("bid_price") or 0.0)
        ask           = float(item.get("ask_price") or 0.0)
        open_interest = float(item.get("open_interest") or 0.0)

        rows.append({
            "ts":            ts,
            "instrument":    instrument,
            "asset":         asset.upper(),
            "spot":          spot,
            "mark_price":    mark_price,
            "mark_iv":       mark_iv,
            "bid":           bid,
            "ask":           ask,
            "open_interest": open_interest,
        })

    return rows


# ── Collection logic ─────────────────────────────────────────────────────────

async def collect_snapshot(
    session: aiohttp.ClientSession,
    con: duckdb.DuckDBPyConnection,
    assets: list[str],
) -> dict[str, int]:
    """
    Collect one snapshot for each asset and persist to DuckDB.

    Returns a mapping of ``asset -> rows_added``.
    """
    results: dict[str, int] = {}

    for asset in assets:
        started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        run_id: Optional[int] = None
        try:
            spot = await _fetch_spot(session, asset)
            rows = await _fetch_chain(session, asset, spot)

            if not rows:
                logger.warning("[%s] Chain fetch returned 0 rows", asset)
                results[asset] = 0
                continue

            # Insert all rows in a single transaction
            con.begin()
            try:
                con.executemany(
                    """
                    INSERT INTO option_chain
                        (ts, instrument, asset, spot, mark_price, mark_iv,
                         bid, ask, open_interest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["ts"], r["instrument"], r["asset"], r["spot"],
                            r["mark_price"], r["mark_iv"], r["bid"],
                            r["ask"], r["open_interest"],
                        )
                        for r in rows
                    ],
                )
                run_id = _log_run(con, started_at, asset, len(rows), "ok")
                con.commit()
            except Exception:
                con.rollback()
                raise

            results[asset] = len(rows)
            logger.info("[%s] Collected %d rows (spot=%.2f)", asset, len(rows), spot)

        except Exception as exc:
            logger.error("[%s] Collection failed: %s", asset, exc, exc_info=True)
            try:
                _log_run(con, started_at, asset, 0, "error")
                con.commit()
            except Exception:
                pass
            results[asset] = 0

    return results


def _log_run(
    con: duckdb.DuckDBPyConnection,
    started_at: datetime,
    asset: str,
    rows_added: int,
    status: str,
) -> int:
    ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    con.execute(
        """
        INSERT INTO collection_runs (id, started_at, ended_at, asset, rows_added, status)
        VALUES (nextval('collection_runs_id_seq'), ?, ?, ?, ?, ?)
        """,
        [started_at, ended_at, asset, rows_added, status],
    )
    result = con.execute("SELECT currval('collection_runs_id_seq')").fetchone()
    return result[0] if result else -1


async def run_once(
    assets: Optional[list[str]] = None,
    db_path: Optional[Path] = None,
) -> dict[str, int]:
    """Collect one snapshot for all assets and return rows-added counts."""
    _assets = assets or COLLECTOR_ASSETS
    _db_path = Path(db_path) if db_path else DB_PATH
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(_db_path))
    _ensure_schema(con)
    try:
        async with aiohttp.ClientSession() as session:
            return await collect_snapshot(session, con, _assets)
    finally:
        con.close()


async def run_loop(
    assets: Optional[list[str]] = None,
    interval_sec: int = COLLECTOR_INTERVAL_SEC,
    db_path: Optional[Path] = None,
) -> None:
    """
    Poll Deribit indefinitely, collecting a snapshot every *interval_sec* seconds.

    Designed to run as a long-lived background process.  Errors in a single
    collection cycle are logged and skipped; the loop never crashes.
    """
    _assets = assets or COLLECTOR_ASSETS
    _db_path = db_path or DB_PATH
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(_db_path))
    _ensure_schema(con)

    logger.info(
        "Collector starting — assets=%s, interval=%ds, db=%s",
        _assets, interval_sec, _db_path,
    )

    async with aiohttp.ClientSession() as session:
        while True:
            t0 = time.monotonic()
            try:
                totals = await collect_snapshot(session, con, _assets)
                logger.info("Snapshot complete: %s", totals)
            except Exception as exc:
                logger.error("Unexpected error in collection loop: %s", exc, exc_info=True)

            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, interval_sec - elapsed)
            logger.debug("Sleeping %.1f s until next collection", sleep_for)
            await asyncio.sleep(sleep_for)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run_loop())
