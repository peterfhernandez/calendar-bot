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
    get_trades_closed_today,
    get_trades_opened_today,
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
    expiry_near: str = "27JUN26",
    expiry_far: str = "25JUL26",
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
        notes=None,
        near_instrument=near_instrument,
        far_instrument=far_instrument,
        date_close=date_close,
        spot_close=None,
        pnl=pnl,
    )


def _make_update(chat_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    return MagicMock()


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
    async def test_positions_with_live_price(self):
        """Reply contains instrument pair and unrealized PnL."""
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trade = _make_trade()
        with patch("telegram_cmd.handlers.get_open_trades", return_value=[trade]):
            await handlers.handle_positions(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "BTC" in text
        assert "90000" in text
        assert "entry=" in text.lower() or "entry" in text

    @pytest.mark.asyncio
    async def test_positions_stale_cache(self):
        """When cache returns None for an instrument, reply includes a stale note."""
        update  = _make_update()
        context = _make_context()
        cache   = MagicMock()
        cache.get.return_value = None  # all instruments stale
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trade = _make_trade()
        with patch("telegram_cmd.handlers.get_open_trades", return_value=[trade]):
            await handlers.handle_positions(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "stale" in text.lower() or "N/A" in text


# ── /closed_today handler ─────────────────────────────────────────────────────

class TestHandleClosedToday:
    @pytest.mark.asyncio
    async def test_no_closed_today(self):
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        init_db(db_path)

        with patch("telegram_cmd.handlers.get_trades_closed_today", return_value=[]):
            await handlers.handle_closed_today(update, context, db_path)

        text = update.message.reply_text.call_args[0][0].lower()
        assert "no trades" in text

    @pytest.mark.asyncio
    async def test_closed_today_with_pnl(self):
        """Reply includes count and total realized PnL."""
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trades = [
            _make_trade(trade_id=1, result="Win (Auto TP)", pnl=50.0,
                        date_close="2026-06-26"),
            _make_trade(trade_id=2, result="Loss (Auto Stop)", pnl=-20.0,
                        date_close="2026-06-26"),
        ]
        with patch("telegram_cmd.handlers.get_trades_closed_today", return_value=trades):
            await handlers.handle_closed_today(update, context, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "2" in text
        assert "30" in text or "+30" in text  # total PnL = 50 - 20 = 30


# ── /new_today handler ────────────────────────────────────────────────────────

class TestHandleNewToday:
    @pytest.mark.asyncio
    async def test_no_new_today(self):
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))
        init_db(db_path)

        with patch("telegram_cmd.handlers.get_trades_opened_today", return_value=[]):
            await handlers.handle_new_today(update, context, db_path)

        text = update.message.reply_text.call_args[0][0].lower()
        assert "no new" in text

    @pytest.mark.asyncio
    async def test_new_today_lists_instruments(self):
        """Reply includes asset names and count."""
        update  = _make_update()
        context = _make_context()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        trades = [_make_trade(trade_id=1), _make_trade(trade_id=2, asset="ETH")]
        with patch("telegram_cmd.handlers.get_trades_opened_today", return_value=trades):
            await handlers.handle_new_today(update, context, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "2" in text
        assert "BTC" in text
        assert "ETH" in text


# ── /status handler ───────────────────────────────────────────────────────────

class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_status_contains_mode_drain_paused(self, monkeypatch):
        """Reply shows trading mode, drain mode, and paused flag."""
        monkeypatch.setattr(config, "TRADING_MODE", "paper")
        monkeypatch.setattr(config, "DRAIN_MODE", False)

        engine  = _make_engine()
        update  = _make_update()
        context = _make_context()

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[]):
            await handlers.handle_status(update, context, engine)

        text = update.message.reply_text.call_args[0][0].upper()
        assert "PAPER" in text
        assert "DRAIN" in text
        assert "PAUSED" in text

    @pytest.mark.asyncio
    async def test_status_shows_paused_when_paused(self, monkeypatch):
        """Reply reflects paused=YES when engine is paused."""
        monkeypatch.setattr(config, "TRADING_MODE", "paper")
        monkeypatch.setattr(config, "DRAIN_MODE", False)

        engine = _make_engine()
        engine.pause()
        update  = _make_update()
        context = _make_context()

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[]):
            await handlers.handle_status(update, context, engine)

        text = update.message.reply_text.call_args[0][0]
        assert "YES" in text or "yes" in text.lower()

    @pytest.mark.asyncio
    async def test_status_shows_open_count(self, monkeypatch):
        """Reply includes the number of open positions."""
        monkeypatch.setattr(config, "TRADING_MODE", "paper")
        monkeypatch.setattr(config, "DRAIN_MODE", False)

        engine  = _make_engine()
        update  = _make_update()
        context = _make_context()

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[_make_trade(), _make_trade(trade_id=2)]):
            await handlers.handle_status(update, context, engine)

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
    async def test_portfolio_includes_iv_and_oi(self):
        """Reply includes IV and OI for each leg."""
        update  = _make_update()
        context = _make_context()
        cache   = _make_cache()
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[_make_trade()]):
            await handlers.handle_portfolio(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "IV" in text
        assert "OI" in text

    @pytest.mark.asyncio
    async def test_portfolio_stale_cache_note(self):
        """Reply includes '(cache stale)' when leg data is unavailable."""
        update  = _make_update()
        context = _make_context()
        cache   = MagicMock()
        cache.get.return_value = None
        db_path = Path(tempfile.mktemp(suffix=".db"))

        with patch("telegram_cmd.handlers.get_open_trades", return_value=[_make_trade()]):
            await handlers.handle_portfolio(update, context, cache, db_path)

        text = update.message.reply_text.call_args[0][0]
        assert "stale" in text.lower()


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
        engine  = _make_engine()
        engine.pause()
        update  = _make_update()
        context = _make_context()

        await handlers.handle_start_drain(update, context, engine)

        assert not engine.paused
        assert config.DRAIN_MODE is True
