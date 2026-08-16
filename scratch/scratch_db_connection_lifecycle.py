"""
scratch/scratch_db_connection_lifecycle.py
==========================================
Offline demonstration of the SQLite connection-lifecycle fix.

Every DB helper in ``db/state.py`` is written as::

    with get_connection(db_path) as conn:
        ...

but plain ``sqlite3`` treats ``with conn:`` as a *transaction* scope — it
commits or rolls back and deliberately leaves the connection **open**.  Worse,
sqlite3 keeps an internal LRU statement cache whose entries reference the
connection back, so the connection sits in a reference cycle and is unreachable
by refcounting; the OS file handle survived until the *cyclic* garbage collector
happened to run.

On Linux nobody noticed.  On Windows an open handle blocks removal of the
directory containing the file, so every test that wrote its DB into a
``tempfile.TemporaryDirectory()`` failed with
``PermissionError: [WinError 32]`` — depending on nothing but GC timing, which
is why it looked like a flaky "environmental" failure.

The fix makes ``get_connection`` return a ``_ScopedConnection`` whose
``__exit__`` keeps sqlite3's commit/rollback semantics and then closes.

This script proves, with the cyclic GC switched off so nothing can be silently
rescued, that:

  1. the old behaviour leaks a handle per helper call,
  2. the new behaviour leaks none,
  3. commit / rollback semantics are unchanged,
  4. rows fetched inside the block are still readable after it,
  5. a temp directory can be removed immediately after use.

No network, no live orders, no DB other than a throwaway temp file.

Run from the repo root:
    python -m scratch.scratch_db_connection_lifecycle

Aborts if TRADING_MODE == "live".
"""

import sys

import config

if getattr(config, "TRADING_MODE", "paper") == "live":
    sys.exit("ERROR: scratch scripts must not run in live mode.")

import gc
import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from db.state import (
    _ScopedConnection,
    create_calendar_trade,
    get_close_status,
    get_connection,
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


def live_connections() -> int:
    """
    Count sqlite3 connections that are still **open**.

    Counting Connection *objects* is not the same thing: a closed connection
    stays alive as a Python object for as long as something references it.
    Reading ``in_transaction`` is side-effect free and raises ProgrammingError
    once the handle is closed, which is exactly the distinction that matters.
    """
    n = 0
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.in_transaction
            except sqlite3.ProgrammingError:
                continue  # already closed — holds no OS handle
            n += 1
    return n


def make_trade(db: Path):
    return create_calendar_trade(
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
        open_fees=5.0,
        db_path=db,
    )


tmpdir = Path(tempfile.mkdtemp(prefix="scratch_conn_lifecycle_"))
try:
    # ── 1. The old behaviour: `with` on a plain connection does not close ────
    print("\n── 1. Old behaviour — `with sqlite3.connect(...)` leaks the handle ──")
    legacy_db = tmpdir / "legacy.db"
    init_db(legacy_db)
    gc.collect()
    gc.disable()
    try:
        before = live_connections()
        legacy = sqlite3.connect(legacy_db)
        with legacy as conn:
            conn.execute("SELECT 1").fetchone()
        leaked_open = live_connections() - before
        still_usable = True
        try:
            legacy.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            still_usable = False
        legacy.close()
    finally:
        gc.enable()
    check("plain sqlite3 `with` leaves the connection alive", leaked_open == 1)
    check("plain sqlite3 `with` leaves the connection usable (i.e. open)", still_usable)

    # ── 2. The new behaviour: no handle survives the block ──────────────────
    print("\n── 2. New behaviour — `with get_connection(...)` closes at block exit ──")
    db = tmpdir / "state.db"
    init_db(db)
    gc.collect()
    gc.disable()
    try:
        before = live_connections()
        with get_connection(db) as conn:
            conn.execute("SELECT 1").fetchone()
        after_block = live_connections() - before

        closed = False
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            closed = True

        # The real regression: the public helpers, GC disabled, must leak none.
        baseline = live_connections()
        init_db(db)
        trade = make_trade(db)
        get_open_trades(db_path=db)
        get_visible_positions(db_path=db)
        load_calendar_state("BTC", db_path=db)
        mark_position_close_stuck(trade.id, error_reason="demo", db_path=db)
        get_close_status(trade.id, db_path=db)
        leaked_helpers = live_connections() - baseline
    finally:
        gc.enable()

    check("get_connection returns a _ScopedConnection", isinstance(conn, _ScopedConnection))
    check("no connection survives the with-block", after_block == 0)
    check("the connection is genuinely closed, not just dropped", closed)
    check(
        f"7 helper calls leak 0 connections with GC disabled (leaked={leaked_helpers})",
        leaked_helpers == 0,
    )

    # ── 3. Transaction semantics are unchanged ──────────────────────────────
    print("\n── 3. Commit / rollback semantics preserved ──")
    tx_db = tmpdir / "tx.db"
    init_db(tx_db)
    insert = (
        "INSERT INTO calendar_trades "
        "(asset, option_type, strike, expiry_near, expiry_far, near_days, "
        " far_days, qty, date_open, spot_open) "
        "VALUES ('BTC','Call',1.0,'a','b',1,7,1.0,'2026-01-01',1.0)"
    )
    with get_connection(tx_db) as conn:
        conn.execute(insert)
    with get_connection(tx_db) as conn:
        committed = conn.execute("SELECT COUNT(*) FROM calendar_trades").fetchone()[0]
    check("success path commits", committed == 1)

    raised = False
    try:
        with get_connection(tx_db) as conn:
            conn.execute(insert)
            raise RuntimeError("boom")
    except RuntimeError:
        raised = True
    with get_connection(tx_db) as conn:
        after_error = conn.execute("SELECT COUNT(*) FROM calendar_trades").fetchone()[0]
    check("exception propagates out of the block", raised)
    check("error path rolls back (row count unchanged)", after_error == committed)

    # ── 4. Rows stay readable after the block ───────────────────────────────
    print("\n── 4. Rows fetched inside the block survive the close ──")
    make_trade(db)
    with get_connection(db) as conn:
        row = conn.execute("SELECT * FROM calendar_trades LIMIT 1").fetchone()
    check("sqlite3.Row is readable after the connection closed", row["asset"] == "BTC")

    # ── 5. The Windows symptom: the directory can be removed at once ────────
    print("\n── 5. The Windows symptom — temp directory removable immediately ──")
    gc.disable()
    try:
        victim = Path(tempfile.mkdtemp(prefix="scratch_conn_victim_"))
        vdb = victim / "test.db"
        init_db(vdb)
        make_trade(vdb)
        get_open_trades(db_path=vdb)
        removed = True
        try:
            shutil.rmtree(victim)  # no ignore_errors: this must genuinely work
        except OSError as exc:
            removed = False
            print(f"      rmtree failed: {exc}")
    finally:
        gc.enable()
    check("temp dir removes cleanly with no GC pass in between", removed)

finally:
    gc.collect()
    shutil.rmtree(tmpdir, ignore_errors=True)


print("\n" + "=" * 72)
failed = [label for label, status in results if status == FAIL]
for label, status in results:
    print(f"  [{status}] {label}")
print("=" * 72)
print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    sys.exit(1)
print("  All checks passed — connection lifecycle is deterministic.")
