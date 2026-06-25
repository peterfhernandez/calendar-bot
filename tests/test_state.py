"""Tests for db/state.py — SQLite calendar trade persistence."""
import pytest
from datetime import date
from pathlib import Path

from db.state import (
    init_db,
    create_calendar_trade,
    close_calendar_trade,
    list_assets_with_open_positions,
    load_calendar_state,
    get_calendar_stats,
    update_near_leg,
)


@pytest.fixture
def db(tmp_path):
    """Temporary database path, initialised fresh for each test."""
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _open_trade(db, asset="BTC", strike=100_000.0, broker="deribit"):
    return create_calendar_trade(
        asset=asset,
        date_open=date(2026, 6, 1),
        option_type="Call",
        strike=strike,
        expiry_near="2026-06-07",
        expiry_far="2026-07-04",
        near_days=7,
        far_days=30,
        qty=1.0,
        spot_open=99_000.0,
        near_prem=500.0,
        far_prem=800.0,
        net_debit=300.0,
        broker=broker,
        near_instrument="BTC-7JUN26-100000-C",
        far_instrument="BTC-4JUL26-100000-C",
        open_fees=5.0,
        db_path=db,
    )


# ── create_calendar_trade ────────────────────────────────────────────────────

class TestCreateCalendarTrade:
    def test_returns_trade_with_id(self, db):
        trade = _open_trade(db)
        assert trade.id is not None
        assert trade.id > 0

    def test_result_is_open(self, db):
        trade = _open_trade(db)
        assert trade.result == "Open"

    def test_fields_round_trip(self, db):
        trade = _open_trade(db)
        assert trade.asset == "BTC"
        assert trade.strike == 100_000.0
        assert trade.net_debit == 300.0
        assert trade.open_fees == 5.0
        assert trade.broker == "deribit"
        assert trade.near_instrument == "BTC-7JUN26-100000-C"

    def test_close_fields_are_null(self, db):
        trade = _open_trade(db)
        assert trade.date_close is None
        assert trade.spot_close is None
        assert trade.pnl is None


# ── close_calendar_trade ─────────────────────────────────────────────────────

class TestCloseCalendarTrade:
    def test_updates_close_fields(self, db):
        trade = _open_trade(db)
        closed = close_calendar_trade(
            trade_id=trade.id,
            date_close=date(2026, 6, 7),
            spot_close=101_000.0,
            pnl=150.0,
            result="Win",
            close_fees=5.0,
            db_path=db,
        )
        assert closed.result == "Win"
        assert closed.pnl == 150.0
        assert closed.spot_close == 101_000.0
        assert closed.close_fees == 5.0

    def test_raises_for_unknown_id(self, db):
        with pytest.raises(ValueError, match="not found"):
            close_calendar_trade(
                trade_id=9999,
                date_close=date(2026, 6, 7),
                spot_close=0.0,
                pnl=0.0,
                result="Closed",
                db_path=db,
            )

    def test_notes_preserved_when_none_passed(self, db):
        trade = create_calendar_trade(
            asset="ETH", date_open=date(2026, 6, 1), option_type="Put",
            strike=3000.0, expiry_near="2026-06-07", expiry_far="2026-07-04",
            near_days=7, far_days=30, qty=1.0, spot_open=2990.0,
            near_prem=50.0, far_prem=80.0, net_debit=30.0,
            notes="initial note", db_path=db,
        )
        closed = close_calendar_trade(
            trade_id=trade.id, date_close=date(2026, 6, 7),
            spot_close=3010.0, pnl=20.0, result="Win", db_path=db,
        )
        assert closed.notes == "initial note"


# ── load_calendar_state ──────────────────────────────────────────────────────

class TestLoadCalendarState:
    def test_empty_db_returns_defaults(self, db):
        state = load_calendar_state("BTC", db_path=db)
        assert state == {
            "open_positions": [], "total_pnl": 0.0, "wins": 0,
            "losses": 0, "trades": 0, "broker": None,
        }

    def test_open_trade_shows_in_state(self, db):
        _open_trade(db)
        state = load_calendar_state("BTC", db_path=db)
        assert len(state["open_positions"]) == 1
        assert state["open_positions"][0]["asset"] == "BTC"
        assert state["trades"] == 0  # not yet closed

    def test_multiple_open_trades_all_returned(self, db):
        _open_trade(db, strike=100_000.0)
        _open_trade(db, strike=105_000.0)
        state = load_calendar_state("BTC", db_path=db)
        assert len(state["open_positions"]) == 2
        strikes = {p["strike"] for p in state["open_positions"]}
        assert strikes == {100_000.0, 105_000.0}

    def test_closed_trade_counted(self, db):
        trade = _open_trade(db)
        close_calendar_trade(
            trade_id=trade.id, date_close=date(2026, 6, 7),
            spot_close=101_000.0, pnl=150.0, result="Win", db_path=db,
        )
        state = load_calendar_state("BTC", db_path=db)
        assert state["open_positions"] == []
        assert state["trades"] == 1
        assert state["wins"] == 1
        assert state["losses"] == 0
        assert state["total_pnl"] == 150.0

    def test_loss_trade_counted(self, db):
        trade = _open_trade(db)
        close_calendar_trade(
            trade_id=trade.id, date_close=date(2026, 6, 7),
            spot_close=95_000.0, pnl=-200.0, result="Loss", db_path=db,
        )
        state = load_calendar_state("BTC", db_path=db)
        assert state["wins"] == 0
        assert state["losses"] == 1
        assert state["total_pnl"] == -200.0

    def test_isolation_by_asset(self, db):
        _open_trade(db, asset="BTC")
        _open_trade(db, asset="ETH", strike=3000.0)
        btc_state = load_calendar_state("BTC", db_path=db)
        eth_state = load_calendar_state("ETH", db_path=db)
        assert btc_state["open_positions"][0]["asset"] == "BTC"
        assert eth_state["open_positions"][0]["asset"] == "ETH"


