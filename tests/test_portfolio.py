"""
Unit tests for portfolio/tracker.py — PortfolioTracker.

All tests use an in-memory SQLite database and mock Deribit REST responses
to avoid any live network calls.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from db.state import init_db, get_connection, create_calendar_trade
from portfolio.tracker import (
    PortfolioTracker,
    PortfolioState,
    _assets_to_currencies,
    _rest_get,
    _rest_post,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_db() -> Path:
    """Create a fresh in-memory-style temp DB and return its path."""
    tmp = tempfile.mktemp(suffix=".db")
    path = Path(tmp)
    init_db(path)
    return path


def _seed_open_trade(db: Path, net_debit: float = 10.0, qty: float = 2.0) -> None:
    """Insert one open calendar trade into the test DB."""
    create_calendar_trade(
        asset="BTC",
        date_open=date.today(),
        option_type="Call",
        strike=90_000.0,
        expiry_near="27JUN25",
        expiry_far="25JUL25",
        near_days=3,
        far_days=31,
        qty=qty,
        spot_open=90_000.0,
        near_prem=5.0,
        far_prem=15.0,
        net_debit=net_debit,
        db_path=db,
    )


def _seed_closed_trade(db: Path, pnl: float = 50.0, date_close: str | None = None) -> None:
    """Insert a closed trade with the given P&L and close date (default=today)."""
    trade = create_calendar_trade(
        asset="ETH",
        date_open=date.today(),
        option_type="Call",
        strike=3_000.0,
        expiry_near="10JUN25",
        expiry_far="08JUL25",
        near_days=5,
        far_days=33,
        qty=1.0,
        spot_open=3_000.0,
        near_prem=2.0,
        far_prem=8.0,
        net_debit=6.0,
        db_path=db,
    )
    close_date = date_close or date.today().isoformat()
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE calendar_trades SET result='Closed', date_close=?, pnl=? WHERE id=?",
            (close_date, pnl, trade.id),
        )


def _make_tracker(db: Path, client_id: str = "", client_secret: str = "") -> PortfolioTracker:
    return PortfolioTracker(
        db_path=db,
        client_id=client_id,
        client_secret=client_secret,
        rest_url="https://test.deribit.com",
    )


# ── Fake REST responses ───────────────────────────────────────────────────────

def _fake_auth_response() -> dict:
    return {"result": {"access_token": "test-token-abc", "expires_in": 900}}


def _fake_rest_post(url: str, payload: dict, timeout: int = 10) -> dict:
    """Fake _rest_post that returns a valid auth token for any URL."""
    return _fake_auth_response()


def _fake_summary_btc(equity: float = 0.5, available: float = 0.4, margin: float = 0.1) -> dict:
    return {
        "result": {
            "currency": "BTC",
            "equity":          equity,
            "available_funds": available,
            "initial_margin":  margin,
            "balance":         equity,
        }
    }


def _fake_summary_eth(equity: float = 5.0, available: float = 4.5, margin: float = 0.5) -> dict:
    return {
        "result": {
            "currency": "ETH",
            "equity":          equity,
            "available_funds": available,
            "initial_margin":  margin,
            "balance":         equity,
        }
    }


def _fake_index_btc() -> dict:
    return {"result": {"index_price": 90_000.0}}


def _fake_index_eth() -> dict:
    return {"result": {"index_price": 3_000.0}}


def _fake_positions_empty() -> dict:
    return {"result": []}


# ── Tests: DB-only mode (no credentials) ─────────────────────────────────────

class TestDbOnlyMode:

    def test_refresh_returns_state_without_credentials(self):
        db = _make_db()
        tracker = _make_tracker(db)
        state = tracker.refresh()
        assert isinstance(state, PortfolioState)

    def test_used_margin_zero_with_no_positions(self):
        db = _make_db()
        tracker = _make_tracker(db)
        tracker.refresh()
        assert tracker.used_margin == pytest.approx(0.0)

    def test_used_margin_equals_sum_of_open_debits(self):
        db = _make_db()
        _seed_open_trade(db, net_debit=10.0, qty=2.0)  # 20.0
        _seed_open_trade(db, net_debit=5.0,  qty=3.0)  # 15.0
        tracker = _make_tracker(db)
        tracker.refresh()
        assert tracker.used_margin == pytest.approx(35.0)

    def test_realized_pnl_today_reflects_closed_trades(self):
        db = _make_db()
        _seed_closed_trade(db, pnl=50.0)
        _seed_closed_trade(db, pnl=30.0)
        tracker = _make_tracker(db)
        tracker.refresh()
        assert tracker.realized_pnl_today == pytest.approx(80.0)

    def test_realized_pnl_excludes_other_dates(self):
        db = _make_db()
        _seed_closed_trade(db, pnl=100.0, date_close="2020-01-01")
        tracker = _make_tracker(db)
        tracker.refresh()
        assert tracker.realized_pnl_today == pytest.approx(0.0)

    def test_available_cash_zero_without_credentials(self):
        """Without API credentials available_cash stays 0."""
        db = _make_db()
        tracker = _make_tracker(db)
        tracker.refresh()
        assert tracker.available_cash == pytest.approx(0.0)

    def test_equity_zero_without_credentials(self):
        db = _make_db()
        tracker = _make_tracker(db)
        tracker.refresh()
        assert tracker.equity_usd == pytest.approx(0.0)


# ── Tests: available_cash calculation from mocked API ────────────────────────

class TestAvailableCashCalculation:

    def _run_refresh_mocked(
        self,
        db: Path,
        summaries: dict,    # currency → raw summary result dict
        positions: dict | None = None,
        spot_prices: dict[str, float] | None = None,
    ) -> PortfolioTracker:
        """
        Run tracker.refresh() with _rest_get fully mocked.

        ``summaries`` maps currency → the dict under "result" (not wrapped).
        ``positions`` maps currency → list of position dicts.
        ``spot_prices`` are passed directly to refresh() to bypass API calls.
        """
        positions = positions or {}

        def fake_rest_get(url: str, bearer_token: str | None = None, timeout: int = 10) -> dict:
            if "get_account_summary" in url:
                for currency, summary in summaries.items():
                    if f"currency={currency}" in url:
                        return {"result": summary}
            if "get_positions" in url:
                for currency, pos_list in positions.items():
                    if f"currency={currency}" in url:
                        return {"result": pos_list}
                return {"result": []}
            return {"result": {}}

        tracker = PortfolioTracker(
            db_path=db,
            client_id="test-id",
            client_secret="test-secret",
            rest_url="https://test.deribit.com",
        )
        with patch("portfolio.tracker._rest_post", side_effect=_fake_rest_post):
            with patch("portfolio.tracker._rest_get", side_effect=fake_rest_get):
                with patch("config.ASSETS", ["BTC"]):
                    tracker.refresh(spot_prices=spot_prices)
        return tracker

    def test_available_cash_uses_api_available_funds(self):
        db = _make_db()
        summaries = {
            "BTC": {"equity": 0.5, "available_funds": 0.4, "initial_margin": 0.1}
        }
        tracker = self._run_refresh_mocked(
            db, summaries, spot_prices={"BTC": 100_000.0}
        )
        # available_funds=0.4 BTC × $100k = $40,000
        assert tracker.available_cash == pytest.approx(40_000.0)

    def test_equity_usd_computed_from_spot(self):
        db = _make_db()
        summaries = {
            "BTC": {"equity": 1.0, "available_funds": 0.8, "initial_margin": 0.2}
        }
        tracker = self._run_refresh_mocked(
            db, summaries, spot_prices={"BTC": 50_000.0}
        )
        assert tracker.equity_usd == pytest.approx(50_000.0)

    def test_available_cash_not_negative(self):
        db = _make_db()
        summaries = {
            "BTC": {"equity": 0.01, "available_funds": -0.05, "initial_margin": 0.1}
        }
        tracker = self._run_refresh_mocked(
            db, summaries, spot_prices={"BTC": 100_000.0}
        )
        assert tracker.available_cash == pytest.approx(0.0)

    def test_unrealized_pnl_from_positions(self):
        db = _make_db()
        summaries = {
            "BTC": {"equity": 0.5, "available_funds": 0.4, "initial_margin": 0.1}
        }
        positions = {
            "BTC": [{"floating_profit_loss": 0.001}, {"floating_profit_loss": -0.0005}]
        }
        tracker = self._run_refresh_mocked(
            db, summaries, positions, spot_prices={"BTC": 100_000.0}
        )
        # (0.001 - 0.0005) × 100,000 = $50
        assert tracker.unrealized_pnl == pytest.approx(50.0)

    def test_api_failure_leaves_cached_state_unchanged(self):
        """If the REST call raises, available_cash stays at its last value."""
        db = _make_db()
        tracker = PortfolioTracker(
            db_path=db,
            client_id="bad-id",
            client_secret="bad-secret",
            rest_url="https://test.deribit.com",
        )
        with patch("portfolio.tracker._rest_post", side_effect=RuntimeError("timeout")):
            with patch("config.ASSETS", ["BTC"]):
                tracker.refresh()
        # equity_usd was never set → stays 0
        assert tracker.equity_usd == pytest.approx(0.0)


# ── Tests: offline state tracking ────────────────────────────────────────────

class TestOfflineTracking:
    """Verify that repeated API failures are suppressed after the first warning."""

    def _make_tracker(self, db):
        return PortfolioTracker(
            db_path=db,
            client_id="id",
            client_secret="secret",
            rest_url="https://test.deribit.com",
        )

    def test_first_failure_sets_offline_flag(self):
        """First REST failure marks tracker as offline."""
        db = _make_db()
        tracker = self._make_tracker(db)
        assert not tracker._api_offline
        with patch("portfolio.tracker._rest_post", side_effect=OSError("unreachable")):
            with patch("config.ASSETS", ["BTC"]):
                tracker.refresh()
        assert tracker._api_offline
        assert tracker._api_fail_count == 1

    def test_repeated_failures_increment_count(self):
        """Subsequent failures increment the counter without resetting offline flag."""
        db = _make_db()
        tracker = self._make_tracker(db)
        with patch("portfolio.tracker._rest_post", side_effect=OSError("unreachable")):
            with patch("config.ASSETS", ["BTC"]):
                tracker.refresh()
                tracker.refresh()
                tracker.refresh()
        assert tracker._api_offline
        assert tracker._api_fail_count == 3

    def test_first_failure_logs_warning_subsequent_log_debug(self, caplog):
        """Only the first failure logs at WARNING; repeats are DEBUG."""
        import logging
        db = _make_db()
        tracker = self._make_tracker(db)
        with patch("portfolio.tracker._rest_post", side_effect=OSError("gone")):
            with patch("config.ASSETS", ["BTC"]):
                with caplog.at_level(logging.DEBUG, logger="portfolio.tracker"):
                    tracker.refresh()
                    tracker.refresh()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        debugs   = [r for r in caplog.records if r.levelno == logging.DEBUG
                    and "still offline" in r.message]
        assert len(warnings) == 1
        assert "offline" in warnings[0].message.lower()
        assert len(debugs) >= 1

    def _fake_rest_get(self, url, **kwargs):
        """Return a minimal valid API response based on the URL path."""
        if "get_account_summary" in url:
            return {"result": {"equity": 1.0, "available_funds": 0.9,
                               "initial_margin": 0.0, "floating_profit_loss": 0.0}}
        if "get_positions" in url:
            return {"result": []}
        return {"result": {}}

    def test_recovery_clears_offline_flag(self):
        """A successful refresh after failures clears the offline flag."""
        db = _make_db()
        tracker = self._make_tracker(db)

        # First go offline
        with patch("portfolio.tracker._rest_post", side_effect=OSError("gone")):
            with patch("config.ASSETS", ["BTC"]):
                tracker.refresh()
        assert tracker._api_offline

        # Then recover
        with patch("portfolio.tracker._rest_post", side_effect=_fake_rest_post):
            with patch("portfolio.tracker._rest_get", side_effect=self._fake_rest_get):
                with patch("config.ASSETS", ["BTC"]):
                    tracker.refresh(spot_prices={"BTC": 100_000.0})

        assert not tracker._api_offline
        assert tracker._api_fail_count == 0

    def test_recovery_logs_info(self, caplog):
        """Recovery from offline state is logged at INFO with retry count."""
        import logging
        db = _make_db()
        tracker = self._make_tracker(db)
        # Force offline state directly
        tracker._api_offline = True
        tracker._api_fail_count = 5

        with patch("portfolio.tracker._rest_post", side_effect=_fake_rest_post):
            with patch("portfolio.tracker._rest_get", side_effect=self._fake_rest_get):
                with patch("config.ASSETS", ["BTC"]):
                    with caplog.at_level(logging.INFO, logger="portfolio.tracker"):
                        tracker.refresh(spot_prices={"BTC": 100_000.0})

        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("back online" in m.lower() and "5" in m for m in info_msgs)


# ── Tests: reconciliation warning ────────────────────────────────────────────

class TestReconciliation:

    def _run_with_margin(
        self, db: Path, deribit_margin: float, db_margin: float
    ) -> tuple[PortfolioTracker, list]:
        """Run refresh() and capture log warnings."""
        summaries = {
            "BTC": {
                "equity":          1.0,
                "available_funds": 0.9,
                "initial_margin":  deribit_margin / 100_000.0,  # convert USD→BTC
            }
        }

        def fake_rest_get(url, bearer_token=None, timeout=10):
            if "get_account_summary" in url:
                return {"result": summaries["BTC"]}
            return {"result": []}

        tracker = PortfolioTracker(
            db_path=db,
            client_id="id",
            client_secret="secret",
            rest_url="https://test.deribit.com",
        )

        import logging
        warnings: list[str] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    warnings.append(record.getMessage())

        log = logging.getLogger("portfolio.tracker")
        handler = CapturingHandler()
        log.addHandler(handler)
        old_level = log.level
        log.setLevel(logging.WARNING)
        try:
            with patch("portfolio.tracker._rest_post", side_effect=_fake_rest_post):
                with patch("portfolio.tracker._rest_get", side_effect=fake_rest_get):
                    with patch("config.ASSETS", ["BTC"]):
                        tracker.refresh(spot_prices={"BTC": 100_000.0})
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)

        return tracker, warnings

    def _seed_margin(self, db: Path, margin_usd: float) -> None:
        """Seed an open trade whose net_debit*qty equals margin_usd."""
        create_calendar_trade(
            asset="BTC",
            date_open=date.today(),
            option_type="Call",
            strike=90_000.0,
            expiry_near="27JUN25",
            expiry_far="25JUL25",
            near_days=3,
            far_days=31,
            qty=1.0,
            spot_open=90_000.0,
            near_prem=5.0,
            far_prem=5.0 + margin_usd,
            net_debit=margin_usd,
            db_path=db,
        )

    def test_no_warning_when_margins_match(self):
        db = _make_db()
        self._seed_margin(db, 1_000.0)  # SQLite margin = $1000
        # Deribit margin ≈ $1000 (same)
        _, warnings = self._run_with_margin(db, deribit_margin=1_000.0, db_margin=1_000.0)
        assert not any("RECONCILE MISMATCH" in w for w in warnings)

    def test_warning_fires_on_large_divergence(self):
        db = _make_db()
        self._seed_margin(db, 100.0)    # SQLite margin = $100
        # Deribit margin = $5000 → divergence > 10%
        _, warnings = self._run_with_margin(db, deribit_margin=5_000.0, db_margin=100.0)
        assert any("RECONCILE MISMATCH" in w for w in warnings)

    def test_no_warning_when_both_zero(self):
        db = _make_db()
        _, warnings = self._run_with_margin(db, deribit_margin=0.0, db_margin=0.0)
        assert not any("RECONCILE MISMATCH" in w for w in warnings)


# ── Tests: portfolio_view formatting ─────────────────────────────────────────

class TestPortfolioView:

    def test_view_contains_all_sections(self):
        db = _make_db()
        tracker = _make_tracker(db)
        tracker.refresh()
        view = tracker.portfolio_view()
        assert "PORTFOLIO SNAPSHOT" in view
        assert "Equity" in view
        assert "Available Cash" in view
        assert "Used Margin" in view
        assert "Unrealized P&L" in view
        assert "Realized P&L Today" in view
        assert "Open Positions" in view

    def test_view_reflects_db_values(self):
        db = _make_db()
        _seed_open_trade(db, net_debit=20.0, qty=1.0)
        tracker = _make_tracker(db)
        tracker.refresh()
        view = tracker.portfolio_view()
        assert "20.00" in view  # used_margin = 20.0


# ── Tests: helper functions ───────────────────────────────────────────────────

class TestHelpers:

    def test_assets_to_currencies_deduplicates(self):
        result = _assets_to_currencies(["BTC", "ETH", "BTC", "SOL"])
        assert result == ["BTC", "ETH", "SOL"]

    def test_assets_to_currencies_uppercases(self):
        result = _assets_to_currencies(["btc", "eth"])
        assert result == ["BTC", "ETH"]

    def test_assets_to_currencies_empty(self):
        assert _assets_to_currencies([]) == []


# ── Tests: DecisionEngine integration ────────────────────────────────────────

class TestDecisionEngineIntegration:
    """
    Verify that DecisionEngine.scan_tick() calls portfolio.refresh() and
    updates _portfolio_value from available_cash.
    """

    def _make_engine_db(self) -> Path:
        """Create a temporary SQLite DB for engine integration tests."""
        from db.state import init_db
        db = Path(tempfile.mktemp(suffix=".db"))
        init_db(db)
        return db

    def test_scan_tick_calls_portfolio_refresh(self):
        from strategy.decision import DecisionEngine

        mock_cache = MagicMock()
        mock_cache.get_spot.return_value = 90_000.0
        mock_cache.get_chain.return_value = []
        mock_cache.get.return_value = None

        mock_portfolio = MagicMock(spec=PortfolioTracker)
        mock_state = MagicMock()
        mock_state.available_cash = 50_000.0
        mock_state.equity_usd = 60_000.0
        mock_portfolio.refresh.return_value = mock_state
        mock_portfolio._client_id = "test"

        db = self._make_engine_db()
        engine = DecisionEngine(
            cache=mock_cache,
            portfolio_value=10_000.0,
            portfolio=mock_portfolio,
            db_path=db,
        )
        engine.scan_tick()
        mock_portfolio.refresh.assert_called_once()

    def test_scan_tick_updates_portfolio_value_from_available_cash(self):
        from strategy.decision import DecisionEngine

        mock_cache = MagicMock()
        mock_cache.get_spot.return_value = 90_000.0
        mock_cache.get_chain.return_value = []
        mock_cache.get.return_value = None

        mock_portfolio = MagicMock(spec=PortfolioTracker)
        mock_state = MagicMock()
        mock_state.available_cash = 75_000.0
        mock_state.equity_usd = 80_000.0
        mock_portfolio.refresh.return_value = mock_state
        mock_portfolio._client_id = "test"

        db = self._make_engine_db()
        engine = DecisionEngine(
            cache=mock_cache,
            portfolio_value=1_000.0,   # initial value — should be overwritten
            portfolio=mock_portfolio,
            db_path=db,
        )
        engine.scan_tick()
        assert engine.portfolio_value == pytest.approx(75_000.0)

    def test_scan_tick_skips_when_available_cash_zero_and_credentials_set(self):
        from strategy.decision import DecisionEngine, BotState

        mock_cache = MagicMock()
        mock_portfolio = MagicMock(spec=PortfolioTracker)
        mock_state = MagicMock()
        mock_state.available_cash = 0.0
        mock_state.equity_usd = 0.0
        mock_portfolio.refresh.return_value = mock_state
        mock_portfolio._client_id = "test"  # credentials are set

        db = self._make_engine_db()
        engine = DecisionEngine(
            cache=mock_cache,
            portfolio_value=10_000.0,
            portfolio=mock_portfolio,
            db_path=db,
        )
        status = engine.scan_tick()
        assert "insufficient" in status.message.lower()

    def test_scan_tick_without_portfolio_uses_original_value(self):
        """Without a portfolio tracker, portfolio_value stays at the constructor value."""
        from strategy.decision import DecisionEngine

        mock_cache = MagicMock()
        mock_cache.get_spot.return_value = 90_000.0
        mock_cache.get_chain.return_value = []
        mock_cache.get.return_value = None

        db = self._make_engine_db()
        engine = DecisionEngine(
            cache=mock_cache,
            portfolio_value=25_000.0,
            db_path=db,
        )
        engine.scan_tick()
        assert engine.portfolio_value == pytest.approx(25_000.0)


# ── Margin utilities (Phase 17) ──────────────────────────────────────────────

class TestMaintenanceMargin:
    """Tracking and calculating margin utilization."""

    def test_maintenance_margin_property(self):
        """maintenance_margin_usd property returns cached value."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(db_path=Path(db))
        tracker._maintenance_margin_usd = 5_000.0
        assert tracker.maintenance_margin_usd == 5_000.0

    def test_margin_utilization_pct(self):
        """margin_utilization_pct = maintenance_margin / equity."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(db_path=Path(db))
        tracker._equity_usd = 100_000.0
        tracker._maintenance_margin_usd = 25_000.0
        assert tracker.margin_utilization_pct == pytest.approx(0.25)

    def test_margin_utilization_pct_zero_equity(self):
        """margin_utilization_pct returns 0.0 when equity is zero."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(db_path=Path(db))
        tracker._equity_usd = 0.0
        tracker._maintenance_margin_usd = 1_000.0
        assert tracker.margin_utilization_pct == 0.0

    def test_simulate_margin_placeholder(self):
        """simulate_margin returns None (placeholder for Phase 17)."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(
            db_path=Path(db),
            client_id="test_id",
            client_secret="test_secret",
        )
        legs = [("BTC-1JAN26-100000-C", 1.0, 0.05)]
        result = tracker.simulate_margin(legs)
        assert result is None  # Placeholder returns None

    def test_simulate_margin_no_credentials(self):
        """simulate_margin returns None when credentials are not configured."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(db_path=Path(db))
        # tracker has no credentials
        legs = [("BTC-1JAN26-100000-C", 1.0, 0.05)]
        result = tracker.simulate_margin(legs)
        assert result is None
