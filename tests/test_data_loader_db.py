"""
tests/test_data_loader_db.py
============================
Unit tests for backtest/data_loader_db.py.

All tests use an in-memory DuckDB populated with synthetic rows — no disk I/O,
no network calls.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from backtest.data_loader_db import load_frames_from_db, db_summary
from backtest.data_collector import _ensure_schema


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """Create a real DuckDB file in tmp_path with synthetic rows."""
    db_path = tmp_path / "test.duckdb"
    con = duckdb.connect(str(db_path))
    _ensure_schema(con)

    if rows:
        con.executemany(
            """
            INSERT INTO option_chain
                (ts, instrument, asset, spot, mark_price, mark_iv,
                 bid, ask, open_interest)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    con.commit()
    con.close()
    return db_path


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def _row(ts: datetime, instrument: str, asset: str = "BTC") -> tuple:
    # Strip tzinfo so DuckDB stores as naive UTC (avoids local-tz shift)
    ts_naive = ts.replace(tzinfo=None)
    return (
        ts_naive,
        instrument,
        asset,
        50_000.0,   # spot
        1.25,       # mark_price
        0.80,       # mark_iv
        1.20,       # bid
        1.30,       # ask
        500.0,      # open_interest
    )


# ── Frame grouping ───────────────────────────────────────────────────────────

class TestFrameGrouping:
    def test_groups_by_timestamp(self, tmp_path):
        t1 = _ts("2025-01-01T00:00:00")
        t2 = _ts("2025-01-01T00:05:00")
        rows = [
            _row(t1, "BTC-27JUN25-50000-C"),
            _row(t1, "BTC-27JUN25-50000-P"),
            _row(t2, "BTC-27JUN25-50000-C"),
        ]
        db = _make_db(tmp_path, rows)
        frames = load_frames_from_db(db, "BTC", _ts("2025-01-01"), _ts("2025-01-02"))
        assert len(frames) == 2
        assert len(frames[0]) == 2   # two instruments at t1
        assert len(frames[1]) == 1   # one instrument at t2

    def test_frames_are_chronological(self, tmp_path):
        t1 = _ts("2025-01-01T00:00:00")
        t2 = _ts("2025-01-01T01:00:00")
        t3 = _ts("2025-01-01T02:00:00")
        rows = [_row(t3, "X-C"), _row(t1, "X-C"), _row(t2, "X-C")]
        db = _make_db(tmp_path, rows)
        frames = load_frames_from_db(db, "BTC", _ts("2025-01-01"), _ts("2025-01-02"))
        timestamps = [f[0].timestamp for f in frames]
        assert timestamps == sorted(timestamps)

    def test_ticker_snapshot_fields(self, tmp_path):
        t1 = _ts("2025-03-15T12:00:00")
        db = _make_db(tmp_path, [_row(t1, "BTC-27JUN25-50000-C")])
        frames = load_frames_from_db(db, "BTC", _ts("2025-03-01"), _ts("2025-03-31"))
        snap = frames[0][0]
        assert snap.instrument == "BTC-27JUN25-50000-C"
        assert snap.asset == "BTC"
        assert snap.spot == pytest.approx(50_000.0)
        assert snap.mark_iv == pytest.approx(0.80)
        assert snap.bid == pytest.approx(1.20)
        assert snap.ask == pytest.approx(1.30)
        assert snap.open_interest == pytest.approx(500.0)


# ── Asset and date filtering ─────────────────────────────────────────────────

