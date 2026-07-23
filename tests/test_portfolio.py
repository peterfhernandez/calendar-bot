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


# Fixture: Default TRADING_MODE to "test" for all tests except paper mode tests
# Paper mode tests will explicitly override this
@pytest.fixture(autouse=True)
def _default_trading_mode_to_test():
    """Default TRADING_MODE to 'test' for all tests, ensuring API path is taken."""
    with patch("config.TRADING_MODE", "test"):
        yield


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
            # Handle authentication endpoint (public/auth with query params)
            if "public/auth" in url:
                return _fake_auth_response()
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
        if "public/auth" in url:
            return _fake_auth_response()
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

        # First go offline — make _rest_get fail on auth
        def fail_on_auth(url: str, *args, **kwargs):
            if "public/auth" in url:
                raise OSError("network error")
            return {"result": {}}

        with patch("portfolio.tracker._rest_get", side_effect=fail_on_auth):
            with patch("config.ASSETS", ["BTC"]):
                tracker.refresh()
        assert tracker._api_offline

        # Then recover
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
            if "public/auth" in url:
                return _fake_auth_response()
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


# ── Tests: Phase 24 — reconcile mismatch remediation ─────────────────────────

def _mark_stuck(db: Path, trade_id: int) -> None:
    """Flag a trade close_stuck directly in the DB."""
    from db.state import mark_position_close_stuck
    mark_position_close_stuck(
        trade_id=trade_id,
        error_reason="Roll retry limit exceeded — manual close needed",
        db_path=db,
    )


def _seed_stuck_trade(
    db: Path,
    near_instrument: str,
    far_instrument: str,
    result_terminal: bool = True,
) -> int:
    """Seed a close_stuck trade with named legs; return its id."""
    trade = create_calendar_trade(
        asset="BTC",
        date_open=date.today(),
        option_type="Call",
        strike=64_000.0,
        expiry_near="15JUL26",
        expiry_far="28AUG26",
        near_days=3,
        far_days=45,
        qty=0.1,
        spot_open=64_000.0,
        near_prem=5.0,
        far_prem=15.0,
        net_debit=10.0,
        near_instrument=near_instrument,
        far_instrument=far_instrument,
        db_path=db,
    )
    if result_terminal:
        # Emulate the observed state: the bot recorded a closure that never
        # executed on Deribit, so result is terminal but close_status='close_stuck'.
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE calendar_trades SET result='Loss (Stop)', date_close=?, pnl=-1.0 WHERE id=?",
                (date.today().isoformat(), trade.id),
            )
    _mark_stuck(db, trade.id)
    return trade.id


def _fake_position(instrument: str, size: float = 0.1) -> dict:
    return {
        "instrument_name": instrument,
        "size":            size,
        "mark_price":      0.0005,
        "index_price":     64_000.0,
    }


