"""
Unit tests for telegram_cmd/ — handlers and security middleware.

All Telegram API interactions are mocked. No real network calls are made.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
from db.state import (
    CalendarTrade,
    get_open_trades,
    get_trades_closed_today_aest,
    get_trades_opened_today_aest,
    init_db,
    DB_PATH,
)
from strategy.decision import BotState, DecisionEngine
from telegram_cmd import handlers
from telegram_cmd.listener import TelegramCommandListener, _require_authorized_chat


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_trade(
    trade_id: int = 1,
    asset: str = "BTC",
    option_type: str = "Call",
    strike: float = 90_000.0,
    expiry_near: str = "2026-06-27",
    expiry_far: str = "2026-07-25",
    qty: float = 1.0,
    net_debit: float = 0.02,
    open_fees: float = 0.001,
    close_fees: float = 0.0,
    result: str = "Open",
    pnl: float | None = None,
    near_instrument: str | None = "BTC-27JUN26-90000-C",
    far_instrument:  str | None = "BTC-25JUL26-90000-C",
    date_open: str = "2026-06-26",
    date_close: str | None = None,
    notes: str | None = None,
    ev_score: float = 0.15,
) -> CalendarTrade:
    return CalendarTrade(
        id=trade_id,
        asset=asset,
        option_type=option_type,
        strike=strike,
        expiry_near=expiry_near,
        expiry_far=expiry_far,
        near_days=1,
        far_days=30,
        qty=qty,
        date_open=date_open,
        spot_open=90_000.0,
        near_prem=0.01,
        far_prem=0.03,
        net_debit=net_debit,
        fees=open_fees,
        open_fees=open_fees,
        close_fees=close_fees,
        result=result,
        broker=None,
        notes=notes,
        near_instrument=near_instrument,
        far_instrument=far_instrument,
        date_close=date_close,
        spot_close=None,
        pnl=pnl,
        ev_score=ev_score,
    )


def _make_update(chat_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args=None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


def _make_engine(db_path: Path | None = None) -> DecisionEngine:
    cache = MagicMock()
    cache.get_spot.return_value = 90_000.0
    return DecisionEngine(
        cache=cache,
        portfolio_value=10_000.0,
        db_path=db_path or Path(tempfile.mktemp(suffix=".db")),
        daily_loss_limit=500.0,
    )


def _make_cache(near_bid=0.01, near_ask=0.015, far_bid=0.03, far_ask=0.035,
                near_iv=0.90, far_iv=0.70, near_oi=500.0, far_oi=500.0) -> MagicMock:
    cache = MagicMock()
    near_snap = MagicMock(bid=near_bid, ask=near_ask, mark_iv=near_iv, open_interest=near_oi)
    far_snap  = MagicMock(bid=far_bid,  ask=far_ask,  mark_iv=far_iv,  open_interest=far_oi)
    def _get(instrument):
        if instrument and "27JUN26" in instrument:
            return near_snap
        if instrument and "25JUL26" in instrument:
            return far_snap
        return None
    cache.get.side_effect = _get
    return cache


# ── Security middleware ────────────────────────────────────────────────────────

class TestSecurityMiddleware:
    @pytest.mark.asyncio
    async def test_authorized_chat_allowed(self, monkeypatch):
        """Updates from the configured chat ID pass through to the handler."""
        monkeypatch.setattr(config, "TELEGRAM_CHAT", "12345")
        called = []

        @_require_authorized_chat
        async def mock_handler(update, context):
            called.append(True)

        await mock_handler(_make_update(chat_id=12345), _make_context())
        assert called == [True]

    @pytest.mark.asyncio
    async def test_unauthorized_chat_dropped(self, monkeypatch):
        """Updates from other chat IDs produce no reply and the handler is not called."""
        monkeypatch.setattr(config, "TELEGRAM_CHAT", "12345")
        called = []

        @_require_authorized_chat
        async def mock_handler(update, context):
            called.append(True)

        update = _make_update(chat_id=99999)
        await mock_handler(update, _make_context())
        assert called == []
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_chat_configured_drops_all(self, monkeypatch):
        """When TELEGRAM_CHAT is empty, all updates are dropped."""
        monkeypatch.setattr(config, "TELEGRAM_CHAT", "")
        called = []

        @_require_authorized_chat
        async def mock_handler(update, context):
            called.append(True)

        await mock_handler(_make_update(chat_id=12345), _make_context())
        assert called == []


# ── /positions handler ─────────────────────────────────────────────────────────

class TestHandlePositions:
    @pytest.mark.asyncio
    async def test_no_open_positions(self):
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        init_db(db_path)

        await handlers.handle_positions(update, context, cache, db_path)

        update.message.reply_text.assert_called_once()
        assert "no open" in update.message.reply_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_positions_shows_ev_and_expiry_range(self):
        """Reply contains ev score, expiry range, and entry cost."""
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trade = _make_trade(ev_score=0.25)
        with patch("telegram_cmd.handlers.get_open_trades", return_value=[trade]):
            await handlers.handle_positions(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "BTC" in text
        assert "90000" in text
        assert "ev=0.25" in text.lower() or "ev=" in text
        assert "→" in text  # expiry range separator
        assert "Call" in text  # full type name
        assert "entry=" in text.lower() or "entry" in text

    @pytest.mark.asyncio
    async def test_positions_shows_full_option_type(self):
        """Option type shown as 'Put' or 'Call', not single letter."""
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trade_put = _make_trade(option_type="Put")
        with patch("telegram_cmd.handlers.get_open_trades", return_value=[trade_put]):
            await handlers.handle_positions(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "Put" in text

    @pytest.mark.asyncio
    async def test_positions_stale_cache(self):
        """When cache returns None for an instrument, reply includes a stale note."""
        update  = _make_update()
        context = _make_context()
        cache   = MagicMock()
        cache.get.return_value = None
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trade = _make_trade()
        with patch("telegram_cmd.handlers.get_open_trades", return_value=[trade]):
            await handlers.handle_positions(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "stale" in text.lower() or "N/A" in text


# ── /close_trades handler ─────────────────────────────────────────────────────

class TestHandleCloseTrades:
    @pytest.mark.asyncio
    async def test_no_closed_today(self):
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        init_db(db_path)

        with patch("telegram_cmd.handlers.get_trades_closed_today_aest", return_value=[]):
            await handlers.handle_close_trades(update, context, db_path)

        text = update.message.reply_text.call_args[0][0].lower()
        assert "no trades" in text

    @pytest.mark.asyncio
    async def test_close_trades_shows_details(self):
        """Reply includes trade id, asset, debit, pnl, and close reason."""
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trades = [
            _make_trade(trade_id=1, result="Win (Auto TP)", pnl=50.0,
                        date_close="2026-06-26", notes="Take-profit (150% of debit)"),
            _make_trade(trade_id=2, result="Loss (Auto Stop)", pnl=-20.0,
                        date_close="2026-06-26", notes="Stop-loss (50% of debit)"),
        ]
        with patch("telegram_cmd.handlers.get_trades_closed_today_aest", return_value=trades):
            await handlers.handle_close_trades(update, context, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "#1" in text
        assert "#2" in text
        assert "BTC" in text
        assert "+30" in text or "30" in text  # total PnL
        assert "Take-profit" in text or "Stop-loss" in text


# ── /new_trades handler ────────────────────────────────────────────────────────

class TestHandleNewTrades:
    @pytest.mark.asyncio
    async def test_no_new_today(self):
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        init_db(db_path)

        with patch("telegram_cmd.handlers.get_trades_opened_today_aest", return_value=[]):
            await handlers.handle_new_trades(update, context, db_path)

        text = update.message.reply_text.call_args[0][0].lower()
        assert "no new" in text

    @pytest.mark.asyncio
    async def test_new_trades_shows_ev_and_expiry(self):
        """Reply includes trade id, asset, debit, ev, strike, expiry range."""
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trades = [
            _make_trade(trade_id=1, ev_score=0.20),
            _make_trade(trade_id=2, asset="ETH", strike=3000.0, ev_score=0.10),
        ]
        with patch("telegram_cmd.handlers.get_trades_opened_today_aest", return_value=trades):
            await handlers.handle_new_trades(update, context, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "2" in text
        assert "BTC" in text
        assert "ETH" in text
        assert "ev=" in text.lower()
        assert "→" in text


# ── /status handler ───────────────────────────────────────────────────────────

class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_status_contains_mode_drain_paused(self, monkeypatch):
        """Reply shows trading mode, drain mode, and paused flag."""
        monkeypatch.setattr(config, "TRADING_MODE", "paper")
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)

        engine  = _make_engine()
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[]), \
             patch("telegram_cmd.handlers.get_trades_closed_today_aest", return_value=[]):
            await handlers.handle_status(update, context, engine, db_path)

        text = update.message.reply_text.call_args[0][0].upper()
        assert "PAPER" in text
        assert "DRAIN" in text
        assert "PAUSED" in text

    @pytest.mark.asyncio
    async def test_status_shows_today_and_session_pnl(self, monkeypatch):
        """Reply includes both today AEST PnL and session PnL lines."""
        monkeypatch.setattr(config, "TRADING_MODE", "paper")
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)

        engine = _make_engine()
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[]), \
             patch("telegram_cmd.handlers.get_trades_closed_today_aest", return_value=[]):
            await handlers.handle_status(update, context, engine, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "today" in text.lower() or "AEST" in text
        assert "since start" in text.lower() or "session" in text.lower()

    @pytest.mark.asyncio
    async def test_status_shows_paused_when_paused(self, monkeypatch):
        """Reply reflects paused=YES when engine is paused."""
        monkeypatch.setattr(config, "TRADING_MODE", "paper")
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)

        engine = _make_engine()
        engine.pause()
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[]), \
             patch("telegram_cmd.handlers.get_trades_closed_today_aest", return_value=[]):
            await handlers.handle_status(update, context, engine, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "YES" in text or "yes" in text.lower()

    @pytest.mark.asyncio
    async def test_status_shows_open_count(self, monkeypatch):
        """Reply includes the number of open positions."""
        monkeypatch.setattr(config, "TRADING_MODE", "paper")
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)

        engine  = _make_engine()
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[_make_trade(), _make_trade(trade_id=2)]), \
             patch("telegram_cmd.handlers.get_trades_closed_today_aest", return_value=[]):
            await handlers.handle_status(update, context, engine, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "2" in text


# ── /portfolio handler ────────────────────────────────────────────────────────

class TestHandlePortfolio:
    @pytest.mark.asyncio
    async def test_no_open_positions(self):
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        init_db(db_path)

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[]):
            await handlers.handle_portfolio(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0].lower()
        assert "no open" in text

    @pytest.mark.asyncio
    async def test_portfolio_shows_ev_and_value(self):
        """Reply includes EV and current value; does NOT include IV or OI."""
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[_make_trade(ev_score=0.30)]):
            await handlers.handle_portfolio(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "EV" in text or "ev" in text.lower()
        assert "Value" in text or "value" in text.lower()
        # IV and OI should NOT appear in the simplified portfolio
        assert "IV" not in text
        assert " OI" not in text

    @pytest.mark.asyncio
    async def test_portfolio_shows_expiry_range(self):
        """Reply shows expiry dates as range with arrow separator."""
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[_make_trade()]):
            await handlers.handle_portfolio(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "→" in text

    @pytest.mark.asyncio
    async def test_portfolio_stale_cache_note(self):
        """Reply includes 'N/A' or stale note when leg data is unavailable."""
        update  = _make_update()
        context = _make_context()
        cache   = MagicMock()
        cache.get.return_value = None
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[_make_trade()]):
            await handlers.handle_portfolio(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "stale" in text.lower() or "N/A" in text


# ── /stop_bot and /start_bot ──────────────────────────────────────────────────

class TestStopStartBot:
    @pytest.mark.asyncio
    async def test_stop_bot_calls_pause(self):
        """/stop_bot calls engine.pause()."""
        engine  = _make_engine()
        update  = _make_update()
        context = _make_context()

        assert not engine.paused
        await handlers.handle_stop_bot(update, context, engine)
        assert engine.paused
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0].lower()
        assert "paused" in text

    @pytest.mark.asyncio
    async def test_start_bot_calls_resume(self):
        """/start_bot calls engine.resume()."""
        engine  = _make_engine()
        engine.pause()
        update  = _make_update()
        context = _make_context()

        assert engine.paused
        await handlers.handle_start_bot(update, context, engine)
        assert not engine.paused
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0].lower()
        assert "resumed" in text


# ── /start_drain ──────────────────────────────────────────────────────────────

class TestStartDrain:
    @pytest.mark.asyncio
    async def test_start_drain_sets_drain_mode(self, monkeypatch):
        """/start_drain sets config.DRAIN_MODE = True."""
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)
        engine  = _make_engine()
        update  = _make_update()
        context = _make_context()

        await handlers.handle_start_drain(update, context, engine)

        assert config.DRAIN_MODE is True
        text = update.message.reply_text.call_args[0][0].lower()
        assert "drain" in text

    @pytest.mark.asyncio
    async def test_start_drain_resumes_if_paused(self, monkeypatch):
        """/start_drain also resumes the engine if it was paused."""
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)
        engine  = _make_engine()
        engine.pause()
        update  = _make_update()
        context = _make_context()

        await handlers.handle_start_drain(update, context, engine)

        assert not engine.paused
        assert config.DRAIN_MODE is True


# ── /start_with_assets ────────────────────────────────────────────────────────

class TestStartWithAssets:
    @pytest.mark.asyncio
    async def test_updates_assets_and_resumes(self, monkeypatch):
        """/start_with_assets BTC,ETH updates config.ASSETS and resumes."""
        monkeypatch.setattr(config, "ASSETS", ["BTC"])
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)

        engine  = _make_engine()
        engine.pause()
        update  = _make_update()
        context = _make_context(args=["BTC,ETH,SOL"])

        await handlers.handle_start_with_assets(update, context, engine)

        assert config.ASSETS == ["BTC", "ETH", "SOL"]
        assert not engine.paused
        text = update.message.reply_text.call_args[0][0]
        assert "BTC" in text and "ETH" in text and "SOL" in text

    @pytest.mark.asyncio
    async def test_no_args_sends_usage(self):
        """/start_with_assets with no args replies with usage instructions."""
        engine  = _make_engine()
        update  = _make_update()
        context = _make_context(args=[])

        await handlers.handle_start_with_assets(update, context, engine)

        text = update.message.reply_text.call_args[0][0].lower()
        assert "usage" in text


# ── /drain_and_new ────────────────────────────────────────────────────────────

class TestDrainAndNew:
    @pytest.mark.asyncio
    async def test_sets_drain_and_new_mode(self, monkeypatch):
        """/drain_and_new sets DRAIN_AND_NEW_MODE and clears DRAIN_MODE."""
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)
        monkeypatch.setattr(config, "PORTFOLIO_OVERRIDE", None)

        engine  = _make_engine()
        update  = _make_update()
        context = _make_context(args=["portfolio=50000", "assets=BTC,ETH"])

        await handlers.handle_drain_and_new(update, context, engine)

        assert config.DRAIN_AND_NEW_MODE is True
        assert config.DRAIN_MODE is False
        assert config.PORTFOLIO_OVERRIDE == 50000.0
        assert config.ASSETS == ["BTC", "ETH"]

    @pytest.mark.asyncio
    async def test_portfolio_override_updates_engine(self, monkeypatch):
        """/drain_and_new portfolio=N also updates engine.portfolio_value."""
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)
        monkeypatch.setattr(config, "PORTFOLIO_OVERRIDE", None)

        engine  = _make_engine()
        update  = _make_update()
        context = _make_context(args=["portfolio=75000"])

        await handlers.handle_drain_and_new(update, context, engine)

        assert engine.portfolio_value == 75000.0

    @pytest.mark.asyncio
    async def test_invalid_portfolio_value_replies_error(self, monkeypatch):
        """/drain_and_new portfolio=abc replies with an error."""
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)

        engine  = _make_engine()
        update  = _make_update()
        context = _make_context(args=["portfolio=abc"])

        await handlers.handle_drain_and_new(update, context, engine)

        text = update.message.reply_text.call_args[0][0].lower()
        assert "invalid" in text

    @pytest.mark.asyncio
    async def test_resumes_if_paused(self, monkeypatch):
        """/drain_and_new resumes the engine if it was paused."""
        monkeypatch.setattr(config, "DRAIN_MODE", False)
        monkeypatch.setattr(config, "DRAIN_AND_NEW_MODE", False)
        monkeypatch.setattr(config, "PORTFOLIO_OVERRIDE", None)

        engine  = _make_engine()
        engine.pause()
        update  = _make_update()
        context = _make_context(args=[])

        await handlers.handle_drain_and_new(update, context, engine)

        assert not engine.paused


# ── /help handler and COMMAND_REGISTRY ───────────────────────────────────────

class TestHandleHelp:
    @pytest.mark.asyncio
    async def test_help_lists_all_commands(self):
        """/help reply contains every command in COMMAND_REGISTRY."""
        from telegram_cmd.listener import COMMAND_REGISTRY

        update  = _make_update()
        context = _make_context()

        await handlers.handle_help(update, context)

        text = update.message.reply_text.call_args[0][0]
        for cmd, _desc in COMMAND_REGISTRY:
            assert f"/{cmd}" in text, f"/{cmd} missing from /help reply"

    @pytest.mark.asyncio
    async def test_help_includes_descriptions(self):
        """/help reply includes descriptions for each command."""
        from telegram_cmd.listener import COMMAND_REGISTRY

        update  = _make_update()
        context = _make_context()

        await handlers.handle_help(update, context)

        text = update.message.reply_text.call_args[0][0]
        for _cmd, desc in COMMAND_REGISTRY:
            assert desc[:20] in text, f"Description '{desc[:20]}...' missing from /help"


class TestSetMyCommands:
    def test_command_registry_covers_all_handlers(self):
        """COMMAND_REGISTRY contains an entry for every registered command including /help."""
        from telegram_cmd.listener import COMMAND_REGISTRY

        command_names = {cmd for cmd, _ in COMMAND_REGISTRY}
        expected = {
            "positions", "close_trades", "new_trades", "status",
            "portfolio", "stop_bot", "start_bot", "start_drain",
            "start_with_assets", "drain_and_new", "help",
        }
        assert expected == command_names

    def test_command_registry_has_non_empty_descriptions(self):
        """Every entry in COMMAND_REGISTRY has a non-empty description."""
        from telegram_cmd.listener import COMMAND_REGISTRY

        for cmd, desc in COMMAND_REGISTRY:
            assert desc.strip(), f"/{cmd} has an empty description"

    @pytest.mark.asyncio
    async def test_set_my_commands_called_on_start(self):
        """set_my_commands is called during start() with the full COMMAND_REGISTRY."""
        from telegram_cmd.listener import COMMAND_REGISTRY, TelegramCommandListener

        engine   = _make_engine()
        cache    = _make_cache()
        listener = TelegramCommandListener(engine, cache)

        mock_bot = AsyncMock()
        mock_updater = MagicMock()
        mock_updater.running = False
        mock_updater.start_polling = AsyncMock()
        mock_updater.idle = AsyncMock()

        mock_app = MagicMock()
        mock_app.bot = mock_bot
        mock_app.updater = mock_updater
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.add_handler = MagicMock()

        mock_bot_command_cls = MagicMock(side_effect=lambda cmd, desc: MagicMock(command=cmd))
        mock_telegram_module = MagicMock()
        mock_telegram_module.BotCommand = mock_bot_command_cls

        with patch("config.TELEGRAM_TOKEN", "fake-token"), \
             patch.object(listener, "_build_app", return_value=mock_app), \
             patch.dict("sys.modules", {"telegram": mock_telegram_module}):
            await listener.start()

        mock_bot.set_my_commands.assert_called_once()
        called_commands = mock_bot.set_my_commands.call_args[0][0]
        assert len(called_commands) == len(COMMAND_REGISTRY)
        called_names = {c.command for c in called_commands}
        registry_names = {cmd for cmd, _ in COMMAND_REGISTRY}
        assert called_names == registry_names
