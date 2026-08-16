"""
scratch/scratch_cross_collateral_reconcile.py
==============================================
Offline demonstration of the Phase 28 cross-collateral / reconcile fixes.

Symptom (2026-08-15 → 2026-08-16 test-mode run): a "RECONCILE MISMATCH
persisting 12 cycles: Deribit margin $994.11 vs SQLite $0.00. Deribit open:
unknown." Telegram alert fired 13 times over two days, every 2–3 hours, while
the bot held no positions at all.

Three separate defects, each reproduced below against the figures actually
returned by the live test account:

28a  The account runs ``margin_model = "cross_pm"`` with cross collateral
     enabled.  On such an account Deribit reports ``initial_margin``,
     ``maintenance_margin`` and ``available_funds`` as ONE ACCOUNT-WIDE figure
     denominated in each currency — the BTC row and the ETH row are the same
     number in different units.  Only ``equity``/``balance`` are per-currency.
     ``_refresh_from_api`` summed everything across ASSETS, so margin and
     available cash came out at exactly 2x (BTC + ETH).  Ground truth from the
     account's BUIDL row (a $1-unit token, so its figures are already USD):
     initial margin $496.83, available $2,988.52 — the bot logged $993.91 and
     $5,978.57.  available_cash feeds position sizing, so every test-mode
     position was sized off double the real deployable capital.

28b  The residual $496.83 is not an orphan position: every currency reports
     zero positions and zero resting orders.  It is the portfolio-margin
     haircut on the account's own crypto collateral (BTC 0.0483 + ETH 0.2364 =
     $3,485 of spot; $496.83 = 14.25% of it, and maintenance/initial = 0.800
     exactly).  The bot's SQLite figure is the sum of open-position net debits,
     so with nothing open the comparison is structurally 100% divergent forever.

28c  The escalation fingerprint was ``(round(api_margin), round(db_margin))``.
     Margin marks to market and drifts every cycle ($993.67 → $993.82 → …), so
     whenever the rounded dollar changed the counter reset AND the "already
     escalated" flag cleared — re-arming the one-shot alert, which then re-fired
     every time the value happened to hold steady for 12 cycles.

No network, no live orders — every figure below is a literal from the observed
account and every call runs against mocked REST responses.

Run from the repo root:
    python -m scratch.scratch_cross_collateral_reconcile

Aborts if TRADING_MODE == "live".
"""

import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode. Aborting.")
    sys.exit(1)

from db.state import init_db
from portfolio.tracker import PortfolioTracker, _is_cross_collateral


# ── Observed live-account figures (2026-08-16 read-only probe) ────────────────

BTC_SPOT = 62_990.79
ETH_SPOT = 1_877.39

# Account-wide truth, read from the BUIDL row where 1 unit == $1.
ACCOUNT_IM_USD    = 496.83
ACCOUNT_MM_USD    = 397.46
ACCOUNT_AVAIL_USD = 2_988.52

# Genuinely per-currency balances.
BTC_EQUITY = 0.04828025    # $3,041.21
ETH_EQUITY = 0.236425      # $443.86

# What the bot actually logged with the double-count in place.
LOGGED_MARGIN    = 993.91
LOGGED_AVAILABLE = 5_978.57
LOGGED_EQUITY    = 3_486.24

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, PASS if ok else FAIL))
    mark = "✓" if ok else "✗"
    print(f"   {mark} {label}")
    if detail:
        print(f"       {detail}")


def _summary(currency: str, equity: float, spot: float, cross: bool) -> dict:
    """One account summary row, account-wide fields in this currency's units."""
    row = {
        "currency":           currency,
        "equity":             equity,
        "balance":            equity,
        "available_funds":    ACCOUNT_AVAIL_USD / spot,
        "initial_margin":     ACCOUNT_IM_USD / spot,
        "maintenance_margin": ACCOUNT_MM_USD / spot,
    }
    if cross:
        row["cross_collateral_enabled"] = True
        row["margin_model"] = "cross_pm"
    return row


