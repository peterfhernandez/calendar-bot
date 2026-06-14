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
from strategy.scanner import CalendarCandidate

logger = logging.getLogger(__name__)

# Minimum allowed quantity to avoid dust trades
_MIN_QTY = 0.1

# Fraction of strike within which two positions are considered correlated
_STRIKE_CORRELATION_PCT = 0.05   # ±5% of strike


@dataclass
class SizeResult:
    """Output from size_candidate."""
    qty:    float   # approved quantity (0.0 = blocked)
    reason: str     # human-readable explanation


def size_candidate(
    candidate:       CalendarCandidate,
    portfolio_value: float,
    open_positions:  list[dict],
    max_loss_pct:    float | None = None,
    max_positions:   int   | None = None,
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

    Returns
    -------
    SizeResult
        qty > 0 if approved; qty == 0 with reason if blocked.
    """
    max_loss_pct = max_loss_pct if max_loss_pct is not None else config.MAX_LOSS_PCT
    max_positions = max_positions if max_positions is not None else config.MAX_POSITIONS

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

    # ── Size from max-loss budget ─────────────────────────────────────────────
    # Maximum USD we're willing to lose on this trade = portfolio * max_loss_pct.
    # Worst case loss per contract = net_debit (the full premium paid).
    max_loss_usd = portfolio_value * max_loss_pct
    if candidate.net_debit <= 0:
        return SizeResult(qty=0.0, reason="net_debit is zero or negative")

    raw_qty = max_loss_usd / candidate.net_debit

    # Round down to one decimal place (Deribit minimum increment is 0.1 for options)
    qty = max(0.0, math.floor(raw_qty * 10) / 10)

    if qty < _MIN_QTY:
        return SizeResult(
            qty=0.0,
            reason=(
                f"Computed qty {qty:.2f} below minimum {_MIN_QTY} "
                f"(net_debit={candidate.net_debit:.2f}, "
                f"max_loss_usd={max_loss_usd:.2f})"
            ),
        )

    logger.debug(
        "Sized %s %s strike=%.0f: qty=%.1f  "
        "(max_loss_usd=%.2f, net_debit=%.2f)",
        candidate.asset, candidate.option_type, candidate.strike,
        qty, max_loss_usd, candidate.net_debit,
    )
    return SizeResult(
        qty=qty,
        reason=f"Approved: qty={qty:.1f} at net_debit={candidate.net_debit:.2f}",
    )

