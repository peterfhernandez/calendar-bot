"""
test_feed.py
============
Unit tests for data/deribit_feed.py and data/chain_cache.py.

These tests do NOT require a real Deribit connection — all WebSocket I/O is
mocked so the suite can run offline and in CI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
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


class TestDeribitFeedOfflineTracking(unittest.IsolatedAsyncioTestCase):
    """Verify that repeated connection failures are suppressed after the first warning."""

    async def test_first_failure_sets_offline_flag(self):
        """First connection failure marks the feed as offline."""
        feed = DeribitFeed(assets=["BTC"], paper=True, backoff_max=0.01)
        assert not feed._offline

        call_count = [0]

        async def fail_then_stop():
            call_count[0] += 1
            if call_count[0] >= 2:
                feed._running = False
            raise OSError("refused")

        with patch.object(feed, "_connect_and_stream", side_effect=fail_then_stop):
            await feed.start()

        assert feed._offline

    async def test_repeated_failures_increment_count(self):
        """After the first failure, _retry_count increments on each subsequent attempt."""
        feed = DeribitFeed(assets=["BTC"], paper=True, backoff_max=0.01)
        call_count = [0]

        async def fail_then_stop():
            call_count[0] += 1
            if call_count[0] >= 4:
                feed._running = False
            raise OSError("refused")

        with patch.object(feed, "_connect_and_stream", side_effect=fail_then_stop):
            await feed.start()

        assert feed._offline
        assert feed._retry_count >= 2

    async def test_recovery_clears_offline_flag(self):
        """A successful _connect_and_stream clears the offline flag and resets counter."""
        feed = DeribitFeed(assets=["BTC"], paper=True, backoff_max=0.01)
        feed._offline = True
        feed._retry_count = 3
        feed._running = True

        async def succeed_and_stop():
            feed._running = False  # exit cleanly after one successful run

        with patch.object(feed, "_connect_and_stream", side_effect=succeed_and_stop):
            await feed.start()

        assert not feed._offline
        assert feed._retry_count == 0


class TestFeedOpenPositionCoverage(unittest.IsolatedAsyncioTestCase):
    """Regression tests for Phase 18 Bug 4.

    Open-position instrument names supplied via the extra_instruments
    provider must stay subscribed on every connect AND reconnect, even when
    their expiry falls outside the day window derived from
    NEAR_DAYS_OPTIONS/FAR_DAYS_OPTIONS.
    """

    # The far leg of test-mode trade_id=1 (far_days=51) — outside the 1–28d
    # window that config_test.py's [7, 14] FAR_DAYS_OPTIONS produces.
    _FAR_LEG = "BTC-28AUG26-56000-P"
    _WINDOW  = ["BTC-17JUL26-56000-P", "BTC-24JUL26-56000-P"]

    def _feed(self, provider) -> DeribitFeed:
        return DeribitFeed(assets=["BTC"], extra_instruments=provider)

    async def _run_subscribe_all(self, feed: DeribitFeed) -> list[list[str]]:
        """Run _subscribe_all with mocked I/O; return the subscribed batches."""
        batches: list[list[str]] = []

        async def fake_fetch(asset):
            feed._instruments[asset] = list(self._WINDOW)
            return list(self._WINDOW)

        async def fake_subscribe(instruments):
            batches.append(list(instruments))

        with patch.object(feed, "fetch_instruments", side_effect=fake_fetch), \
             patch.object(feed, "_subscribe_tickers", side_effect=fake_subscribe):
            await feed._subscribe_all()
        return batches

    async def test_open_position_instrument_outside_window_is_subscribed(self):
        feed = self._feed(lambda: [self._FAR_LEG])
        batches = await self._run_subscribe_all(feed)
        subscribed = [name for batch in batches for name in batch]
        self.assertIn(self._FAR_LEG, subscribed)

    async def test_open_position_instrument_stays_subscribed_across_reconnect(self):
        feed = self._feed(lambda: [self._FAR_LEG])
        # First connect
        first = await self._run_subscribe_all(feed)
        # Simulated reconnect — _subscribe_all runs again from _connect_and_stream
        second = await self._run_subscribe_all(feed)
        for batches in (first, second):
            subscribed = [name for batch in batches for name in batch]
            self.assertIn(self._FAR_LEG, subscribed)

    async def test_instrument_already_in_window_not_duplicated(self):
        feed = self._feed(lambda: [self._WINDOW[0]])
        batches = await self._run_subscribe_all(feed)
        subscribed = [name for batch in batches for name in batch]
        self.assertEqual(subscribed.count(self._WINDOW[0]), 1)

    async def test_provider_recomputed_on_each_connect(self):
        """A position closed between reconnects drops out of the extras."""
        extras = [[self._FAR_LEG]]  # mutable so we can change between passes

        feed = self._feed(lambda: extras[0])
        first = await self._run_subscribe_all(feed)
        self.assertIn(self._FAR_LEG, [n for b in first for n in b])

        extras[0] = []  # position closed
        second = await self._run_subscribe_all(feed)
        self.assertNotIn(self._FAR_LEG, [n for b in second for n in b])

    async def test_provider_failure_does_not_break_feed(self):
        def boom():
            raise RuntimeError("db unavailable")

        feed = self._feed(boom)
        batches = await self._run_subscribe_all(feed)
        # Day-window subscription must still have happened
        self.assertEqual(batches, [self._WINDOW])

    async def test_no_provider_behaves_as_before(self):
        feed = DeribitFeed(assets=["BTC"])
        batches = await self._run_subscribe_all(feed)
        self.assertEqual(batches, [self._WINDOW])

    async def test_extras_recorded_in_instruments_map(self):
        feed = self._feed(lambda: [self._FAR_LEG])
        await self._run_subscribe_all(feed)
        self.assertIn(self._FAR_LEG, feed._instruments["BTC"])


# ── Feed freshness watchdog (Phase 23) ─────────────────────────────────────────

class _FakeWS:
    """Minimal async-context-manager / async-iterator stand-in for a WebSocket.

    Yields any queued messages then ends the pump loop; records close() calls.
    """

    def __init__(self, messages=None):
        self._messages = list(messages or [])
        self.closed = False
        self.sent: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


async def _dummy_watchdog(ws):
    await asyncio.sleep(3600)


class TestFeedFreshnessWatchdog(unittest.IsolatedAsyncioTestCase):
    """Phase 23 — the watchdog closes a silently-dead WS so start() reconnects."""

    def _ticker_msg(self) -> str:
        return json.dumps({
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

    async def test_watchdog_closes_ws_when_no_ticker(self):
        feed = DeribitFeed(assets=["BTC"])
        feed._running = True
        feed._last_ticker_at = time.time() - 1000  # long past the timeout
        ws = AsyncMock()
        with patch.object(config, "FEED_WATCHDOG_TIMEOUT_SEC", 2):
            await asyncio.wait_for(feed._watchdog(ws), timeout=5)
        ws.close.assert_awaited_once()

    async def test_watchdog_does_not_close_when_ticker_fresh(self):
        feed = DeribitFeed(assets=["BTC"])
        feed._running = True
        feed._last_ticker_at = time.time()
        ws = AsyncMock()
        with patch.object(config, "FEED_WATCHDOG_TIMEOUT_SEC", 2):
            task = asyncio.create_task(feed._watchdog(ws))
            await asyncio.sleep(1.3)  # > one interval (1.0s), < timeout (2s)
            feed._running = False
            ws.close.assert_not_called()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_handle_message_updates_last_ticker_at(self):
        feed = DeribitFeed(assets=["BTC"])
        feed._last_ticker_at = 0.0
        before = time.time()
        await feed._handle_message(self._ticker_msg())
        self.assertGreaterEqual(feed._last_ticker_at, before)

    async def test_watchdog_created_and_cancelled_cleanly(self):
        feed = DeribitFeed(assets=["BTC"])
        feed._subscribe_all = AsyncMock()
        feed._watchdog = MagicMock(side_effect=_dummy_watchdog)
        fake = _FakeWS()  # no messages → pump ends immediately
        with patch.object(config, "FEED_WATCHDOG_TIMEOUT_SEC", 60), \
             patch("data.deribit_feed.websockets.connect", return_value=fake):
            await feed._connect_and_stream()
        feed._watchdog.assert_called_once()   # watchdog task was created
        self.assertFalse(fake.closed)         # pump finished first; no forced close

    async def test_no_watchdog_task_when_disabled(self):
        feed = DeribitFeed(assets=["BTC"])
        feed._subscribe_all = AsyncMock()
        feed._watchdog = MagicMock(side_effect=_dummy_watchdog)
        fake = _FakeWS()
        with patch.object(config, "FEED_WATCHDOG_TIMEOUT_SEC", 0), \
             patch("data.deribit_feed.websockets.connect", return_value=fake):
            await feed._connect_and_stream()
        feed._watchdog.assert_not_called()    # disabled → no watchdog task created


if __name__ == "__main__":
    unittest.main()
