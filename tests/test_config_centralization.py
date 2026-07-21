"""
tests/test_config_centralization.py
===================================
Phase 20 — verify that previously-hardcoded config-like values are now
sourced from config.py, and that the two functional config-bypass bugs
(SOL order reconciliation, cache-TTL divergence) are fixed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import logging.handlers
from pathlib import Path
from unittest.mock import patch

import pytest

import config


# ── 20a — logging ─────────────────────────────────────────────────────────────

class TestLoggingConfig:
    def test_config_has_logging_keys(self):
        for key in (
            "LOG_LEVEL", "LOG_FORMAT", "LOG_DATE_FORMAT",
            "LOG_FILE_MAX_BYTES", "LOG_BACKUP_COUNT", "LOG_DIR",
            "NOISY_LOGGERS", "LOG_LEVEL_OVERRIDES",
        ):
            assert hasattr(config, key), f"config.{key} missing"

    def test_setup_logging_reads_rotation_from_config(self, tmp_path: Path):
        import core.logging_setup as ls

        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            ls.setup_logging(log_dir=tmp_path / "logs", force=True)
            file_handlers = [
                h for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
                and h not in original_handlers
            ]
            assert file_handlers, "setup_logging added no rotating file handler"
            fh = file_handlers[-1]
            assert fh.maxBytes == config.LOG_FILE_MAX_BYTES
            assert fh.backupCount == config.LOG_BACKUP_COUNT
            assert (tmp_path / "logs" / "bot.log").exists()
        finally:
            for h in list(root.handlers):
                if h not in original_handlers:
                    root.removeHandler(h)
                    if hasattr(h, "close"):
                        h.close()
            ls._configured = False

    def test_setup_logging_applies_noisy_loggers_from_config(self, tmp_path: Path):
        import core.logging_setup as ls

        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            ls.setup_logging(log_dir=tmp_path / "logs", force=True)
            for name, level in config.NOISY_LOGGERS.items():
                expected = getattr(logging, level.upper()) if isinstance(level, str) else level
                assert logging.getLogger(name).level == expected
        finally:
            for h in list(root.handlers):
                if h not in original_handlers:
                    root.removeHandler(h)
                    if hasattr(h, "close"):
                        h.close()
            ls._configured = False

    def test_monitor_loop_configure_logging_is_shared_helper(self):
        """monitor.loop must delegate to core.logging_setup, not duplicate it."""
        import monitor.loop as ml
        from core.logging_setup import SecretRedactor

        assert ml._SecretRedactor is SecretRedactor


# ── 20b — previously fake-configurable keys ───────────────────────────────────

class TestFakeConfigurableKeysNowReal:
    def test_new_keys_exist_in_config(self):
        for key in (
            "SLIPPAGE_LIMIT_PCT", "ORDER_TIMEOUT_SEC", "MAX_ORDER_RETRIES",
            "STUCK_ORDER_TIMEOUT_SEC", "INITIAL_CAPITAL", "COLLECTOR_INTERVAL_SEC",
        ):
            assert hasattr(config, key), f"config.{key} missing"

    def test_executor_constants_sourced_from_config(self):
        import execution.executor as ex

        assert ex.SLIPPAGE_LIMIT_PCT == config.SLIPPAGE_LIMIT_PCT
        assert ex.ORDER_TIMEOUT_SEC == config.ORDER_TIMEOUT_SEC
        assert ex.MAX_RETRIES == config.MAX_ORDER_RETRIES
        assert ex._RETRY_DELAYS == config.ORDER_RETRY_DELAYS

    def test_order_manager_stuck_timeout_from_config(self):
        import execution.order_manager as om

        assert om.STUCK_ORDER_TIMEOUT == config.STUCK_ORDER_TIMEOUT_SEC

    def test_collector_interval_from_config(self):
        import backtest.data_collector as dc

        assert dc.COLLECTOR_INTERVAL_SEC == config.COLLECTOR_INTERVAL_SEC

    def test_redundant_getattr_fallbacks_removed(self):
        """The call sites must reference config.X directly, not getattr(config, "X", ...)."""
        import execution.executor as ex
        import strategy.scanner as sc
        import strategy.sizer as sz

        for mod in (ex, sc, sz):
            src = inspect.getsource(mod)
            assert 'getattr(config, "MAX_FAR_DAYS_FOR_1D_NEAR"' not in src
            assert 'getattr(config, "MIN_NET_DEBIT"' not in src
            assert 'getattr(config, "MAX_QTY"' not in src
            assert 'getattr(config, "COMBO_FILL_TIMEOUT_SEC"' not in src


# ── 20c — network/timeout/alert constants ─────────────────────────────────────

class TestNetworkAndAlertConstants:
    def test_new_keys_exist_in_config(self):
        for key in (
            "DERIBIT_WS_PING_INTERVAL", "DERIBIT_WS_PING_TIMEOUT",
            "DERIBIT_WS_OPEN_TIMEOUT", "DERIBIT_WS_MAX_SIZE",
            "RPC_TIMEOUT_SEC", "ORDER_RETRY_DELAYS",
            "ALERT_COOLDOWN_SEC", "SMTP_TIMEOUT_SEC", "TELEGRAM_TIMEOUT_SEC",
            "SMTP_FROM",
        ):
            assert hasattr(config, key), f"config.{key} missing"

    def test_notifier_smtp_settings_sourced_from_config(self):
        import alerts.notifier as nt

        assert nt._SMTP_HOST == config.SMTP_HOST
        assert nt._SMTP_PORT == config.SMTP_PORT
        assert nt._SMTP_USER == config.SMTP_USER
        assert nt._SMTP_PASS == config.SMTP_PASSWORD
        assert nt._SMTP_FROM == config.SMTP_FROM

    def test_notifier_default_cooldown_from_config(self):
        from alerts.notifier import Notifier

        assert Notifier()._cooldown == config.ALERT_COOLDOWN_SEC

    def test_feed_rpc_timeout_default_from_config(self):
        from data.deribit_feed import DeribitFeed

        sig = inspect.signature(DeribitFeed._rpc)
        assert sig.parameters["timeout"].default == config.RPC_TIMEOUT_SEC


# ── 20d — the two functional config-bypass bugs ───────────────────────────────

class _FakeWS:
    """Minimal fake WebSocket: every RPC gets an empty-list result."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, payload: str) -> None:
        msg = json.loads(payload)
        self.sent.append(msg)
        await self._queue.put(
            json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": []})
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()


class _FakeConnect:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws
        self.kwargs: dict = {}

    def __call__(self, endpoint, **kwargs):
        self.endpoint = endpoint
        self.kwargs = kwargs
        return self

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *args):
        return False