def _refresh(db: Path, cross: bool, single_count: bool) -> PortfolioTracker:
    summaries = {
        "BTC": _summary("BTC", BTC_EQUITY, BTC_SPOT, cross),
        "ETH": _summary("ETH", ETH_EQUITY, ETH_SPOT, cross),
    }

    def fake_rest_get(url, bearer_token=None, timeout=10):
        if "public/auth" in url:
            return {"result": {"access_token": "tok", "expires_in": 900}}
        if "get_account_summary" in url:
            for currency, row in summaries.items():
                if f"currency={currency}" in url:
                    return {"result": row}
        return {"result": []}

    tracker = PortfolioTracker(
        db_path=db, client_id="id", client_secret="secret",
        rest_url="https://test.deribit.com",
    )
    with patch("portfolio.tracker._rest_post",
               side_effect=lambda *a, **k: {"result": {"access_token": "tok"}}), \
         patch("portfolio.tracker._rest_get", side_effect=fake_rest_get), \
         patch("config.TRADING_MODE", "test"), \
         patch("config.ASSETS", ["BTC", "ETH"]), \
         patch("config.CROSS_COLLATERAL_SINGLE_COUNT", single_count):
        tracker.refresh(spot_prices={"BTC": BTC_SPOT, "ETH": ETH_SPOT})
    return tracker


def _capture(level=logging.WARNING):
    records: list[logging.LogRecord] = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("portfolio.tracker")
    handler = Handler()
    log.addHandler(handler)
    old = log.level
    log.setLevel(level)

    def detach():
        log.removeHandler(handler)
        log.setLevel(old)

    return records, detach


# ── 1. The double-count, old vs new ───────────────────────────────────────────

def demo_double_count(db: Path) -> None:
    print("\n1. Cross-collateral aggregation (28a)")
    print("   Both currencies report the SAME account-wide figure in own units:")
    print(f"       BTC initial_margin {ACCOUNT_IM_USD / BTC_SPOT:.8f} BTC "
          f"→ ${ACCOUNT_IM_USD:.2f}")
    print(f"       ETH initial_margin {ACCOUNT_IM_USD / ETH_SPOT:.8f} ETH "
          f"→ ${ACCOUNT_IM_USD:.2f}")

    old = _refresh(db, cross=True, single_count=False)
    new = _refresh(db, cross=True, single_count=True)

    print(f"\n   OLD (summed):        margin ${old._deribit_margin_usd:8.2f}   "
          f"available ${old.available_cash:8.2f}")
    print(f"   NEW (single-count):  margin ${new._deribit_margin_usd:8.2f}   "
          f"available ${new.available_cash:8.2f}")
    print(f"   TRUTH (BUIDL row):   margin ${ACCOUNT_IM_USD:8.2f}   "
          f"available ${ACCOUNT_AVAIL_USD:8.2f}")

    check(
        "old behaviour reproduces the logged $993.91 margin",
        abs(old._deribit_margin_usd - LOGGED_MARGIN) < 1.0,
        f"reproduced ${old._deribit_margin_usd:.2f} vs logged ${LOGGED_MARGIN:.2f}",
    )
    check(
        "old behaviour reproduces the logged $5,978.57 available cash",
        abs(old.available_cash - LOGGED_AVAILABLE) < 3.0,
        f"reproduced ${old.available_cash:.2f} vs logged ${LOGGED_AVAILABLE:.2f}",
    )
    check(
        "new margin matches the account-wide truth",
        abs(new._deribit_margin_usd - ACCOUNT_IM_USD) < 0.5,
    )
    check(
        "new available cash matches the account-wide truth",
        abs(new.available_cash - ACCOUNT_AVAIL_USD) < 0.5,
    )
    check(
        "equity is still summed (it IS per-currency) and matches the log",
        abs(new.equity_usd - LOGGED_EQUITY) < 2.0,
        f"${new.equity_usd:.2f} vs logged ${LOGGED_EQUITY:.2f}",
    )
    check(
        "available cash no longer exceeds equity",
        new.available_cash <= new.equity_usd,
        f"${new.available_cash:.2f} <= ${new.equity_usd:.2f} "
        f"(was ${old.available_cash:.2f} > ${old.equity_usd:.2f})",
    )

    sized_old = old.available_cash * config.MAX_LOSS_PCT
    sized_new = new.available_cash * config.MAX_LOSS_PCT
    print(f"\n   Sizing impact at MAX_LOSS_PCT={config.MAX_LOSS_PCT}: "
          f"max_loss_usd ${sized_old:.2f} → ${sized_new:.2f}")
    check(
        "per-trade risk budget halves back to the intended figure",
        abs(sized_old / sized_new - 2.0) < 0.02,
    )

    seg = _refresh(db, cross=False, single_count=True)
    check(
        "a segregated (non-cross) account still sums — no behaviour change",
        abs(seg._deribit_margin_usd - 2 * ACCOUNT_IM_USD) < 1.0,
    )
    check(
        "cross detection reads margin_model and cross_collateral_enabled",
        _is_cross_collateral({"margin_model": "cross_pm"})
        and _is_cross_collateral({"cross_collateral_enabled": True})
        and not _is_cross_collateral({"margin_model": "segregated_pm"})
        and not _is_cross_collateral({"equity": 1.0}),
    )