class TestReconcileEnhanced:

    def _make_fake_rest_get(self, margin_btc: float, positions: list[dict]):
        def fake(url, bearer_token=None, timeout=10):
            if "public/auth" in url:
                return _fake_auth_response()
            if "get_account_summary" in url:
                return {"result": {
                    "equity":             1.0,
                    "available_funds":    0.9,
                    "initial_margin":     margin_btc,
                    "maintenance_margin": margin_btc,
                }}
            if "get_positions" in url:
                return {"result": positions}
            return {"result": []}
        return fake

    def _run_refresh(self, db: Path, margin_btc: float, positions: list[dict]) -> list[str]:
        tracker = PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
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
                with patch("portfolio.tracker._rest_get",
                           side_effect=self._make_fake_rest_get(margin_btc, positions)):
                    with patch("config.ASSETS", ["BTC"]):
                        tracker.refresh(spot_prices={"BTC": 100_000.0})
        finally:
            log.removeHandler(handler)
            log.setLevel(old_level)
        return warnings

    def test_mismatch_warning_names_instruments(self):
        db = _make_db()
        # SQLite margin $0 (no open non-stuck trades), Deribit margin large →
        # divergence 100%. get_positions returns a live BTC instrument.
        positions = [_fake_position("BTC-15JUL26-64000-C")]
        warnings = self._run_refresh(db, margin_btc=0.05, positions=positions)
        mismatch = [w for w in warnings if "RECONCILE MISMATCH" in w]
        assert mismatch, "expected a reconcile mismatch warning"
        assert any("BTC-15JUL26-64000-C" in w for w in mismatch)
        assert any("Deribit open:" in w for w in mismatch)

    def test_sync_closes_when_both_legs_absent(self):
        db = _make_db()
        tid = _seed_stuck_trade(db, "BTC-15JUL26-64000-C", "BTC-28AUG26-64000-C")
        tracker = PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com",
        )
        with patch("portfolio.tracker._rest_get",
                   side_effect=self._make_fake_rest_get(0.0, positions=[])):
            with patch("config.ASSETS", ["BTC"]):
                reconciled = tracker.sync_stuck_positions(db)
        assert reconciled == [tid]
        from db.state import get_close_status
        assert get_close_status(tid, db_path=db) == "closed"

    def test_sync_leaves_when_leg_still_open(self):
        db = _make_db()
        tid = _seed_stuck_trade(db, "BTC-15JUL26-64000-C", "BTC-28AUG26-64000-C")
        # Near leg is still open on Deribit → must not reconcile.
        positions = [_fake_position("BTC-15JUL26-64000-C")]
        tracker = PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com",
        )
        with patch("portfolio.tracker._rest_get",
                   side_effect=self._make_fake_rest_get(0.05, positions=positions)):
            with patch("config.ASSETS", ["BTC"]):
                reconciled = tracker.sync_stuck_positions(db)
        assert reconciled == []
        from db.state import get_close_status
        assert get_close_status(tid, db_path=db) == "close_stuck"

    def test_sync_aborts_on_fetch_failure(self):
        """An API failure must never falsely reconcile every stuck trade."""
        db = _make_db()
        tid = _seed_stuck_trade(db, "BTC-15JUL26-64000-C", "BTC-28AUG26-64000-C")
        tracker = PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com",
        )
        with patch("portfolio.tracker._rest_get", side_effect=OSError("unreachable")):
            with patch("config.ASSETS", ["BTC"]):
                reconciled = tracker.sync_stuck_positions(db)
        assert reconciled == []
        from db.state import get_close_status
        assert get_close_status(tid, db_path=db) == "close_stuck"

    def test_reconcile_resolves_same_cycle_after_sync(self):
        db = _make_db()
        tid = _seed_stuck_trade(db, "BTC-15JUL26-64000-C", "BTC-28AUG26-64000-C")
        # Operator closed on Deribit: no positions, margin 0. refresh() should
        # auto-reconcile the stuck trade and emit no mismatch warning.
        warnings = self._run_refresh(db, margin_btc=0.0, positions=[])
        assert not any("RECONCILE MISMATCH" in w for w in warnings)
        from db.state import get_close_status
        assert get_close_status(tid, db_path=db) == "closed"

    def test_get_deribit_open_positions_paper_mode_returns_empty(self):
        db = _make_db()
        tracker = PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com",
        )
        with patch("config.TRADING_MODE", "paper"):
            assert tracker.get_deribit_open_positions("BTC") == []