class TestConfigBypassBugs:
    def test_reconciliation_iterates_config_assets_including_sol(self):
        """Phase 20d bug fix: SOL orders must be reconciled on restart."""
        from execution.order_manager import _fetch_deribit_open_orders

        ws = _FakeWS()
        fake_connect = _FakeConnect(ws)
        with patch("execution.order_manager.websockets.connect", fake_connect), \
             patch.object(config, "ASSETS", ["BTC", "ETH", "SOL"]):
            result = asyncio.run(
                _fetch_deribit_open_orders(paper=True, client_id="", client_secret="")
            )

        assert result == []
        currencies = [
            m["params"]["currency"] for m in ws.sent
            if m["method"] == "private/get_open_orders_by_currency"
        ]
        assert currencies == ["BTC", "ETH", "SOL"]

    def test_reconciliation_uses_config_ws_url(self):
        from execution.order_manager import _fetch_deribit_open_orders

        ws = _FakeWS()
        fake_connect = _FakeConnect(ws)
        with patch("execution.order_manager.websockets.connect", fake_connect):
            asyncio.run(
                _fetch_deribit_open_orders(paper=True, client_id="", client_secret="")
            )
        assert fake_connect.endpoint == config.DERIBIT_WS_URL
        assert fake_connect.kwargs["ping_interval"] == config.DERIBIT_WS_PING_INTERVAL
        assert fake_connect.kwargs["open_timeout"] == config.DERIBIT_WS_OPEN_TIMEOUT
        assert fake_connect.kwargs["max_size"] == config.DERIBIT_WS_MAX_SIZE

    def test_chain_cache_default_ttl_from_config(self):
        from data.chain_cache import ChainCache

        assert ChainCache().ttl == float(config.CHAIN_CACHE_TTL_SEC)

    def test_chain_cache_explicit_ttl_still_wins(self):
        from data.chain_cache import ChainCache

        assert ChainCache(ttl=99.0).ttl == 99.0

    def test_debug_viewer_uses_default_cache_ttl(self):
        """The viewer's cache must not hardcode its own TTL any more."""
        import data.debug_viewer as dv

        src = inspect.getsource(dv._run)
        assert "ChainCache()" in src
        assert "ttl=60" not in src

    def test_dead_duplicate_url_constants_removed(self):
        import backtest.data_collector as dc
        import data.deribit_feed as feed
        import execution.order_manager as om

        assert not hasattr(feed, "_WS_PAPER")
        assert not hasattr(feed, "_WS_LIVE")
        assert not hasattr(om, "_WS_PAPER")
        assert not hasattr(om, "_WS_LIVE")
        assert not hasattr(dc, "_PAPER_HOST")
        assert not hasattr(dc, "_LIVE_HOST")


