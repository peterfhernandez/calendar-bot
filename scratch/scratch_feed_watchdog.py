"""
scratch/scratch_feed_watchdog.py
================================
Demonstrates the Phase 23 Feed Freshness Watchdog.

Deribit's WebSocket ping/pong heartbeat detects a dropped TCP connection, but
NOT the failure mode observed on 2026-07-19: Deribit silently stopped pushing
ticker data while leaving the socket open. All cached snapshots aged past the
30s TTL within seconds, the scanner found 0 candidates, and the bot idled for
7+ hours with no reconnect — the only recovery was a manual restart.

The watchdog fixes this by tracking the timestamp of the last ticker update and
closing the WS (which triggers the existing reconnect loop) if no update arrives
within FEED_WATCHDOG_TIMEOUT_SEC.

This script exercises the REAL DeribitFeed._watchdog and the REAL start()
reconnect loop against a controllable in-memory transport (no network, no live
account, no orders):

  1. The feed connects and receives a short burst of ticker updates.
  2. The transport then goes silent while the socket stays "open" — simulating
     Deribit ceasing its push stream.
  3. The watchdog detects the staleness after FEED_WATCHDOG_TIMEOUT_SEC and
     closes the WS.
  4. start()'s reconnect loop opens a fresh connection and the cycle repeats —
     proving the bot recovers automatically instead of idling forever.

Read-only with respect to the exchange. Run with:
    python -m scratch.scratch_feed_watchdog

Does NOT run when TRADING_MODE = "live".
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from unittest.mock import AsyncMock, patch

import config

if config.TRADING_MODE == "live":
    print("scratch_feed_watchdog.py: TRADING_MODE is 'live' — refusing to run.")
    sys.exit(0)

from data.deribit_feed import DeribitFeed, TickerSnapshot


def _banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def _ticker_msg(i: int) -> str:
    return json.dumps({
        "method": "subscription",
        "params": {
            "channel": "ticker.BTC-27JUN25-100000-C.raw",
            "data": {
                "index_price":    100_000.0,
                "mark_iv":        80.0,
                "mark_price":     0.020 + i * 0.0001,
                "best_bid_price": 0.019,
                "best_ask_price": 0.021,
                "open_interest":  200.0,
            },
        },
    })


class _SilentAfterBurstWS:
    """In-memory WS: yields a burst of tickers, then stays open but silent.

    close() unblocks the silent wait by raising OSError from the iterator, which
    start()'s reconnect loop catches — exactly what happens when the real
    watchdog calls ws.close() on a dead-but-open socket.
    """

    _connect_count = 0

    def __init__(self, burst: int = 3):
        _SilentAfterBurstWS._connect_count += 1
        self.conn_id = _SilentAfterBurstWS._connect_count
        self._burst = [_ticker_msg(i) for i in range(burst)]
        self._closed = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        print(f"  [transport] connection #{self.conn_id} opened")
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._burst:
            await asyncio.sleep(0.1)
            return self._burst.pop(0)
        # Socket stays open but no more data arrives — the silent-blackout case.
        await self._closed.wait()
        raise OSError("watchdog closed the silent socket")

    async def send(self, payload):  # subscribe/auth RPCs are mocked out anyway
        pass

    async def close(self):
        self.closed = True
        self._closed.set()


async def main() -> None:
    _banner("Phase 23 — Feed Freshness Watchdog demonstration")

    watchdog_timeout = 3
    tickers_seen = 0

    async def on_ticker(snap: TickerSnapshot) -> None:
        nonlocal tickers_seen
        tickers_seen += 1

    feed = DeribitFeed(assets=["BTC"], on_ticker=on_ticker, backoff_max=1.0)

    print(f"FEED_WATCHDOG_TIMEOUT_SEC (patched for demo) = {watchdog_timeout}s")
    print("Each connection delivers 3 tickers, then goes silent.\n")

    # Mock the subscription pass so no real RPC/network is needed; the watchdog
    # and reconnect loop under test are the genuine article.
    with patch.object(config, "FEED_WATCHDOG_TIMEOUT_SEC", watchdog_timeout), \
         patch("data.deribit_feed.websockets.connect",
               side_effect=lambda *a, **k: _SilentAfterBurstWS()), \
         patch.object(DeribitFeed, "_subscribe_all", new=AsyncMock()):

        task = asyncio.create_task(feed.start())

        # Run long enough to observe two watchdog-driven reconnects.
        run_for = watchdog_timeout * 2 + 6
        start = time.time()
        while time.time() - start < run_for:
            await asyncio.sleep(0.5)

        await feed.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    connects = _SilentAfterBurstWS._connect_count
    _banner("Result")
    print(f"Total connections opened : {connects}")
    print(f"Ticker updates received  : {tickers_seen}")
    if connects >= 2:
        print("\n✅ PASS — the watchdog closed each silent socket and start()")
        print("   reconnected automatically. Without the watchdog the feed would")
        print("   have stayed connected but idle indefinitely (the 2026-07-19 bug).")
    else:
        print("\n❌ Only one connection was opened — the watchdog did not fire.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
