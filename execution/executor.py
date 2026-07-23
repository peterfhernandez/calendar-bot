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
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
from typing import Optional

import websockets
import websockets.exceptions

import config
from strategy.scanner import CalendarCandidate
from execution.order_manager import OrderManager, OrderState, TrackedOrder

logger = logging.getLogger(__name__)

# ── Defaults (all sourced from config.py — Phase 20b/20c) ─────────────────────
SLIPPAGE_LIMIT_PCT: float = config.SLIPPAGE_LIMIT_PCT
ORDER_TIMEOUT_SEC:  int   = config.ORDER_TIMEOUT_SEC
MAX_RETRIES:        int   = config.MAX_ORDER_RETRIES
_RETRY_DELAYS = config.ORDER_RETRY_DELAYS   # seconds between retry attempts

# ── Tick-size cache for price rounding ─────────────────────────────────────────
_TICK_SIZE_CACHE: dict[str, float] = {}          # instrument_name → base tick_size
_TICK_STEPS_CACHE: dict[str, list] = {}          # instrument_name → tick_size_steps (per-price-band overrides)

# ── Amount-validity cache (Phase 25a) ─────────────────────────────────────────
# instrument_name → (min_trade_amount, amount_step).  Deribit rejects an order
# whose amount is below the instrument minimum or not on the amount step with
# "-32602 Invalid params" (the cause of every ETH entry failing in the 2026-07
# test run, since the executor floored everything to 0.1 while ETH options
# require a minimum of 1 in integer steps).
_AMOUNT_INFO_CACHE: dict[str, tuple[float, float]] = {}


# ── Exceptions ────────────────────────────────────────────────────────────────

class SlippageError(Exception):
    """Fill price deviates too far from mid; trade rejected."""