class TestReconcileEscalation:
    """Phase 26f: a persistent identical mismatch escalates to a one-shot alert."""

    def _make_tracker(self, notifier):
        tracker = PortfolioTracker(
            db_path=_make_db(), client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com", notifier=notifier,
        )
        # Avoid live position-fetch calls from the actionable warning path.
        tracker._describe_deribit_positions = MagicMock(
            return_value="BTC-15JUL26-64000-C (option) qty=0.1"
        )
        tracker._used_margin = 0.0          # SQLite margin
        tracker._deribit_margin_usd = 1500.0  # Deribit margin → 100% divergence
        return tracker

    def test_escalates_once_after_threshold(self):
        import config as cfg
        notifier = MagicMock()
        tracker = self._make_tracker(notifier)
        n = cfg.RECONCILE_ESCALATE_AFTER_CYCLES
        for _ in range(n - 1):
            tracker._reconcile()
        notifier.notify_warning.assert_not_called()
        tracker._reconcile()  # nth identical cycle → escalate
        notifier.notify_warning.assert_called_once()
        # One-shot: further identical cycles do not re-alert.
        tracker._reconcile()
        tracker._reconcile()
        notifier.notify_warning.assert_called_once()

    def test_resolved_mismatch_resets_escalation(self):
        import config as cfg
        notifier = MagicMock()
        tracker = self._make_tracker(notifier)
        for _ in range(cfg.RECONCILE_ESCALATE_AFTER_CYCLES):
            tracker._reconcile()
        notifier.notify_warning.assert_called_once()
        # Mismatch resolves (margins now agree, non-zero) → escalation resets.
        tracker._deribit_margin_usd = 1500.0
        tracker._used_margin = 1500.0
        tracker._reconcile()
        assert tracker._reconcile_repeat_count == 0
        assert tracker._reconcile_escalated is False


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

    def test_simulate_margin_success(self):
        """simulate_margin calls Deribit API and returns projected margin."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(
            db_path=Path(db),
            client_id="test_id",
            client_secret="test_secret",
        )
        legs = [
            ("BTC-1JAN26-100000-C", 1.0, 0.05),
            ("BTC-1FEB26-100000-C", 1.0, 0.03),
        ]

        # Mock the API calls
        auth_response = {"result": {"access_token": "fake_token"}}
        margin_response = {
            "result": {
                "initial_margin": 0.5,      # 0.5 BTC initial margin
                "maintenance_margin": 0.3,  # 0.3 BTC maintenance margin
            }
        }

        with patch("portfolio.tracker._rest_get") as mock_get, \
             patch("portfolio.tracker._rest_post") as mock_post, \
             patch("portfolio.tracker._resolve_spot") as mock_spot:
            mock_get.return_value = auth_response
            mock_post.return_value = margin_response
            mock_spot.return_value = 100_000.0  # BTC spot price in USD

            result = tracker.simulate_margin(legs)

            assert result is not None
            assert result.projected_initial_margin_usd > 0
            assert result.projected_maintenance_margin_usd > 0
            # Assuming BTC spot of 100,000, 0.5 BTC = $50,000
            assert result.projected_initial_margin_usd == pytest.approx(50_000.0)
            assert result.projected_maintenance_margin_usd == pytest.approx(30_000.0)

    def test_simulate_margin_no_credentials(self):
        """simulate_margin returns None when credentials are not configured."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(db_path=Path(db))
        # tracker has no credentials
        legs = [("BTC-1JAN26-100000-C", 1.0, 0.05)]
        result = tracker.simulate_margin(legs)
        assert result is None

    def test_simulate_margin_empty_legs(self):
        """simulate_margin returns None for empty legs list."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(
            db_path=Path(db),
            client_id="test_id",
            client_secret="test_secret",
        )
        result = tracker.simulate_margin([])
        assert result is None

    def test_simulate_margin_api_failure(self):
        """simulate_margin returns None if the API call fails."""
        db = tempfile.mktemp(suffix=".db")
        tracker = PortfolioTracker(
            db_path=Path(db),
            client_id="test_id",
            client_secret="test_secret",
        )
        legs = [("BTC-1JAN26-100000-C", 1.0, 0.05)]

        # Mock authentication to succeed but margin call to fail
        auth_response = {"result": {"access_token": "fake_token"}}

        with patch("portfolio.tracker._rest_get") as mock_get, \
             patch("portfolio.tracker._rest_post") as mock_post:
            mock_get.return_value = auth_response
            mock_post.side_effect = RuntimeError("API error")

            result = tracker.simulate_margin(legs)
            # Should gracefully handle the error and return None
            assert result is None


# ── Paper mode portfolio isolation (Phase 17b) ─────────────────────────────────

class TestPaperModePortfolioIsolation:
    """Verify that paper mode makes zero Deribit API calls and uses DB+cache only."""

    def test_no_deribit_api_calls_in_paper_mode(self):
        """In paper mode, refresh() should not call Deribit REST APIs."""
        db = _make_db()
        _seed_open_trade(db, net_debit=10.0, qty=2.0)

        tracker = PortfolioTracker(
            db_path=db,
            client_id="test_id",
            client_secret="test_secret",
        )

        # Mock both _rest_get and _rest_post to fail if called
        with patch("portfolio.tracker._rest_get") as mock_get, \
             patch("portfolio.tracker._rest_post") as mock_post, \
             patch("config.TRADING_MODE", "paper"):
            # Set up mocks to fail if they are called
            mock_get.side_effect = RuntimeError("REST GET should not be called in paper mode")
            mock_post.side_effect = RuntimeError("REST POST should not be called in paper mode")

            # Refresh should succeed without calling REST APIs
            state = tracker.refresh()

            # Verify refresh succeeded (returned a state)
            assert state is not None
            # Verify the REST methods were NOT called
            mock_get.assert_not_called()
            mock_post.assert_not_called()

    def test_no_reconciliation_warning_in_paper_mode(self):
        """In paper mode, refresh() should not emit reconciliation warnings."""
        db = _make_db()
        _seed_open_trade(db, net_debit=10.0, qty=2.0)

        tracker = PortfolioTracker(db_path=db)

        with patch("config.TRADING_MODE", "paper"), \
             patch("portfolio.tracker.logger") as mock_logger:
            state = tracker.refresh()

            # Verify no WARNING logs for reconciliation
            warning_calls = [
                call for call in mock_logger.warning.call_args_list
                if "RECONCILE" in str(call)
            ]
            assert len(warning_calls) == 0, "Paper mode should not emit reconciliation warnings"

    def test_equity_calculated_from_db_in_paper_mode(self):
        """In paper mode, equity should be non-zero and calculated from DB."""
        db = _make_db()
        _seed_open_trade(db, net_debit=10.0, qty=2.0)

        tracker = PortfolioTracker(db_path=db)

        # Mock config.INITIAL_CAPITAL if it exists, otherwise use default
        with patch("config.TRADING_MODE", "paper"), \
             patch("config.INITIAL_CAPITAL", 50_000.0, create=True):
            state = tracker.refresh()

            # Equity should be non-zero and calculated from DB
            assert state.equity_usd > 0, "Paper mode should calculate non-zero equity"
            # Since no realized PnL and unrealized from cache (0 without real cache),
            # equity should be initial capital (or close to it due to rounding)
            assert state.equity_usd >= 10_000.0  # At least the default if INITIAL_CAPITAL not found

    def test_unrealized_pnl_from_cache_in_paper_mode(self):
        """In paper mode, unrealized P&L should come from live cache prices."""
        db = _make_db()
        # Seed with a known instrument pair
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT INTO calendar_trades
                (asset, date_open, option_type, strike, expiry_near, expiry_far,
                 near_days, far_days, qty, spot_open, net_debit, near_prem, far_prem,
                 near_instrument, far_instrument, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("BTC", date.today().isoformat(), "Call", 90_000.0,
                 "27JUN25", "25JUL25", 3, 31, 2.0, 90_000.0, 10.0,
                 9.0, 11.0, "BTC-27JUN25-90000-C", "BTC-25JUL25-90000-C", "Open"),
            )
            conn.commit()

        # Create a mock cache that returns ticker snapshots
        mock_cache = MagicMock()
        mock_near_snap = MagicMock()
        mock_near_snap.bid = 9.0
        mock_near_snap.ask = 9.2
        mock_far_snap = MagicMock()
        mock_far_snap.bid = 11.0
        mock_far_snap.ask = 11.2

        # Set up the cache to return these snapshots
        def get_ticker_side_effect(instr):
            if "27JUN" in instr or "near" in instr.lower():
                return mock_near_snap
            else:
                return mock_far_snap

        mock_cache.get_ticker.side_effect = get_ticker_side_effect

        tracker = PortfolioTracker(db_path=db, cache=mock_cache)

        with patch("config.TRADING_MODE", "paper"):
            state = tracker.refresh()

            # Unrealized should be calculated from cache (spread_value - net_debit*qty)
            # spread_value ≈ (11.1 - 9.1) * 2.0 = 4.0
            # net_debit*qty = 10.0 * 2.0 = 20.0
            # unrealized ≈ 4.0 - 20.0 = -16.0
            assert state.unrealized_pnl < 0, "Negative unrealized should come from cache calculation"

    def test_test_mode_still_uses_deribit_api(self):
        """Regression test: test/live modes should still call Deribit API."""
        db = _make_db()
        _seed_open_trade(db, net_debit=10.0, qty=2.0)

        tracker = PortfolioTracker(
            db_path=db,
            client_id="test_id",
            client_secret="test_secret",
        )

        # Mock Deribit responses
        auth_response = {"result": {"access_token": "fake_token"}}
        summary_response = {
            "result": {
                "equity": 100.0,
                "available_funds": 90.0,
                "initial_margin": 5.0,
                "maintenance_margin": 4.0,
            }
        }
        positions_response = {"result": []}

        with patch("portfolio.tracker._rest_get") as mock_get, \
             patch("portfolio.tracker._resolve_spot") as mock_spot, \
             patch("config.TRADING_MODE", "test"):
            def get_side_effect(url, **kwargs):
                if "auth" in url:
                    return auth_response
                elif "get_account_summary" in url:
                    return summary_response
                elif "get_positions" in url:
                    return positions_response
                else:
                    return {}

            mock_get.side_effect = get_side_effect
            mock_spot.return_value = 100_000.0  # BTC spot price

            state = tracker.refresh()

            # In test mode, API calls should be made
            assert mock_get.called, "Test mode should call Deribit REST API"
            # Equity should come from API (100.0 BTC * 100,000 spot = $10M)
            assert state.equity_usd > 0


# ── Phase 25e — kind=any reconcile visibility ─────────────────────────────────

class TestKindAnyReconcile:
    def _tracker(self, db):
        return PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com",
        )

    def _rest_get_with(self, positions, orders):
        def fake(url, bearer_token=None, timeout=10):
            if "public/auth" in url:
                return _fake_auth_response()
            if "get_open_orders_by_currency" in url:
                return {"result": orders}
            if "get_positions" in url:
                return {"result": positions}
            return {"result": []}
        return fake

    def test_kind_any_includes_futures(self):
        db = _make_db()
        positions = [{
            "instrument_name": "BTC-PERPETUAL", "kind": "future",
            "size": 10.0, "mark_price": 1.0, "index_price": 100_000.0,
        }]
        with patch("portfolio.tracker._rest_post", side_effect=_fake_rest_post), \
             patch("portfolio.tracker._rest_get", side_effect=self._rest_get_with(positions, [])), \
             patch("config.TRADING_MODE", "test"):
            out = self._tracker(db).get_deribit_open_positions("BTC", kind="any")
        assert any(p["kind"] == "future" and p["instrument_name"] == "BTC-PERPETUAL" for p in out)

    def test_get_open_orders(self):
        db = _make_db()
        orders = [{"instrument_name": "BTC-15JUL26-64000-C", "direction": "buy",
                   "amount": 0.1, "price": 0.01}]
        with patch("portfolio.tracker._rest_post", side_effect=_fake_rest_post), \
             patch("portfolio.tracker._rest_get", side_effect=self._rest_get_with([], orders)), \
             patch("config.TRADING_MODE", "test"):
            out = self._tracker(db).get_deribit_open_orders("BTC")
        assert len(out) == 1
        assert out[0]["direction"] == "buy"

    def test_get_open_orders_paper_mode_empty(self):
        db = _make_db()
        with patch("config.TRADING_MODE", "paper"):
            assert self._tracker(db).get_deribit_open_orders("BTC") == []