# ── get_calendar_stats ───────────────────────────────────────────────────────

class TestGetCalendarStats:
    def test_empty_returns_zeros(self, db):
        stats = get_calendar_stats(db_path=db)
        assert stats["trades"] == 0
        assert stats["win_rate"] == 0.0

    def test_win_rate_calculation(self, db):
        for _ in range(3):
            t = _open_trade(db)
            close_calendar_trade(
                trade_id=t.id, date_close=date(2026, 6, 7),
                spot_close=101_000.0, pnl=100.0, result="Win", db_path=db,
            )
        t = _open_trade(db)
        close_calendar_trade(
            trade_id=t.id, date_close=date(2026, 6, 7),
            spot_close=95_000.0, pnl=-200.0, result="Loss", db_path=db,
        )
        stats = get_calendar_stats(db_path=db)
        assert stats["trades"] == 4
        assert stats["wins"] == 3
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(75.0)
        assert stats["total_pnl"] == pytest.approx(100.0)

    def test_filter_by_asset(self, db):
        t = _open_trade(db, asset="BTC")
        close_calendar_trade(
            trade_id=t.id, date_close=date(2026, 6, 7),
            spot_close=101_000.0, pnl=100.0, result="Win", db_path=db,
        )
        t2 = _open_trade(db, asset="ETH", strike=3000.0)
        close_calendar_trade(
            trade_id=t2.id, date_close=date(2026, 6, 7),
            spot_close=3100.0, pnl=50.0, result="Win", db_path=db,
        )
        btc_stats = get_calendar_stats(asset="BTC", db_path=db)
        assert btc_stats["trades"] == 1
        assert btc_stats["total_pnl"] == pytest.approx(100.0)

    def test_open_trades_excluded(self, db):
        _open_trade(db)  # stays open
        stats = get_calendar_stats(db_path=db)
        assert stats["trades"] == 0


# ── update_near_leg ───────────────────────────────────────────────────────────

class TestUpdateNearLeg:
    def test_updates_near_instrument_and_expiry(self, db):
        trade = _open_trade(db)
        updated = update_near_leg(
            trade.id,
            new_near_instrument="BTC-14JUN26-100000-C",
            new_expiry_near="14JUN26",
            db_path=db,
        )
        assert updated.near_instrument == "BTC-14JUN26-100000-C"
        assert updated.expiry_near == "14JUN26"

    def test_sets_result_to_near_leg_rolled(self, db):
        trade = _open_trade(db)
        updated = update_near_leg(
            trade.id,
            new_near_instrument="BTC-14JUN26-100000-C",
            new_expiry_near="14JUN26",
            db_path=db,
        )
        assert updated.result == "Near Leg Rolled"

    def test_far_leg_and_other_fields_unchanged(self, db):
        trade = _open_trade(db)
        updated = update_near_leg(
            trade.id,
            new_near_instrument="BTC-14JUN26-100000-C",
            new_expiry_near="14JUN26",
            db_path=db,
        )
        assert updated.far_instrument == trade.far_instrument
        assert updated.strike == trade.strike
        assert updated.net_debit == trade.net_debit

    def test_reflected_in_load_calendar_state(self, db):
        trade = _open_trade(db)
        update_near_leg(
            trade.id,
            new_near_instrument="BTC-14JUN26-100000-C",
            new_expiry_near="14JUN26",
            db_path=db,
        )
        state = load_calendar_state("BTC", db_path=db)
        assert len(state["open_positions"]) == 1
        pos = state["open_positions"][0]
        assert pos["near_instrument"] == "BTC-14JUN26-100000-C"
        assert pos["expiry_near"] == "14JUN26"

    def test_raises_on_unknown_trade_id(self, db):
        with pytest.raises(ValueError, match="not found"):
            update_near_leg(9999, "BTC-14JUN26-100000-C", "14JUN26", db_path=db)


class TestListAssetsWithOpenPositions:
    def test_empty_when_no_trades(self, db):
        assert list_assets_with_open_positions(db_path=db) == []

    def test_returns_asset_with_open_trade(self, db):
        _open_trade(db, asset="BTC")
        assert list_assets_with_open_positions(db_path=db) == ["BTC"]

    def test_excludes_closed_trades(self, db):
        trade = _open_trade(db, asset="BTC")
        close_calendar_trade(trade.id, date_close=date(2026, 6, 10), spot_close=100000.0,
                             pnl=50.0, result="win", close_fees=1.0, db_path=db)
        assert list_assets_with_open_positions(db_path=db) == []

    def test_multiple_assets_sorted(self, db):
        _open_trade(db, asset="ETH")
        _open_trade(db, asset="BTC")
        assert list_assets_with_open_positions(db_path=db) == ["BTC", "ETH"]

    def test_deduplicates_same_asset(self, db):
        _open_trade(db, asset="BTC", strike=100_000.0)
        _open_trade(db, asset="BTC", strike=105_000.0)
        assert list_assets_with_open_positions(db_path=db) == ["BTC"]
