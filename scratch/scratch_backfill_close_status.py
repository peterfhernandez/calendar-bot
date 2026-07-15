"""
scratch/scratch_backfill_close_status.py
=========================================
One-off data-hygiene backfill for the Phase 21e ``close_status`` bug.

Before Phase 21e, ``db/state.py::close_calendar_trade()`` (the normal auto-close
path used for every stop-loss, take-profit, expiry, and forced close) updated
``result``/``date_close``/``pnl``/``close_fees`` but never set ``close_status``.
Every auto-closed trade therefore still shows ``close_status='open'`` in the DB
even though it is fully closed — including all 131 trades from the 2026-07-14
paper-mode run.

This script sets ``close_status='closed'`` for every row whose ``result`` is a
terminal state (anything not in ``_OPEN_STATUSES``) but whose ``close_status`` is
still the ``'open'`` default.  It deliberately does NOT touch rows already marked
``'close_stuck'`` (those are still open on the exchange) or rows still genuinely
open (result IN _OPEN_STATUSES).

Run from the repo root:
    python -m scratch.scratch_backfill_close_status              # backfill config.DB_PATH
    python -m scratch.scratch_backfill_close_status --dry-run    # report only, no writes
    python -m scratch.scratch_backfill_close_status --db some.db # a specific DB file

Aborts if TRADING_MODE == "live" (per project convention — scratch scripts never
run against the live exchange; this only touches a local SQLite file regardless).
"""

import argparse
import sys
from pathlib import Path

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

from db.state import _OPEN_STATUSES, get_connection, init_db


def backfill(db_path: Path, dry_run: bool) -> int:
    """Set close_status='closed' on terminal rows still marked 'open'.

    Returns the number of rows that were (or would be) updated.
    """
    init_db(db_path)
    placeholders = ",".join("?" * len(_OPEN_STATUSES))
    where = (
        f"result IS NOT NULL "
        f"AND result NOT IN ({placeholders}) "
        f"AND close_status = 'open'"
    )
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, asset, strike, option_type, result FROM calendar_trades WHERE {where}",
            _OPEN_STATUSES,
        ).fetchall()

        print(f"Found {len(rows)} terminal row(s) still marked close_status='open':")
        for r in rows:
            print(
                f"  #{r['id']}  {r['asset']} {r['strike']:.0f} {r['option_type']}"
                f"  result={r['result']!r}"
            )

        if not rows:
            print("Nothing to backfill.")
            return 0

        if dry_run:
            print(f"\n[dry-run] Would set close_status='closed' on {len(rows)} row(s).")
            return len(rows)

        conn.execute(
            f"UPDATE calendar_trades SET close_status = 'closed' WHERE {where}",
            _OPEN_STATUSES,
        )
        print(f"\nUpdated close_status='closed' on {len(rows)} row(s).")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(config.DB_PATH),
        help="Path to the SQLite DB (default: config.DB_PATH)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report affected rows without writing.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"ERROR: DB not found: {db_path}")

    print(f"Backfilling close_status in {db_path}")
    backfill(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
