"""
strategy/sizer.py
=================
Position sizing for calendar spread entries.

Computes contract quantity from portfolio value and risk parameters, enforces
concurrent-position limits, and detects correlation conflicts with open trades.

Public API
----------
size_candidate(candidate, portfolio_value, open_positions) -> SizeResult
    Returns approved quantity and reason; qty=0 means the trade is blocked.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import config
from core.fees import round_trip_fees
from strategy.scanner import CalendarCandidate

logger = logging.getLogger(__name__)

# Minimum allowed quantity to avoid dust trades
_MIN_QTY = 0.1

# Fraction of strike within which two positions are considered correlated
_STRIKE_CORRELATION_PCT = 0.05   # ±5% of strike


@dataclass
class SizeResult:
    """Output from size_candidate."""
    qty:            float   # approved quantity (0.0 = blocked)
    reason:         str     # human-readable explanation
    estimated_fees: float = 0.0  # estimated round-trip fees in USD (entry + exit)


def size_candidate(
    candidate:          CalendarCandidate,
    portfolio_value:    float,
    open_positions:     list[dict],
    max_loss_pct:       float | None = None,
    max_positions:      int   | None = None,
    max_total_risk_pct: float | None = None,
) -> SizeResult:
    """
    Compute the approved contract quantity for a calendar spread candidate.

    Parameters
    ----------
    candidate
        A CalendarCandidate produced by the scanner.
    portfolio_value
        Total portfolio value in USD.
    open_positions
        List of open position dicts. Each dict must have keys:
        ``asset``, ``strike``, ``option_type``, ``net_debit``, ``qty``.
    max_loss_pct
        Max fraction of portfolio at risk per trade (default: config.MAX_LOSS_PCT).
    max_positions
        Max concurrent open positions (default: config.MAX_POSITIONS).
    max_total_risk_pct
        Hard cap on total capital-at-risk across all open positions as a fraction
        of portfolio value (default: config.MAX_TOTAL_RISK_PCT).

    Returns
    -------
    SizeResult
        qty > 0 if approved; qty == 0 with reason if blocked.
    """
    max_loss_pct       = max_loss_pct       if max_loss_pct       is not None else config.MAX_LOSS_PCT
    max_positions      = max_positions      if max_positions       is not None else config.MAX_POSITIONS
    max_total_risk_pct = max_total_risk_pct if max_total_risk_pct is not None else config.MAX_TOTAL_RISK_PCT

    # ── Concurrent position limit ─────────────────────────────────────────────
    if len(open_positions) >= max_positions:
        return SizeResult(
            qty=0.0,
            reason=f"Max positions reached ({len(open_positions)}/{max_positions})",
        )

    # ── Correlation check ─────────────────────────────────────────────────────
    for pos in open_positions:
        if pos.get("asset") != candidate.asset:
            continue
        if pos.get("option_type") != candidate.option_type:
            continue
        existing_strike = float(pos.get("strike", 0))
        if existing_strike <= 0:
            continue
        strike_diff_pct = abs(candidate.strike - existing_strike) / existing_strike
        if strike_diff_pct <= _STRIKE_CORRELATION_PCT:
            return SizeResult(
                qty=0.0,
                reason=(
                    f"Correlated position already open: "
                    f"{pos.get('asset')} {candidate.option_type} "
                    f"strike={existing_strike:.0f} "
                    f"(within {_STRIKE_CORRELATION_PCT*100:.0f}% of {candidate.strike:.0f})"
                ),
            )

    # ── Total portfolio risk budget ───────────────────────────────────────────
    # Capital already at risk = sum of (net_debit × qty) for all open positions.
    # The new trade must not push total risk past the configured hard cap.
    risk_in_use = sum(
        p.get("net_debit", 0.0) * p.get("qty", 0.0) for p in open_positions
    )
    max_total_risk_usd = portfolio_value * max_total_risk_pct
    risk_remaining     = max_total_risk_usd - risk_in_use
    if risk_remaining <= 0:
        return SizeResult(
            qty=0.0,
            reason=(
                f"Total risk budget exhausted  "
                f"(in_use=${risk_in_use:.2f}, limit=${max_total_risk_usd:.2f})"
            ),
        )

    # ── Size from max-loss budget ─────────────────────────────────────────────
    # Maximum USD we're willing to lose on this trade = portfolio * max_loss_pct,
    # clamped to whatever budget remains under the total risk cap.
    max_loss_usd = min(portfolio_value * max_loss_pct, risk_remaining)
    if candidate.net_debit <= 0:
        return SizeResult(qty=0.0, reason="net_debit is zero or negative")

    min_net_debit = getattr(config, "MIN_NET_DEBIT", 0.10)
    if candidate.net_debit < min_net_debit:
        return SizeResult(
            qty=0.0,
            reason=(
                f"net_debit {candidate.net_debit:.4f} below minimum {min_net_debit:.4f} "
                f"— spread is effectively free and cannot be sized safely"
            ),
        )

    # ── Fee-aware sizing ──────────────────────────────────────────────────────
    # True max-loss per trade = net_debit × qty + round_trip_fees(qty).
    # Since fees scale linearly with qty: fee_per_unit = round_trip_fees(1 contract).
    # Solve: (net_debit + fee_per_unit) × qty ≤ max_loss_usd
    try:
        fee_per_unit = round_trip_fees(
            candidate.asset,
            candidate.spot,
            qty=1.0,
            near_price=candidate.near_bid,
            far_price=candidate.far_ask,
            via_combo=True,
        )
    except Exception:
        fee_per_unit = 0.0

    effective_cost_per_unit = candidate.net_debit + fee_per_unit
    raw_qty = max_loss_usd / effective_cost_per_unit if effective_cost_per_unit > 0 else 0.0

    # Round down to one decimal place (Deribit minimum increment is 0.1 for options)
    qty = max(0.0, math.floor(raw_qty * 10) / 10)

    # Hard cap to prevent runaway sizes from low-debit candidates that slip past the floor
    max_qty = getattr(config, "MAX_QTY", 100.0)
    if qty > max_qty:
        qty = math.floor(max_qty * 10) / 10

    if qty < _MIN_QTY:
        return SizeResult(
            qty=0.0,
            reason=(
                f"Computed qty {qty:.2f} below minimum {_MIN_QTY} "
                f"(net_debit={candidate.net_debit:.2f}, "
                f"max_loss_usd={max_loss_usd:.2f})"
            ),
        )

    estimated_fees = fee_per_unit * qty
    logger.debug(
        "Sized %s %s strike=%.0f: qty=%.1f  "
        "(max_loss_usd=%.2f, net_debit=%.2f, fee_per_unit=%.2f, est_fees=%.2f, "
        "risk_in_use=%.2f, risk_cap=%.2f)",
        candidate.asset, candidate.option_type, candidate.strike,
        qty, max_loss_usd, candidate.net_debit, fee_per_unit, estimated_fees,
        risk_in_use, max_total_risk_usd,
    )
    return SizeResult(
        qty=qty,
        reason=f"Approved: qty={qty:.1f} at net_debit={candidate.net_debit:.2f} est_fees={estimated_fees:.2f}",
        estimated_fees=estimated_fees,
    )

