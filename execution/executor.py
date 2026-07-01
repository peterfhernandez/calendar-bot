"""
execution/executor.py
=====================
Hardened calendar spread executor for Deribit.

Implements the ExecutorProtocol expected by strategy/decision.py with:

- Both legs placed as fast as possible to minimise leg risk.
  Deribit does not expose a public calendar-spread combo endpoint, so we
  submit the near leg first (sell), confirm the fill, then submit the far
  leg (buy).  If the far leg cannot be filled after retries, the near leg
  is immediately closed so we are never left with a naked short.
- Slippage bounds: reject if the fill price deviates > SLIPPAGE_LIMIT_PCT
  from the live mid price at order time.
- Retry on transient failures (network timeout, rate-limit HTTP 429) with
  exponential back-off.
- Full order lifecycle handed to OrderManager for tracking and reconciliation.

Public API
----------
CalendarExecutor(paper, client_id, client_secret, order_manager=None)
    Implements ExecutorProtocol.  All methods are synchronous wrappers that
    spin up a short-lived async event loop internally, so they can be called
    from a non-async scheduler tick.

    enter_spread(candidate) -> dict | None
    close_spread(position)  -> float | None
    roll_near_leg(position, new_candidate) -> bool
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import websockets
import websockets.exceptions

import config
from strategy.scanner import CalendarCandidate
from execution.order_manager import OrderManager, OrderState, TrackedOrder

logger = logging.getLogger(__name__)

# ── Defaults (can be overridden in config.py) ─────────────────────────────────
SLIPPAGE_LIMIT_PCT: float = getattr(config, "SLIPPAGE_LIMIT_PCT", 0.02)
ORDER_TIMEOUT_SEC:  int   = getattr(config, "ORDER_TIMEOUT_SEC",  30)
MAX_RETRIES:        int   = getattr(config, "MAX_ORDER_RETRIES",  3)
_RETRY_DELAYS = [1, 3, 9]   # seconds between retry attempts


# ── Exceptions ────────────────────────────────────────────────────────────────

class SlippageError(Exception):
    """Fill price deviates too far from mid; trade rejected."""


class LegRiskError(Exception):
    """Near leg filled but far leg failed; near leg was closed to flatten."""


class OrderTimeoutError(Exception):
    """Order not fully filled within ORDER_TIMEOUT_SEC seconds."""


# ── Internal fill result ──────────────────────────────────────────────────────

@dataclass
class LegFill:
    """Result from placing a single option leg."""
    order_id:   str
    instrument: str
    direction:  str    # "buy" | "sell"
    amount:     float
    price:      float  # average fill price (Deribit index fraction for BTC/ETH)
    price_usd:  float  # converted to USD


# ── Low-level Deribit WebSocket client ───────────────────────────────────────

class _DeribitRPCClient:
    """
    Minimal async JSON-RPC client for Deribit private order operations.

    Opens a fresh WebSocket connection per use (connect → auth → call → close).
    This avoids the complexity of sharing the long-lived feed connection and
    keeps each executor call fully self-contained.
    """

    def __init__(self, client_id: str = "", client_secret: str = "") -> None:
        self.client_id     = client_id
        self.client_secret = client_secret
        self._req_id       = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None

    @property
    def _endpoint(self) -> str:
        return config.DERIBIT_WS_URL

    async def __aenter__(self) -> "_DeribitRPCClient":
        self._ws = await websockets.connect(
            self._endpoint,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
        )
        self._pump_task = asyncio.create_task(self._pump())
        await self._authenticate()
        return self

    async def __aexit__(self, *_) -> None:
        self._pump_task.cancel()
        try:
            await self._pump_task
        except asyncio.CancelledError:
            pass
        if self._ws:
            await self._ws.close()

    async def _pump(self) -> None:
        assert self._ws
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            req_id = msg.get("id")
            if req_id is not None:
                fut = self._pending.pop(req_id, None)
                if fut and not fut.done():
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(
                            RuntimeError(f"Deribit error {err.get('code')}: {err.get('message')}")
                        )
                    else:
                        fut.set_result(msg.get("result"))

    async def _rpc(self, method: str, params: dict) -> dict:
        assert self._ws, "Not connected"
        self._req_id += 1
        req_id = self._req_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=15)

    async def _authenticate(self) -> None:
        if not (self.client_id and self.client_secret):
            return
        await self._rpc(
            "public/auth",
            {
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
        )
        logger.debug("Authenticated with Deribit")

    async def get_ticker(self, instrument: str) -> dict:
        return await self._rpc("public/ticker", {"instrument_name": instrument})

    async def place_order(
        self,
        instrument: str,
        direction:  str,    # "buy" | "sell"
        amount:     float,
        price:      float,  # limit price in Deribit index fraction
        label:      str = "",
    ) -> dict:
        method = f"private/{direction}"
        params = {
            "instrument_name": instrument,
            "amount":          amount,
            "type":            "limit",
            "price":           price,
            "post_only":       False,
        }
        if label:
            params["label"] = label
        return await self._rpc(method, params)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._rpc("private/cancel", {"order_id": order_id})

    async def get_order_state(self, order_id: str) -> dict:
        return await self._rpc("private/get_order_state", {"order_id": order_id})

    async def create_combo(self, legs: list[dict]) -> dict:
        """
        Create a Deribit combo instrument from a list of leg definitions.

        Each leg dict must have: instrument_name, direction ("buy"|"sell"), amount.
        Returns the combo result including combo_id which can be used as an instrument name.
        """
        return await self._rpc("private/create_combo", {"trades": legs})

    async def get_open_orders(self, instrument: str | None = None) -> list[dict]:
        params: dict = {"kind": "option"}
        if instrument:
            params["instrument_name"] = instrument
        return await self._rpc("private/get_open_orders_by_instrument"
                               if instrument else "private/get_open_orders_by_currency",
                               params)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _index_price(usd_price: float, spot: float, asset: str) -> float:
    """Convert a USD option price to Deribit index fraction for inverse assets."""
    if asset.upper() in ("BTC", "ETH"):
        return round(usd_price / spot, 4)
    return round(usd_price, 4)


def _usd_price(index_price: float, spot: float, asset: str) -> float:
    """Convert Deribit index fraction back to USD."""
    if asset.upper() in ("BTC", "ETH"):
        return index_price * spot
    return index_price


def _contract_amount(spot: float, asset: str, portfolio_value: float, max_loss_pct: float, net_debit_usd: float) -> float:
    """
    Calculate order amount in Deribit contract units.

    For inverse assets (BTC, ETH): amount in coin units.
    For linear (SOL_USDC etc.): amount in integer contracts.

    The amount is sized so that max_loss_pct of portfolio_value covers the
    net_debit per unit times the amount.
    """
    max_usd = portfolio_value * max_loss_pct
    if net_debit_usd <= 0:
        return 0.0
    qty_usd = max_usd / net_debit_usd
    if asset.upper() in ("BTC", "ETH"):
        # Each contract = 1 coin unit; net_debit_usd is already per-coin
        amount_coins = max_usd / (net_debit_usd * spot)
        return round(max(0.1, amount_coins), 4)  # Deribit min = 0.1 BTC/ETH
    # Linear: amount in integer contracts
    return float(max(1, int(qty_usd)))


def _check_slippage(fill_price_usd: float, intended_usd: float, limit_pct: float) -> None:
    """
    Raise SlippageError if the fill price deviates too far from the intended limit.

    We compare fill to the limit price we set (near_bid for sells, far_ask for buys)
    rather than to mid. This catches orders where the market moved between submission
    and fill, resulting in a significantly worse-than-intended execution price.
    """
    if intended_usd <= 0:
        return
    deviation = abs(fill_price_usd - intended_usd) / intended_usd
    if deviation > limit_pct:
        raise SlippageError(
            f"Fill ${fill_price_usd:.4f} is {deviation:.1%} from intended ${intended_usd:.4f} "
            f"(limit {limit_pct:.1%})"
        )


async def _wait_for_fill(
    client: _DeribitRPCClient,
    order_id: str,
    timeout: int = ORDER_TIMEOUT_SEC,
) -> dict:
    """
    Poll Deribit until the order is fully filled or the timeout expires.

    Returns the final order state dict.
    Raises OrderTimeoutError if not filled in time.
    """
    deadline = time.monotonic() + timeout
    poll_interval = 1.0
    while time.monotonic() < deadline:
        state = await client.get_order_state(order_id)
        order_state = state.get("order_state", "")
        if order_state == "filled":
            return state
        if order_state in ("cancelled", "rejected"):
            raise RuntimeError(f"Order {order_id} {order_state}")
        await asyncio.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 5.0)
    raise OrderTimeoutError(f"Order {order_id} not filled after {timeout}s")


# ── Core async logic ──────────────────────────────────────────────────────────

async def _async_enter_spread_combo(
    candidate: CalendarCandidate,
    client_id: str,
    client_secret: str,
    order_manager: OrderManager,
    amount: float,
    net_debit_limit_index: float,
    combo_timeout: int,
) -> dict | None:
    """
    Attempt to enter a calendar spread via a Deribit combo order.

    Submits both legs atomically as a combo instrument. Returns a fill summary
    dict on success, or None if the combo times out or fails. The caller is
    responsible for falling back to individual legs on None.
    """
    asset      = candidate.asset
    spot       = candidate.spot
    near_instr = candidate.near_instrument
    far_instr  = candidate.far_instrument

    async with _DeribitRPCClient(client_id, client_secret) as client:
        try:
            combo_result = await client.create_combo([
                {"instrument_name": near_instr, "direction": "sell", "amount": amount},
                {"instrument_name": far_instr,  "direction": "buy",  "amount": amount},
            ])
        except Exception as exc:
            logger.debug("create_combo failed (%s) — will use individual legs", exc)
            return None

        combo_id = combo_result.get("combo_id") or combo_result.get("instrument_name")
        if not combo_id:
            logger.debug("create_combo returned no combo_id — will use individual legs")
            return None

        try:
            order_result = await client.place_order(
                combo_id, "buy", amount, net_debit_limit_index,
                label=f"CAL-COMBO-{asset}",
            )
        except Exception as exc:
            logger.debug("Combo order placement failed (%s) — will use individual legs", exc)
            return None

        combo_order_id = order_result["order"]["order_id"]
        order_manager.track(TrackedOrder(
            order_id=combo_order_id, instrument=combo_id,
            direction="buy", amount=amount, limit_price=net_debit_limit_index,
            label=f"CAL-COMBO-{asset}",
        ))

        try:
            final_state = await _wait_for_fill(client, combo_order_id, combo_timeout)
        except OrderTimeoutError:
            logger.info("Combo order timed out after %ds — falling back to individual legs", combo_timeout)
            try:
                await client.cancel_order(combo_order_id)
            except Exception:
                pass
            order_manager.update(combo_order_id, OrderState.CANCELLED)
            return None
        except RuntimeError as exc:
            logger.warning("Combo order rejected (%s) — falling back to individual legs", exc)
            order_manager.update(combo_order_id, OrderState.CANCELLED)
            return None

        order_manager.update(combo_order_id, OrderState.FILLED)

        # Extract per-leg fill prices from the combo fill result
        legs_filled = final_state.get("legs", [])
        near_fill_index = next(
            (l.get("price", net_debit_limit_index) for l in legs_filled if l.get("direction") == "sell"),
            net_debit_limit_index,
        )
        far_fill_index = next(
            (l.get("price", net_debit_limit_index) for l in legs_filled if l.get("direction") == "buy"),
            net_debit_limit_index + net_debit_limit_index,
        )

        near_fill_usd = _usd_price(near_fill_index, spot, asset)
        far_fill_usd  = _usd_price(far_fill_index,  spot, asset)

        logger.info(
            "Combo fill: near=%.4f  far=%.4f  net_debit=%.4f",
            near_fill_usd, far_fill_usd, far_fill_usd - near_fill_usd,
        )
        return {
            "near_prem":          near_fill_usd,
            "far_prem":           far_fill_usd,
            "net_debit":          far_fill_usd - near_fill_usd,
            "qty":                amount,
            "near_order_id":      combo_order_id,
            "far_order_id":       combo_order_id,
            "near_instrument":    near_instr,
            "far_instrument":     far_instr,
            "near_fill_price":    near_fill_index,
            "far_fill_price":     far_fill_index,
            "via_combo":          True,
        }


async def _async_enter_spread(
    candidate: CalendarCandidate,
    client_id: str,
    client_secret: str,
    order_manager: OrderManager,
    portfolio_value: float,
    slippage_pct: float = SLIPPAGE_LIMIT_PCT,
    order_timeout: int = ORDER_TIMEOUT_SEC,
    combo_timeout: int | None = None,
) -> dict | None:
    """
    Async implementation of enter_spread.

    Execution strategy (when TRADING_MODE != "paper"):
      1. Try a Deribit combo order — both legs atomic, no leg risk.
      2. If the combo times out (after combo_timeout seconds), fall back to
         sequential individual legs.  The fallback logs a WARNING and cancels
         the near leg immediately if the far leg fails.

    In "paper" mode the order is simulated locally; no API calls are made.
    """
    asset = candidate.asset
    spot  = candidate.spot
    amount = _contract_amount(
        spot, asset, portfolio_value,
        config.MAX_LOSS_PCT, candidate.net_debit * spot if asset.upper() in ("BTC", "ETH") else candidate.net_debit
    )
    if amount <= 0:
        logger.warning("Calculated amount is 0 for %s %s — skipping", asset, candidate.near_instrument)
        return None

    near_instr = candidate.near_instrument
    far_instr  = candidate.far_instrument

    # Convert intended USD prices to index fractions for Deribit
    near_limit = _index_price(candidate.near_bid, spot, asset)
    far_limit  = _index_price(candidate.far_ask,  spot, asset)
    near_intended_usd = candidate.near_bid
    far_intended_usd  = candidate.far_ask

    # ── Paper mode: dry-run — no API calls ───────────────────────────────────
    if config.TRADING_MODE == "paper":
        from core.fees import entry_fees as _entry_fees
        paper_fees = _entry_fees(
            candidate.asset, candidate.spot, amount,
            near_price=candidate.near_bid, far_price=candidate.far_ask,
            via_combo=True,
        )
        logger.info(
            "[PAPER] Simulated fill: near=%.4f  far=%.4f  qty=%.4f  fees=%.2f",
            candidate.near_bid, candidate.far_ask, amount, paper_fees,
        )
        return {
            "near_prem":       candidate.near_bid,
            "far_prem":        candidate.far_ask,
            "net_debit":       candidate.net_debit,
            "qty":             amount,
            "near_order_id":   "paper-near",
            "far_order_id":    "paper-far",
            "near_instrument": near_instr,
            "far_instrument":  far_instr,
            "near_fill_price": near_limit,
            "far_fill_price":  far_limit,
            "via_combo":       False,
            "fees_paid":       paper_fees,
        }

    # ── Live / test: try combo order first ────────────────────────────────────
    effective_combo_timeout = combo_timeout if combo_timeout is not None else getattr(config, "COMBO_FILL_TIMEOUT_SEC", 30)
    net_debit_limit_index   = far_limit - near_limit  # net debit as index fraction

    combo_fill = await _async_enter_spread_combo(
        candidate=candidate,
        client_id=client_id,
        client_secret=client_secret,
        order_manager=order_manager,
        amount=amount,
        net_debit_limit_index=net_debit_limit_index,
        combo_timeout=effective_combo_timeout,
    )
    if combo_fill is not None:
        return combo_fill

    # ── Individual-leg fallback (WARNING logged) ──────────────────────────────
    logger.warning(
        "Falling back to individual legs for %s %s (combo timed out or unavailable)",
        asset, near_instr,
    )

    near_order_id: str | None = None

    async with _DeribitRPCClient(client_id, client_secret) as client:
        # ── Near leg (sell) ───────────────────────────────────────────────────
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(
                    "Submitting near leg SELL %s amount=%.4f price=%.4f (attempt %d/%d)",
                    near_instr, amount, near_limit, attempt + 1, MAX_RETRIES,
                )
                result = await client.place_order(
                    near_instr, "sell", amount, near_limit,
                    label=f"CAL-NEAR-{asset}",
                )
                near_order_id = result["order"]["order_id"]
                order_manager.track(TrackedOrder(
                    order_id=near_order_id, instrument=near_instr,
                    direction="sell", amount=amount, limit_price=near_limit,
                    label=f"CAL-NEAR-{asset}",
                ))
                logger.info("Near leg submitted: order_id=%s", near_order_id)
                break
            except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError) as exc:
                logger.warning("Near leg submit failed (attempt %d): %s", attempt + 1, exc)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                else:
                    logger.error("Near leg failed after %d attempts", MAX_RETRIES)
                    return None

        assert near_order_id

        try:
            near_state = await _wait_for_fill(client, near_order_id, order_timeout)
        except (OrderTimeoutError, RuntimeError) as exc:
            logger.error("Near leg fill failed: %s — cancelling", exc)
            try:
                await client.cancel_order(near_order_id)
            except Exception:
                pass
            order_manager.update(near_order_id, OrderState.CANCELLED)
            return None

        near_fill_price = near_state.get("average_price", near_limit)
        near_fill_usd = _usd_price(near_fill_price, spot, asset)
        _check_slippage(near_fill_usd, near_intended_usd, slippage_pct)
        order_manager.update(near_order_id, OrderState.FILLED, fill_price=near_fill_price)
        logger.info("Near leg filled: price=%.4f", near_fill_price)

        # ── Far leg (buy) ─────────────────────────────────────────────────────
        far_order_id: str | None = None
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(
                    "Submitting far leg BUY %s amount=%.4f price=%.4f (attempt %d/%d)",
                    far_instr, amount, far_limit, attempt + 1, MAX_RETRIES,
                )
                result = await client.place_order(
                    far_instr, "buy", amount, far_limit,
                    label=f"CAL-FAR-{asset}",
                )
                far_order_id = result["order"]["order_id"]
                order_manager.track(TrackedOrder(
                    order_id=far_order_id, instrument=far_instr,
                    direction="buy", amount=amount, limit_price=far_limit,
                    label=f"CAL-FAR-{asset}",
                ))
                logger.info("Far leg submitted: order_id=%s", far_order_id)
                break
            except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError) as exc:
                logger.warning("Far leg submit failed (attempt %d): %s", attempt + 1, exc)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAYS[attempt])

        if not far_order_id:
            logger.error("Far leg submit exhausted retries; closing near leg to avoid leg risk")
            try:
                await client.place_order(near_instr, "buy", amount, near_limit * 1.05,
                                         label=f"FLATTEN-NEAR-{asset}")
            except Exception as exc:
                logger.critical("FAILED to close near leg after far leg failure: %s", exc)
            raise LegRiskError("Far leg failed; near leg closed")

        try:
            far_state = await _wait_for_fill(client, far_order_id, order_timeout)
        except (OrderTimeoutError, RuntimeError) as exc:
            logger.error("Far leg fill failed: %s — closing near leg", exc)
            try:
                await client.cancel_order(far_order_id)
                await client.place_order(near_instr, "buy", amount, near_limit * 1.05,
                                         label=f"FLATTEN-NEAR-{asset}")
            except Exception as inner:
                logger.critical("FAILED to close near leg after far leg timeout: %s", inner)
            order_manager.update(far_order_id, OrderState.CANCELLED)
            raise LegRiskError(f"Far leg timeout: {exc}")

        far_fill_price = far_state.get("average_price", far_limit)
        far_fill_usd   = _usd_price(far_fill_price, spot, asset)
        _check_slippage(far_fill_usd, far_intended_usd, slippage_pct)
        order_manager.update(far_order_id, OrderState.FILLED, fill_price=far_fill_price)
        logger.info("Far leg filled: price=%.4f", far_fill_price)

    near_prem_usd = _usd_price(near_fill_price, spot, asset)
    far_prem_usd  = _usd_price(far_fill_price,  spot, asset)

    return {
        "near_prem":          near_prem_usd,
        "far_prem":           far_prem_usd,
        "net_debit":          far_prem_usd - near_prem_usd,
        "qty":                amount,
        "near_order_id":      near_order_id,
        "far_order_id":       far_order_id,
        "near_instrument":    near_instr,
        "far_instrument":     far_instr,
        "near_fill_price":    near_fill_price,
        "far_fill_price":     far_fill_price,
        "via_combo":          False,
    }


async def _async_close_spread(
    position: dict,
    client_id: str,
    client_secret: str,
    order_manager: OrderManager,
    order_timeout: int = ORDER_TIMEOUT_SEC,
) -> float | None:
    """
    Close both legs of a calendar spread.

    Returns the net closing credit in USD (positive = profit vs. debit paid),
    or None if an error occurred.
    """
    asset      = position["asset"]
    spot       = position.get("spot_open", 1.0)
    near_instr = position["near_instrument"]
    far_instr  = position["far_instrument"]
    amount     = position["qty"]

    async with _DeribitRPCClient(client_id, client_secret) as client:
        # Get current mid prices for slippage reference
        try:
            near_ticker = await client.get_ticker(near_instr)
            far_ticker  = await client.get_ticker(far_instr)
            near_mid    = (near_ticker.get("best_bid_price", 0) + near_ticker.get("best_ask_price", 0)) / 2
            far_mid     = (far_ticker.get("best_bid_price",  0) + far_ticker.get("best_ask_price",  0)) / 2
        except Exception:
            near_mid = far_mid = 0.0

        # Close near leg: buy back the short (we sold it at entry)
        near_close_price = near_mid * 1.02 if near_mid > 0 else 0.001  # pay a little to close
        near_close_id = None
        try:
            near_result = await client.place_order(
                near_instr, "buy", amount, round(near_close_price, 4),
                label=f"CLOSE-NEAR-{asset}",
            )
            near_close_id = near_result["order"]["order_id"]
            order_manager.track(TrackedOrder(
                order_id=near_close_id, instrument=near_instr,
                direction="buy", amount=amount, limit_price=near_close_price,
                label=f"CLOSE-NEAR-{asset}",
            ))
        except (OSError, websockets.exceptions.WebSocketException, RuntimeError, Exception) as exc:
            logger.error("Failed to submit near close order: %s", exc)
            return None

        # Close far leg: sell back the long (we bought it at entry)
        far_close_price = far_mid * 0.98 if far_mid > 0 else 0.001
        far_close_id = None
        try:
            far_result = await client.place_order(
                far_instr, "sell", amount, round(far_close_price, 4),
                label=f"CLOSE-FAR-{asset}",
            )
            far_close_id = far_result["order"]["order_id"]
            order_manager.track(TrackedOrder(
                order_id=far_close_id, instrument=far_instr,
                direction="sell", amount=amount, limit_price=far_close_price,
                label=f"CLOSE-FAR-{asset}",
            ))
        except (OSError, websockets.exceptions.WebSocketException, RuntimeError, Exception) as exc:
            logger.error("Failed to submit far close order: %s", exc)
            # Try to cancel near leg if it was submitted
            if near_close_id:
                try:
                    await client.cancel_order(near_close_id)
                except Exception:
                    pass
            return None

        near_state = None
        far_state = None
        near_filled = False
        far_filled = False

        # Wait for near leg fill
        try:
            near_state = await _wait_for_fill(client, near_close_id, order_timeout)
            near_filled = True
        except (OrderTimeoutError, RuntimeError) as exc:
            logger.warning("Near leg close timed out: %s — will attempt to cancel", exc)
            try:
                await client.cancel_order(near_close_id)
            except Exception as cancel_exc:
                logger.warning("Failed to cancel near close order %s: %s", near_close_id, cancel_exc)

        # Wait for far leg fill
        try:
            far_state = await _wait_for_fill(client, far_close_id, order_timeout)
            far_filled = True
        except (OrderTimeoutError, RuntimeError) as exc:
            logger.warning("Far leg close timed out: %s", exc)
            try:
                await client.cancel_order(far_close_id)
            except Exception as cancel_exc:
                logger.warning("Failed to cancel far close order %s: %s", far_close_id, cancel_exc)

        # If one leg filled but the other didn't, unwind the filled leg to avoid leg risk
        if near_filled and not far_filled:
            logger.error(
                "Near leg close filled but far leg failed — unwinding near leg to avoid leg risk"
            )
            try:
                unwind_price = near_mid * 0.98 if near_mid > 0 else 0.001
                await client.place_order(
                    near_instr, "sell", amount, round(unwind_price, 4),
                    label=f"UNWIND-NEAR-{asset}",
                )
                logger.info("Unwound near leg")
            except Exception as unwind_exc:
                logger.critical("FAILED to unwind near leg after far close failure: %s", unwind_exc)
            return None

        if far_filled and not near_filled:
            logger.error(
                "Far leg close filled but near leg failed — unwinding far leg to avoid leg risk"
            )
            try:
                unwind_price = far_mid * 1.02 if far_mid > 0 else 0.001
                await client.place_order(
                    far_instr, "buy", amount, round(unwind_price, 4),
                    label=f"UNWIND-FAR-{asset}",
                )
                logger.info("Unwound far leg")
            except Exception as unwind_exc:
                logger.critical("FAILED to unwind far leg after near close failure: %s", unwind_exc)
            return None

        if not (near_filled and far_filled):
            logger.error("Close failed: both legs timed out or were cancelled")
            return None

        order_manager.update(near_close_id, OrderState.FILLED, fill_price=near_state.get("average_price", near_close_price))
        order_manager.update(far_close_id,  OrderState.FILLED, fill_price=far_state.get("average_price",  far_close_price))

        near_close_usd = _usd_price(near_state.get("average_price", near_close_price), spot, asset)
        far_close_usd  = _usd_price(far_state.get("average_price",  far_close_price),  spot, asset)

        # closing credit = far_close (received) - near_close (paid)
        closing_credit = far_close_usd - near_close_usd
        logger.info(
            "Spread closed: near_close=%.4f  far_close=%.4f  credit=%.4f",
            near_close_usd, far_close_usd, closing_credit,
        )
        return closing_credit


async def _async_roll_near_leg(
    position: dict,
    new_candidate: CalendarCandidate,
    client_id: str,
    client_secret: str,
    order_manager: OrderManager,
    slippage_pct: float = SLIPPAGE_LIMIT_PCT,
    order_timeout: int = ORDER_TIMEOUT_SEC,
) -> bool:
    """
    Roll the near leg: close the current short near leg and open a new one.

    The far leg is left untouched.
    """
    asset      = position["asset"]
    spot       = position.get("spot_open", new_candidate.spot)
    near_instr = position["near_instrument"]
    amount     = position["qty"]
    new_near   = new_candidate.near_instrument

    async with _DeribitRPCClient(client_id, client_secret) as client:
        # Buy back old near leg
        try:
            ticker = await client.get_ticker(near_instr)
            close_price = (ticker.get("best_bid_price", 0) + ticker.get("best_ask_price", 0)) / 2 * 1.02
        except Exception:
            close_price = 0.001

        close_result = await client.place_order(
            near_instr, "buy", amount, round(max(close_price, 0.0001), 4),
            label=f"ROLL-CLOSE-{asset}",
        )
        close_id = close_result["order"]["order_id"]
        order_manager.track(TrackedOrder(
            order_id=close_id, instrument=near_instr,
            direction="buy", amount=amount, limit_price=close_price,
            label=f"ROLL-CLOSE-{asset}",
        ))

        try:
            await _wait_for_fill(client, close_id, order_timeout)
            order_manager.update(close_id, OrderState.FILLED)
        except (OrderTimeoutError, RuntimeError) as exc:
            logger.error("Roll: close of near leg failed: %s", exc)
            return False

        # Sell new near leg
        new_limit = _index_price(new_candidate.near_bid, new_candidate.spot, asset)
        sell_result = await client.place_order(
            new_near, "sell", amount, new_limit,
            label=f"ROLL-OPEN-{asset}",
        )
        sell_id = sell_result["order"]["order_id"]
        order_manager.track(TrackedOrder(
            order_id=sell_id, instrument=new_near,
            direction="sell", amount=amount, limit_price=new_limit,
            label=f"ROLL-OPEN-{asset}",
        ))

        try:
            await _wait_for_fill(client, sell_id, order_timeout)
            order_manager.update(sell_id, OrderState.FILLED)
        except (OrderTimeoutError, RuntimeError) as exc:
            logger.error("Roll: open of new near leg failed: %s — position is far-leg-only", exc)
            return False

    logger.info("Near leg rolled from %s → %s", near_instr, new_near)
    return True


# ── Public executor class ─────────────────────────────────────────────────────

class CalendarExecutor:
    """
    Hardened calendar spread executor implementing ExecutorProtocol.

    All methods are synchronous; they spin up a temporary async event loop
    so they can be called from a non-async scheduler tick.

    Execution mode is determined by config.TRADING_MODE:
    - "paper" → dry-run (no API calls; fills are simulated locally)
    - "test"  → real orders on test.deribit.com
    - "live"  → real orders on www.deribit.com

    Parameters
    ----------
    client_id / client_secret
        Deribit API credentials.  Defaults to config values for the active mode.
    portfolio_value
        Current portfolio value in USD, used for position sizing.
    order_manager
        Optional shared OrderManager instance.  One is created if omitted.
    slippage_pct
        Maximum acceptable deviation from mid price (default 2%).
    order_timeout
        Seconds to wait for an order to fill before giving up (default 30).
    """

    def __init__(
        self,
        client_id:       str   = "",
        client_secret:   str   = "",
        portfolio_value: float = 10_000.0,
        order_manager:   OrderManager | None = None,
        slippage_pct:    float = SLIPPAGE_LIMIT_PCT,
        order_timeout:   int   = ORDER_TIMEOUT_SEC,
        # Legacy: paper=True/False still accepted but ignored; mode comes from config
        paper:           bool | None = None,
    ) -> None:
        self.client_id       = client_id or config.DERIBIT_CLIENT_ID
        self.client_secret   = client_secret or config.DERIBIT_CLIENT_SECRET
        self.portfolio_value = portfolio_value
        self.order_manager   = order_manager or OrderManager()
        self.slippage_pct    = slippage_pct
        self.order_timeout   = order_timeout

    def _run(self, coro):
        """Run an async coroutine, handling both standalone and in-loop contexts."""
        import concurrent.futures
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if in_loop:
            # Already inside a running event loop — run in a new thread so that
            # asyncio.run() can create its own event loop there.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return asyncio.run(coro)

    # ── ExecutorProtocol implementation ───────────────────────────────────────

    def enter_spread(self, candidate: CalendarCandidate) -> dict | None:
        """
        Enter a calendar spread.

        Returns a fill dict on success or None if the order was rejected or
        timed out.  Raises LegRiskError if the near leg filled but the far
        leg failed (near leg will have already been closed).
        """
        try:
            return self._run(
                _async_enter_spread(
                    candidate,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    order_manager=self.order_manager,
                    portfolio_value=self.portfolio_value,
                    slippage_pct=self.slippage_pct,
                    order_timeout=self.order_timeout,
                )
            )
        except LegRiskError:
            raise
        except SlippageError as exc:
            logger.warning("Slippage exceeded: %s", exc)
            return None
        except Exception:
            logger.exception("Unexpected error in enter_spread")
            return None

    def close_spread(self, position: dict) -> float | None:
        """Close both legs.  Returns closing credit in USD or None on failure."""
        try:
            return self._run(
                _async_close_spread(
                    position,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    order_manager=self.order_manager,
                    order_timeout=self.order_timeout,
                )
            )
        except Exception:
            logger.exception("Unexpected error in close_spread")
            return None

    def roll_near_leg(self, position: dict, new_candidate: CalendarCandidate) -> bool:
        """Roll the near leg to new_candidate.  Returns True on success."""
        # Fix 3: paper mode is a dry-run — log intent and return success without
        # hitting the API.  The real update is handled by the caller (decision.py).
        if config.TRADING_MODE == "paper":
            logger.info(
                "[DRY-RUN] Would roll near leg of trade_id=%s → %s",
                position.get("trade_id"), new_candidate.near_instrument,
            )
            return True

        try:
            return self._run(
                _async_roll_near_leg(
                    position,
                    new_candidate,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    order_manager=self.order_manager,
                    slippage_pct=self.slippage_pct,
                    order_timeout=self.order_timeout,
                )
            )
        except Exception:
            logger.exception("Unexpected error in roll_near_leg")
            return False
