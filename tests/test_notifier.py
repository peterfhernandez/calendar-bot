"""
tests/test_notifier.py
======================
Unit tests for alerts/notifier.py.

All network calls (smtplib, aiohttp) are mocked so the tests run offline.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
from alerts.notifier import Notifier


class TestCooldownDeduplication(unittest.TestCase):
    """Alerts with the same key within the cooldown window should be suppressed."""

    def test_first_send_passes(self):
        n = Notifier(cooldown_sec=60)
        dispatched = []

        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram") as tg:
            n.send("test_event", "subject A", "body")
            em.assert_called_once()
            tg.assert_called_once()

    def test_duplicate_within_cooldown_suppressed(self):
        n = Notifier(cooldown_sec=60)

        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram") as tg:
            n.send("test_event", "subject A", "body")
            n.send("test_event", "subject A", "body")  # should be suppressed
            assert em.call_count == 1
            assert tg.call_count == 1

    def test_different_keys_both_sent(self):
        n = Notifier(cooldown_sec=60)

        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram") as tg:
            n.send("stop_loss", "subject A", "body")
            n.send("stop_loss", "subject B", "body")  # different subject → different key
            assert em.call_count == 2
            assert tg.call_count == 2

    def test_resend_after_cooldown(self):
        n = Notifier(cooldown_sec=1)

        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram"):
            n.send("test_event", "subject A", "body")
            time.sleep(1.05)
            n.send("test_event", "subject A", "body")
            assert em.call_count == 2


class TestHelperMethods(unittest.TestCase):
    """Helper convenience methods delegate to send() with the right arguments."""

    def _make_notifier_with_spy(self):
        n = Notifier()
        calls = []
        original = n.send

        def spy(event_type, subject, body):
            calls.append((event_type, subject, body))
            # prevent actual dispatch
            with patch.object(n, "_dispatch_email"), \
                 patch.object(n, "_dispatch_telegram"):
                original(event_type, subject, body)

        n.send = spy
        return n, calls

    def test_send_stop_loss(self):
        n = Notifier()
        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram"):
            n.send_stop_loss("BTC-27JUN25-60000-C", -120.5)
            em.assert_called_once()
            args = em.call_args[0]
            assert "stop_loss" in args[0].lower() or "stop" in args[0].lower()

    def test_send_take_profit(self):
        n = Notifier()
        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram"):
            n.send_take_profit("ETH-27JUN25-3500-P", 80.0)
            em.assert_called_once()

    def test_send_daily_limit(self):
        n = Notifier()
        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram"):
            n.send_daily_limit(520.0)
            em.assert_called_once()
            _, body = em.call_args[0]
            assert "520" in body

    def test_send_error(self):
        n = Notifier()
        with patch.object(n, "_dispatch_email") as em, \
             patch.object(n, "_dispatch_telegram"):
            n.send_error("scanner", "NoneType error at line 42")
            em.assert_called_once()


class TestEmailDispatch(unittest.TestCase):
    """Email path: correct MIME message, uses starttls, skips when unconfigured."""

    def test_skips_when_no_recipient(self):
        n = Notifier()
        original = config.ALERT_EMAIL
        config.ALERT_EMAIL = ""
        try:
            with patch("smtplib.SMTP") as mock_smtp:
                n._dispatch_email("Test Subject", "Test body")
                mock_smtp.assert_not_called()
        finally:
            config.ALERT_EMAIL = original

    def test_skips_when_no_smtp_credentials(self):
        n = Notifier()
        original = config.ALERT_EMAIL
        config.ALERT_EMAIL = "someone@example.com"
        try:
            import alerts.notifier as notifier_mod
            orig_user = notifier_mod._SMTP_USER
            orig_pass = notifier_mod._SMTP_PASS
            notifier_mod._SMTP_USER = ""
            notifier_mod._SMTP_PASS = ""
            with patch("smtplib.SMTP") as mock_smtp:
                n._dispatch_email("Subject", "Body")
                mock_smtp.assert_not_called()
        finally:
            config.ALERT_EMAIL = original
            notifier_mod._SMTP_USER = orig_user
            notifier_mod._SMTP_PASS = orig_pass

    def test_sends_email_with_correct_fields(self):
        n = Notifier()
        original_email = config.ALERT_EMAIL
        config.ALERT_EMAIL = "test@example.com"

        import alerts.notifier as notifier_mod
        orig_user = notifier_mod._SMTP_USER
        orig_pass = notifier_mod._SMTP_PASS
        notifier_mod._SMTP_USER = "bot@example.com"
        notifier_mod._SMTP_PASS = "secret"

        try:
            mock_smtp_instance = MagicMock()
            mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
            mock_smtp_instance.__exit__ = MagicMock(return_value=False)

            with patch("smtplib.SMTP", return_value=mock_smtp_instance):
                n._dispatch_email("Test Subject", "Test body")

            mock_smtp_instance.starttls.assert_called_once()
            mock_smtp_instance.login.assert_called_once_with("bot@example.com", "secret")
            assert mock_smtp_instance.sendmail.call_count == 1
            _, recipients, raw_msg = mock_smtp_instance.sendmail.call_args[0]
            assert "test@example.com" in recipients
            assert "[CalendarBot]" in raw_msg
            assert "Test Subject" in raw_msg
        finally:
            config.ALERT_EMAIL = original_email
            notifier_mod._SMTP_USER = orig_user
            notifier_mod._SMTP_PASS = orig_pass

    def test_email_exception_logged_not_raised(self):
        n = Notifier()
        original_email = config.ALERT_EMAIL
        config.ALERT_EMAIL = "test@example.com"

        import alerts.notifier as notifier_mod
        orig_user = notifier_mod._SMTP_USER
        orig_pass = notifier_mod._SMTP_PASS
        notifier_mod._SMTP_USER = "bot@example.com"
        notifier_mod._SMTP_PASS = "secret"

        try:
            with patch("smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "refused")):
                n._dispatch_email("Subject", "Body")  # must not raise
        finally:
            config.ALERT_EMAIL = original_email
            notifier_mod._SMTP_USER = orig_user
            notifier_mod._SMTP_PASS = orig_pass


class TestTelegramDispatch(unittest.TestCase):
    """Telegram path: posts to correct URL, skips when unconfigured."""

    def test_skips_when_no_token(self):
        n = Notifier()
        original_token = config.TELEGRAM_TOKEN
        original_chat  = config.TELEGRAM_CHAT
        config.TELEGRAM_TOKEN = ""
        config.TELEGRAM_CHAT  = "12345"
        try:
            with patch("aiohttp.ClientSession") as mock_session:
                n._dispatch_telegram("Subject", "Body")
                mock_session.assert_not_called()
        finally:
            config.TELEGRAM_TOKEN = original_token
            config.TELEGRAM_CHAT  = original_chat

    def test_skips_when_no_chat(self):
        n = Notifier()
        original_token = config.TELEGRAM_TOKEN
        original_chat  = config.TELEGRAM_CHAT
        config.TELEGRAM_TOKEN = "abc:token"
        config.TELEGRAM_CHAT  = ""
        try:
            with patch("aiohttp.ClientSession") as mock_session:
                n._dispatch_telegram("Subject", "Body")
                mock_session.assert_not_called()
        finally:
            config.TELEGRAM_TOKEN = original_token
            config.TELEGRAM_CHAT  = original_chat

    def test_post_telegram_success(self):
        """_post_telegram calls the Telegram Bot API with correct payload."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__  = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__  = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            asyncio.run(Notifier._post_telegram("TOKEN", "CHAT_ID", "hello"))

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert "sendMessage" in call_kwargs[0][0]
        payload = call_kwargs[1]["json"]
        assert payload["chat_id"] == "CHAT_ID"
        assert "hello" in payload["text"]

    def test_post_telegram_api_error_logged_not_raised(self):
        """A non-200 response is logged but does not raise."""
        mock_response = AsyncMock()
        mock_response.status = 400
        mock_response.text = AsyncMock(return_value='{"error": "bad request"}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__  = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__  = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            asyncio.run(Notifier._post_telegram("TOKEN", "CHAT_ID", "hello"))


if __name__ == "__main__":
    unittest.main()
