"""
execution/order_manager.py
==========================
Order lifecycle tracking and reconciliation for the calendar spread executor.

Maintains an in-memory registry of all orders placed in the current session
and can reconcile against the Deribit REST API on startup to recover from
crashes or restarts.

Public API
----------
OrderState
    Enum of all order states.

TrackedOrder
    Dataclass representing a single tracked order.

OrderManager
    Thread-safe order registry.  Used by CalendarExecutor to record and
    update order states, and to detect and cancel stuck orders.

reconcile_with_deribit(manager, client_id, client_secret, paper)
    Async function to fetch open orders from Deribit and mark any locally
    tracked orders that are no longer open as CANCELLED or FILLED.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import websockets
import websockets.exceptions

import config

logger = logging.getLogger(__name__)

# Orders older than this that haven't filled are considered "stuck"
STUCK_ORDER_TIMEOUT: int = config.STUCK_ORDER_TIMEOUT_SEC


# ── Order state ───────────────────────────────────────────────────────────────

class OrderState(Enum):
    SUBMITTED = "submitted"
    PARTIAL   = "partial"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    # Cancelled after a timeout with a non-zero filled portion that had to be
    # flattened separately (Phase 26a).  The filled amount is recorded on the
    # TrackedOrder so the partial exposure is never invisible.
    CANCELLED_PARTIAL = "cancelled_partial"
    FAILED    = "failed"


# ── Tracked order ─────────────────────────────────────────────────────────────

@dataclass
class TrackedOrder:
    """Single order tracked through its lifecycle."""

    order_id:    str
    instrument:  str
    direction:   str    # "buy" | "sell"
    amount:      float
    limit_price: float
    label:       str    = ""
    state:       OrderState = OrderState.SUBMITTED
    fill_price:  Optional[float] = None
    filled_amount: float = 0.0   # partial fill recorded on CANCELLED_PARTIAL (Phase 26a)
    submitted_at: float = field(default_factory=time.monotonic)
    updated_at:   float = field(default_factory=time.monotonic)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.submitted_at

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.CANCELLED_PARTIAL,
            OrderState.FAILED,
        )

    @property
    def is_stuck(self) -> bool:
        return (
            not self.is_terminal
            and self.age_seconds > STUCK_ORDER_TIMEOUT
        )


# ── Order manager ─────────────────────────────────────────────────────────────

class OrderManager:
    """
    Thread-safe in-memory registry of all orders for the current session.

    The executor calls track() when submitting and update() when a fill or
    cancellation is confirmed.  The scheduler loop can call find_stuck() and
    cancel stuck orders via the executor or directly via Deribit.
    """

    def __init__(self) -> None:
        self._orders: dict[str, TrackedOrder] = {}
        self._lock = threading.Lock()

    # ── Write ─────────────────────────────────────────────────────────────────

    def track(self, order: TrackedOrder) -> None:
        """Register a newly submitted order."""
        with self._lock:
            self._orders[order.order_id] = order
            logger.debug("Tracking order %s  %s %s  amount=%.4f  price=%.4f",
                         order.order_id, order.direction, order.instrument,
                         order.amount, order.limit_price)

    def update(
        self,
        order_id:      str,
        state:         OrderState,
        fill_price:    Optional[float] = None,
        filled_amount: Optional[float] = None,
    ) -> None:
        """Update an order's state (and optionally its fill price / filled amount)."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                logger.warning("update() called for unknown order_id=%s", order_id)
                return
            order.state      = state
            order.updated_at = time.monotonic()
            if fill_price is not None:
                order.fill_price = fill_price
            if filled_amount is not None:
                order.filled_amount = filled_amount
            logger.debug(
                "Order %s → %s%s",
                order_id, state.value,
                f"  fill={fill_price:.4f}" if fill_price else "",
            )

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, order_id: str) -> TrackedOrder | None:
        with self._lock:
            return self._orders.get(order_id)

    def all_orders(self) -> list[TrackedOrder]:
        with self._lock:
            return list(self._orders.values())

    def open_orders(self) -> list[TrackedOrder]:
        """Orders that are not yet in a terminal state."""
        with self._lock:
            return [o for o in self._orders.values() if not o.is_terminal]

    def find_stuck(self) -> list[TrackedOrder]:
        """Orders that have been open longer than STUCK_ORDER_TIMEOUT."""
        with self._lock:
            return [o for o in self._orders.values() if o.is_stuck]

    def summary(self) -> dict:
        """Return aggregate counts by state for logging/monitoring."""
        with self._lock:
            counts: dict[str, int] = {}
            for o in self._orders.values():
                counts[o.state.value] = counts.get(o.state.value, 0) + 1
            return counts


