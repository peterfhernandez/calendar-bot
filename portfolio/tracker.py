"""
portfolio/tracker.py
====================
Real-time portfolio state tracker for the calendar spread bot.

Fetches account equity and available funds from the Deribit REST API,
reconciles against the SQLite position table, and exposes available_cash
to the sizing engine on each scan cycle.

Public API
----------
PortfolioTracker(db_path=None, client_id=None, client_secret=None, rest_url=None)
    Main tracker class.  Call refresh() before each scan cycle.

PortfolioState
    Dataclass snapshot returned by refresh() and portfolio_view().
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, NamedTuple

import config
from data.chain_cache import ChainCache
from db.state import (
    DB_PATH,
    get_connection,
    get_stuck_positions,
    mark_stuck_position_reconciled,
)

logger = logging.getLogger(__name__)

# If Deribit-reported margin and SQLite-computed margin diverge by more than
# this fraction, a warning is logged (possible manual trade or missed fill).
_RECONCILE_THRESHOLD = config.RECONCILE_THRESHOLD_PCT

# Currencies supported on Deribit that map 1:1 to asset names.
# USDC/USDT trade at spot=1.0 so no price fetch is needed.
_STABLECOIN_CURRENCIES = {"USDC", "USDT", "USD"}


class MarginImpact(NamedTuple):
    """Impact of a hypothetical position on account margin."""
    projected_initial_margin_usd: float
    projected_maintenance_margin_usd: float


@dataclass
class PortfolioState:
    """Point-in-time snapshot of the portfolio."""
    equity_usd:              float
    available_cash:          float   # available_funds from Deribit, converted to USD
    used_margin:             float   # SQLite: sum(net_debit * qty) for open positions
    unrealized_pnl:          float   # floating P&L from Deribit positions, converted to USD
    realized_pnl_today:      float   # SQLite: closed-trade P&L since midnight UTC
    open_position_count:     int
    deribit_margin_usd:      float   # initial_margin from Deribit REST, converted to USD
    maintenance_margin_usd:  float   # maintenance_margin from Deribit REST, converted to USD
    last_refresh:            Optional[float]  # epoch seconds of last successful refresh
    fees_paid_today:         float = 0.0   # sum of open_fees + close_fees for trades today
    fees_paid_total:         float = 0.0   # sum of all open_fees + close_fees across all trades


class PortfolioTracker:
    """
    Tracks account equity, available cash, and position P&L for the bot.

    Behavior depends on TRADING_MODE:

    **Paper mode (TRADING_MODE == "paper"):**
    - Zero Deribit REST API calls (completely isolated from exchange)
    - Equity calculated as: initial_capital + realized_pnl_today + unrealized_pnl_from_cache
    - Unrealized P&L from live ChainCache mid-prices on open positions
    - Available cash calculated from DB metrics only
    - No reconciliation warnings
    - Result: portfolio view based purely on SQLite + live cache

    **Test/live modes (TRADING_MODE in ["test", "live"]):**
    - Full Deribit REST API integration (Phase 8b)
    - Fetches account summaries, positions, and margin data
    - Reconciles Deribit-reported margin against SQLite
    - Margin simulation API for Cross Portfolio Margin gate (Phase 17)
    - Result: portfolio synchronized with actual Deribit account state

    Parameters
    ----------
    db_path
        SQLite database path (defaults to db/calendar_bot.db).
    client_id / client_secret
        Deribit API credentials.  Default: from config.
    rest_url
        Deribit REST base URL.  Default: derived from config.DERIBIT_REST_URL.
    cache
        Optional ChainCache for live option pricing data.
        Used in paper mode to compute unrealized P&L from live prices.
    """

    def __init__(
        self,
        db_path:       Path | None = None,
        client_id:     str  | None = None,
        client_secret: str  | None = None,
        rest_url:      str  | None = None,
        cache:         ChainCache | None = None,
        notifier=None,
    ) -> None:
        self._db_path       = db_path or DB_PATH
        self._client_id     = client_id     if client_id     is not None else config.DERIBIT_CLIENT_ID
        self._client_secret = client_secret if client_secret is not None else config.DERIBIT_CLIENT_SECRET
        self._rest_url      = rest_url or config.DERIBIT_REST_URL
        self._cache         = cache
        # Optional notifier for the persistent-mismatch escalation alert (Phase 26f).
        self._notifier      = notifier
        # Reconcile-escalation state (Phase 26f): a mismatch fingerprint that
        # recurs unchanged for RECONCILE_ESCALATE_AFTER_CYCLES cycles is escalated
        # from a warn-only log to a one-shot operator alert.
        self._reconcile_fingerprint: tuple | None = None
        self._reconcile_repeat_count: int = 0
        self._reconcile_escalated: bool = False

        # Cached state — updated by refresh()
        self._equity_usd:             float = 0.0
        self._available_cash:         float = 0.0
        self._used_margin:            float = 0.0
        self._unrealized_pnl:         float = 0.0
        self._realized_pnl_today:     float = 0.0
        self._deribit_margin_usd:     float = 0.0
        self._maintenance_margin_usd: float = 0.0
        self._open_position_count:    int   = 0
        self._last_refresh:           float | None = None
        self._fees_paid_today:        float = 0.0
        self._fees_paid_total:        float = 0.0
        # Offline tracking — log once on first failure, once on recovery
        self._api_offline:     bool = False
        self._api_fail_count:  int  = 0

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def available_cash(self) -> float:
        """Available cash for new positions in USD (from last successful refresh)."""
        return self._available_cash

    @property
    def equity_usd(self) -> float:
        """Total account equity in USD (from last successful refresh)."""
        return self._equity_usd

    @property
    def used_margin(self) -> float:
        """Sum of (net_debit × qty) for all open positions, computed from SQLite."""
        return self._used_margin

    @property
    def unrealized_pnl(self) -> float:
        """Unrealized (floating) P&L in USD from Deribit position data."""
        return self._unrealized_pnl

    @property
    def realized_pnl_today(self) -> float:
        """Realized P&L from trades closed today (UTC), from SQLite."""
        return self._realized_pnl_today

    @property
    def maintenance_margin_usd(self) -> float:
        """Maintenance margin from Deribit REST API, in USD."""
        return self._maintenance_margin_usd

    @property
    def margin_utilization_pct(self) -> float:
        """Current margin utilization: maintenance_margin / equity.

        Returns 0.0 if equity is zero or unavailable.
        """
        if self._equity_usd <= 0:
            return 0.0
        return self._maintenance_margin_usd / self._equity_usd

    # ── Main refresh ──────────────────────────────────────────────────────────

    def refresh(self, spot_prices: dict[str, float] | None = None) -> PortfolioState:
        """
        Refresh portfolio state from Deribit REST API and/or SQLite.

        Behavior depends on TRADING_MODE:

        **Paper mode (TRADING_MODE == "paper"):**
        - Skips all Deribit REST API calls (no network access)
        - Calculates equity/available_cash from SQLite + live ChainCache only
        - No reconciliation warnings

        **Test/live modes (TRADING_MODE in ["test", "live"]):**
        - Calls Deribit REST API for account summaries and position data
        - Reconciles DB margin against Deribit-reported margin

        Parameters
        ----------
        spot_prices
            Optional asset→USD spot price map to skip index-price API calls
            (useful in tests and paper mode where the feed is already running).

        Returns
        -------
        PortfolioState
            Current portfolio snapshot.
        """
        # ── SQLite-derived values (always available, no network) ──────────────
        self._used_margin         = self._calc_used_margin()
        self._realized_pnl_today  = self._calc_realized_pnl_today()
        self._open_position_count = self._count_open_positions()
        self._fees_paid_today     = self._calc_fees_paid_today()
        self._fees_paid_total     = self._calc_fees_paid_total()

        # ── Paper mode: DB-only portfolio calculation (no Deribit API) ──────────
        if config.TRADING_MODE == "paper":
            self._unrealized_pnl = self._calculate_unrealized_pnl_from_cache()
            db_only_state = self._calculate_db_only_portfolio()
            self._equity_usd = db_only_state.get("equity_usd", 0.0)
            self._available_cash = db_only_state.get("available_cash", 0.0)
            # Leave Deribit-specific fields at zero (no API call)
            self._deribit_margin_usd = 0.0
            self._maintenance_margin_usd = 0.0
            self._last_refresh = time.time()
            # Skip reconciliation in paper mode
            return PortfolioState(
                equity_usd              = self._equity_usd,
                available_cash          = self._available_cash,
                used_margin             = self._used_margin,
                unrealized_pnl          = self._unrealized_pnl,
                realized_pnl_today      = self._realized_pnl_today,
                open_position_count     = self._open_position_count,
                deribit_margin_usd      = self._deribit_margin_usd,
                maintenance_margin_usd  = self._maintenance_margin_usd,
                last_refresh            = self._last_refresh,
                fees_paid_today         = self._fees_paid_today,
                fees_paid_total         = self._fees_paid_total,
            )

        # ── Test/live mode: Deribit REST API ──────────────────────────────────
        # Phase 24b: auto-reconcile any close_stuck trades the operator has
        # already closed on Deribit, before the margin comparison runs, so the
        # reconcile-mismatch warning resolves in the same cycle it is detected.
        reconciled_ids = self.sync_stuck_positions(self._db_path)
        if reconciled_ids:
            # DB changed — recompute the SQLite-derived margin/count so the
            # reconciliation below compares against fresh numbers.
            self._used_margin         = self._calc_used_margin()
            self._open_position_count = self._count_open_positions()

        has_credentials = bool(self._client_id and self._client_secret)

        if has_credentials:
            try:
                self._refresh_from_api(spot_prices)
                if self._api_offline:
                    logger.info(
                        "Portfolio API back online after %d failed attempt(s)",
                        self._api_fail_count,
                    )
                    self._api_offline = False
                    self._api_fail_count = 0
            except Exception as exc:
                self._api_fail_count += 1
                if not self._api_offline:
                    logger.warning(
                        "Portfolio API offline (%s) — equity/available_cash unchanged; "
                        "further failures will be suppressed until connectivity returns",
                        exc,
                    )
                    self._api_offline = True
                else:
                    logger.debug(
                        "Portfolio API still offline (attempt %d): %s",
                        self._api_fail_count,
                        exc,
                    )
        else:
            logger.debug("No API credentials configured — portfolio tracker in DB-only mode")

        self._last_refresh = time.time()

        # ── Reconciliation (test/live only) ───────────────────────────────────
        if self._deribit_margin_usd > 0:
            self._reconcile()

        return PortfolioState(
            equity_usd              = self._equity_usd,
            available_cash          = self._available_cash,
            used_margin             = self._used_margin,
            unrealized_pnl          = self._unrealized_pnl,
            realized_pnl_today      = self._realized_pnl_today,
            open_position_count     = self._open_position_count,
            deribit_margin_usd      = self._deribit_margin_usd,
            maintenance_margin_usd  = self._maintenance_margin_usd,
            last_refresh            = self._last_refresh,
            fees_paid_today         = self._fees_paid_today,
            fees_paid_total         = self._fees_paid_total,
        )

    # ── Portfolio view ────────────────────────────────────────────────────────

    def portfolio_view(self) -> str:
        """Return a formatted multi-line snapshot suitable for logging."""
        lines = [
            "─" * 52,
            "  PORTFOLIO SNAPSHOT",
            "─" * 52,
            f"  Equity (USD)      : ${self._equity_usd:>12,.2f}",
            f"  Available Cash    : ${self._available_cash:>12,.2f}",
            f"  Used Margin (DB)  : ${self._used_margin:>12,.2f}",
            f"  Unrealized P&L    : ${self._unrealized_pnl:>+12,.2f}",
            f"  Realized P&L Today: ${self._realized_pnl_today:>+12,.2f}",
            f"  Fees Paid Today   : ${self._fees_paid_today:>12,.2f}",
            f"  Fees Paid Total   : ${self._fees_paid_total:>12,.2f}",
            f"  Open Positions    : {self._open_position_count}",
            "─" * 52,
        ]
        return "\n".join(lines)

    # ── Deribit REST helpers ──────────────────────────────────────────────────

    def _refresh_from_api(self, spot_prices: dict[str, float] | None) -> None:
        """Fetch account summaries and position P&L from Deribit REST.

        This method is only called in test/live modes; paper mode skips it entirely.
        """
        if config.TRADING_MODE == "paper":
            logger.debug("_refresh_from_api: skipped in paper mode")
            return

        token = self._authenticate()

        total_equity_usd         = 0.0
        total_available_usd      = 0.0
        total_margin_usd         = 0.0
        total_maintenance_usd    = 0.0
        total_unrealized         = 0.0

        currencies = _assets_to_currencies(config.ASSETS)

        for currency in currencies:
            try:
                summary   = self._get_account_summary(token, currency)
                spot      = _resolve_spot(currency, spot_prices, self._rest_url)
                positions = self._get_positions(token, currency)

                equity_usd      = summary.get("equity",              0.0) * spot
                available_usd   = summary.get("available_funds",     0.0) * spot
                margin_usd      = summary.get("initial_margin",      0.0) * spot
                maintenance_usd = summary.get("maintenance_margin",  0.0) * spot
                float_pnl_usd   = sum(
                    p.get("floating_profit_loss", 0.0) * spot for p in positions
                )

                total_equity_usd         += equity_usd
                total_available_usd      += available_usd
                total_margin_usd         += margin_usd
                total_maintenance_usd    += maintenance_usd
                total_unrealized         += float_pnl_usd

                logger.debug(
                    "Portfolio %s: equity=%.4f spot=%.2f → $%.2f "
                    "avail=$%.2f margin=$%.2f maint=$%.2f float_pnl=$%.2f",
                    currency,
                    summary.get("equity", 0.0), spot,
                    equity_usd, available_usd, margin_usd, maintenance_usd, float_pnl_usd,
                )

            except Exception as exc:
                logger.debug("Could not fetch %s summary: %s", currency, exc)
                raise

        self._equity_usd             = total_equity_usd
        self._available_cash         = max(0.0, total_available_usd)
        self._deribit_margin_usd     = total_margin_usd
        self._maintenance_margin_usd = total_maintenance_usd
        self._unrealized_pnl         = total_unrealized

    def _authenticate(self) -> str:
        """Obtain a short-lived Deribit access token via client_credentials.

        Credentials are sent as URL query parameters (required by Deribit API).
        The _SecretRedactor filter on the root logger redacts them from output.
        """
        params = urllib.parse.urlencode({
            "grant_type":    "client_credentials",
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
        })
        url  = f"{self._rest_url}/api/v2/public/auth?{params}"
        data = _rest_get(url)
        token: str = data["result"]["access_token"]
        logger.debug("Deribit REST authentication successful")
        return token

    def _get_account_summary(self, token: str, currency: str) -> dict:
        """Fetch /private/get_account_summary for one currency."""
        params = urllib.parse.urlencode({"currency": currency})
        url    = f"{self._rest_url}/api/v2/private/get_account_summary?{params}"
        data   = _rest_get(url, bearer_token=token)
        return data["result"]

    def _get_positions(self, token: str, currency: str, kind: str | None = "option") -> list[dict]:
        """Fetch open positions for one currency.

        ``kind`` selects the instrument class (``option``, ``future``, …).  Pass
        ``None`` (or ``"any"``) to omit the filter so Deribit returns positions of
        every kind — used by the Phase 25e residual-margin reconciliation, since
        margin can be tied up by a future/perpetual that the option-only filter
        never sees.
        """
        query = {"currency": currency}
        if kind and kind != "any":
            query["kind"] = kind
        params = urllib.parse.urlencode(query)
        url    = f"{self._rest_url}/api/v2/private/get_positions?{params}"
        data   = _rest_get(url, bearer_token=token)
        return data.get("result", [])

    def _get_open_orders(self, token: str, currency: str) -> list[dict]:
        """Fetch resting open orders for one currency (Phase 25e).

        Resting orders can reserve margin that no position explains; surfacing
        them makes an otherwise-invisible reconcile mismatch actionable.
        """
        params = urllib.parse.urlencode({"currency": currency})
        url    = f"{self._rest_url}/api/v2/private/get_open_orders_by_currency?{params}"
        data   = _rest_get(url, bearer_token=token)
        return data.get("result", [])

    def get_deribit_open_positions(self, currency: str, kind: str | None = "option") -> list[dict]:
        """Return the account's live positions for one currency (Phase 24a / 25e).

        Calls ``private/get_positions`` and normalises each non-flat position to a
        small dict the reconcile logger and the ``/deribit_positions`` Telegram
        command can format directly:

            {instrument_name, kind, size, mark_price, index_price, mark_value}

        ``kind`` defaults to ``"option"`` (Phase 24 behaviour); pass ``"any"`` to
        also include futures/perpetuals (Phase 25e), so margin held by a
        non-option position is no longer invisible to reconcile.

        Returns ``[]`` on any failure (network, auth, paper mode, no
        credentials) so callers never block on it.  Because a failure is
        indistinguishable from "genuinely no positions" here, the
        safety-critical auto-reconcile path (``sync_stuck_positions``) fetches
        positions itself and aborts on error rather than relying on this
        method's empty-list-on-failure contract.
        """
        if config.TRADING_MODE == "paper":
            return []
        if not self._client_id or not self._client_secret:
            return []
        try:
            token = self._authenticate()
            positions = self._get_positions(token, currency, kind=kind)
        except Exception as exc:
            logger.debug("get_deribit_open_positions(%s, kind=%s) failed: %s", currency, kind, exc)
            return []

        result: list[dict] = []
        for p in positions:
            size = p.get("size", 0.0)
            if not size:
                continue
            mark_price = p.get("mark_price", 0.0)
            result.append({
                "instrument_name": p.get("instrument_name", ""),
                "kind":            p.get("kind", "option"),
                "size":            size,
                "mark_price":      mark_price,
                "index_price":     p.get("index_price", 0.0),
                "mark_value":      mark_price * abs(size),
            })
        return result

    def get_deribit_open_orders(self, currency: str) -> list[dict]:
        """Return the account's resting open orders for one currency (Phase 25e).

        Normalises each order to ``{instrument_name, direction, amount, price}``.
        Returns ``[]`` on any failure (network, auth, paper mode, no credentials).
        """
        if config.TRADING_MODE == "paper":
            return []
        if not self._client_id or not self._client_secret:
            return []
        try:
            token = self._authenticate()
            orders = self._get_open_orders(token, currency)
        except Exception as exc:
            logger.debug("get_deribit_open_orders(%s) failed: %s", currency, exc)
            return []
        return [
            {
                "instrument_name": o.get("instrument_name", ""),
                "direction":       o.get("direction", ""),
                "amount":          o.get("amount", 0.0),
                "price":           o.get("price", 0.0),
            }
            for o in orders
        ]

    def _reconcile_currencies(self) -> list[str]:
        """Currency set scanned when locating margin sources (Phase 25e).

        Widened from ``ASSETS`` to the ``COLLECTOR_ASSETS`` superset so margin
        held in a currency the bot doesn't actively trade (but does know about)
        is still visible to reconcile.  The read-only ``scratch_account_margin_
        audit.py`` covers the full account for currencies outside even this set.
        """
        assets = list(dict.fromkeys([*config.ASSETS, *config.COLLECTOR_ASSETS]))
        return _assets_to_currencies(assets)

    def _describe_deribit_positions(self) -> str:
        """Return a one-line description of every live Deribit margin source.

        Used to make the reconcile-mismatch warning actionable (Phase 24a / 25e)
        by naming exactly what Deribit still holds — options *and* futures across
        the widened currency set — plus a count of resting open orders (which can
        reserve margin no position explains).  Returns an empty string if nothing
        is open or the fetches fail.
        """
        descs: list[str] = []
        open_order_count = 0
        for currency in self._reconcile_currencies():
            for p in self.get_deribit_open_positions(currency, kind="any"):
                descs.append(f"{p['instrument_name']} ({p['kind']}) qty={p['size']}")
            open_order_count += len(self.get_deribit_open_orders(currency))
        if open_order_count:
            descs.append(f"{open_order_count} resting open order(s)")
        return ", ".join(descs)

    def sync_stuck_positions(self, db_path: Path | None = None) -> list[int]:
        """Auto-close ``close_stuck`` DB trades the operator has closed on Deribit (Phase 24b).

        For every trade flagged ``close_stuck`` in the DB, checks whether *both*
        its legs are absent from Deribit's live position list.  If both are gone
        the operator has already closed the position on the exchange, so the DB
        row is reconciled to ``close_status='closed'``.  A partially-closed
        position (one leg still live) is left untouched.

        The live position list is fetched here directly (not via
        ``get_deribit_open_positions``' empty-on-failure contract) and the whole
        sync aborts on any fetch error, so an API failure can never be mistaken
        for "no positions open" and falsely reconcile every stuck trade.

        Returns the list of reconciled ``trade_id``s.
        """
        if config.TRADING_MODE == "paper":
            return []
        if not self._client_id or not self._client_secret:
            return []

        path = db_path or self._db_path
        stuck = get_stuck_positions(path)
        if not stuck:
            return []

        try:
            token = self._authenticate()
            live: set[str] = set()
            for currency in _assets_to_currencies(config.ASSETS):
                for p in self._get_positions(token, currency):
                    name = p.get("instrument_name")
                    if name and p.get("size", 0.0):
                        live.add(name)
        except Exception as exc:
            logger.debug(
                "sync_stuck_positions: could not fetch Deribit positions "
                "(%s) — skipping auto-reconcile this cycle",
                exc,
            )
            return []

        reconciled: list[int] = []
        for t in stuck:
            near, far = t.near_instrument, t.far_instrument
            near_gone = (not near) or (near not in live)
            far_gone  = (not far)  or (far  not in live)
            if near_gone and far_gone:
                try:
                    mark_stuck_position_reconciled(t.id, db_path=path)
                    logger.info(
                        "trade_id=%d auto-reconciled: both legs confirmed closed on Deribit",
                        t.id,
                    )
                    reconciled.append(t.id)
                except Exception as exc:
                    logger.warning(
                        "Failed to auto-reconcile stuck trade_id=%d: %s", t.id, exc
                    )
        return reconciled

    def simulate_margin(
        self,
        legs: list[tuple[str, float, float]],  # [(instrument_name, amount, price), ...]
    ) -> MarginImpact | None:
        """Simulate the impact of a hypothetical position on account margin.

        Calls Deribit's private/get_margins endpoint to compute the projected
        maintenance margin if the specified legs were added to the account.
        This is used by the Cross Portfolio Margin (X:PM) entry gate (Phase 17)
        to prevent positions that would push margin utilization too high.

        Parameters
        ----------
        legs
            List of (instrument_name, amount, price) tuples for each leg.
            amount is quantity in contracts; price is the entry price (mid or limit).

        Returns
        -------
        MarginImpact | None
            Projected initial and maintenance margin in USD if simulation succeeds,
            or None if the API call fails, is unavailable, or in paper mode (caller falls back to local proxy).

        Note
        ----
        This method is only called in test/live modes. In paper mode, the margin gate
        no-ops in DecisionEngine._check_margin_gate() so this is never called.
        """
        if config.TRADING_MODE == "paper":
            logger.debug("simulate_margin: skipped in paper mode")
            return None

        if not self._client_id or not self._client_secret:
            logger.debug("simulate_margin: no credentials configured")
            return None

        if not legs:
            logger.debug("simulate_margin: no legs provided")
            return None

        try:
            token = self._authenticate()

            # Deribit's private/get_margins API estimates margin for hypothetical positions.
            # Takes a list of instruments, amounts, and prices, and returns margin requirements.
            # Example response: {"initial_margin": 0.5, "maintenance_margin": 0.3, ...}
            params = {
                "legs": [
                    {
                        "instrument_name": instrument,
                        "amount": amount,
                        "price": price,
                    }
                    for instrument, amount, price in legs
                ]
            }
            url = f"{self._rest_url}/api/v2/private/get_margins"

            # Make the REST call (note: private/get_margins likely uses POST)
            result_data = _rest_post(url, params, bearer_token=token)

            # Extract margin values from response
            result = result_data.get("result", {})
            initial_margin = result.get("initial_margin", 0.0)
            maintenance_margin = result.get("maintenance_margin", 0.0)

            # The response from Deribit gives margin in the base currency (BTC, ETH, SOL).
            # We need to convert to USD using the current spot prices.
            # For now, assume legs are all in the same currency and infer it from
            # the first instrument name (e.g., "BTC-1JAN26-60000-C" → "BTC")
            if legs:
                first_instrument = legs[0][0]  # e.g., "BTC-1JAN26-60000-C"
                currency = first_instrument.split("-")[0]  # Extract "BTC"
                spot = _resolve_spot(currency, None, self._rest_url)

                initial_margin_usd = initial_margin * spot
                maintenance_margin_usd = maintenance_margin * spot

                logger.debug(
                    "Margin simulation: %s × %.2f = initial=$%.2f, maint=$%.2f",
                    currency, spot, initial_margin_usd, maintenance_margin_usd,
                )

                return MarginImpact(
                    projected_initial_margin_usd=initial_margin_usd,
                    projected_maintenance_margin_usd=maintenance_margin_usd,
                )

            return None

        except Exception as exc:
            logger.debug("simulate_margin failed: %s", exc)
            return None

    # ── SQLite helpers ────────────────────────────────────────────────────────

    def _calc_used_margin(self) -> float:
        """Sum of (net_debit × qty) for all open positions in the DB."""
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_debit * qty), 0.0) AS total "
                "FROM calendar_trades WHERE result = 'Open'"
            ).fetchone()
        return float(row["total"])

    def _calc_realized_pnl_today(self) -> float:
        """Sum of pnl for trades whose date_close is today (UTC)."""
        today = date.today().isoformat()
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) AS total "
                "FROM calendar_trades "
                "WHERE date_close = ? AND pnl IS NOT NULL",
                (today,),
            ).fetchone()
        return float(row["total"])

    def _count_open_positions(self) -> int:
        """Count rows with result='Open' in the DB."""
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM calendar_trades WHERE result = 'Open'"
            ).fetchone()
        return int(row["cnt"])

    def _calc_fees_paid_today(self) -> float:
        """Sum of open_fees + close_fees for trades opened or closed today (UTC)."""
        today = date.today().isoformat()
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(open_fees + close_fees), 0.0) AS total
                FROM calendar_trades
                WHERE date_open = ? OR date_close = ?
                """,
                (today, today),
            ).fetchone()
        return float(row["total"])

    def _calc_fees_paid_total(self) -> float:
        """Sum of open_fees + close_fees across all trades."""
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(open_fees + close_fees), 0.0) AS total "
                "FROM calendar_trades"
            ).fetchone()
        return float(row["total"])

    # ── Paper mode portfolio calculation (DB + cache only) ────────────────────

    def _calculate_unrealized_pnl_from_cache(self) -> float:
        """Calculate unrealized P&L from live ChainCache mid-prices on open positions.

        Queries SQLite for all open positions, fetches their current spread values
        from ChainCache, and sums the unrealized P&L across all positions.

        Used in paper mode when Deribit API is not called (completely isolated mode).

        Returns
        -------
        float
            Total unrealized P&L in USD across all open positions.
            Falls back to 0.0 if cache is unavailable.
        """
        if not self._cache:
            logger.debug("_calculate_unrealized_pnl_from_cache: no cache available, returning 0.0")
            return 0.0

        try:
            total_unrealized = 0.0
            with get_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT id, near_instrument, far_instrument, qty, net_debit "
                    "FROM calendar_trades WHERE result = 'Open'"
                ).fetchall()

            for row in rows:
                trade_id = row["id"]
                near_instr = row["near_instrument"]
                far_instr = row["far_instrument"]
                qty = row["qty"]
                net_debit = row["net_debit"]

                try:
                    # Fetch live mid prices from cache
                    near_snap = self._cache.get_ticker(near_instr)
                    far_snap = self._cache.get_ticker(far_instr)

                    if near_snap and far_snap:
                        near_mid = (near_snap.bid + near_snap.ask) / 2.0 if (near_snap.bid + near_snap.ask) > 0 else 0.0
                        far_mid = (far_snap.bid + far_snap.ask) / 2.0 if (far_snap.bid + far_snap.ask) > 0 else 0.0
                        spread_value = max(0.0, far_mid - near_mid) * qty
                        unrealized = spread_value - net_debit * qty
                        total_unrealized += unrealized
                        logger.debug(
                            "Position %d: spread_value=$%.2f, net_debit=%.4f, qty=%.1f, "
                            "unrealized=$%.2f",
                            trade_id, spread_value, net_debit, qty, unrealized,
                        )
                    else:
                        logger.debug(
                            "Position %d: cache stale or missing (near=%s, far=%s)",
                            trade_id,
                            "present" if near_snap else "missing",
                            "present" if far_snap else "missing",
                        )
                except Exception as e:
                    logger.debug("Error calculating unrealized for position %d: %s", trade_id, e)

            return total_unrealized

        except Exception as exc:
            logger.debug("_calculate_unrealized_pnl_from_cache failed: %s", exc)
            return 0.0

    def _calculate_db_only_portfolio(self) -> dict:
        """Calculate portfolio equity and available cash from DB metrics and cache.

        Used in paper mode to compute portfolio state without any Deribit API calls.

        Returns
        -------
        dict
            Dictionary with keys:
            - equity_usd: Total account equity (calculated from capital + realized + unrealized)
            - available_cash: Cash available for new positions
            - unrealized_pnl: Current unrealized P&L (populated separately in refresh())
        """
        # In paper mode, we estimate starting equity from the initial capital
        initial_capital = config.INITIAL_CAPITAL
        unrealized = self._unrealized_pnl

        # Equity = initial capital + realized today + unrealized (from cache)
        equity_usd = initial_capital + self._realized_pnl_today + unrealized

        # Available cash = equity - amount locked in open positions
        # (locked = sum of net_debit * qty for all open positions)
        available_cash = max(0.0, equity_usd - self._used_margin)

        return {
            "equity_usd": equity_usd,
            "available_cash": available_cash,
            "unrealized_pnl": unrealized,
        }

    # ── Reconciliation ────────────────────────────────────────────────────────

    def _reconcile(self) -> None:
        """
        Warn if Deribit-reported margin and SQLite-computed margin diverge.

        Divergence > 10% may indicate a manual trade was placed outside the bot,
        a fill was missed, or a position closed on-exchange but not in the DB.
        """
        db_margin  = self._used_margin
        api_margin = self._deribit_margin_usd

        if db_margin <= 0 and api_margin <= 0:
            return

        max_val    = max(db_margin, api_margin)
        divergence = abs(api_margin - db_margin) / max_val

        if divergence > _RECONCILE_THRESHOLD:
            # Name the live Deribit instruments so the warning is actionable
            # (Phase 24a): the operator can see exactly what is open on the
            # exchange without logging into the Deribit UI separately.
            open_desc = self._describe_deribit_positions()
            if open_desc:
                logger.warning(
                    "RECONCILE MISMATCH: Deribit margin $%.2f vs SQLite margin $%.2f "
                    "(divergence %.0f%%) — Deribit open: %s",
                    api_margin, db_margin, divergence * 100, open_desc,
                )
            else:
                logger.warning(
                    "RECONCILE MISMATCH: Deribit margin $%.2f vs SQLite margin $%.2f "
                    "(divergence %.0f%%) — possible manual trade or missed fill",
                    api_margin, db_margin, divergence * 100,
                )

            # ── Persistent-mismatch escalation (Phase 26f) ────────────────────
            # A mismatch that recurs unchanged cycle after cycle is an alarm, not
            # noise — warn-only forever means the operator may never notice.  Once
            # the same fingerprint (rounded Deribit/SQLite margins) has persisted
            # RECONCILE_ESCALATE_AFTER_CYCLES cycles, fire a single Telegram alert.
            fingerprint = (round(api_margin), round(db_margin))
            if fingerprint == self._reconcile_fingerprint:
                self._reconcile_repeat_count += 1
            else:
                self._reconcile_fingerprint = fingerprint
                self._reconcile_repeat_count = 1
                self._reconcile_escalated = False
            if (
                self._reconcile_repeat_count >= config.RECONCILE_ESCALATE_AFTER_CYCLES
                and not self._reconcile_escalated
                and self._notifier is not None
            ):
                try:
                    self._notifier.notify_warning(
                        f"RECONCILE MISMATCH persisting {self._reconcile_repeat_count} "
                        f"cycles: Deribit margin ${api_margin:.2f} vs SQLite "
                        f"${db_margin:.2f}. Deribit open: {open_desc or 'unknown'}. "
                        f"Manual reconciliation required on Deribit."
                    )
                    self._reconcile_escalated = True
                    logger.warning(
                        "RECONCILE MISMATCH escalated to operator alert after %d cycles",
                        self._reconcile_repeat_count,
                    )
                except Exception as exc:
                    logger.warning("Reconcile escalation alert failed: %s", exc)
        else:
            # Resolved — reset the escalation tracking so a future mismatch starts
            # its own fresh cycle count.
            self._reconcile_fingerprint = None
            self._reconcile_repeat_count = 0
            self._reconcile_escalated = False
            logger.debug(
                "RECONCILE OK: Deribit $%.2f vs SQLite $%.2f (divergence %.1f%%)",
                api_margin, db_margin, divergence * 100,
            )