class AmountBelowMinimumError(Exception):
    """Order amount is below the instrument's exchange minimum after clamping to step."""


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
            ping_interval=config.DERIBIT_WS_PING_INTERVAL,
            ping_timeout=config.DERIBIT_WS_PING_TIMEOUT,
            open_timeout=config.DERIBIT_WS_OPEN_TIMEOUT,
            max_size=config.DERIBIT_WS_MAX_SIZE,  # large Deribit responses
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
        return await asyncio.wait_for(fut, timeout=config.RPC_TIMEOUT_SEC)

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

    async def get_instrument(self, instrument: str) -> dict:
        """
        Fetch a single instrument's metadata (including ``tick_size`` and
        ``tick_size_steps``) for price rounding.

        Uses Deribit's ``public/get_instrument`` (singular) endpoint, which
        accepts an ``instrument_name`` and returns the instrument object
        directly.  (The plural ``public/get_instruments`` endpoint takes a
        ``currency``/``kind`` and returns a list — it does not accept
        ``instrument_name`` — so calling it here silently returned the wrong
        shape and broke tick-size lookup.)
        """
        return await self._rpc("public/get_instrument", {"instrument_name": instrument})

    async def _fetch_tick_info(self, instrument: str) -> tuple[float | None, list | None]:
        """
        Fetch and cache an instrument's ``tick_size`` and ``tick_size_steps``.

        Retries up to ``config.TICK_SIZE_FETCH_RETRIES`` extra times on failure.
        A failure is logged loudly (naming the instrument) rather than silently
        swallowed — an off-tick price is the deterministic cause of the
        "-32602 Invalid params" close/roll rejections (Phase 22d).  Returns
        ``(None, None)`` when the tick size cannot be obtained, so the caller
        falls back to 4-decimal rounding.
        """
        attempts = config.TICK_SIZE_FETCH_RETRIES + 1
        for attempt in range(attempts):
            try:
                meta = await self._rpc(
                    "public/get_instrument", {"instrument_name": instrument}
                )
                # public/get_instrument returns the instrument object directly.
                if meta and isinstance(meta, dict):
                    tick_size = meta.get("tick_size")
                    tick_steps = meta.get("tick_size_steps") or []
                    if tick_size:
                        _TICK_SIZE_CACHE[instrument] = tick_size
                        _TICK_STEPS_CACHE[instrument] = tick_steps
                        return tick_size, tick_steps
            except Exception as exc:
                logger.warning(
                    "Tick-size fetch failed for %s (attempt %d/%d): %s",
                    instrument, attempt + 1, attempts, exc,
                )
        logger.warning(
            "Could not obtain tick size for %s after %d attempt(s) — falling back "
            "to 4-decimal rounding; the order price may be rejected as off-tick",
            instrument, attempts,
        )
        return None, None

    async def _fetch_amount_info(self, instrument: str) -> tuple[float, float] | None:
        """
        Fetch and cache an instrument's ``min_trade_amount`` and amount step
        (``contract_size``) from ``public/get_instrument`` (Phase 25a).

        Returns ``(min_trade_amount, amount_step)`` or ``None`` on failure so the
        caller falls back to the static per-asset table in config.
        """
        try:
            meta = await self._rpc("public/get_instrument", {"instrument_name": instrument})
        except Exception as exc:
            logger.warning("Amount-info fetch failed for %s: %s", instrument, exc)
            return None
        if not isinstance(meta, dict):
            return None
        min_amt = meta.get("min_trade_amount")
        step    = meta.get("contract_size") or min_amt
        if min_amt and step:
            info = (float(min_amt), float(step))
            _AMOUNT_INFO_CACHE[instrument] = info
            return info
        return None

    async def clamp_amount(self, instrument: str, amount: float) -> float | None:
        """
        Round ``amount`` down to the instrument's amount step and return it, or
        ``None`` if the result is below the exchange minimum (Phase 25a).

        Uses live ``public/get_instrument`` metadata (cached); on fetch failure
        falls back to ``config.DEFAULT_MIN_TRADE_AMOUNTS`` keyed by the asset and
        logs the fallback loudly — never silently submits an unvalidated amount.
        """
        info = _AMOUNT_INFO_CACHE.get(instrument)
        if info is None:
            info = await self._fetch_amount_info(instrument)
        if info is None:
            asset = _asset_from_instrument(instrument)
            info = config.DEFAULT_MIN_TRADE_AMOUNTS.get(asset, config.DEFAULT_MIN_TRADE_AMOUNT)
            logger.warning(
                "AMOUNT GATE: could not fetch live minimum for %s — using static "
                "fallback (min=%s, step=%s)", instrument, info[0], info[1],
            )
        return _clamp_amount_to_step(amount, info[0], info[1])

    async def place_order(
        self,
        instrument: str,
        direction:  str,    # "buy" | "sell"
        amount:     float,
        price:      float,  # limit price in Deribit index fraction
        label:      str = "",
        validate_amount: bool = True,
    ) -> dict:
        # Validate/clamp the order amount to the instrument's exchange minimum
        # and step (Phase 25a) — an off-minimum amount is rejected with "-32602
        # Invalid params".  Combo placements pass validate_amount=False (the
        # combo instrument has its own minimum handled by the exchange).
        if validate_amount:
            clamped = await self.clamp_amount(instrument, amount)
            if clamped is None:
                raise AmountBelowMinimumError(
                    f"amount {amount} below exchange minimum for {instrument}"
                )
            amount = clamped

        # Round price to instrument's valid tick size to prevent "-32602 Invalid
        # params" errors.  Tick size varies with option price level
        # (tick_size_steps); try cache first, then fetch (loud on failure).
        tick_size = _TICK_SIZE_CACHE.get(instrument)
        tick_steps = _TICK_STEPS_CACHE.get(instrument)
        if tick_size is None:
            tick_size, tick_steps = await self._fetch_tick_info(instrument)

        # Round to the valid tick (or default 4 decimals)
        rounded_price = _round_to_tick(price, instrument, tick_size, tick_steps)

        method = f"private/{direction}"
        params = {
            "instrument_name": instrument,
            "amount":          amount,
            "type":            "limit",
            "price":           rounded_price,
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


def _asset_from_instrument(instrument: str) -> str:
    """Return the upper-cased asset prefix of a Deribit instrument name.

    (Phase 25b removed the old ``_contract_amount`` sizing helper — the executor
    now submits the sizer-approved qty directly instead of recomputing it.)
    """
    return instrument.split("-", 1)[0].upper() if instrument else ""


def _clamp_amount_to_step(amount: float, min_amount: float, step: float) -> float | None:
    """
    Round *amount* down to a multiple of *step* and return it, or ``None`` if the
    result is below *min_amount* (Phase 25a).

    Rounding is done in ``Decimal`` step-count space to avoid float drift so the
    submitted amount lands exactly on the exchange grid.
    """
    if step <= 0:
        step = min_amount if min_amount > 0 else 1.0
    step_dec = Decimal(str(step))
    steps = (Decimal(str(amount)) / step_dec).to_integral_value(rounding=ROUND_FLOOR)
    clamped = float(steps * step_dec)
    if clamped < min_amount or clamped <= 0:
        return None
    return clamped


def _effective_tick_size(price: float, base_tick: float, tick_size_steps: list | None) -> float:
    """
    Resolve the tick size that applies at *price*, honouring Deribit's per-band
    ``tick_size_steps`` overrides.

    Each step is a dict ``{"above_price": X, "tick_size": Y}`` meaning that once
    the price is >= X, tick Y applies instead of the base tick.  Steps are
    ascending by ``above_price``; the last matching step wins.  Reading these
    (rather than only the flat ``tick_size``) prevents an off-grid price for an
    instrument trading above its first tick threshold (Phase 22d).
    """
    tick = base_tick
    if tick_size_steps:
        for step in tick_size_steps:
            try:
                above = step.get("above_price")
                step_tick = step.get("tick_size")
            except AttributeError:
                continue
            if above is not None and step_tick and price >= above:
                tick = step_tick
    return tick


def _round_to_tick(
    price: float,
    instrument: str,
    tick_size: float | None = None,
    tick_size_steps: list | None = None,
) -> float:
    """
    Round a price to the instrument's valid tick size (Deribit's minimum price increment).

    Deribit's options use variable tick sizes that scale with price (tick_size_steps).
    Rounding is done in tick-count space using ``Decimal`` (not float division) so the
    result cannot drift off-grid due to floating-point representation error — that drift,
    combined with the old synthetic mid-based close prices, reproduced the
    "-32602 Invalid params" rejections (Phase 22d).

    Parameters
    ----------
    price : float
        The price to round (in index fraction for BTC/ETH, USD for linear assets).
    instrument : str
        The instrument name (e.g., "BTC-3JAN26-60000-C").
    tick_size : float | None
        Explicit base tick size. If None, attempts to fetch from cache or uses a safe default.
    tick_size_steps : list | None
        Optional per-price-band tick overrides. If None, falls back to the cached steps.

    Returns
    -------
    float
        The price rounded to the nearest valid tick.
    """
    # If tick_size not provided, try to fetch from cache
    if tick_size is None:
        tick_size = _TICK_SIZE_CACHE.get(instrument)
    if tick_size_steps is None:
        tick_size_steps = _TICK_STEPS_CACHE.get(instrument)

    # If still no tick size, use a conservative default
    # (Deribit typically uses 0.0001 or larger increments for options)
    if tick_size is None:
        return round(price, 4)  # Safe default: 4 decimal places

    if tick_size <= 0:
        return price  # Safeguard

    # Resolve the tick applicable at this price band, then round in integer
    # tick-count space with Decimal to avoid float drift off the grid.
    eff_tick = _effective_tick_size(price, tick_size, tick_size_steps)
    if eff_tick <= 0:
        eff_tick = tick_size
    tick_dec = Decimal(str(eff_tick))
    steps = (Decimal(str(price)) / tick_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(steps * tick_dec)


def _check_slippage(
    fill_price_usd: float,
    intended_usd:   float,
    limit_pct:      float,
    side:           str | None = None,   # "buy" | "sell" | None (legacy symmetric)
) -> None:
    """
    Raise SlippageError only on an *adverse* fill (Phase 26b).

    We compare the fill to the limit price we set (near_bid for sells, far_ask for
    buys) rather than to mid.  The check is directional: for a ``sell`` only a fill
    *below* the intended price is adverse (we received less); for a ``buy`` only a
    fill *above* it is adverse (we paid more).  Price *improvement* always passes —
    the old symmetric ``abs()`` check rejected the only deviation a limit order can
    actually produce (an improved fill), abandoning good spreads.

    When ``side`` is ``None`` the legacy symmetric behaviour is kept so existing
    callers and tests are unaffected.  Since both legs are limit orders, an adverse
    fill is in principle unreachable — this remains purely as a sanity invariant.
    """
    if intended_usd <= 0:
        return
    signed = fill_price_usd - intended_usd
    if side == "buy":
        adverse = max(0.0, signed)      # filled above intended → paid more
    elif side == "sell":
        adverse = max(0.0, -signed)     # filled below intended → received less
    else:
        adverse = abs(signed)           # legacy symmetric
    deviation = adverse / intended_usd
    if deviation > limit_pct:
        raise SlippageError(
            f"Adverse fill ${fill_price_usd:.4f} is {deviation:.1%} worse than "
            f"intended ${intended_usd:.4f} (limit {limit_pct:.1%}, side={side})"
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


async def _cancel_and_flatten(
    client:        _DeribitRPCClient,
    order_manager: OrderManager,
    order_id:      str,
    instrument:    str,
    orig_direction: str,     # direction of the order being cancelled ("buy"|"sell")
    reverse_price: float,    # crossed limit price for the flattening order
    asset:         str,
    notifier=None,
    context:       str = "",
) -> float:
    """
    Cancel *order_id* and flatten any partial fill it left behind (Phase 26a).

    Cancelling a partially-filled Deribit limit order only removes the *unfilled*
    remainder — the contracts that already filled stay on the exchange.  The old
    timeout-cancel paths ignored this, leaving untracked naked inventory (the
    2026-07 run left −13 naked short puts from three timed-out legged entries).

    This reads the order's ``filled_amount`` (from the ``private/cancel`` response,
    falling back to ``private/get_order_state``) and, if non-zero, submits an
    immediate reverse order for exactly that amount at a crossed price so the
    position returns to its pre-order state.  A flatten failure is logged
    ``CRITICAL`` and raises a one-shot operator alert naming the exact naked
    exposure — it must never fail silently.

    Returns the flattened (filled) amount; ``0.0`` when nothing had filled.
    """
    filled = 0.0
    try:
        cancel_result = await client.cancel_order(order_id)
        if isinstance(cancel_result, dict):
            filled = float(cancel_result.get("filled_amount", 0.0) or 0.0)
    except Exception as exc:
        logger.warning("Cancel of %s (%s) failed: %s", order_id, instrument, exc)

    # Fall back to an explicit order-state query when the cancel response did not
    # carry the filled amount (or the cancel itself raised).
    if filled <= 0:
        try:
            state = await client.get_order_state(order_id)
            filled = float(state.get("filled_amount", 0.0) or 0.0)
        except Exception:
            pass

    if filled <= 0:
        order_manager.update(order_id, OrderState.CANCELLED)
        return 0.0

    reverse = "sell" if orig_direction == "buy" else "buy"
    logger.warning(
        "Partial fill on cancelled %s order %s (%s): %.4f filled — flattening with "
        "a %s at %.4f%s",
        orig_direction, order_id, instrument, filled, reverse, reverse_price,
        f" [{context}]" if context else "",
    )
    order_manager.update(order_id, OrderState.CANCELLED_PARTIAL, filled_amount=filled)
    try:
        await client.place_order(
            instrument, reverse, filled, max(reverse_price, 0.0001),
            label=f"FLATTEN-PARTIAL-{asset}",
        )
        logger.info("Flattened %.4f of %s after partial fill", filled, instrument)
    except Exception as exc:
        logger.critical(
            "FAILED to flatten %.4f naked %s of %s after partial fill: %s — "
            "MANUAL ACTION REQUIRED",
            filled, orig_direction, instrument, exc,
        )
        if notifier is not None:
            try:
                notifier.notify_warning(
                    f"MANUAL ACTION REQUIRED: {filled} naked {orig_direction} of "
                    f"{instrument} left on the exchange after a partial fill could "
                    f"not be flattened ({exc})"
                )
            except Exception:
                pass
    return filled


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
                validate_amount=False,  # combo instrument has its own exchange minimum
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
    notifier=None,
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
    # Phase 25b: submit the sizer-approved quantity directly instead of the old
    # dimensionally-wrong _contract_amount() recompute (which divided by spot a
    # second time and always collapsed to the 0.1 floor — valid for BTC but
    # rejected for ETH).  The sizer rounds qty to the instrument step, and
    # place_order re-validates it against the live per-instrument minimum.
    amount = candidate.qty
    if amount <= 0:
        logger.warning(
            "Sizer-approved qty is %s for %s %s — skipping", amount, asset, candidate.near_instrument
        )
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
    effective_combo_timeout = combo_timeout if combo_timeout is not None else config.COMBO_FILL_TIMEOUT_SEC
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
            # Flatten any partial fill (Phase 26a): the near leg is a SELL, so a
            # partial fill leaves a naked short — buy it back at a crossed price.
            await _cancel_and_flatten(
                client, order_manager, near_order_id, near_instr,
                orig_direction="sell", reverse_price=near_limit * 1.05,
                asset=asset, notifier=notifier, context="near-entry-timeout",
            )
            return None

        near_fill_price = near_state.get("average_price", near_limit)
        near_fill_usd = _usd_price(near_fill_price, spot, asset)
        # Directional slippage check (Phase 26b): the near leg is a SELL, so only
        # a fill *below* the intended price is adverse; a better (higher) fill passes.
        _check_slippage(near_fill_usd, near_intended_usd, slippage_pct, side="sell")
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
            logger.error("Far leg fill failed: %s — flattening far partial + closing near leg", exc)
            # Flatten any partial far fill (Phase 26a): far is a BUY, so a partial
            # fill leaves a naked long — sell it back at a crossed price.
            await _cancel_and_flatten(
                client, order_manager, far_order_id, far_instr,
                orig_direction="buy", reverse_price=far_limit * 0.95,
                asset=asset, notifier=notifier, context="far-entry-timeout",
            )
            # Close the fully-filled near leg to avoid leg risk.
            try:
                await client.place_order(near_instr, "buy", amount, near_limit * 1.05,
                                         label=f"FLATTEN-NEAR-{asset}")
            except Exception as inner:
                logger.critical("FAILED to close near leg after far leg timeout: %s", inner)
            raise LegRiskError(f"Far leg timeout: {exc}")

        far_fill_price = far_state.get("average_price", far_limit)
        far_fill_usd   = _usd_price(far_fill_price, spot, asset)
        # Directional slippage check (Phase 26b): far is a BUY, so only a fill
        # *above* the intended price is adverse.  If it ever fires here — both legs
        # are already filled — unwind both legs before raising so an executed
        # spread is never abandoned unrecorded on the exchange.
        try:
            _check_slippage(far_fill_usd, far_intended_usd, slippage_pct, side="buy")
        except SlippageError:
            logger.error(
                "Far-leg slippage after both legs filled — unwinding both legs to "
                "avoid an abandoned unrecorded spread"
            )
            try:
                # Reverse near (we sold it → buy back) and far (we bought → sell).
                await client.place_order(near_instr, "buy", amount, near_limit * 1.05,
                                         label=f"UNWIND-NEAR-{asset}")
                await client.place_order(far_instr, "sell", amount, far_limit * 0.95,
                                         label=f"UNWIND-FAR-{asset}")
                logger.info("Unwound both legs after far-leg slippage")
            except Exception as unwind_exc:
                logger.critical(
                    "FAILED to unwind both legs after far-leg slippage: %s — "
                    "MANUAL ACTION REQUIRED", unwind_exc,
                )
                if notifier is not None:
                    try:
                        notifier.notify_warning(
                            f"MANUAL ACTION REQUIRED: {asset} calendar spread "
                            f"({near_instr}/{far_instr}) filled then failed to unwind "
                            f"after a slippage check — verify positions on Deribit"
                        )
                    except Exception:
                        pass
            raise
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
    notifier=None,
) -> tuple[float, float, float] | None:
    """
    Close both legs of a calendar spread.

    Returns ``(closing_credit_usd, near_close_usd, far_close_usd)`` — the net
    closing credit (positive = profit vs. debit paid) plus the two per-leg close
    fill prices in USD so the caller can compute accurate exit fees (Phase 25c) —
    or None if an error occurred.
    """
    asset      = position["asset"]
    spot       = position.get("spot_open", 1.0)
    near_instr = position["near_instrument"]
    far_instr  = position["far_instrument"]
    amount     = position["qty"]

    buffer = config.CLOSE_PRICE_CROSS_BUFFER_PCT

    async with _DeribitRPCClient(client_id, client_secret) as client:
        # Get current best bid/ask so we can cross the spread with a real quote
        # rather than a synthetic mid × multiplier (Phase 22d).
        try:
            near_ticker = await client.get_ticker(near_instr)
            far_ticker  = await client.get_ticker(far_instr)
            near_bid = near_ticker.get("best_bid_price", 0) or 0.0
            near_ask = near_ticker.get("best_ask_price", 0) or 0.0
            far_bid  = far_ticker.get("best_bid_price",  0) or 0.0
            far_ask  = far_ticker.get("best_ask_price",  0) or 0.0
        except Exception:
            near_bid = near_ask = far_bid = far_ask = 0.0

        near_mid = (near_bid + near_ask) / 2 if (near_bid > 0 and near_ask > 0) else 0.0
        far_mid  = (far_bid + far_ask) / 2 if (far_bid > 0 and far_ask > 0) else 0.0

        # Close near leg: buy back the short — lift the ask (plus buffer) so the
        # marketable limit fills; fall back to bid, then a tiny price.
        if near_ask > 0:
            near_close_price = near_ask * (1 + buffer)
        elif near_bid > 0:
            near_close_price = near_bid * (1 + buffer)
        else:
            near_close_price = 0.001
        near_close_id = None
        try:
            near_result = await client.place_order(
                near_instr, "buy", amount, near_close_price,
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

        # Close far leg: sell back the long — hit the bid (minus buffer) so the
        # marketable limit fills; fall back to ask, then a tiny price.
        if far_bid > 0:
            far_close_price = far_bid * (1 - buffer)
        elif far_ask > 0:
            far_close_price = far_ask * (1 - buffer)
        else:
            far_close_price = 0.001
        far_close_id = None
        try:
            far_result = await client.place_order(
                far_instr, "sell", amount, far_close_price,
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
            logger.warning("Near leg close timed out: %s — cancelling + flattening any partial", exc)
            # near-close is a BUY (buying back the short); a partial fill is
            # reversed by selling the filled portion back (Phase 26a) so the
            # position stays whole for a clean retry.
            near_sell_px = (near_bid if near_bid > 0 else near_mid) * (1 - buffer)
            await _cancel_and_flatten(
                client, order_manager, near_close_id, near_instr,
                orig_direction="buy", reverse_price=near_sell_px or 0.001,
                asset=asset, notifier=notifier, context="near-close-timeout",
            )

        # Wait for far leg fill
        try:
            far_state = await _wait_for_fill(client, far_close_id, order_timeout)
            far_filled = True
        except (OrderTimeoutError, RuntimeError) as exc:
            logger.warning("Far leg close timed out: %s — cancelling + flattening any partial", exc)
            # far-close is a SELL; a partial fill is reversed by buying it back.
            far_buy_px = (far_ask if far_ask > 0 else far_mid) * (1 + buffer)
            await _cancel_and_flatten(
                client, order_manager, far_close_id, far_instr,
                orig_direction="sell", reverse_price=far_buy_px or 0.001,
                asset=asset, notifier=notifier, context="far-close-timeout",
            )

        # If one leg filled but the other didn't, unwind the filled leg to avoid leg risk
        if near_filled and not far_filled:
            logger.error(
                "Near leg close filled but far leg failed — unwinding near leg to avoid leg risk"
            )
            try:
                # Sell the near leg we just bought back — hit the bid (minus buffer).
                if near_bid > 0:
                    unwind_price = near_bid * (1 - buffer)
                elif near_mid > 0:
                    unwind_price = near_mid * (1 - buffer)
                else:
                    unwind_price = 0.001
                await client.place_order(
                    near_instr, "sell", amount, unwind_price,
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
                # Buy back the far leg we just sold — lift the ask (plus buffer).
                if far_ask > 0:
                    unwind_price = far_ask * (1 + buffer)
                elif far_mid > 0:
                    unwind_price = far_mid * (1 + buffer)
                else:
                    unwind_price = 0.001
                await client.place_order(
                    far_instr, "buy", amount, unwind_price,
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
        return closing_credit, near_close_usd, far_close_usd


async def _async_roll_near_leg(
    position: dict,
    new_candidate: CalendarCandidate,
    client_id: str,
    client_secret: str,
    order_manager: OrderManager,
    slippage_pct: float = SLIPPAGE_LIMIT_PCT,
    order_timeout: int = ORDER_TIMEOUT_SEC,
    notifier=None,
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

    buffer = config.CLOSE_PRICE_CROSS_BUFFER_PCT

    async with _DeribitRPCClient(client_id, client_secret) as client:
        # Buy back old near leg — lift the ask (plus buffer) to cross the book,
        # rather than a synthetic mid × 1.02 that lands off-tick (Phase 22d).
        try:
            ticker = await client.get_ticker(near_instr)
            near_bid = ticker.get("best_bid_price", 0) or 0.0
            near_ask = ticker.get("best_ask_price", 0) or 0.0
        except Exception:
            near_bid = near_ask = 0.0

        if near_ask > 0:
            close_price = near_ask * (1 + buffer)
        elif near_bid > 0:
            close_price = near_bid * (1 + buffer)
        else:
            close_price = 0.001

        close_result = await client.place_order(
            near_instr, "buy", amount, max(close_price, 0.0001),
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
            logger.error("Roll: close of near leg failed: %s — flattening any partial", exc)
            # roll-close is a BUY; reverse a partial by selling it back (Phase 26a).
            roll_sell_px = (near_bid if near_bid > 0 else near_ask) * (1 - buffer)
            await _cancel_and_flatten(
                client, order_manager, close_id, near_instr,
                orig_direction="buy", reverse_price=roll_sell_px or 0.001,
                asset=asset, notifier=notifier, context="roll-close-timeout",
            )
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
            logger.error("Roll: open of new near leg failed: %s — flattening any partial", exc)
            # roll-open is a SELL; reverse a partial by buying it back (Phase 26a).
            await _cancel_and_flatten(
                client, order_manager, sell_id, new_near,
                orig_direction="sell", reverse_price=new_limit * 1.05,
                asset=asset, notifier=notifier, context="roll-open-timeout",
            )
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
        portfolio_value: float = config.DEFAULT_PORTFOLIO_VALUE,
        order_manager:   OrderManager | None = None,
        slippage_pct:    float = SLIPPAGE_LIMIT_PCT,
        order_timeout:   int   = ORDER_TIMEOUT_SEC,
        notifier=None,
        # Legacy: paper=True/False still accepted but ignored; mode comes from config
        paper:           bool | None = None,
    ) -> None:
        self.client_id       = client_id or config.DERIBIT_CLIENT_ID
        self.client_secret   = client_secret or config.DERIBIT_CLIENT_SECRET
        self.portfolio_value = portfolio_value
        self.order_manager   = order_manager or OrderManager()
        self.slippage_pct    = slippage_pct
        self.order_timeout   = order_timeout
        # Optional notifier for one-shot operator alerts on a failed partial-fill
        # flatten (Phase 26a) — logged CRITICAL regardless, alerted when present.
        self._notifier       = notifier
        # Populated by close_spread() with the last close's per-leg fill prices
        # so the decision engine can compute exit fees from real fills (Phase 25c).
        self.last_close_fills: dict[str, float] | None = None

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

    def _fetch_and_cache_tick_size(self, instrument: str) -> float | None:
        """
        Fetch an instrument's tick size from Deribit and cache it.

        Returns None if the fetch fails (e.g., in paper mode or network error).
        The tick size is stored in the global _TICK_SIZE_CACHE for reuse.
        """
        if config.TRADING_MODE == "paper":
            return None  # Paper mode doesn't make API calls; use default rounding

        async def _fetch():
            try:
                async with _DeribitRPCClient(self.client_id, self.client_secret) as client:
                    instr = await client.get_instrument(instrument)
                    # public/get_instrument returns the instrument object directly.
                    if not isinstance(instr, dict):
                        return None
                    tick_size = instr.get("tick_size")
                    if tick_size is not None:
                        _TICK_SIZE_CACHE[instrument] = tick_size
                        _TICK_STEPS_CACHE[instrument] = instr.get("tick_size_steps") or []
                        logger.debug("Cached tick_size for %s: %s", instrument, tick_size)
                    return tick_size
            except Exception as e:
                logger.debug("Failed to fetch tick_size for %s: %s", instrument, e)
                return None

        return self._run(_fetch())

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
                    notifier=self._notifier,
                )
            )
        except LegRiskError:
            raise
        except AmountBelowMinimumError as exc:
            logger.warning("AMOUNT GATE: entry aborted — %s", exc)
            return None
        except SlippageError as exc:
            logger.warning("Slippage exceeded: %s", exc)
            return None
        except Exception:
            logger.exception("Unexpected error in enter_spread")
            return None

    def close_spread(self, position: dict) -> float | None:
        """
        Close both legs.  Returns closing credit in USD or None on failure.

        On success the per-leg close fill prices are stashed on
        ``self.last_close_fills`` (``{"near_close_usd", "far_close_usd"}``) so the
        decision engine can compute accurate exit fees from the actual fills
        (Phase 25c) rather than falling back to stale entry-time premiums.
        """
        self.last_close_fills = None
        try:
            result = self._run(
                _async_close_spread(
                    position,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    order_manager=self.order_manager,
                    order_timeout=self.order_timeout,
                    notifier=self._notifier,
                )
            )
        except Exception:
            logger.exception("Unexpected error in close_spread")
            return None
        if result is None:
            return None
        credit, near_close_usd, far_close_usd = result
        self.last_close_fills = {
            "near_close_usd": near_close_usd,
            "far_close_usd":  far_close_usd,
        }
        return credit

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
                    notifier=self._notifier,
                )
            )
        except Exception:
            logger.exception("Unexpected error in roll_near_leg")
            return False
