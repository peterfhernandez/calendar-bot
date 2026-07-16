"""
scratch/scratch_stuck_position_visibility.py
============================================
Offline demonstration of the Phase 22a/22b/22e fixes for stuck-position handling.

A position marked ``close_stuck`` used to (a) vanish from ``/positions`` and
``/portfolio`` — exactly when the operator needed to see it — while (b) still
being retried (and re-notified about) by the monitor every tick.  This script
proves the new split:

    22a  load_calendar_state()  → EXCLUDES stuck positions (monitor leaves them alone)
    22b  get_visible_positions() → INCLUDES stuck positions (Telegram shows them, flagged)
         get_open_trades()       → still EXCLUDES stuck positions (unchanged)
    22e  get_close_status()      → lets the engine skip re-notifying an already-stuck row

No network, no live orders — runs against a temp SQLite DB.

Run from the repo root:
    python -m scratch.scratch_stuck_position_visibility

Aborts if TRADING_MODE == "live".
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from db.state import (
    create_calendar_trade,
    get_close_status,
    get_open_trades,
    get_visible_positions,
    init_db,
    load_calendar_state,
    mark_position_close_stuck,
)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, condition: bool) -> None:
    results.append((label, PASS if condition else FAIL))
    print(f"  {'✓' if condition else '✗'}  {label}")


db_path = Path(tempfile.mktemp(suffix=".db"))
init_db(db_path)

trade = create_calendar_trade(
    asset="BTC",
    date_open=date(2026, 6, 1),
    option_type="Call",
    strike=100_000.0,
    expiry_near="2026-06-07",
    expiry_far="2026-07-04",
    near_days=7,
    far_days=30,
    qty=1.0,
    spot_open=99_000.0,
    near_prem=500.0,
    far_prem=800.0,
    net_debit=300.0,
    near_instrument="BTC-7JUN26-100000-C",
    far_instrument="BTC-4JUL26-100000-C",
    db_path=db_path,
)

print("\n── Before the position is marked stuck ──")
check("monitor sees it (load_calendar_state)", len(load_calendar_state("BTC", db_path=db_path)["open_positions"]) == 1)
check("Telegram sees it (get_visible_positions)", len(get_visible_positions(db_path=db_path)) == 1)
check("get_close_status == 'open'", get_close_status(trade.id, db_path=db_path) == "open")

print("\n── Mark it close_stuck (simulates a repeated close rejection) ──")
mark_position_close_stuck(trade.id, error_reason="far close rejected (-32602)", db_path=db_path)

print("\n── After the position is marked stuck ──")
mon = load_calendar_state("BTC", db_path=db_path)["open_positions"]
check("22a: monitor NO LONGER retries it (excluded)", mon == [])
check("22a: get_open_trades excludes it too", get_open_trades(db_path=db_path) == [])
vis = get_visible_positions(db_path=db_path)
check("22b: Telegram STILL shows it (flagged)", len(vis) == 1 and vis[0].close_status == "close_stuck")
check("22b: error reason is preserved", vis[0].close_error_reason == "far close rejected (-32602)")
check("22e: get_close_status == 'close_stuck' (restart-safe dedup)",
      get_close_status(trade.id, db_path=db_path) == "close_stuck")

failed = [l for l, s in results if s == FAIL]
print("\n" + "=" * 60)
print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
print("=" * 60)
sys.exit(1 if failed else 0)