# ── 2. The invariant that would have caught it ────────────────────────────────

def demo_invariant(db: Path) -> None:
    print("\n2. Available-cash invariant (28a)")
    tracker = PortfolioTracker(
        db_path=db, client_id="id", client_secret="secret",
        rest_url="https://test.deribit.com",
    )
    tracker._equity_usd = LOGGED_EQUITY
    tracker._available_cash = LOGGED_AVAILABLE
    records, detach = _capture()
    try:
        tracker._check_available_cash_invariant()
    finally:
        detach()
    breached = any("AVAILABLE CASH INVARIANT BREACH" in r.getMessage() for r in records)
    check(
        "the exact logged pair ($5,978 available vs $3,486 equity) is flagged",
        breached,
        "available funds are equity minus initial margin — they cannot exceed equity",
    )

    tracker._available_cash = ACCOUNT_AVAIL_USD
    records, detach = _capture()
    try:
        tracker._check_available_cash_invariant()
    finally:
        detach()
    check(
        "a healthy pair logs nothing",
        not any("INVARIANT BREACH" in r.getMessage() for r in records),
    )


# ── 3. Collateral-only mismatch is not actionable ─────────────────────────────

def demo_collateral_only(db: Path) -> None:
    print("\n3. Collateral-only mismatch suppression (28b)")

    def tracker_with(open_desc: str, db_margin: float) -> PortfolioTracker:
        t = PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com", notifier=MagicMock(),
        )
        t._describe_deribit_positions = MagicMock(return_value=open_desc)
        t._used_margin = db_margin
        t._deribit_margin_usd = ACCOUNT_IM_USD
        return t

    # The live case: nothing open anywhere, margin is collateral haircut.
    t = tracker_with(open_desc="", db_margin=0.0)
    records, detach = _capture(logging.INFO)
    try:
        for _ in range(config.RECONCILE_ESCALATE_AFTER_CYCLES + 5):
            t._reconcile()
    finally:
        detach()
    msgs = [r.getMessage() for r in records]
    check(
        "no RECONCILE MISMATCH warning when nothing is open on either side",
        not any("RECONCILE MISMATCH" in m for m in msgs),
        f"{ACCOUNT_IM_USD:.2f} is {ACCOUNT_IM_USD / 3485.07 * 100:.2f}% of the "
        f"account's crypto balance — the portfolio-margin collateral haircut",
    )
    check(
        "the condition is still surfaced once at INFO, not silently dropped",
        sum("collateral margin on the account balance" in m for m in msgs) == 1,
    )
    check(
        "no operator alert is fired for it",
        t._notifier.notify_warning.call_count == 0,
    )

    # Regression guard: untracked exchange inventory must still shout.
    t = tracker_with(open_desc="BTC-15JUL26-64000-C (option) qty=0.1", db_margin=0.0)
    records, detach = _capture()
    try:
        t._reconcile()
    finally:
        detach()
    check(
        "a real untracked Deribit position still warns (Phase 24a preserved)",
        any("RECONCILE MISMATCH" in r.getMessage() for r in records),
    )

    t = tracker_with(open_desc="", db_margin=1_500.0)
    t._deribit_margin_usd = 10.0
    records, detach = _capture()
    try:
        t._reconcile()
    finally:
        detach()
    check(
        "an open DB position missing from Deribit still warns",
        any("RECONCILE MISMATCH" in r.getMessage() for r in records),
    )