# ── Deribit reconciliation ────────────────────────────────────────────────────

async def _fetch_deribit_open_orders(
    paper: bool,
    client_id: str,
    client_secret: str,
    currencies: list[str] | None = None,
) -> list[dict]:
    """
    Fetch all open option orders from Deribit via a short-lived WS connection.
    Returns the raw list of order dicts from the Deribit API.

    Orders are fetched for every currency in *currencies* (default:
    config.ASSETS).  Previously this loop hardcoded BTC and ETH, so SOL
    orders were never reconciled on restart (Phase 20d bug fix).

    The endpoint always comes from config.DERIBIT_WS_URL — the *paper*
    argument is kept for backwards API compatibility but the URL is
    resolved by TRADING_MODE like every other module.
    """
    endpoint = config.DERIBIT_WS_URL
    req_id = 0
    pending: dict[int, asyncio.Future] = {}

    async def pump(ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            if rid is not None:
                fut = pending.pop(rid, None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))

    async def rpc(ws, method: str, params: dict) -> dict:
        nonlocal req_id
        req_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending[req_id] = fut
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=config.RPC_TIMEOUT_SEC)

    all_orders: list[dict] = []
    async with websockets.connect(
        endpoint,
        ping_interval=config.DERIBIT_WS_PING_INTERVAL,
        open_timeout=config.DERIBIT_WS_OPEN_TIMEOUT,
        max_size=config.DERIBIT_WS_MAX_SIZE,
    ) as ws:
        pump_task = asyncio.create_task(pump(ws))
        try:
            if client_id and client_secret:
                await rpc(ws, "public/auth", {
                    "grant_type":    "client_credentials",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                })
            for currency in (currencies or config.ASSETS):
                result = await rpc(ws, "private/get_open_orders_by_currency", {
                    "currency": currency.upper(),
                    "kind": "option",
                })
                if isinstance(result, list):
                    all_orders.extend(result)
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass

    return all_orders


async def reconcile_with_deribit(
    manager:       OrderManager,
    paper:         bool = True,
    client_id:     str  = "",
    client_secret: str  = "",
) -> None:
    """
    Fetch open orders from Deribit and reconcile with the local OrderManager.

    Any locally tracked order that is not present in the Deribit open order
    list is assumed to have been filled or cancelled externally and is marked
    CANCELLED (conservatively — the caller should re-check fill records if
    an accurate final state is needed).

    Any stuck orders detected are logged at WARNING level.
    """
    try:
        deribit_open = await _fetch_deribit_open_orders(paper, client_id, client_secret)
    except Exception as exc:
        logger.error("Reconciliation failed: could not fetch Deribit orders: %s", exc)
        return

    deribit_ids = {o["order_id"] for o in deribit_open}
    reconciled = 0
    for order in manager.open_orders():
        if order.order_id not in deribit_ids:
            manager.update(order.order_id, OrderState.CANCELLED)
            logger.info(
                "Reconcile: order %s (%s) not in Deribit open orders → marked CANCELLED",
                order.order_id, order.instrument,
            )
            reconciled += 1

    stuck = manager.find_stuck()
    if stuck:
        logger.warning(
            "%d stuck order(s) detected: %s",
            len(stuck), [o.order_id for o in stuck],
        )

    logger.info(
        "Reconciliation complete: %d Deribit open orders, %d local orders updated",
        len(deribit_open), reconciled,
    )


def reconcile_sync(
    manager:       OrderManager,
    paper:         bool = True,
    client_id:     str  = "",
    client_secret: str  = "",
) -> None:
    """Synchronous wrapper for reconcile_with_deribit."""
    asyncio.run(reconcile_with_deribit(manager, paper, client_id, client_secret))