# ── 20e — business-logic thresholds ───────────────────────────────────────────

class TestBusinessLogicThresholds:
    def test_new_keys_exist_in_config(self):
        for key in (
            "STRIKE_INCREMENT_TABLE", "STRIKE_INCREMENT_DEFAULT",
            "FAR_LEG_SPREAD_TABLE", "FAR_LEG_SPREAD_DEFAULT",
            "FAR_LEG_LIQUIDITY_PENALTY_PER_30D",
            "NEAR_DAY_TOLERANCE", "FAR_DAY_TOLERANCE",
            "ROLL_TRIGGER_DAYS", "POSITION_FAILURE_RETRY_CAP",
            "RECONCILE_THRESHOLD_PCT", "MIN_CONTRACT_SIZE",
            "STRIKE_CORRELATION_PCT", "DEFAULT_PORTFOLIO_VALUE",
            "EV_SAMPLE_COUNT", "BREAKEVEN_SCAN_STEPS", "BREAKEVEN_SCAN_RANGE",
            "SPREAD_WARN_PCT",
        ):
            assert hasattr(config, key), f"config.{key} missing"

    def test_strike_increment_reads_config_table(self):
        from core.pricing import strike_increment

        # Values must match today's behaviour…
        assert strike_increment(50_000) == config.STRIKE_INCREMENT_DEFAULT
        assert strike_increment(3) == 0.50
        assert strike_increment(50) == 5.0
        # …and changing the config table must change the result (late binding).
        with patch.object(config, "STRIKE_INCREMENT_TABLE", [(1_000_000, 7.0)]):
            assert strike_increment(50_000) == 7.0

    def test_adjust_far_leg_price_reads_config_table(self):
        from core.pricing import adjust_far_leg_price

        assert adjust_far_leg_price(1000.0, 7, is_buy=True) == pytest.approx(1005.0)
        with patch.object(config, "FAR_LEG_SPREAD_TABLE", [(7, 0.10)]):
            assert adjust_far_leg_price(1000.0, 7, is_buy=True) == pytest.approx(1100.0)

    def test_breakeven_scan_defaults_from_config(self):
        from core.calendar_engine import find_breakevens

        sig = inspect.signature(find_breakevens)
        assert sig.parameters["n_steps"].default == config.BREAKEVEN_SCAN_STEPS

    def test_spread_warn_threshold_from_config(self):
        from core.calendar_engine import check_calendar_status

        op = {"net_debit": 100.0, "qty": 1.0, "strike": 100.0, "option_type": "Call"}
        # 90% of debit is normally "ok"; with a 0.95 warn threshold it must warn.
        status, *_ = check_calendar_status(100.0, 0.8, 5, 30, op, market_sv=90.0)
        assert status == "ok"
        with patch.object(config, "SPREAD_WARN_PCT", 0.95):
            status, *_ = check_calendar_status(100.0, 0.8, 5, 30, op, market_sv=90.0)
        assert status == "warn"

    def test_scanner_tolerances_default_to_config(self):
        from strategy.scanner import scan

        sig = inspect.signature(scan)
        assert sig.parameters["near_day_tolerance"].default is None
        assert sig.parameters["far_day_tolerance"].default is None
        src = inspect.getsource(scan)
        assert "config.NEAR_DAY_TOLERANCE" in src
        assert "config.FAR_DAY_TOLERANCE" in src

    def test_decision_constants_from_config(self):
        import strategy.decision as dec

        assert dec._ROLL_TRIGGER_DAYS == config.ROLL_TRIGGER_DAYS
        src = inspect.getsource(dec)
        assert "config.POSITION_FAILURE_RETRY_CAP" in src
        assert "failure_count >= 3" not in src

    def test_sizer_constants_from_config(self):
        import strategy.sizer as sz

        assert sz._MIN_QTY == config.MIN_CONTRACT_SIZE
        assert sz._STRIKE_CORRELATION_PCT == config.STRIKE_CORRELATION_PCT

    def test_tracker_reconcile_threshold_from_config(self):
        import portfolio.tracker as pt

        assert pt._RECONCILE_THRESHOLD == config.RECONCILE_THRESHOLD_PCT

    def test_default_portfolio_value_shared(self):
        import execution.executor as ex
        from backtest.engine import BacktestEngine

        assert (
            inspect.signature(ex.CalendarExecutor.__init__).parameters["portfolio_value"].default
            == config.DEFAULT_PORTFOLIO_VALUE
        )
        assert (
            inspect.signature(BacktestEngine.__init__).parameters["portfolio_value"].default
            == config.DEFAULT_PORTFOLIO_VALUE
        )