# ── 4. Escalation no longer re-arms on a drifting number ──────────────────────

def demo_escalation(db: Path) -> None:
    print("\n4. Escalation stability (28c)")

    def build() -> PortfolioTracker:
        t = PortfolioTracker(
            db_path=db, client_id="id", client_secret="secret",
            rest_url="https://test.deribit.com", notifier=MagicMock(),
        )
        t._describe_deribit_positions = MagicMock(
            return_value="BTC-15JUL26-64000-C (option) qty=0.1"
        )
        t._used_margin = 0.0
        t._deribit_margin_usd = LOGGED_MARGIN
        return t

    # Margin figures taken from the actual alerts the operator received.
    drift = [994.11, 994.53, 994.36, 995.45, 993.99, 994.82, 993.91,
             993.67, 993.82, 993.88, 994.00, 993.69, 993.68, 992.40]

    t = build()
    for i in range(120):
        t._deribit_margin_usd = drift[i % len(drift)]
        t._reconcile()
    check(
        "120 cycles of drifting margin produce exactly ONE alert",
        t._notifier.notify_warning.call_count == 1,
        f"got {t._notifier.notify_warning.call_count} "
        f"(the operator received 13 for this condition)",
    )

    # Notifier.send deduplicates on subject == first 80 chars of the message.
    subjects = set()
    for margin in (993.67, 995.45, 991.26):
        t2 = build()
        t2._deribit_margin_usd = margin
        for _ in range(config.RECONCILE_ESCALATE_AFTER_CYCLES):
            t2._reconcile()
        subjects.add(t2._notifier.notify_warning.call_args[0][0][:80])
    check(
        "the alert's dedup subject is stable across differing margins",
        len(subjects) == 1,
        next(iter(subjects)),
    )

    # A genuinely different situation should still alert.
    t3 = build()
    for _ in range(config.RECONCILE_ESCALATE_AFTER_CYCLES):
        t3._reconcile()
    t3._describe_deribit_positions = MagicMock(
        return_value="ETH-14AUG26-1700-P (option) qty=12.0"
    )
    for _ in range(config.RECONCILE_ESCALATE_AFTER_CYCLES):
        t3._reconcile()
    check(
        "a changed instrument set re-arms and alerts again",
        t3._notifier.notify_warning.call_count == 2,
    )

    t4 = build()
    for _ in range(config.RECONCILE_ESCALATE_AFTER_CYCLES):
        t4._reconcile()
    t4._used_margin = t4._deribit_margin_usd     # margins now agree
    t4._reconcile()
    check(
        "a resolved mismatch clears the escalation state",
        t4._reconcile_repeat_count == 0 and not t4._reconcile_escalated,
    )


def main() -> None:
    print("=" * 72)
    print("  Phase 28 — cross-collateral accounting and reconcile scope")
    print("=" * 72)

    # `with get_connection(...)` now closes the handle at block exit, so a
    # plain TemporaryDirectory cleans up reliably on Windows too.  (This used
    # to need mkdtemp + a tolerant rmtree because the connection stayed open
    # until a cyclic-GC pass ran — see scratch/scratch_db_connection_lifecycle.py.)
    with tempfile.TemporaryDirectory(prefix="scratch_phase28_") as td:
        db = Path(td) / "scratch_phase28.db"
        init_db(db)
        demo_double_count(db)
        demo_invariant(db)
        demo_collateral_only(db)
        demo_escalation(db)

    print("\n" + "=" * 72)
    failed = [lbl for lbl, r in results if r == FAIL]
    if failed:
        print(f"  {len(failed)} CHECK(S) FAILED:")
        for lbl in failed:
            print(f"    ✗ {lbl}")
        sys.exit(1)
    print(f"  ALL {len(results)} CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
