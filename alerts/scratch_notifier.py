"""
alerts/scratch_notifier.py
===========================
End-to-end verification script for the Notifier.

Runs a series of checks without connecting to any real SMTP server or Telegram
API.  All network calls are intercepted and printed so you can verify the
correct payloads are being built.

Run from the repo root:
    python -m alerts.scratch_notifier

What it checks
--------------
1. Basic send() dispatches to both channels
2. Cooldown deduplication suppresses repeated alerts
3. Cooldown resets after the window expires
4. All four helper methods (stop_loss, take_profit, daily_limit, error)
5. Email skipped when ALERT_EMAIL is blank
6. Telegram skipped when token/chat is blank
7. Email payload contains expected fields
8. Telegram payload contains expected fields
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure repo root is on the path
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import aiohttp
import config
import alerts.notifier as notifier_mod
from alerts.notifier import Notifier

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

results: list[tuple[str, bool, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    results.append((label, condition, detail))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _smtp_mock():
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__  = MagicMock(return_value=False)
    return m

def _telegram_mock(status: int = 200):
    resp = AsyncMock()
    resp.status = status
    resp.text   = AsyncMock(return_value="ok")
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__  = AsyncMock(return_value=False)

    session = AsyncMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__  = AsyncMock(return_value=False)
    return session


def with_smtp_creds(fn):
    """Decorator: temporarily inject SMTP credentials and a recipient."""
    def wrapper(*args, **kwargs):
        orig_email = config.ALERT_EMAIL
        orig_user  = notifier_mod._SMTP_USER
        orig_pass  = notifier_mod._SMTP_PASS
        config.ALERT_EMAIL      = "recipient@example.com"
        notifier_mod._SMTP_USER = "bot@example.com"
        notifier_mod._SMTP_PASS = "s3cr3t"
        try:
            return fn(*args, **kwargs)
        finally:
            config.ALERT_EMAIL      = orig_email
            notifier_mod._SMTP_USER = orig_user
            notifier_mod._SMTP_PASS = orig_pass
    return wrapper


def with_tg_creds(fn):
    """Decorator: temporarily inject Telegram credentials."""
    def wrapper(*args, **kwargs):
        orig_token = config.TELEGRAM_TOKEN
        orig_chat  = config.TELEGRAM_CHAT
        config.TELEGRAM_TOKEN = "123:TESTTOKEN"
        config.TELEGRAM_CHAT  = "99999"
        try:
            return fn(*args, **kwargs)
        finally:
            config.TELEGRAM_TOKEN = orig_token
            config.TELEGRAM_CHAT  = orig_chat
    return wrapper


# ── Test sections ──────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


@with_smtp_creds
@with_tg_creds
def test_basic_dispatch():
    section("1. Basic dispatch — both channels called")
    n = Notifier()
    smtp_mock     = _smtp_mock()
    tg_dispatched = []

    def fake_tg(subject, body):
        tg_dispatched.append((subject, body))

    with patch("smtplib.SMTP", return_value=smtp_mock), \
         patch.object(n, "_dispatch_telegram", side_effect=fake_tg):
        n.send("test_event", "Hello World", "This is a test body.")

    check("Email: starttls called",       smtp_mock.starttls.call_count == 1)
    check("Email: sendmail called",       smtp_mock.sendmail.call_count == 1)
    check("Telegram dispatch called",     len(tg_dispatched) == 1)


@with_smtp_creds
@with_tg_creds
def test_cooldown_deduplication():
    section("2. Cooldown deduplication")
    n = Notifier(cooldown_sec=5)
    smtp_mock = _smtp_mock()

    with patch("smtplib.SMTP", return_value=smtp_mock), \
         patch.object(n, "_dispatch_telegram"):
        n.send("stop_loss", "BTC position closed", "body")
        n.send("stop_loss", "BTC position closed", "body")  # same key — suppressed
        n.send("stop_loss", "BTC position closed", "body")  # still suppressed

    check("Email sent exactly once (2nd+3rd suppressed)", smtp_mock.sendmail.call_count == 1)


@with_smtp_creds
@with_tg_creds
def test_cooldown_expires():
    section("3. Cooldown resets after window expires")
    n = Notifier(cooldown_sec=1)
    smtp_mock  = _smtp_mock()

    with patch("smtplib.SMTP", return_value=smtp_mock), \
         patch.object(n, "_dispatch_telegram"):
        n.send("error", "Reconnect failed", "body")

    time.sleep(1.1)

    smtp_mock2 = _smtp_mock()
    with patch("smtplib.SMTP", return_value=smtp_mock2), \
         patch.object(n, "_dispatch_telegram"):
        n.send("error", "Reconnect failed", "body")

    check("Second send after cooldown goes through", smtp_mock2.sendmail.call_count == 1)


@with_smtp_creds
@with_tg_creds
def test_helper_methods():
    section("4. Helper methods produce correct subjects")

    cases = [
        ("send_stop_loss",   ("BTC-27JUN25-60000-C", -120.5), "stop"),
        ("send_take_profit", ("ETH-27JUN25-3500-P",   80.0),  "take"),
        ("send_daily_limit", (520.0,),                         "daily"),
        ("send_error",       ("scanner", "NoneType"),          "error"),
    ]

    for method_name, args, expected_fragment in cases:
        n = Notifier()
        smtp_mock  = _smtp_mock()
        tg_session = _telegram_mock()

        with patch("smtplib.SMTP", return_value=smtp_mock), \
             patch("aiohttp.ClientSession", return_value=tg_session):
            getattr(n, method_name)(*args)

        _, _, raw_msg = smtp_mock.sendmail.call_args[0]
        check(
            f"{method_name}: subject contains '{expected_fragment}'",
            expected_fragment.lower() in raw_msg.lower(),
            raw_msg[:80].replace("\n", " "),
        )


def test_email_skipped_no_recipient():
    section("5. Email skipped when ALERT_EMAIL is blank")
    n = Notifier()
    original = config.ALERT_EMAIL
    config.ALERT_EMAIL = ""
    try:
        with patch("smtplib.SMTP") as mock_smtp:
            n._dispatch_email("Subject", "Body")
        check("SMTP never instantiated", mock_smtp.call_count == 0)
    finally:
        config.ALERT_EMAIL = original


def test_telegram_skipped_no_credentials():
    section("6. Telegram skipped when token / chat blank")
    n = Notifier()
    orig_token = config.TELEGRAM_TOKEN
    orig_chat  = config.TELEGRAM_CHAT
    config.TELEGRAM_TOKEN = ""
    config.TELEGRAM_CHAT  = "12345"
    try:
        with patch("aiohttp.ClientSession") as mock_sess:
            n._dispatch_telegram("Subject", "Body")
        check("No aiohttp session created (no token)", mock_sess.call_count == 0)
    finally:
        config.TELEGRAM_TOKEN = orig_token
        config.TELEGRAM_CHAT  = orig_chat


@with_smtp_creds
def test_email_payload():
    section("7. Email payload fields")
    n = Notifier()
    smtp_mock = _smtp_mock()

    with patch("smtplib.SMTP", return_value=smtp_mock), \
         patch.object(n, "_dispatch_telegram"):
        n.send("stop_loss", "Position closed at stop", "PnL: -120 USD")

    _, recipients, raw_msg = smtp_mock.sendmail.call_args[0]

    # MIME body may be base64-encoded; decode to get the plaintext
    import email as _email
    import base64 as _base64
    parsed = _email.message_from_string(raw_msg)
    payload = parsed.get_payload(decode=True)
    body_text = payload.decode("utf-8") if payload else raw_msg

    check("Recipient is correct",           "recipient@example.com" in recipients)
    check("[CalendarBot] tag in headers",   "[CalendarBot]" in raw_msg)
    check("Subject line present",          "Position closed at stop" in raw_msg)
    check("Body text present",             "PnL: -120 USD" in body_text)


@with_tg_creds
def test_telegram_payload():
    section("8. Telegram payload fields")

    received_payload: dict[str, Any] = {}

    async def fake_post_telegram(token, chat, text):
        received_payload["token"] = token
        received_payload["chat"]  = chat
        received_payload["text"]  = text

    n = Notifier()
    with patch.object(n, "_dispatch_email"), \
         patch("alerts.notifier.Notifier._post_telegram", side_effect=fake_post_telegram):
        n.send("take_profit", "ETH hit TP", "P&L: +$200")

    # Give any spawned thread a moment to run
    time.sleep(0.1)

    check("Token passed correctly",         received_payload.get("token") == "123:TESTTOKEN")
    check("Chat ID passed correctly",       received_payload.get("chat")  == "99999")
    check("Text contains [CalendarBot]",    "[CalendarBot]" in received_payload.get("text", ""))
    check("Text contains subject",         "ETH hit TP" in received_payload.get("text", ""))


# ── Live Telegram send ─────────────────────────────────────────────────────────

def test_live_telegram():
    """Send a real Telegram message using credentials from config / .env."""
    section("9. Live Telegram send (requires TELEGRAM_TOKEN + TELEGRAM_CHAT in .env)")

    token = config.TELEGRAM_TOKEN
    chat  = config.TELEGRAM_CHAT

    if not token or not chat:
        print("  [SKIP] TELEGRAM_TOKEN or TELEGRAM_CHAT not set — skipping live send")
        return

    # Show masked credentials so the user can verify what's loaded
    masked_token = token[:10] + "..." + token[-4:] if len(token) > 14 else "(too short)"
    print(f"  Token : {masked_token}")
    print(f"  Chat  : {chat}")

    holder: dict[str, Any] = {}

    async def _run():
        async with aiohttp.ClientSession() as session:
            # Step 1: verify the token with getMe
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                holder["me_status"] = resp.status
                holder["me_body"]   = await resp.text()

            if holder["me_status"] != 200:
                return  # no point trying sendMessage

            # Step 2: send the message
            async with session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat,
                    "text": "[CalendarBot] Scratch script live test — if you see this, alerts are working!",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                holder["send_status"] = resp.status
                holder["send_body"]   = await resp.text()

    asyncio.run(_run())

    me_status   = holder.get("me_status", 0)
    me_body     = holder.get("me_body", "")
    send_status = holder.get("send_status")
    send_body   = holder.get("send_body", "")

    if me_status != 200:
        check(
            "Token valid (getMe)",
            False,
            f"HTTP {me_status}: {me_body[:120]} — check TELEGRAM_TOKEN in .env",
        )
        return

    check("Token valid (getMe)", True, me_body[:80])

    ok     = send_status == 200
    detail = "message delivered — check your Telegram" if ok else f"HTTP {send_status}: {send_body[:120]}"
    check("Message sent to chat (sendMessage)", ok, detail)


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Calendar-Bot Notifier — Verification Script")
    print("="*60)

    test_basic_dispatch()
    test_cooldown_deduplication()
    test_cooldown_expires()
    test_helper_methods()
    test_email_skipped_no_recipient()
    test_telegram_skipped_no_credentials()
    test_email_payload()
    test_telegram_payload()
    test_live_telegram()

    total  = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed

    print("\n" + "="*60)
    print(f"  Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)")
        for label, ok, detail in results:
            if not ok:
                print(f"    FAIL: {label}" + (f" — {detail}" if detail else ""))
    else:
        print("  — all checks passed")
    print("="*60 + "\n")

    sys.exit(0 if failed == 0 else 1)