# ── Module-level helpers ──────────────────────────────────────────────────────

def _assets_to_currencies(assets: list[str]) -> list[str]:
    """Deduplicate and return Deribit currency codes for the given asset list."""
    seen: set[str] = set()
    result: list[str] = []
    for a in assets:
        c = a.upper()
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _resolve_spot(
    currency: str,
    spot_prices: dict[str, float] | None,
    rest_url: str,
) -> float:
    """Return USD spot price for a Deribit currency, from cache or API."""
    if currency in _STABLECOIN_CURRENCIES:
        return 1.0
    if spot_prices and currency in spot_prices:
        return float(spot_prices[currency])
    return _fetch_index_price(currency, rest_url)


def _fetch_index_price(currency: str, rest_url: str) -> float:
    """Fetch current index price for a currency via Deribit public endpoint."""
    index_name = f"{currency.lower()}_usd"
    params     = urllib.parse.urlencode({"index_name": index_name})
    url        = f"{rest_url}/api/v2/public/get_index_price?{params}"
    data       = _rest_get(url)
    return float(data["result"]["index_price"])


def _rest_get(url: str, bearer_token: str | None = None, timeout: int = 10) -> dict:
    """GET a Deribit REST endpoint and return the parsed JSON response."""
    req = urllib.request.Request(url)
    if bearer_token:
        req.add_header("Authorization", f"Bearer {bearer_token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc


def _rest_post(url: str, payload: dict, bearer_token: str | None = None, timeout: int = 10) -> dict:
    """POST a JSON body to a Deribit REST endpoint and return the parsed JSON response.

    Parameters
    ----------
    url : str
        The full REST endpoint URL.
    payload : dict
        The JSON payload to send.
    bearer_token : str | None
        Optional Bearer token for authorization. If provided, added as Authorization header.
    timeout : int
        Request timeout in seconds.

    Returns
    -------
    dict
        Parsed JSON response.

    Note
    ----
    Credentials for authentication endpoints are sent in the request body rather
    than as URL query parameters (which would appear in exception messages and logs).
    """
    encoded = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/json")
    if bearer_token:
        req.add_header("Authorization", f"Bearer {bearer_token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
