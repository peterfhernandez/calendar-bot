"""
deribit_feed.py
===============
Async Deribit WebSocket feed.

Connects to Deribit (paper or live), authenticates, subscribes to ticker
channels, and pushes updates into a ChainCache.  Reconnects automatically
with exponential back-off on any disconnect.

Usage (standalone debug)::

    python -m data.deribit_feed          # prints raw ticker updates
    python -m data.deribit_feed --assets BTC --timeout 30
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable

import websockets
import websockets.exceptions

import config

logger = logging.getLogger(__name__)

# ── Deribit endpoints ─────────────────────────────────────────────────────────

_WS_PAPER = "wss://test.deribit.com/ws/api/v2"
_WS_LIVE  = "wss://www.deribit.com/ws/api/v2"

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class TickerSnapshot:
    """Normalised ticker data for a single option instrument."""
    instrument: str          # e.g. "BTC-27JUN25-100000-C"
    asset:      str          # "BTC" or "ETH"
    spot:       float        # index price (underlying spot)
    mark_price: float        # Deribit mark price in USD
    mark_iv:    float        # implied vol from mark price (decimal, e.g. 0.80)
    bid:        float        # best bid in USD
    ask:        float        # best ask in USD
    open_interest: float     # open interest in contracts
    bid_size:   float = 0.0  # best bid quantity (contracts); 0 = no size data
    ask_size:   float = 0.0  # best ask quantity (contracts); 0 = no size data
    timestamp:  float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.mark_price


# ── Feed ──────────────────────────────────────────────────────────────────────

TickerCallback = Callable[[TickerSnapshot], Awaitable[None]]


class DeribitFeed:
    """
    Async WebSocket feed for Deribit option tickers.

    Parameters
    ----------
    assets:
        List of underlying assets to track, e.g. ["BTC", "ETH"].
    paper:
        Connect to test.deribit.com when True (default).
    client_id / client_secret:
        API credentials.  If both are empty the feed connects without auth
        (public channels only — sufficient for ticker data).
    on_ticker:
        Async callback invoked for every ticker update received.
    backoff_max:
        Maximum seconds between reconnect attempts.
    """

    def __init__(
        self,
        assets: list[str],
        paper: bool = True,
        client_id: str = "",
        client_secret: str = "",
        on_ticker: TickerCallback | None = None,
        backoff_max: float = 60.0,
    ) -> None:
        self.assets        = [a.upper() for a in assets]
        self.paper         = paper
        self.client_id     = client_id
        self.client_secret = client_secret
        self.on_ticker     = on_ticker
        self.backoff_max   = backoff_max

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running  = False
        self._req_id   = 0
        self._pending:  dict[int, asyncio.Future] = {}
        self._instruments: dict[str, list[str]] = {}  # asset → instrument names

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect and begin streaming.  Blocks until stop() is called."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                await self._connect_and_stream()
                backoff = 1.0  # reset on clean exit
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
            ) as exc:
                if not self._running:
                    break
                logger.warning("Feed disconnected (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)
            except Exception:
                logger.exception("Unexpected error in feed; reconnecting in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)

    async def stop(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def fetch_instruments(self, asset: str) -> list[str]:
        """
        Return all option instrument names for *asset* that are currently
        tradeable on Deribit (e.g. ``["BTC-27JUN25-100000-C", ...]``).

        Requires an active connection; raises RuntimeError if not connected.
        """
        if not self._ws:
            raise RuntimeError("Not connected — call start() first or use standalone fetch.")
        result = await self._rpc(
            "public/get_instruments",
            {"currency": asset, "kind": "option", "expired": False},
        )
        now = datetime.now(timezone.utc).timestamp() * 1000  # ms
        min_ms = config.NEAR_DAYS_OPTIONS[0]  * 86_400_000
        max_ms = config.FAR_DAYS_OPTIONS[-1]  * 86_400_000
        names = [
            r["instrument_name"] for r in result
            if min_ms <= (r["expiration_timestamp"] - now) <= max_ms * 2
        ]
        self._instruments[asset] = names
        return names

    async def fetch_ticker(self, instrument: str) -> TickerSnapshot | None:
        """
        One-shot REST-style ticker fetch via JSON-RPC over the WebSocket.

        Returns None if the instrument data is incomplete.
        """
        if not self._ws:
            raise RuntimeError("Not connected.")
        raw = await self._rpc("public/ticker", {"instrument_name": instrument})
        return self._parse_ticker(instrument, raw)

    # ── Internal helpers ──────────────────────────────────────────────────────

    @property
    def _endpoint(self) -> str:
        return config.DERIBIT_WS_URL

    async def _connect_and_stream(self) -> None:
        logger.info("Connecting to %s", self._endpoint)
        async with websockets.connect(
            self._endpoint,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            logger.info("Connected")

            # Start the message pump concurrently BEFORE making any RPC calls.
            # Without this, _rpc() awaits a future that only gets resolved inside
            # the pump loop — causing a deadlock where setup never completes.
            pump_task = asyncio.create_task(self._pump(ws))

            try:
                if self.client_id and self.client_secret:
                    await self._authenticate()

                for asset in self.assets:
                    instruments = await self.fetch_instruments(asset)
                    logger.info("Subscribing to %d %s instruments", len(instruments), asset)
                    await self._subscribe_tickers(instruments)

                await pump_task
            except Exception:
                pump_task.cancel()
                raise

    async def _pump(self, ws) -> None:
        """Read messages from the WebSocket and dispatch them."""
        async for raw_msg in ws:
            if not self._running:
                break
            await self._handle_message(raw_msg)

    async def _authenticate(self) -> None:
        logger.info("Authenticating as %s", self.client_id)
        await self._rpc(
            "public/auth",
            {
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
        )
        logger.info("Authenticated")

    async def _subscribe_tickers(self, instruments: list[str]) -> None:
        # .raw requires authentication; 100ms is the public equivalent
        suffix = "raw" if (self.client_id and self.client_secret) else "100ms"
        channels = [f"ticker.{i}.{suffix}" for i in instruments]
        # Deribit accepts up to 500 channels per subscribe call
        for chunk_start in range(0, len(channels), 500):
            chunk = channels[chunk_start: chunk_start + 500]
            await self._rpc("public/subscribe", {"channels": chunk}, timeout=60.0)

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Non-JSON message: %s", raw)
            return

        # JSON-RPC response to a request we sent
        if "id" in msg:
            req_id = msg["id"]
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return

        # Subscription notification
        method = msg.get("method")
        if method == "subscription":
            params = msg.get("params", {})
            channel: str = params.get("channel", "")
            data = params.get("data", {})
            if channel.startswith("ticker.") and (channel.endswith(".raw") or channel.endswith(".100ms")):
                instrument = channel.split(".")[1]
                snapshot = self._parse_ticker(instrument, data)
                if snapshot and self.on_ticker:
                    result = self.on_ticker(snapshot)
                    if asyncio.iscoroutine(result):
                        await result

    def _parse_ticker(self, instrument: str, data: dict) -> TickerSnapshot | None:
        try:
            asset = instrument.split("-")[0]
            spot  = float(data.get("index_price") or data.get("underlying_price") or 0)
            iv    = float(data.get("mark_iv") or 0) / 100.0  # Deribit returns IV as %
            mp    = float(data.get("mark_price") or 0)

            bids  = data.get("best_bid_price") or data.get("bid_price") or 0
            asks  = data.get("best_ask_price") or data.get("ask_price") or 0
            oi    = float(data.get("open_interest") or 0)
            bid_sz = float(data.get("best_bid_amount") or 0)
            ask_sz = float(data.get("best_ask_amount") or 0)

            # Deribit mark_price for options is in BTC/ETH terms — convert to USD
            if spot > 0 and mp < 10:
                mp = mp * spot
                bids = float(bids) * spot if bids else 0.0
                asks = float(asks) * spot if asks else 0.0
            else:
                bids = float(bids)
                asks = float(asks)

            if spot <= 0 or iv <= 0:
                return None

            return TickerSnapshot(
                instrument=instrument,
                asset=asset,
                spot=spot,
                mark_price=mp,
                mark_iv=iv,
                bid=bids,
                ask=asks,
                open_interest=oi,
                bid_size=bid_sz,
                ask_size=ask_sz,
            )
        except (KeyError, ValueError, TypeError):
            logger.debug("Could not parse ticker for %s: %s", instrument, data)
            return None

    # ── JSON-RPC helpers ──────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _rpc(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        """Send a JSON-RPC request and await the response."""
        if not self._ws:
            raise RuntimeError("Not connected")
        req_id = self._next_id()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        await self._ws.send(payload)

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"RPC {method} timed out after {timeout}s")


# ── Standalone / debug entry point ────────────────────────────────────────────

async def _debug_main(assets: list[str], paper: bool, timeout: int) -> None:
    """
    Connect to Deribit and print ticker updates to stdout for *timeout* seconds.
    Useful for verifying the feed without running the full bot.
    """
    import sys

    count = 0

    async def on_ticker(snap: TickerSnapshot) -> None:
        nonlocal count
        count += 1
        print(
            f"[{count:>5}] {snap.instrument:<40} "
            f"spot={snap.spot:>10,.2f}  IV={snap.mark_iv*100:>6.2f}%  "
            f"bid={snap.bid:>8.2f}  ask={snap.ask:>8.2f}  "
            f"OI={snap.open_interest:>8.0f}",
            flush=True,
        )
        sys.stdout.flush()

    feed = DeribitFeed(assets=assets, paper=paper, on_ticker=on_ticker)

    print(f"Connecting to Deribit {'paper' if paper else 'LIVE'} — assets: {assets}")
    print(f"Running for {timeout}s (Ctrl+C to stop early)\n")

    task = asyncio.create_task(feed.start())
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        pass
    finally:
        await feed.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    print(f"\nDone. Received {count} ticker updates.")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Debug Deribit ticker feed")
    parser.add_argument("--assets",  nargs="+", default=["BTC"], help="Assets to stream (default: BTC)")
    parser.add_argument("--live",    action="store_true",         help="Use live endpoint instead of paper")
    parser.add_argument("--timeout", type=int, default=60,        help="Seconds to run (default: 60)")
    args = parser.parse_args()

    asyncio.run(_debug_main(args.assets, paper=not args.live, timeout=args.timeout))
