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
    get_all_closed_trades,
    get_open_instrument_names,
    get_visible_positions,
    get_close_status,
    get_open_trades,
    mark_position_close_stuck,
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

    def test_sets_close_status_closed(self, db):
        """Phase 21e: the normal auto-close path must set close_status='closed'
        (previously only mark_position_manually_closed did, so auto-closed trades
        wrongly kept close_status='open')."""
        trade = _open_trade(db)
        assert trade.close_status == "open"
        closed = close_calendar_trade(
            trade_id=trade.id,
            date_close=date(2026, 6, 7),
            spot_close=101_000.0,
            pnl=150.0,
            result="Win (Auto TP)",
            close_fees=5.0,
            db_path=db,
        )
        assert closed.close_status == "closed"

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


class TestGetAllClosedTrades:
    def test_empty_when_no_trades(self, db):
        trades = get_all_closed_trades(db_path=db)
        assert trades == []

    def test_returns_only_closed_trades(self, db):
        t1 = _open_trade(db)
        _open_trade(db)  # stays open
        close_calendar_trade(
            t1.id, date_close=date(2026, 6, 7),
            spot_close=101_000.0, pnl=100.0, result="Win", db_path=db,
        )
        trades = get_all_closed_trades(db_path=db)
        assert len(trades) == 1
        assert trades[0].id == t1.id

    def test_ordered_chronologically(self, db):
        # Create trades on different dates
        t1 = _open_trade(db)
        t2 = create_calendar_trade(
            asset="BTC", date_open=date(2026, 6, 5),
            option_type="Put", strike=100_000.0,
            expiry_near="2026-06-10", expiry_far="2026-07-05",
            near_days=5, far_days=30, qty=1.0, spot_open=99_000.0,
            near_prem=400.0, far_prem=700.0, net_debit=300.0,
            near_instrument="BTC-10JUN26-100000-P",
            far_instrument="BTC-5JUL26-100000-P", db_path=db,
        )

        # Close both but in reverse order
        close_calendar_trade(
            t1.id, date_close=date(2026, 6, 10),
            spot_close=101_000.0, pnl=100.0, result="Win", db_path=db,
        )
        close_calendar_trade(
            t2.id, date_close=date(2026, 6, 8),
            spot_close=99_500.0, pnl=50.0, result="Win", db_path=db,
        )

        trades = get_all_closed_trades(db_path=db)
        assert len(trades) == 2
        # Should be ordered by date_close, not by creation order
        assert trades[0].id == t2.id  # closed 2026-06-08
        assert trades[1].id == t1.id  # closed 2026-06-10

    def test_includes_pnl_and_fees(self, db):
        t = _open_trade(db)
        close_calendar_trade(
            t.id, date_close=date(2026, 6, 7),
            spot_close=101_000.0, pnl=100.5, result="Win",
            close_fees=2.0, db_path=db,
        )
        trades = get_all_closed_trades(db_path=db)
        assert len(trades) == 1
        assert trades[0].pnl == pytest.approx(100.5)
        assert trades[0].close_fees == pytest.approx(2.0)

    def test_multiple_closed_trades_mix_of_wins_losses(self, db):
        trades_created = []
        for i in range(5):
            t = _open_trade(db, strike=100_000.0 + i * 1000)
            trades_created.append(t)
            pnl = 100.0 if i % 2 == 0 else -50.0
            close_calendar_trade(
                t.id, date_close=date(2026, 6, 1 + i),
                spot_close=101_000.0, pnl=pnl, result="Win" if pnl > 0 else "Loss",
                db_path=db,
            )

        trades = get_all_closed_trades(db_path=db)
        assert len(trades) == 5
        # Check chronological order
        for i in range(len(trades) - 1):
            assert trades[i].date_close <= trades[i + 1].date_close


# ── get_open_instrument_names ────────────────────────────────────────────────

