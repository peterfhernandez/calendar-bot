"""
backtest/loader.py
==================
Load historical Deribit option chain snapshots from CSV or JSON files and
normalise them to the same TickerSnapshot schema used by ChainCache.

Snapshot files group rows by timestamp; each unique timestamp becomes one
"frame" — a market snapshot at a single point in time.  The engine replays
frames in chronological order.

CSV format (one row per instrument per snapshot)
------------------------------------------------
timestamp,instrument,asset,spot,mark_price,mark_iv,bid,ask,open_interest

JSON format
-----------
List of objects with the same field names (timestamp, instrument, asset,
spot, mark_price, mark_iv, bid, ask, open_interest).  Alternatively, a
dict with a top-level "snapshots" key containing such a list.

Public API
----------
from_records(records)   Convert a list of dicts to chronological frames.
load_csv(path)          Load a CSV file and return frames.
load_json(path)         Load a JSON file and return frames.
load(path)              Auto-detect format from file extension and load.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from data.deribit_feed import TickerSnapshot

logger = logging.getLogger(__name__)

# Each frame = all snapshots sharing one timestamp
Frame = list[TickerSnapshot]


def from_records(records: list[dict]) -> list[Frame]:
    """
    Convert a list of flat dicts into chronological frames.

    Each dict must contain: timestamp, instrument, asset, spot, mark_price,
    mark_iv, bid, ask, open_interest.  Rows sharing the same timestamp are
    grouped into one frame.  Malformed rows are skipped with a warning.
    """
    buckets: dict[float, list[TickerSnapshot]] = {}
    skipped = 0

    for row in records:
        try:
            ts = float(row["timestamp"])
            snap = TickerSnapshot(
                instrument    = str(row["instrument"]),
                asset         = str(row["asset"]).upper(),
                spot          = float(row["spot"]),
                mark_price    = float(row["mark_price"]),
                mark_iv       = float(row["mark_iv"]),
                bid           = float(row["bid"]),
                ask           = float(row["ask"]),
                open_interest = float(row["open_interest"]),
                timestamp     = ts,
            )
            buckets.setdefault(ts, []).append(snap)
        except (KeyError, ValueError, TypeError) as exc:
            skipped += 1
            logger.debug("Skipping malformed row: %s — %s", row, exc)

    if skipped:
        logger.warning("Skipped %d malformed rows during load", skipped)

    frames = [snaps for _, snaps in sorted(buckets.items())]
    logger.info(
        "Loaded %d frames (%d instruments total)",
        len(frames), sum(len(f) for f in frames),
    )
    return frames


def load_csv(path: str | Path) -> list[Frame]:
    """
    Load a CSV file and return a list of chronological frames.

    The file must have a header row matching the fields in the module
    docstring.  Extra columns are ignored.
    """
    path = Path(path)
    records: list[dict] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            records.append(dict(row))
    logger.info("Read %d rows from %s", len(records), path)
    return from_records(records)


def load_json(path: str | Path) -> list[Frame]:
    """
    Load a JSON file and return a list of chronological frames.

    Accepts either a bare list of snapshot dicts or a dict with a
    top-level ``"snapshots"`` key.
    """
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, dict):
        records = data.get("snapshots", [])
    elif isinstance(data, list):
        records = data
    else:
        raise ValueError(f"Unexpected JSON structure in {path}: expected list or dict")

    logger.info("Read %d records from %s", len(records), path)
    return from_records(records)


def load(path: str | Path) -> list[Frame]:
    """
    Auto-detect format from file extension (.csv or .json) and load.

    Returns
    -------
    list[Frame]
        Chronological list of market frames, each a list of TickerSnapshots.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv(path)
    if suffix in (".json", ".jsonl"):
        return load_json(path)
    raise ValueError(f"Unsupported file format '{suffix}' — expected .csv or .json")
