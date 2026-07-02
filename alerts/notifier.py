"""
alerts/notifier.py
==================
Alert dispatcher for the calendar spread bot.

Supports two channels:
  - Email  via smtplib (SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS env vars)
  - Telegram via the Bot API (TELEGRAM_TOKEN / TELEGRAM_CHAT config values)

Both channels are optional: if credentials are absent the channel is silently
skipped.  Each channel is configured through config.py and the .env file.

Deduplication
-------------
Each alert has a *key* (event_type + subject).  A cooldown window
(default 300 s) prevents the same alert from being re-sent within that window.

Public API
----------
Notifier(cooldown_sec=300)
    send(event_type, subject, body)  — dispatches to all configured channels
    send_stop_loss(instrument, pnl)
    send_take_profit(instrument, pnl)
    send_daily_limit(current_loss)
    send_error(context, detail)

All methods are thread-safe.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import threading
import time
from email.mime.text import MIMEText
from typing import Any

import aiohttp

import config

logger = logging.getLogger(__name__)

# ── SMTP settings (read from .env / environment) ──────────────────────────────

import os as _os

_SMTP_HOST = _os.environ.get("SMTP_HOST", "smtp.gmail.com")
_SMTP_PORT = int(_os.environ.get("SMTP_PORT", "587"))
_SMTP_USER = _os.environ.get("SMTP_USER", "")
_SMTP_PASS = _os.environ.get("SMTP_PASS", "")
_SMTP_FROM = _os.environ.get("SMTP_FROM", _SMTP_USER)


class Notifier:
    """
    Dispatches alerts to email and/or Telegram with per-key cooldown deduplication.

    Parameters
    ----------
    cooldown_sec : int
        Minimum seconds between two alerts with the same (event_type, subject) key.
    """

    def __init__(self, cooldown_sec: int = 300) -> None:
        self._cooldown = cooldown_sec
        self._sent_at: dict[str, float] = {}   # key → last-sent timestamp
        self._lock = threading.Lock()

    # ── Public helpers ────────────────────────────────────────────────────────

    # ── Decision-engine helpers ───────────────────────────────────────────────

    def notify_entry(
        self,
        trade_id: int,
        asset: str,
        option_type: str,
        strike: float,
        qty: float,
        net_debit: float,
    ) -> None:
        """Alert when a new calendar spread position is entered."""
        instrument = f"{asset} {option_type} strike={strike:.0f}"
        self.send(
            event_type="entry",
            subject=f"Position entered: {instrument}",
            body=(
                f"Trade #{trade_id} entered.\n"
                f"  Asset:     {asset} {option_type}\n"
                f"  Strike:    {strike:,.0f}\n"
                f"  Qty:       {qty}\n"
                f"  Net debit: ${net_debit:.4f}"
            ),
        )

    def notify_stop(self, trade_id: int, asset: str, strike: float, pnl: float) -> None:
        """Alert when a stop-loss triggers."""
        instrument = f"{asset} strike={strike:.0f}"
        self.send(
            event_type="stop_loss",
            subject=f"Stop-loss triggered: {instrument} (trade #{trade_id})",
            body=(
                f"Trade #{trade_id} hit the stop-loss threshold and was closed.\n"
                f"  Instrument: {instrument}\n"
                f"  Realised P&L: ${pnl:+.2f}"
            ),
        )

    def notify_take_profit(self, trade_id: int, asset: str, strike: float, pnl: float) -> None:
        """Alert when a take-profit triggers."""
        instrument = f"{asset} strike={strike:.0f}"
        self.send(
            event_type="take_profit",
            subject=f"Take-profit triggered: {instrument} (trade #{trade_id})",
            body=(
                f"Trade #{trade_id} hit the take-profit threshold and was closed.\n"
                f"  Instrument: {instrument}\n"
                f"  Realised P&L: ${pnl:+.2f}"
            ),
        )

    def notify_roll(self, trade_id: int, asset: str, strike: float, new_near_instrument: str) -> None:
        """Alert when a near leg is rolled."""
        self.send(
            event_type="roll",
            subject=f"Near leg rolled: {asset} strike={strike:.0f} (trade #{trade_id})",
            body=(
                f"Trade #{trade_id} near leg rolled to a new expiry.\n"
                f"  Asset:              {asset}\n"
                f"  Strike:             {strike:,.0f}\n"
                f"  New near instrument: {new_near_instrument}"
            ),
        )

    def notify_close(self, trade_id: int, asset: str, strike: float, pnl: float, reason: str) -> None:
        """Alert when a position is closed (expiry, roll-fail, or manual close)."""
        instrument = f"{asset} strike={strike:.0f}"
        self.send(
            event_type="close",
            subject=f"Position closed: {instrument} (trade #{trade_id})",
            body=(
                f"Trade #{trade_id} closed.\n"
                f"  Instrument:   {instrument}\n"
                f"  Reason:       {reason}\n"
                f"  Realised P&L: ${pnl:+.2f}"
            ),
        )

    def notify_close_stuck(
        self,
        trade_id: int,
        asset: str,
        strike: float,
        reason: str,
        error: str,
    ) -> None:
        """Alert when a position close fails repeatedly and needs manual intervention."""
        instrument = f"{asset} strike={strike:.0f}"
        self.send(
            event_type="close_stuck",
            subject=f"⚠️  MANUAL ACTION REQUIRED: Position close failed (trade #{trade_id})",
            body=(
                f"Trade #{trade_id} close failed after multiple attempts.\n\n"
                f"  Instrument:   {instrument}\n"
                f"  Close reason: {reason}\n"
                f"  Error:        {error}\n\n"
                f"The position is still open on Deribit and needs manual intervention.\n\n"
                f"Use one of these commands:\n"
                f"  • `/info trade_id={trade_id}` — Check current position status on Deribit\n"
                f"  • `/close trade_id={trade_id}` — Retry automatic close\n"
                f"  • `/close_manually trade_id={trade_id} spread=VALUE` — Manually close with known spread value"
            ),
        )

    def notify_daily_limit(self, daily_pnl: float) -> None:
        """Alert when the daily loss limit is breached and the bot halts."""
        self.send(
            event_type="daily_limit",
            subject="Daily loss limit breached — bot halted",
            body=(
                f"Cumulative daily P&L ${daily_pnl:.2f} breached the "
                f"configured limit of -${config.DAILY_LOSS_LIMIT:.2f}.\n"
                "All trading has been halted for the remainder of the day."
            ),
        )

    def notify_error(self, context: str, exc: Exception) -> None:
        """Alert on unexpected runtime errors."""
        self.send(
            event_type="error",
            subject=f"Bot error: {context}",
            body=f"An error occurred in {context}:\n\n{type(exc).__name__}: {exc}",
        )

    def notify_warning(self, msg: str) -> None:
        """Alert for recoverable but notable events (e.g. combo fallback used)."""
        self.send(
            event_type="warning",
            subject=f"Bot warning: {msg[:80]}",
            body=msg,
        )

    def notify_startup(self, trading_mode: str, assets: list, exchange_url: str) -> None:
        """Alert sent on bot startup. Failures are logged but must not abort startup."""
        self.send(
            event_type="startup",
            subject=f"Bot started ({trading_mode} mode)",
            body=(
                f"Calendar Spread Bot started in {trading_mode.upper()} mode.\n"
                f"Assets: {assets}\n"
                f"Exchange: {exchange_url.split('://', 1)[-1]}"
            ),
        )

    # ── Legacy helpers (kept for backward compatibility) ──────────────────────

    def send_stop_loss(self, instrument: str, pnl: float) -> None:
        """Alert when a position hits the stop-loss threshold."""
        self.send(
            event_type="stop_loss",
            subject=f"Stop-loss triggered: {instrument}",
            body=(
                f"Position {instrument} hit the stop-loss threshold.\n"
                f"Realised P&L: ${pnl:+.2f}"
            ),
        )

    def send_take_profit(self, instrument: str, pnl: float) -> None:
        """Alert when a position hits the take-profit threshold."""
        self.send(
            event_type="take_profit",
            subject=f"Take-profit triggered: {instrument}",
            body=(
                f"Position {instrument} hit the take-profit threshold.\n"
                f"Realised P&L: ${pnl:+.2f}"
            ),
        )

    def send_daily_limit(self, current_loss: float) -> None:
        """Alert when the daily loss limit is breached."""
        self.send(
            event_type="daily_limit",
            subject="Daily loss limit breached — bot halted",
            body=(
                f"Cumulative daily loss ${current_loss:.2f} exceeded the "
                f"configured limit of ${config.DAILY_LOSS_LIMIT:.2f}.\n"
                "All trading has been halted for the remainder of the day."
            ),
        )

    def send_error(self, context: str, detail: str) -> None:
        """Alert on unexpected runtime errors."""
        self.send(
            event_type="error",
            subject=f"Bot error: {context}",
            body=f"An error occurred in {context}:\n\n{detail}",
        )

    # ── Core dispatch ─────────────────────────────────────────────────────────

    def send(self, event_type: str, subject: str, body: str) -> None:
        """
        Dispatch an alert to all configured channels.

        Deduplication: if the same (event_type, subject) key was sent within
        `cooldown_sec` seconds, the call is a no-op and a DEBUG log is written.

        Parameters
        ----------
        event_type : str
            Machine-readable category, e.g. "stop_loss", "error".
        subject : str
            Human-readable subject line.
        body : str
            Full alert body text (plain text).
        """
        key = f"{event_type}:{subject}"
        now = time.monotonic()

        with self._lock:
            last = self._sent_at.get(key, 0.0)
            if now - last < self._cooldown:
                remaining = int(self._cooldown - (now - last))
                logger.debug(
                    "Alert suppressed (cooldown %ds remaining): %s", remaining, subject
                )
                return
            self._sent_at[key] = now

        logger.info("Sending alert [%s]: %s", event_type, subject)

        self._dispatch_email(subject, body)
        self._dispatch_telegram(subject, body)

    # ── Email ─────────────────────────────────────────────────────────────────

    def _dispatch_email(self, subject: str, body: str) -> None:
        recipient = getattr(config, "ALERT_EMAIL", "")
        if not recipient:
            return
        if not _SMTP_USER or not _SMTP_PASS:
            logger.warning(
                "Email alert skipped: SMTP_USER / SMTP_PASS not configured"
            )
            return

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[CalendarBot] {subject}"
        msg["From"]    = _SMTP_FROM or _SMTP_USER
        msg["To"]      = recipient

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.login(_SMTP_USER, _SMTP_PASS)
                smtp.sendmail(msg["From"], [recipient], msg.as_string())
            logger.info("Email sent to %s", recipient)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send email alert: %s", exc)

    # ── Telegram ──────────────────────────────────────────────────────────────

    def _dispatch_telegram(self, subject: str, body: str) -> None:
        token = getattr(config, "TELEGRAM_TOKEN", "")
        chat  = getattr(config, "TELEGRAM_CHAT",  "")
        if not token or not chat:
            logger.debug("Telegram alert skipped: token or chat not configured")
            return

        text = f"*[CalendarBot]* {subject}\n\n{body}"

        # Fire-and-forget: schedule on any running event loop, or run a new one
        # in a background thread if none is available.
        try:
            loop = asyncio.get_running_loop()
            # Create task with error handling callback
            task = loop.create_task(self._post_telegram(token, chat, text, subject))
            task.add_done_callback(lambda t: self._log_telegram_result(t, subject))
        except RuntimeError:
            # No running event loop — create one in a background thread
            threading.Thread(
                target=lambda: asyncio.run(self._post_telegram(token, chat, text, subject)),
                daemon=True,
            ).start()

    def _log_telegram_result(self, task, subject: str) -> None:
        """Log the result of an async Telegram send task."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass  # task was cancelled, not an error
        except Exception as exc:  # noqa: BLE001
            logger.error("Telegram notification for '%s' failed: %s", subject, exc)

    @staticmethod
    async def _post_telegram(token: str, chat: str, text: str, subject: str = "") -> bool:
        """
        Send a message to Telegram.

        Returns True on success, raises an exception on failure.
        Retries once on network errors.
        """
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat, "text": text, "parse_mode": "Markdown"}

        # Retry logic: try twice
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("ok"):
                                logger.info("Telegram message sent to chat %s (subject: %s)", chat, subject)
                                return True
                            else:
                                error_msg = data.get("description", "unknown error")
                                if attempt == 0:
                                    logger.warning("Telegram API error (retrying): %s", error_msg)
                                    await asyncio.sleep(1)
                                    continue
                                else:
                                    raise RuntimeError(f"Telegram API error: {error_msg}")
                        else:
                            body = await resp.text()
                            if attempt == 0:
                                logger.warning("Telegram HTTP %d (retrying): %s", resp.status, body[:200])
                                await asyncio.sleep(1)
                                continue
                            else:
                                raise RuntimeError(f"Telegram HTTP {resp.status}: {body[:200]}")
            except asyncio.TimeoutError:
                if attempt == 0:
                    logger.warning("Telegram timeout (retrying)...")
                    await asyncio.sleep(1)
                    continue
                else:
                    raise RuntimeError("Telegram API timeout (both attempts)")
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    logger.warning("Telegram network error (retrying): %s", exc)
                    await asyncio.sleep(1)
                    continue
                else:
                    raise

        raise RuntimeError("Telegram send failed after 2 attempts")
