"""
scratch_account_margin_audit.py
===============================
Debug script — Phase 25e residual-margin audit.

A ~$1,583 ``RECONCILE MISMATCH`` was observed on the test account with **zero**
tracked option positions and while Phase 24a's enhanced warning listed only the
one open trade's two legs.  That means the margin belongs to something the
option-only position fetch cannot see: a future/perpetual, a position in a
non-configured currency, or margin reserved by resting open orders.

This script dumps the **full** account state so the operator can locate the
source of any residual margin:

1. Per-currency account summary (equity, maintenance margin, available funds)
2. Positions of *every* kind (options AND futures) — ``kind="any"``
3. Resting open orders per currency

Read-only: it makes only ``private/get_account_summary`` / ``get_positions`` /
``get_open_orders_by_currency`` GET calls — nothing is placed, cancelled, or
mutated.  Aborts in live mode.

Run from the repo root:
    python -m scratch.scratch_account_margin_audit
"""

from __future__ import annotations

import sys

import config

if config.TRADING_MODE == "live":
    print("ERROR: scratch scripts must not run in live mode. Aborting.")
    sys.exit(1)

from portfolio.tracker import PortfolioTracker, _assets_to_currencies


def _currencies() -> list[str]:
    assets = list(dict.fromkeys([*config.ASSETS, *config.COLLECTOR_ASSETS]))
    return _assets_to_currencies(assets)


def main() -> None:
    if config.TRADING_MODE == "paper":
        print(
            "NOTE: TRADING_MODE is 'paper' — Deribit account queries are "
            "disabled. Run in 'test' mode with credentials to audit the "
            "live test-exchange account.\n"
        )

    tracker = PortfolioTracker(
        client_id=config.DERIBIT_CLIENT_ID,
        client_secret=config.DERIBIT_CLIENT_SECRET,
        cache=None,
    )

    currencies = _currencies()

    # ── Account summaries ─────────────────────────────────────────────────────
    print("─" * 68)
    print("  Per-currency account summary")
    print("─" * 68)
    token = None
    if config.TRADING_MODE != "paper" and config.DERIBIT_CLIENT_ID:
        try:
            token = tracker._authenticate()
        except Exception as exc:  # noqa: BLE001 — read-only diagnostic
            print(f"  auth failed: {exc}")
    if token:
        for currency in currencies:
            try:
                s = tracker._get_account_summary(token, currency)
            except Exception as exc:  # noqa: BLE001
                print(f"  {currency}: unavailable ({exc})")
                continue
            print(
                f"  {currency}: equity={s.get('equity', 0.0)}  "
                f"maintenance_margin={s.get('maintenance_margin', 0.0)}  "
                f"initial_margin={s.get('initial_margin', 0.0)}  "
                f"available_funds={s.get('available_funds', 0.0)}"
            )
    else:
        print("  (unavailable — no token)")

    # ── Positions of every kind ───────────────────────────────────────────────
    print()
    print("─" * 68)
    print("  Live positions (kind=any — options AND futures)")
    print("─" * 68)
    any_pos = False
    for currency in currencies:
        positions = tracker.get_deribit_open_positions(currency, kind="any")
        if not positions:
            continue
        any_pos = True
        print(f"  {currency}:")
        for p in positions:
            print(
                f"    {p['instrument_name']} [{p['kind']}]  size={p['size']}  "
                f"index=${p['index_price']:,.2f}  mark=${p['mark_value']:.4f}"
            )
    if not any_pos:
        print("  (none / unavailable)")

    # ── Resting open orders ───────────────────────────────────────────────────
    print()
    print("─" * 68)
    print("  Resting open orders")
    print("─" * 68)
    any_ord = False
    for currency in currencies:
        orders = tracker.get_deribit_open_orders(currency)
        if not orders:
            continue
        any_ord = True
        print(f"  {currency}:")
        for o in orders:
            print(
                f"    {o['instrument_name']} {o['direction']} "
                f"amount={o['amount']} @ {o['price']}"
            )
    if not any_ord:
        print("  (none / unavailable)")

    print()
    print(
        "If a maintenance-margin figure above has no option position to explain "
        "it, look for a future/perpetual or resting order in the same currency, "
        "then clear it manually on test.deribit.com."
    )
    print("Done.")


if __name__ == "__main__":
    main()
