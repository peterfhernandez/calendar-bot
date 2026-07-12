"""
chain_cache.py
==============
Thread-safe in-memory cache for Deribit option chain snapshots.

Stores the latest TickerSnapshot per instrument with a configurable TTL.
Stale entries (older than TTL) are reported as warnings and excluded from
chain queries so downstream logic never consumes outdated data.

Usage::

    cache = ChainCache(ttl=30.0)
    cache.update(snapshot)                          # called by DeribitFeed
    chain = cache.get_chain("BTC")                  # list[TickerSnapshot]
    spot  = cache.get_spot("BTC")                   # float | None
    cache.print_summary()                           # debug output
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterator

import config
from data.deribit_feed import TickerSnapshot

logger = logging.getLogger(__name__)


class ChainCache:
    """
    In-memory option chain cache with TTL-based stale detection.

    Parameters
    ----------
    ttl:
        Seconds before a snapshot is considered stale
        (default: config.CHAIN_CACHE_TTL_SEC).
    """

    def __init__(self, ttl: float | None = None) -> None:
        self.ttl = ttl if ttl is not None else float(config.CHAIN_CACHE_TTL_SEC)
        self._lock   = threading.RLock()
        self._data:  dict[str, TickerSnapshot] = {}   # instrument → snapshot
        self._spots: dict[str, float] = {}            # asset → latest spot

    # ── Write ─────────────────────────────────────────────────────────────────

    def update(self, snap: TickerSnapshot) -> None:
        """Store or overwrite a snapshot.  Thread-safe."""
        with self._lock:
            self._data[snap.instrument] = snap
            if snap.spot > 0:
                self._spots[snap.asset] = snap.spot

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, instrument: str) -> TickerSnapshot | None:
        """Return the snapshot for *instrument*, or None if absent / stale."""
        with self._lock:
            snap = self._data.get(instrument)
        if snap is None:
            return None
        if self._is_stale(snap):
            logger.warning("Stale data for %s (age %.1fs > TTL %.1fs)", instrument, self._age(snap), self.ttl)
            return None
        return snap

    def get_chain(self, asset: str, include_stale: bool = False) -> list[TickerSnapshot]:
        """
        Return all cached snapshots for *asset*.

        Stale snapshots are excluded by default; pass ``include_stale=True``
        to include them (useful for debug/diagnostics).
        """
        prefix = f"{asset.upper()}-"
        with self._lock:
            snaps = [s for k, s in self._data.items() if k.startswith(prefix)]
        if not include_stale:
            fresh = [s for s in snaps if not self._is_stale(s)]
            stale_count = len(snaps) - len(fresh)
            if stale_count:
                logger.warning("%d stale instrument(s) excluded from %s chain", stale_count, asset)
            return fresh
        return snaps

    def get_spot(self, asset: str) -> float | None:
        """Return the most recent spot price for *asset*, or None."""
        with self._lock:
            return self._spots.get(asset.upper())

    def all_instruments(self) -> list[str]:
        """Return all instrument names currently in the cache."""
        with self._lock:
            return list(self._data.keys())

    def stats(self) -> dict:
        """Return a summary dict suitable for logging or debug display."""
        with self._lock:
            total = len(self._data)
            fresh = sum(1 for s in self._data.values() if not self._is_stale(s))
            by_asset: dict[str, int] = {}
            for snap in self._data.values():
                by_asset[snap.asset] = by_asset.get(snap.asset, 0) + 1
            spots = dict(self._spots)
        return {
            "total":    total,
            "fresh":    fresh,
            "stale":    total - fresh,
            "by_asset": by_asset,
            "spots":    spots,
        }

    # ── Debug / visibility ────────────────────────────────────────────────────

    def print_summary(self, asset: str | None = None) -> None:
        """
        Print a human-readable summary of the cache to stdout.

        Pass *asset* to limit output to a single underlying.
        """
        st = self.stats()
        print(
            f"\n{'─'*60}\n"
            f" ChainCache summary\n"
            f"{'─'*60}\n"
            f"  Total instruments : {st['total']}\n"
            f"  Fresh (<{self.ttl:.0f}s)     : {st['fresh']}\n"
            f"  Stale             : {st['stale']}\n"
        )
        for ast, count in st["by_asset"].items():
            spot = st["spots"].get(ast, 0)
            print(f"  {ast:<4}: {count:>5} instruments   spot={spot:>12,.2f}")

        if asset:
            print(f"\n  Top 20 fresh {asset.upper()} instruments:")
            chain = self.get_chain(asset)
            chain.sort(key=lambda s: s.instrument)
            for snap in chain[:20]:
                age = self._age(snap)
                print(
                    f"    {snap.instrument:<42} "
                    f"IV={snap.mark_iv*100:>6.2f}%  "
                    f"mid={snap.mid:>8.2f}  "
                    f"OI={snap.open_interest:>7.0f}  "
                    f"age={age:>4.0f}s"
                )
        print(f"{'─'*60}\n")

    def iter_fresh(self, asset: str | None = None) -> Iterator[TickerSnapshot]:
        """Yield all non-stale snapshots, optionally filtered by asset."""
        with self._lock:
            snaps = list(self._data.values())
        for snap in snaps:
            if self._is_stale(snap):
                continue
            if asset and snap.asset != asset.upper():
                continue
            yield snap

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _age(self, snap: TickerSnapshot) -> float:
        return time.time() - snap.timestamp

    def _is_stale(self, snap: TickerSnapshot) -> bool:
        return self._age(snap) > self.ttl
