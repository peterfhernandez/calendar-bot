"""
scratch/scratch_close_retry_stuck.py
====================================
Demonstrates the Phase 19 close-retry restoration and the notifier
boot-window cooldown fix.

Sections
--------
1. Close-failure retry ladder — a position whose close keeps failing is
   retried 3 times (counter 1 → 2 → 3), then marked close_stuck on the
   4th tick with exactly ONE operator notification, after which it is
   excluded from routine monitoring.
2. /close-style operator reset — clearing the stuck flag and the retry
   counter puts the position back into normal monitoring with a fresh
   set of retry attempts.
3. Notifier boot-window fix — the first alert is dispatched even when
   time.monotonic() is smaller than the cooldown window (i.e. the host
   booted moments before the bot started).

Everything runs offline against a temporary database with a fake
executor — no orders are placed and no network connections are made.
Refuses to run when TRADING_MODE == "live" (project convention).

Run with:  python -m scratch.scratch_close_retry_stuck
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import config

if config.TRADING_MODE == "live":
    print("ABORT: scratch scripts must never run in live mode.")
    sys.exit(1)

from db.state import (
    create_calendar_trade,
    get_open_trades,
    init_db,
    reset_close_stuck_position,
)
from strategy.decision import DecisionEngine


def _past_label(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _future_label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="scratch_close_retry_")
    db_path = Path(tmp) / "scratch_close_retry.db"
    init_db(db_path)

    # A BTC calendar whose near leg expired 2 days ago — every monitor tick
    # will try to close it, and our fake executor will refuse.
    near_label = _past_label(2)
    far_label = _future_label(30)
    trade = create_calendar_trade(
        asset="BTC",
        date_open=date.today() - timedelta(days=10),
        option_type="Call",
        strike=59_000.0,
        expiry_near=near_label,
        expiry_far=far_label,
        near_days=-2,
        far_days=30,
        qty=1.0,
        spot_open=60_000.0,
        near_prem=0.01,
        far_prem=0.025,
        net_debit=0.02,
        near_instrument=f"BTC-{near_label}-59000-C",
        far_instrument=f"BTC-{far_label}-59000-C",
        db_path=db_path,
    )

    failing_executor = MagicMock()
    failing_executor.close_spread.return_value = None  # every close fails

    notifier = MagicMock()

    cache = MagicMock()
    cache.get_spot.return_value = 60_000.0
    cache.get_chain.return_value = []

    engine = DecisionEngine(
        cache=cache,
        portfolio_value=10_000.0,
        executor=failing_executor,
        db_path=db_path,
    )
    engine._notifier = notifier

    print("=" * 72)
    print("Section 1 — close-failure retry ladder")
    print("=" * 72)
    for tick in range(1, 5):
        status = engine.monitor_tick()
        counter = engine._close_roll_failures.get(trade.id)
        stuck_alerts = notifier.notify_close_stuck.call_count
        print(
            f"tick {tick}: counter={counter}  "
            f"stuck_alerts_sent={stuck_alerts}  msg={status.message!r}"
        )

    open_now = get_open_trades(db_path)
    assert not open_now, "stuck position should be excluded from monitoring"
    assert notifier.notify_close_stuck.call_count == 1, "exactly one alert"
    assert trade.id not in engine._close_roll_failures, "counter dropped"
    print("→ 3 retries, then marked close_stuck with ONE alert; "
          "position excluded from routine monitoring. OK")

    print()
    print("=" * 72)
    print("Section 2 — /close-style operator reset")
    print("=" * 72)
    reset_close_stuck_position(trade.id, db_path)
    engine._notified_stuck.discard(trade.id)
    engine._close_roll_failures.pop(trade.id, None)  # same as /close handler
    status = engine.monitor_tick()
    print(f"after reset: counter={engine._close_roll_failures.get(trade.id)}  "
          f"msg={status.message!r}")
    assert engine._close_roll_failures.get(trade.id) == 1, (
        "retried close should start from a fresh counter"
    )
    print("→ position is monitored again and the retry ladder restarts at 1. OK")

    print()
    print("=" * 72)
    print("Section 3 — notifier boot-window cooldown fix")
    print("=" * 72)
    from alerts.notifier import Notifier

    n = Notifier(cooldown_sec=300)
    # Simulate a host that booted 42 seconds ago: with the old 0.0 sentinel,
    # 42 - 0.0 < 300 meant this first-ever alert was silently suppressed.
    with patch("alerts.notifier.time.monotonic", return_value=42.0), \
         patch.object(n, "_dispatch_email") as em, \
         patch.object(n, "_dispatch_telegram") as tg:
        n.send("stop_loss", "Stop-loss triggered: BTC-…", "body")
        first = (em.call_count, tg.call_count)
        n.send("stop_loss", "Stop-loss triggered: BTC-…", "body")  # duplicate
        second = (em.call_count, tg.call_count)
    print(f"first send dispatched (email, telegram) = {first}")
    print(f"after duplicate send                    = {second}")
    assert first == (1, 1), "first alert must be dispatched even near boot"
    assert second == (1, 1), "duplicate within cooldown still suppressed"
    print("→ first alert delivered even moments after boot; dedup intact. OK")

    print()
    print("All scratch checks passed.")


if __name__ == "__main__":
    main()