class TestGetOpenInstrumentNames:
    def test_returns_both_legs_of_open_positions(self, db):
        _open_trade(db)
        names = get_open_instrument_names(db_path=db)
        assert "BTC-7JUN26-100000-C" in names
        assert "BTC-4JUL26-100000-C" in names

    def test_excludes_closed_positions(self, db):
        t = _open_trade(db)
        close_calendar_trade(
            t.id, date_close=date(2026, 6, 7),
            spot_close=101_000.0, pnl=10.0, result="Win", db_path=db,
        )
        assert get_open_instrument_names(db_path=db) == []

    def test_includes_close_stuck_positions(self, db):
        # A stuck position is still open on the exchange and needs coverage.
        t = _open_trade(db)
        mark_position_close_stuck(t.id, error_reason="timeout", db_path=db)
        names = get_open_instrument_names(db_path=db)
        assert "BTC-7JUN26-100000-C" in names
        assert "BTC-4JUL26-100000-C" in names

    def test_deduplicates_and_sorts(self, db):
        _open_trade(db)
        _open_trade(db)  # same instruments, second open position
        names = get_open_instrument_names(db_path=db)
        assert names == sorted(set(names))
        assert len(names) == 2

    def test_skips_null_instruments(self, db):
        create_calendar_trade(
            asset="ETH",
            date_open=date(2026, 6, 1),
            option_type="Put",
            strike=1_400.0,
            expiry_near="2026-06-07",
            expiry_far="2026-07-04",
            near_days=7,
            far_days=30,
            qty=1.0,
            spot_open=1_500.0,
            near_prem=50.0,
            far_prem=80.0,
            net_debit=30.0,
            near_instrument=None,
            far_instrument="ETH-4JUL26-1400-P",
            db_path=db,
        )
        assert get_open_instrument_names(db_path=db) == ["ETH-4JUL26-1400-P"]

    def test_empty_db_returns_empty_list(self, db):
        assert get_open_instrument_names(db_path=db) == []


# ── Phase 22 — stuck-position visibility & monitor exclusion ──────────────────

class TestStuckPositionVisibility:
    def test_load_calendar_state_excludes_close_stuck(self, db):
        """22a: the monitor's read path must not re-surface a stuck position."""
        t = _open_trade(db)
        mark_position_close_stuck(t.id, error_reason="timeout", db_path=db)
        state = load_calendar_state("BTC", db_path=db)
        assert state["open_positions"] == []

    def test_load_calendar_state_includes_healthy_open(self, db):
        _open_trade(db)
        state = load_calendar_state("BTC", db_path=db)
        assert len(state["open_positions"]) == 1

    def test_get_visible_positions_includes_close_stuck(self, db):
        """22b: /positions and /portfolio must still SEE the stuck position."""
        t = _open_trade(db)
        mark_position_close_stuck(t.id, error_reason="timeout", db_path=db)
        visible = get_visible_positions(db_path=db)
        assert len(visible) == 1
        assert visible[0].close_status == "close_stuck"
        assert visible[0].close_error_reason == "timeout"

    def test_get_open_trades_still_excludes_stuck(self, db):
        """get_open_trades() must keep excluding stuck rows (22a depends on it)."""
        t = _open_trade(db)
        mark_position_close_stuck(t.id, error_reason="timeout", db_path=db)
        assert get_open_trades(db_path=db) == []
        # But the stuck-inclusive query still shows it.
        assert len(get_visible_positions(db_path=db)) == 1

    def test_get_visible_positions_excludes_closed(self, db):
        t = _open_trade(db)
        close_calendar_trade(
            t.id, date_close=date(2026, 6, 7),
            spot_close=101_000.0, pnl=10.0, result="Win", db_path=db,
        )
        assert get_visible_positions(db_path=db) == []


class TestGetCloseStatus:
    def test_open_position(self, db):
        t = _open_trade(db)
        assert get_close_status(t.id, db_path=db) == "open"

    def test_stuck_position(self, db):
        t = _open_trade(db)
        mark_position_close_stuck(t.id, error_reason="boom", db_path=db)
        assert get_close_status(t.id, db_path=db) == "close_stuck"

    def test_closed_position(self, db):
        t = _open_trade(db)
        close_calendar_trade(
            t.id, date_close=date(2026, 6, 7),
            spot_close=101_000.0, pnl=10.0, result="Win", db_path=db,
        )
        assert get_close_status(t.id, db_path=db) == "closed"

    def test_missing_trade_returns_none(self, db):
        assert get_close_status(9999, db_path=db) is None