# ── 20f — paths, timezone, date format ────────────────────────────────────────

class TestPathsTimezoneDateFormat:
    def test_new_keys_exist_in_config(self):
        for key in ("DB_PATH", "HISTORIC_DATA_DB_PATH", "TIMEZONE", "DATE_FORMAT"):
            assert hasattr(config, key), f"config.{key} missing"

    def test_db_state_path_from_config(self):
        import db.state as st

        assert st.DB_PATH == config.DB_PATH

    def test_db_state_timezone_from_config(self):
        import db.state as st

        assert str(st._AEST) == config.TIMEZONE

    def test_data_collector_db_path_from_config(self):
        import backtest.data_collector as dc

        assert dc.DB_PATH == config.HISTORIC_DATA_DB_PATH

    def test_pnl_chart_uses_config_date_format(self):
        import telegram_cmd.pnl_chart as pc

        src = inspect.getsource(pc)
        assert "config.DATE_FORMAT" in src
        assert 'strptime(t.date_close, "%Y-%m-%d")' not in src

    def test_bot_db_path_env_override_still_works(self, monkeypatch, tmp_path: Path):
        """The --db pre-parser contract: BOT_DB_PATH must drive config.DB_PATH."""
        import importlib

        monkeypatch.setenv("BOT_DB_PATH", str(tmp_path / "other.db"))
        cfg = importlib.reload(config)
        try:
            assert cfg.DB_PATH == tmp_path / "other.db"
        finally:
            monkeypatch.delenv("BOT_DB_PATH")
            importlib.reload(config)


# ── 21f — config_test.py parity with config.py ────────────────────────────────

class TestConfigTestParity:
    """
    config_test.py is a full standalone config exec'd into config.py's namespace.
    Every public UPPER_CASE constant in config.py must also be defined in
    config_test.py so the file stays honest about what the test-mode instance
    runs with (Phase 21f) and never silently drifts as new keys are added.
    """

    def _upper_names(self, module) -> set[str]:
        return {k for k in dir(module) if k.isupper() and not k.startswith("_")}

    def test_config_test_defines_every_config_key(self):
        import config_test

        missing = self._upper_names(config) - self._upper_names(config_test)
        assert not missing, f"config_test.py is missing config.py keys: {sorted(missing)}"

    def test_phase21_keys_present_in_both(self):
        import config_test

        for key in (
            "EV_SCORE_RANKING_CAP", "MAX_MONEYNESS_PCT",
            "MARKET_SV_REQUIRE_TWO_SIDED", "CLOSE_CONFIRM_TICKS",
            "REENTRY_COOLDOWN_SEC",
        ):
            assert hasattr(config, key), f"config.py missing {key}"
            assert hasattr(config_test, key), f"config_test.py missing {key}"

    def test_phase25_keys_present_in_both(self):
        import config_test

        for key in (
            "MAX_LEG_SPREAD_ABS_TICKS", "MAX_LEG_SPREAD_ABS_USD",
            "DEFAULT_MIN_TRADE_AMOUNTS", "DEFAULT_MIN_TRADE_AMOUNT",
        ):
            assert hasattr(config, key), f"config.py missing {key}"
            assert hasattr(config_test, key), f"config_test.py missing {key}"

        # config.py disables the absolute floor by default (live unchanged);
        # config_test.py enables the tick floor for thin testnet books.
        assert config.MAX_LEG_SPREAD_ABS_TICKS == 0
        assert config_test.MAX_LEG_SPREAD_ABS_TICKS > 0
