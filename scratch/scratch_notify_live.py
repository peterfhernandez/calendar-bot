"""
scratch/scratch_notify_live.py
==============================
Sends a real test alert via the configured SMTP and/or Telegram channel to
confirm end-to-end delivery.  Exits with a non-zero code if no channels are
configured so the result is unambiguous.

Run with:
    python -m scratch.scratch_notify_live

Prerequisites
-------------
- ALERT_EMAIL / SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS in .env for email.
- TELEGRAM_TOKEN / TELEGRAM_CHAT in .env for Telegram.

Safety guard
------------
This script aborts if TRADING_MODE == "live" — scratch scripts must never run
against the live exchange.
"""

import sys
import os

# ── Safety guard ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode. Set TRADING_MODE to 'paper' or 'test'.")
    sys.exit(1)

# ── Imports ───────────────────────────────────────────────────────────────────

import logging

from alerts.notifier import Notifier

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

# ── Check configuration ───────────────────────────────────────────────────────

email_configured    = bool(config.ALERT_EMAIL and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))
telegram_configured = bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT)

print("=" * 60)
print("Notification channels:")
print(f"  Email:    {'CONFIGURED → ' + config.ALERT_EMAIL if email_configured else 'NOT configured (set ALERT_EMAIL, SMTP_USER, SMTP_PASS in .env)'}")
print(f"  Telegram: {'CONFIGURED → chat ' + config.TELEGRAM_CHAT if telegram_configured else 'NOT configured (set TELEGRAM_TOKEN, TELEGRAM_CHAT in .env)'}")
print("=" * 60)

if not email_configured and not telegram_configured:
    print("\nNo alert channels configured — nothing to send.")
    print("To test email: set ALERT_EMAIL, SMTP_USER, SMTP_PASS in .env")
    print("To test Telegram: set TELEGRAM_TOKEN, TELEGRAM_CHAT in .env")
    sys.exit(1)

# ── Send test alerts ──────────────────────────────────────────────────────────

notifier = Notifier(cooldown_sec=0)   # cooldown=0 so the test always fires

print("\nSending startup notification…")
notifier.send(
    event_type="startup",
    subject="Test alert: Bot started (scratch_notify_live)",
    body=(
        "This is a test message from scratch_notify_live.py.\n"
        f"Trading mode: {config.TRADING_MODE}\n"
        "If you received this, end-to-end alert delivery is working."
    ),
)

print("\nSending entry notification…")
notifier.notify_entry(
    trade_id=9999,
    asset="BTC",
    option_type="Call",
    strike=100_000.0,
    qty=1.0,
    net_debit=250.0,
)

print("\nSending stop-loss notification…")
notifier.notify_stop(
    trade_id=9999,
    asset="BTC",
    strike=100_000.0,
    pnl=-125.0,
)

print("\nSending take-profit notification…")
notifier.notify_take_profit(
    trade_id=9998,
    asset="ETH",
    strike=3_000.0,
    pnl=+180.0,
)

print("\nSending roll notification…")
notifier.notify_roll(
    trade_id=9997,
    asset="BTC",
    strike=100_000.0,
    new_near_instrument="BTC-14JUL25-100000-C",
)

print("\nSending close notification…")
notifier.notify_close(
    trade_id=9996,
    asset="BTC",
    strike=98_000.0,
    pnl=+55.0,
    reason="Near leg expired",
)

print("\nSending daily-limit notification…")
notifier.notify_daily_limit(daily_pnl=-510.0)

print("\nSending error notification…")
notifier.notify_error(
    context="scratch_notify_live",
    exc=RuntimeError("Simulated test error"),
)

print("\nSending warning notification…")
notifier.notify_warning("Combo order timed out — fell back to individual legs (simulated test).")

print("\nAll test notifications dispatched.")
print("Check your inbox / Telegram chat to confirm delivery.")
