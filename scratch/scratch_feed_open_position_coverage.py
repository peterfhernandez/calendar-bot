"""
scratch/scratch_feed_open_position_coverage.py
==============================================
Demonstrates the Phase 18 Bug 4 fix: the WebSocket feed's ticker-subscription
list now unions the day-window candidate list with the near/far instrument
names of every open position, recomputed on every connect AND reconnect.

Before the fix, an open position whose far leg fell outside the window derived
from NEAR_DAYS_OPTIONS/FAR_DAYS_OPTIONS (e.g. trade_id=1's BTC-28AUG26-56000-P
at far_days=51 after FAR_DAYS_OPTIONS was trimmed to [7, 14]) silently lost WS
coverage after a reconnect, disabling stop-loss/take-profit monitoring
("No IV for trade N — skipping status check" on every monitor tick).

This script:
  1. Creates a temporary DB with an open position whose far leg expires well
     outside the configured day window.
  2. Simulates the feed's subscription pass (initial connect) with mocked
     WS I/O and shows the out-of-window far leg being subscribed.
  3. Simulates a reconnect and shows the far leg is STILL subscribed.
  4. Closes the position and shows a subsequent reconnect no longer carries it.
  5. Contrasts with the old behaviour (no extra_instruments provider).

Read-only with respect to the exchange — no orders, no live account access.
Run with:
    python -m scratch.scratch_feed_open_position_coverage

Does NOT run when TRADING_MODE = "live".
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config

if config.TRADING_MODE == "live":
    print("scratch_feed_open_position_coverage.py: TRADING_MODE is 'live' — refusing to run.")
    sys.exit(0)

from data.deribit_feed import DeribitFeed
from db.state import (
    close_calendar_trade,
    create_calendar_trade,
    get_open_instrument_names,
    init_db,
)


def _label(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.day}{dt.strftime('%b%y').upper()}"


def _banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


async def _simulate_connect(feed: DeribitFeed, window_instruments: list[str]) -> list[str]:
    """Run the feed's subscription pass with mocked WS I/O.

    Returns the flat list of instrument names subscribed during the pass —
    exactly what a real connect/reconnect would subscribe.
    """
    subscribed: list[str] = []

    async def fake_fetch(asset: str) -> list[str]:
        names = [n for n in window_instruments if n.startswith(f"{asset}-")]
        feed._instruments[asset] = list(names)
        return names

    async def fake_subscribe(instruments: list[str]) -> None:
        subscribed.extend(instruments)

    with patch.object(feed, "fetch_instruments", side_effect=fake_fetch), \
         patch.object(feed, "_subscribe_tickers", side_effect=fake_subscribe):
        await feed._subscribe_all()
    return subscribed


async def main() -> None:
    # ── Setup: temp DB with an open position outside the day window ──────────
    tmp = tempfile.mkdtemp(prefix="scratch_feed_cov_")
    db_path = Path(tmp) / "scratch_feed_coverage.db"
    init_db(db_path)

    near_days, far_days = 7, 51  # far leg well outside a [1, 14]-day window
    near_leg = f"BTC-{_label(near_days)}-56000-P"
    far_leg  = f"BTC-{_label(far_days)}-56000-P"

    trade = create_calendar_trade(
        asset="BTC",
        date_open=date.today(),
        option_type="Put",
        strike=56_000.0,
        expiry_near=_label(near_days),
        expiry_far=_label(far_days),
        near_days=near_days,
        far_days=far_days,
        qty=1.0,
        spot_open=108_000.0,
        near_prem=90.0,
        far_prem=250.0,
        net_debit=160.0,
        near_instrument=near_leg,
        far_instrument=far_leg,
        db_path=db_path,
    )

    # The day-window list the scanner config would produce — does NOT include
    # the 51-day far leg (mirrors config_test.py FAR_DAYS_OPTIONS=[7, 14]).
    window = [
        f"BTC-{_label(7)}-56000-P",
        f"BTC-{_label(14)}-56000-P",
        f"BTC-{_label(14)}-58000-P",
    ]

    _banner("Setup")
    print(f"Temp DB:            {db_path}")
    print(f"Open position:      trade_id={trade.id}  near={near_leg}  far={far_leg}")
    print(f"Day-window list:    {window}")
    print(f"Far leg in window?  {far_leg in window}  (far_days={far_days} > window max)")

    # ── Old behaviour: no extra_instruments provider ──────────────────────────
    _banner("1. OLD behaviour — feed built without extra_instruments")
    old_feed = DeribitFeed(assets=["BTC"])
    subs = await _simulate_connect(old_feed, window)
    print(f"Subscribed ({len(subs)}): {subs}")
    ok = far_leg not in subs
    print(f"Far leg subscribed: {far_leg in subs}  → position invisible to the "
          f"monitor (the Bug 4 gap)  [{'demonstrated' if ok else 'UNEXPECTED'}]")

    # ── New behaviour: provider unions open-position legs ─────────────────────
    _banner("2. FIXED behaviour — initial connect with extra_instruments provider")
    feed = DeribitFeed(
        assets=["BTC"],
        extra_instruments=lambda: get_open_instrument_names(db_path=db_path),
    )
    subs = await _simulate_connect(feed, window)
    print(f"Subscribed ({len(subs)}): {subs}")
    assert far_leg in subs, "far leg should be subscribed on initial connect"
    print(f"Far leg subscribed: True  [PASS]")

    _banner("3. Simulated WS reconnect — coverage must survive")
    subs = await _simulate_connect(feed, window)
    print(f"Subscribed ({len(subs)}): {subs}")
    assert far_leg in subs, "far leg should be re-subscribed after reconnect"
    print("Far leg re-subscribed after reconnect: True  [PASS]")

    _banner("4. Position closed — next reconnect drops the extra subscription")
    close_calendar_trade(
        trade.id, date_close=date.today(), spot_close=108_500.0,
        pnl=12.0, result="Win", db_path=db_path,
    )
    subs = await _simulate_connect(feed, window)
    print(f"Subscribed ({len(subs)}): {subs}")
    assert far_leg not in subs, "closed position's far leg should no longer be subscribed"
    print("Far leg dropped after close: True  [PASS]")

    _banner("Summary")
    print("Open-position legs outside the scanner day window stay subscribed")
    print("across connects and reconnects, and are dropped once the position")
    print("closes. Stop-loss/take-profit monitoring can no longer be silently")
    print("disabled by a config-window change or a WS reconnect.")


if __name__ == "__main__":
    asyncio.run(main())
