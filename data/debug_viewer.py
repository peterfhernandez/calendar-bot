"""
debug_viewer.py
===============
Interactive terminal viewer for the live Deribit data feed.

Connects to Deribit, populates a ChainCache, and refreshes a summary table
in-place every few seconds so you can see exactly what the feed is producing.

Usage::

    # From the project root with the venv active:
    python -m data.debug_viewer
    python -m data.debug_viewer --assets BTC ETH --refresh 5 --rows 30
    python -m data.debug_viewer --live          # use live endpoint (careful!)

Columns
-------
Instrument          Full Deribit name (asset-expiry-strike-type)
Spot                Underlying index price
IV %                Mark implied vol
Bid / Ask           Best bid and ask in USD
OI                  Open interest (contracts)
Age s               Seconds since last update
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from data.deribit_feed import DeribitFeed, TickerSnapshot
from data.chain_cache import ChainCache


def _clear() -> None:
    os.system("cls" if sys.platform == "win32" else "clear")


def _render(cache: ChainCache, assets: list[str], rows: int, start_time: float) -> None:
    _clear()
    elapsed = time.time() - start_time
    st = cache.stats()

    print(f" Deribit Feed Debug Viewer   elapsed={elapsed:>6.0f}s  "
          f"total={st['total']}  fresh={st['fresh']}  stale={st['stale']}")
    print("─" * 100)

    for asset in assets:
        spot = cache.get_spot(asset) or 0
        chain = cache.get_chain(asset, include_stale=True)
        chain.sort(key=lambda s: (s.instrument.split("-")[1], float(s.instrument.split("-")[2])))

        print(f"\n  {asset}  spot={spot:>12,.2f}   ({len(chain)} instruments)")
        print(f"  {'Instrument':<42} {'IV%':>6} {'Bid':>9} {'Ask':>9} {'OI':>8} {'Age':>5}")
        print(f"  {'─'*42} {'─'*6} {'─'*9} {'─'*9} {'─'*8} {'─'*5}")

        displayed = 0
        for snap in chain:
            if displayed >= rows:
                remaining = len(chain) - rows
                print(f"  ... {remaining} more instruments (use --rows to show more)")
                break
            age = time.time() - snap.timestamp
            stale_flag = " !" if age > cache.ttl else "  "
            print(
                f"{stale_flag} {snap.instrument:<42} "
                f"{snap.mark_iv*100:>6.2f} "
                f"{snap.bid:>9.2f} "
                f"{snap.ask:>9.2f} "
                f"{snap.open_interest:>8.0f} "
                f"{age:>4.0f}s"
            )
            displayed += 1

    print("\n  [Ctrl+C to exit]", end="", flush=True)


async def _run(assets: list[str], paper: bool, refresh: float, rows: int) -> None:
    # TTL defaults to config.CHAIN_CACHE_TTL_SEC so the viewer's staleness
    # matches the live bot's cache (previously an independently hardcoded 60s
    # that could silently disagree — Phase 20d).
    cache = ChainCache()
    start_time = time.time()

    async def on_ticker(snap: TickerSnapshot) -> None:
        cache.update(snap)

    feed = DeribitFeed(assets=assets, paper=paper, on_ticker=on_ticker)
    feed_task = asyncio.create_task(feed.start())

    try:
        while True:
            _render(cache, assets, rows, start_time)
            await asyncio.sleep(refresh)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await feed.stop()
        feed_task.cancel()
        try:
            await feed_task
        except (asyncio.CancelledError, Exception):
            pass
        print("\n\nFeed stopped.")
        cache.print_summary()


if __name__ == "__main__":
    from core.logging_setup import setup_logging

    setup_logging(level="WARNING")  # suppress INFO noise while viewing the table

    parser = argparse.ArgumentParser(description="Live Deribit feed debug viewer")
    parser.add_argument("--assets",  nargs="+", default=["BTC"],
                        help="Assets to stream (default: BTC)")
    parser.add_argument("--live",    action="store_true",
                        help="Use live endpoint (default: paper/testnet)")
    parser.add_argument("--refresh", type=float, default=3.0,
                        help="Table refresh interval in seconds (default: 3)")
    parser.add_argument("--rows",    type=int, default=25,
                        help="Max instrument rows per asset (default: 25)")
    args = parser.parse_args()

    asyncio.run(_run(args.assets, paper=not args.live, refresh=args.refresh, rows=args.rows))
