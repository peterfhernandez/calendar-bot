"""
test_feed.py
============
Unit tests for data/deribit_feed.py and data/chain_cache.py.

These tests do NOT require a real Deribit connection — all WebSocket I/O is
mocked so the suite can run offline and in CI.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from data.deribit_feed import DeribitFeed, TickerSnapshot
from data.chain_cache import ChainCache


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_snap(
    instrument="BTC-27JUN25-100000-C",
    asset="BTC",
    spot=100_000.0,
    mark_price=2_000.0,
    mark_iv=0.80,
    bid=1_900.0,
    ask=2_100.0,
    open_interest=500.0,
    ts: float | None = None,
) -> TickerSnapshot:
    s = TickerSnapshot(
        instrument=instrument,
        asset=asset,
        spot=spot,
        mark_price=mark_price,
        mark_iv=mark_iv,
        bid=bid,
        ask=ask,
        open_interest=open_interest,
    )
    if ts is not None:
        s.timestamp = ts
    return s


# ── TickerSnapshot tests ──────────────────────────────────────────────────────

class TestTickerSnapshot(unittest.TestCase):

    def test_mid_with_bid_ask(self):
        snap = _make_snap(bid=1_900.0, ask=2_100.0)
        self.assertAlmostEqual(snap.mid, 2_000.0)

    def test_mid_falls_back_to_mark_price_when_no_bid_ask(self):
        snap = _make_snap(bid=0.0, ask=0.0, mark_price=1_800.0)
        self.assertAlmostEqual(snap.mid, 1_800.0)


# ── DeribitFeed._parse_ticker tests ──────────────────────────────────────────

class TestDeribitFeedParseTicker(unittest.TestCase):

    def _feed(self) -> DeribitFeed:
        return DeribitFeed(assets=["BTC"])

    def test_parses_valid_ticker_data(self):
        feed = self._feed()
        data = {
            "index_price":     100_000.0,
            "mark_iv":         80.0,          # Deribit returns IV as %
            "mark_price":      0.020,          # BTC-denominated
            "best_bid_price":  0.019,
            "best_ask_price":  0.021,
            "open_interest":   400.0,
        }
        snap = feed._parse_ticker("BTC-27JUN25-100000-C", data)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.asset, "BTC")
        self.assertAlmostEqual(snap.spot, 100_000.0)
        self.assertAlmostEqual(snap.mark_iv, 0.80)
        # mark_price should be converted to USD (0.020 × 100_000 = 2_000)
        self.assertAlmostEqual(snap.mark_price, 2_000.0, delta=1.0)

    def test_returns_none_for_zero_spot(self):
        feed = self._feed()
        data = {"index_price": 0, "mark_iv": 80.0, "mark_price": 0.02}
        snap = feed._parse_ticker("BTC-27JUN25-100000-C", data)
        self.assertIsNone(snap)

    def test_returns_none_for_zero_iv(self):
        feed = self._feed()
        data = {"index_price": 100_000.0, "mark_iv": 0.0, "mark_price": 0.02}
        snap = feed._parse_ticker("BTC-27JUN25-100000-C", data)
        self.assertIsNone(snap)

    def test_returns_none_for_malformed_data(self):
        feed = self._feed()
        snap = feed._parse_ticker("BTC-27JUN25-100000-C", {"garbage": True})
        self.assertIsNone(snap)


# ── DeribitFeed._handle_message tests ────────────────────────────────────────

class TestDeribitFeedHandleMessage(unittest.TestCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_ticker_notification_calls_on_ticker(self):
        received: list[TickerSnapshot] = []

        async def on_ticker(snap: TickerSnapshot) -> None:
            received.append(snap)

        feed = DeribitFeed(assets=["BTC"], on_ticker=on_ticker)
        msg = json.dumps({
            "method": "subscription",
            "params": {
                "channel": "ticker.BTC-27JUN25-100000-C.raw",
                "data": {
                    "index_price":    100_000.0,
                    "mark_iv":        80.0,
                    "mark_price":     0.020,
                    "best_bid_price": 0.019,
                    "best_ask_price": 0.021,
                    "open_interest":  200.0,
                },
            },
        })
        self._run(feed._handle_message(msg))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].instrument, "BTC-27JUN25-100000-C")

    def test_non_ticker_notification_ignored(self):
        received: list[TickerSnapshot] = []

        async def on_ticker(snap):
            received.append(snap)

        feed = DeribitFeed(assets=["BTC"], on_ticker=on_ticker)
        msg = json.dumps({"method": "heartbeat", "params": {"type": "test"}})
        self._run(feed._handle_message(msg))
        self.assertEqual(received, [])

    def test_rpc_response_resolves_pending_future(self):
        async def _inner():
            feed = DeribitFeed(assets=["BTC"])
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            feed._pending[42] = fut
            msg = json.dumps({"jsonrpc": "2.0", "id": 42, "result": {"ok": True}})
            await feed._handle_message(msg)
            self.assertTrue(fut.done())
            self.assertEqual(fut.result(), {"ok": True})

        asyncio.run(_inner())

    def test_rpc_error_response_sets_exception(self):
        async def _inner():
            feed = DeribitFeed(assets=["BTC"])
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            feed._pending[7] = fut
            msg = json.dumps({"jsonrpc": "2.0", "id": 7, "error": {"message": "bad request"}})
            await feed._handle_message(msg)
            self.assertTrue(fut.done())
            self.assertIsInstance(fut.exception(), RuntimeError)

        asyncio.run(_inner())


# ── ChainCache tests ──────────────────────────────────────────────────────────

class TestChainCache(unittest.TestCase):

    def test_update_and_get(self):
        cache = ChainCache(ttl=30.0)
        snap = _make_snap()
        cache.update(snap)
        result = cache.get("BTC-27JUN25-100000-C")
        self.assertIsNotNone(result)
        self.assertEqual(result.instrument, "BTC-27JUN25-100000-C")

    def test_get_returns_none_for_stale(self):
        cache = ChainCache(ttl=1.0)
        snap = _make_snap(ts=time.time() - 5.0)
        cache.update(snap)
        self.assertIsNone(cache.get("BTC-27JUN25-100000-C"))

    def test_get_chain_excludes_stale(self):
        cache = ChainCache(ttl=1.0)
        fresh = _make_snap(instrument="BTC-27JUN25-100000-C")
        stale = _make_snap(instrument="BTC-27JUN25-90000-C", ts=time.time() - 5.0)
        cache.update(fresh)
        cache.update(stale)
        chain = cache.get_chain("BTC")
        instruments = [s.instrument for s in chain]
        self.assertIn("BTC-27JUN25-100000-C", instruments)
        self.assertNotIn("BTC-27JUN25-90000-C", instruments)

    def test_get_chain_include_stale(self):
        cache = ChainCache(ttl=1.0)
        stale = _make_snap(ts=time.time() - 5.0)
        cache.update(stale)
        chain = cache.get_chain("BTC", include_stale=True)
        self.assertEqual(len(chain), 1)

    def test_get_chain_filters_by_asset(self):
        cache = ChainCache(ttl=30.0)
        btc = _make_snap(instrument="BTC-27JUN25-100000-C", asset="BTC")
        eth = _make_snap(instrument="ETH-27JUN25-4000-C",   asset="ETH", spot=4_000.0, mark_iv=0.70)
        cache.update(btc)
        cache.update(eth)
        btc_chain = cache.get_chain("BTC")
        eth_chain = cache.get_chain("ETH")
        self.assertEqual(len(btc_chain), 1)
        self.assertEqual(len(eth_chain), 1)
        self.assertEqual(btc_chain[0].asset, "BTC")
        self.assertEqual(eth_chain[0].asset, "ETH")

    def test_get_spot(self):
        cache = ChainCache(ttl=30.0)
        cache.update(_make_snap(spot=95_000.0))
        self.assertAlmostEqual(cache.get_spot("BTC"), 95_000.0)

    def test_get_spot_returns_none_for_unknown_asset(self):
        cache = ChainCache(ttl=30.0)
        self.assertIsNone(cache.get_spot("SOL"))

    def test_stats(self):
        cache = ChainCache(ttl=30.0)
        cache.update(_make_snap(instrument="BTC-27JUN25-100000-C", asset="BTC"))
        cache.update(_make_snap(instrument="BTC-27JUN25-90000-C",  asset="BTC"))
        st = cache.stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["fresh"], 2)
        self.assertEqual(st["stale"], 0)
        self.assertIn("BTC", st["by_asset"])

    def test_iter_fresh(self):
        cache = ChainCache(ttl=1.0)
        cache.update(_make_snap(instrument="BTC-27JUN25-100000-C"))
        cache.update(_make_snap(instrument="BTC-27JUN25-90000-C", ts=time.time() - 5.0))
        fresh = list(cache.iter_fresh("BTC"))
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0].instrument, "BTC-27JUN25-100000-C")

    def test_update_overwrites_existing(self):
        cache = ChainCache(ttl=30.0)
        cache.update(_make_snap(mark_iv=0.80))
        cache.update(_make_snap(mark_iv=0.90))
        snap = cache.get("BTC-27JUN25-100000-C")
        self.assertAlmostEqual(snap.mark_iv, 0.90)

    def test_all_instruments(self):
        cache = ChainCache(ttl=30.0)
        cache.update(_make_snap(instrument="BTC-27JUN25-100000-C"))
        cache.update(_make_snap(instrument="BTC-27JUN25-90000-C"))
        instruments = cache.all_instruments()
        self.assertIn("BTC-27JUN25-100000-C", instruments)
        self.assertIn("BTC-27JUN25-90000-C", instruments)


if __name__ == "__main__":
    unittest.main()