class TestFiltering:
    def test_filters_by_asset(self, tmp_path):
        t1 = _ts("2025-01-01T00:00:00")
        rows = [
            _row(t1, "BTC-27JUN25-50000-C", "BTC"),
            _row(t1, "ETH-27JUN25-2000-C",  "ETH"),
        ]
        db = _make_db(tmp_path, rows)
        frames = load_frames_from_db(db, "ETH", _ts("2025-01-01"), _ts("2025-01-02"))
        assert len(frames) == 1
        assert frames[0][0].asset == "ETH"

    def test_filters_by_date_range(self, tmp_path):
        in_range  = _ts("2025-06-15T00:00:00")
        out_range = _ts("2025-07-01T00:00:00")
        rows = [
            _row(in_range,  "BTC-C"),
            _row(out_range, "BTC-C"),
        ]
        db = _make_db(tmp_path, rows)
        frames = load_frames_from_db(
            db, "BTC",
            _ts("2025-06-01"), _ts("2025-06-30"),
        )
        assert len(frames) == 1
        assert frames[0][0].timestamp == pytest.approx(in_range.timestamp())

    def test_empty_result_returns_empty_list(self, tmp_path):
        db = _make_db(tmp_path, [])
        frames = load_frames_from_db(db, "BTC", _ts("2025-01-01"), _ts("2025-01-31"))
        assert frames == []


# ── Gap detection ────────────────────────────────────────────────────────────

class TestGapDetection:
    def test_warns_on_large_gap(self, tmp_path, caplog):
        t1 = _ts("2025-01-01T00:00:00")
        t2 = _ts("2025-01-01T02:00:00")   # 120-minute gap
        rows = [_row(t1, "BTC-C"), _row(t2, "BTC-C")]
        db = _make_db(tmp_path, rows)
        with caplog.at_level(logging.WARNING, logger="backtest.data_loader_db"):
            load_frames_from_db(db, "BTC", _ts("2025-01-01"), _ts("2025-01-02"),
                                max_gap_minutes=30)
        assert any("gap" in r.message.lower() for r in caplog.records)

    def test_no_warn_when_gap_within_threshold(self, tmp_path, caplog):
        t1 = _ts("2025-01-01T00:00:00")
        t2 = _ts("2025-01-01T00:10:00")   # 10-minute gap
        rows = [_row(t1, "BTC-C"), _row(t2, "BTC-C")]
        db = _make_db(tmp_path, rows)
        with caplog.at_level(logging.WARNING, logger="backtest.data_loader_db"):
            load_frames_from_db(db, "BTC", _ts("2025-01-01"), _ts("2025-01-02"),
                                max_gap_minutes=30)
        gap_warnings = [r for r in caplog.records if "gap" in r.message.lower()]
        assert len(gap_warnings) == 0


# ── db_summary ───────────────────────────────────────────────────────────────

class TestDbSummary:
    def test_returns_empty_for_missing_db(self, tmp_path):
        result = db_summary(tmp_path / "nonexistent.duckdb")
        assert result == []

    def test_summary_shape(self, tmp_path):
        t1 = _ts("2025-01-01T00:00:00")
        t2 = _ts("2025-01-01T00:05:00")
        rows = [
            _row(t1, "BTC-27JUN25-50000-C", "BTC"),
            _row(t2, "BTC-27JUN25-50000-C", "BTC"),
            _row(t1, "ETH-27JUN25-2000-C",  "ETH"),
        ]
        db = _make_db(tmp_path, rows)
        summary = db_summary(db)
        assert len(summary) == 2
        assets = {s["asset"] for s in summary}
        assert assets == {"BTC", "ETH"}

    def test_summary_counts(self, tmp_path):
        t1 = _ts("2025-02-01T00:00:00")
        t2 = _ts("2025-02-01T00:05:00")
        rows = [
            _row(t1, "BTC-A"),
            _row(t1, "BTC-B"),
            _row(t2, "BTC-A"),
        ]
        db = _make_db(tmp_path, rows)
        summary = db_summary(db)
        s = summary[0]
        assert s["total_rows"]      == 3
        assert s["total_snapshots"] == 2
        assert s["distinct_instruments"] == 2


# ── Missing DB ───────────────────────────────────────────────────────────────

class TestMissingDb:
    def test_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_frames_from_db(
                tmp_path / "missing.duckdb",
                "BTC",
                _ts("2025-01-01"),
                _ts("2025-01-31"),
            )
