"""
backtest/data_loader_db.py
==========================
Load historical option-chain frames directly from the DuckDB database
populated by :mod:`backtest.data_collector`.

The output is a ``list[Frame]`` — identical in shape to what
:mod:`backtest.loader` returns from CSV/JSON files — so
:class:`backtest.engine.BacktestEngine` works without modification.

Public API
----------
load_frames_from_db(db_path, asset, start, end, max_gap_minutes=30)
    Pull frames for one asset over a date range.

db_summary(db_path)
    Return row counts and date coverage (useful for quick validation).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from backtest.loader import Frame
from data.deribit_feed import TickerSnapshot

logger = logging.getLogger(__name__)


def load_frames_from_db(
    db_path: str | Path,
    asset: str,
    start: datetime,
    end: datetime,
    max_gap_minutes: int = 30,
) -> list[Frame]:
    """
    Load all option-chain frames for *asset* between *start* and *end*.

    Parameters
    ----------
    db_path:
        Path to the DuckDB database file.
    asset:
        Asset symbol, e.g. ``'BTC'`` or ``'ETH'``.
    start / end:
        Inclusive datetime bounds (timezone-aware or naive UTC).
    max_gap_minutes:
        Warn when consecutive frames are separated by more than this many
        minutes.  Does not raise; only logs a warning.

    Returns
    -------
    list[Frame]
        Chronological list of frames (each frame = list of TickerSnapshot),
        compatible with ``BacktestEngine.run()``.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"DuckDB database not found: {db_path}")

    # Normalise to UTC-aware for consistent comparison
    start = _ensure_utc(start)
    end   = _ensure_utc(end)

    # Strip tzinfo: DB stores naive UTC; passing tz-aware values causes a
    # local-tz shift that breaks the BETWEEN comparison.
    start_naive = start.replace(tzinfo=None)
    end_naive   = end.replace(tzinfo=None)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT
                epoch(ts)     AS timestamp,
                instrument,
                asset,
                spot,
                mark_price,
                mark_iv,
                bid,
                ask,
                open_interest
            FROM  option_chain
            WHERE asset = ?
              AND ts BETWEEN ? AND ?
            ORDER BY ts, instrument
            """,
            [asset.upper(), start_naive, end_naive],
        ).fetchall()
    finally:
        con.close()

    if not rows:
        logger.warning(
            "No rows found for asset=%s between %s and %s", asset, start, end
        )
        return []

    # Group by timestamp into frames
    buckets: dict[float, list[TickerSnapshot]] = {}
    for ts, instrument, a, spot, mark_price, mark_iv, bid, ask, oi in rows:
        snap = TickerSnapshot(
            instrument    = instrument,
            asset         = a,
            spot          = float(spot),
            mark_price    = float(mark_price),
            mark_iv       = float(mark_iv),
            bid           = float(bid),
            ask           = float(ask),
            open_interest = float(oi),
            timestamp     = float(ts),
        )
        buckets.setdefault(float(ts), []).append(snap)

    frames = [snaps for _, snaps in sorted(buckets.items())]

    # Gap detection
    sorted_ts = sorted(buckets.keys())
    gap_threshold_sec = max_gap_minutes * 60
    for i in range(1, len(sorted_ts)):
        gap = sorted_ts[i] - sorted_ts[i - 1]
        if gap > gap_threshold_sec:
            t0 = datetime.fromtimestamp(sorted_ts[i - 1], tz=timezone.utc)
            t1 = datetime.fromtimestamp(sorted_ts[i], tz=timezone.utc)
            logger.warning(
                "Data gap detected for %s: %.1f min between %s and %s",
                asset, gap / 60, t0, t1,
            )

    logger.info(
        "Loaded %d frames (%d rows) for %s [%s → %s]",
        len(frames),
        len(rows),
        asset,
        start.date(),
        end.date(),
    )
    return frames


def db_summary(db_path: str | Path) -> list[dict]:
    """
    Return a list of per-asset summary dicts from the database.

    Each dict has keys: ``asset``, ``total_rows``, ``total_snapshots``,
    ``earliest_ts``, ``latest_ts``, ``span_days``, ``distinct_instruments``.
    Returns an empty list if the database does not exist or has no data.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT
                asset,
                COUNT(*)                   AS total_rows,
                COUNT(DISTINCT ts)         AS total_snapshots,
                MIN(ts)                    AS earliest_ts,
                MAX(ts)                    AS latest_ts,
                ROUND(
                    (epoch(MAX(ts)) - epoch(MIN(ts))) / 86400.0, 2
                )                          AS span_days,
                COUNT(DISTINCT instrument) AS distinct_instruments
            FROM  option_chain
            GROUP BY asset
            ORDER BY asset
            """
        ).fetchall()
    finally:
        con.close()

    return [
        {
            "asset":                a,
            "total_rows":           total_rows,
            "total_snapshots":      total_snapshots,
            "earliest_ts":          earliest_ts,
            "latest_ts":            latest_ts,
            "span_days":            span_days,
            "distinct_instruments": distinct_instruments,
        }
        for a, total_rows, total_snapshots, earliest_ts, latest_ts, span_days, distinct_instruments
        in rows
    ]


# ── Internal helpers ─────────────────────────────────────────────────────────

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
