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
from db.state import DB_PATH, get_connection

logger = logging.getLogger(__name__)

# If Deribit-reported margin and SQLite-computed margin diverge by more than
# this fraction, a warning is logged (possible manual trade or missed fill).
_RECONCILE_THRESHOLD = 0.10  # 10%

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
    ) -> None:
        self._db_path       = db_path or DB_PATH
        self._client_id     = client_id     if client_id     is not None else config.DERIBIT_CLIENT_ID
        self._client_secret = client_secret if client_secret is not None else config.DERIBIT_CLIENT_SECRET
        self._rest_url      = rest_url or config.DERIBIT_REST_URL
        self._cache         = cache

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

    def _get_positions(self, token: str, currency: str) -> list[dict]:
        """Fetch open option positions for one currency."""
        params = urllib.parse.urlencode({"currency": currency, "kind": "option"})
        url    = f"{self._rest_url}/api/v2/private/get_positions?{params}"
        data   = _rest_get(url, bearer_token=token)
        return data.get("result", [])

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
        # If not explicitly set, we can derive it from DB + realized/unrealized
        initial_capital = getattr(config, "INITIAL_CAPITAL", 10000.0)  # Default fallback
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
            logger.warning(
                "RECONCILE MISMATCH: Deribit margin $%.2f vs SQLite margin $%.2f "
                "(divergence %.0f%%) — possible manual trade or missed fill",
                api_margin, db_margin, divergence * 100,
            )
        else:
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
