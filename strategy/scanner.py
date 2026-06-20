"""
strategy/scanner.py
===================
Calendar spread opportunity scanner and ranker.

Reads live option chain data from a ChainCache, identifies valid near/far
expiry pairs, filters them by liquidity and IV term structure, scores each
candidate by expected value, and returns a ranked list.

Public API
----------
scan(cache, assets, near_days_options, far_days_options, ...) -> list[CalendarCandidate]
    Full scan pipeline: enumerate → filter → score → rank.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from config import RISK_FREE_RATE
from core.calendar_engine import find_breakevens, pnl_at_near_expiry
from data.chain_cache import ChainCache
from data.deribit_feed import TickerSnapshot

logger = logging.getLogger(__name__)

# Deribit instrument name pattern: BTC-27JUN25-100000-C
_INSTRUMENT_RE = re.compile(
    r"^(?P<asset>[A-Z]+)-(?P<expiry>\d{1,2}[A-Z]{3}\d{2})-(?P<strike>\d+)-(?P<type>[CP])$"
)
_EXPIRY_FMT = "%d%b%y"   # e.g. "27JUN25"


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class CalendarCandidate:
    """A fully evaluated and scored calendar spread candidate."""

    asset:       str    # "BTC" or "ETH"
    strike:      float  # strike price (USD)
    option_type: str    # "Call" or "Put"

    near_instrument: str    # Deribit instrument name for the near leg
    far_instrument:  str    # Deribit instrument name for the far leg
    near_days:       int    # days to near-leg expiry
    far_days:        int    # days to far-leg expiry

    spot:    float  # spot price at scan time
    near_iv: float  # implied vol of near leg (decimal)
    far_iv:  float  # implied vol of far leg (decimal)
    iv_contango: float  # near_iv - far_iv (positive = contango)

    near_ask: float  # ask price of near leg
    near_bid: float  # bid price of near leg
    far_ask:  float  # ask price of far leg (we buy it)
    far_bid:  float  # bid price of far leg

    net_debit: float  # estimated entry cost: far_ask - near_bid

    near_oi: float  # open interest on near leg
    far_oi:  float  # open interest on far leg

    pop:       float  # probability of profit at near-leg expiry
    be_lo:     float  # lower breakeven price
    be_hi:     float  # upper breakeven price
    ev_score:  float  # expected value as a fraction of net_debit (e.g. 0.25 = EV is 25% of debit paid)

    # Fields set after sizing
    qty: float = 0.0

    # Metadata
    scanned_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


# ── Instrument parsing ────────────────────────────────────────────────────────

def parse_instrument(name: str) -> tuple[str, datetime, float, str] | None:
    """
    Parse a Deribit instrument name into (asset, expiry_dt, strike, option_type).

    Returns None if the name doesn't match the expected pattern.
    """
    m = _INSTRUMENT_RE.match(name)
    if not m:
        return None
    try:
        expiry_dt = datetime.strptime(m.group("expiry"), _EXPIRY_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    asset       = m.group("asset")
    strike      = float(m.group("strike"))
    option_type = "Call" if m.group("type") == "C" else "Put"
    return asset, expiry_dt, strike, option_type


def days_to_expiry(expiry_dt: datetime) -> int:
    """Return calendar days from now (UTC) to *expiry_dt*."""
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = expiry_dt.replace(hour=8, minute=0, second=0, microsecond=0) - now
    return max(int(delta.days), 0)


# ── Core scan pipeline ────────────────────────────────────────────────────────

def _group_chain(
    snaps: list[TickerSnapshot],
) -> dict[tuple[float, str], list[tuple[int, TickerSnapshot]]]:
    """
    Group snapshots by (strike, option_type) → [(days_to_expiry, snap), ...].

    Snapshots that can't be parsed are silently skipped.
    """
    groups: dict[tuple[float, str], list[tuple[int, TickerSnapshot]]] = {}
    for snap in snaps:
        parsed = parse_instrument(snap.instrument)
        if parsed is None:
            continue
        _, expiry_dt, strike, opt_type = parsed
        dte = days_to_expiry(expiry_dt)
        key = (strike, opt_type)
        groups.setdefault(key, []).append((dte, snap))
    return groups


def _ev_score(
    spot: float,
    strike: float,
    near_dte: int,
    far_dte: int,
    near_iv: float,
    net_debit: float,
    opt_type: str,
    pop: float,
    n_samples: int = 40,
) -> float:
    """
    Estimate expected value score via a grid of P&L samples weighted by
    a log-normal spot distribution at near-leg expiry.
    """
    T_near = near_dte / 365.0
    sigma  = near_iv * math.sqrt(T_near)
    mu     = math.log(spot) + (RISK_FREE_RATE - 0.5 * near_iv ** 2) * T_near

    # Uniform grid spanning ±3σ in log-space
    spots_grid = [
        math.exp(mu + sigma * (i / (n_samples - 1) * 6 - 3))
        for i in range(n_samples)
    ]
    pnls = [
        pnl_at_near_expiry(
            s, strike, near_dte, far_dte,
            RISK_FREE_RATE, near_iv, qty=1.0,
            net_debit=net_debit, option_type=opt_type,
        )
        for s in spots_grid
    ]

    gains  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win  = sum(gains)  / len(gains)  if gains  else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return pop * avg_win - (1.0 - pop) * abs(avg_loss)


def _eval_candidate(
    asset:      str,
    strike:     float,
    opt_type:   str,
    near_dte:   int,
    near_snap:  TickerSnapshot,
    far_dte:    int,
    far_snap:   TickerSnapshot,
    spot:       float,
    min_oi_near:     float,
    min_oi_far:      float,
    min_iv_contango: float,
    min_pop:         float,
) -> CalendarCandidate | None:
    """
    Evaluate a single near/far pair. Returns None if any filter fails.
    """
    # ── OI filter ─────────────────────────────────────────────────────────────
    if near_snap.open_interest < min_oi_near:
        return None
    if far_snap.open_interest < min_oi_far:
        return None

    # ── IV contango filter ────────────────────────────────────────────────────
    near_iv = near_snap.mark_iv
    far_iv  = far_snap.mark_iv
    if near_iv <= 0 or far_iv <= 0:
        return None
    iv_contango = near_iv - far_iv
    if iv_contango < min_iv_contango:
        return None

    # ── Entry cost (net debit) ────────────────────────────────────────────────
    # BUY the far leg (pay far_ask); SELL the near leg (receive near_bid).
    near_bid = near_snap.bid if near_snap.bid > 0 else near_snap.mark_price
    far_ask  = far_snap.ask  if far_snap.ask  > 0 else far_snap.mark_price
    if near_bid <= 0 or far_ask <= 0:
        return None
    net_debit = far_ask - near_bid
    if net_debit <= 0:
        return None

    # ── Probability of profit ─────────────────────────────────────────────────
    be_lo, be_hi = find_breakevens(
        spot, strike, near_dte, far_dte,
        RISK_FREE_RATE, near_iv, qty=1.0, net_debit=net_debit,
        option_type=opt_type,
    )
    # When the spread is profitable across the entire scan range, find_breakevens
    # returns (0, 0).  Treat this as a full-range profit: be_lo/be_hi span the
    # scan window so pop can still be computed accurately.
    if be_lo <= 0 and be_hi <= 0:
        be_lo = spot * 0.50
        be_hi = spot * 1.50

    T_near = near_dte / 365.0
    if T_near <= 0:
        return None

    sigma = near_iv * math.sqrt(T_near)
    mu    = math.log(spot) + (RISK_FREE_RATE - 0.5 * near_iv ** 2) * T_near

    def _ln_cdf(x: float) -> float:
        if x <= 0:
            return 0.0
        z = (math.log(x) - mu) / sigma
        return 0.5 * math.erfc(-z / math.sqrt(2))

    pop = max(0.0, min(1.0, _ln_cdf(be_hi) - _ln_cdf(be_lo)))
    if pop < min_pop:
        return None

    ev = _ev_score(spot, strike, near_dte, far_dte, near_iv, net_debit, opt_type, pop)
    ev_ratio = ev / net_debit if net_debit > 0 else 0.0

    return CalendarCandidate(
        asset=asset,
        strike=strike,
        option_type=opt_type,
        near_instrument=near_snap.instrument,
        far_instrument=far_snap.instrument,
        near_days=near_dte,
        far_days=far_dte,
        spot=spot,
        near_iv=near_iv,
        far_iv=far_iv,
        iv_contango=iv_contango,
        near_ask=near_snap.ask if near_snap.ask > 0 else near_snap.mark_price,
        near_bid=near_bid,
        far_ask=far_ask,
        far_bid=far_snap.bid if far_snap.bid > 0 else far_snap.mark_price,
        net_debit=net_debit,
        near_oi=near_snap.open_interest,
        far_oi=far_snap.open_interest,
        pop=pop,
        be_lo=be_lo,
        be_hi=be_hi,
        ev_score=ev_ratio,
    )


def scan(
    cache: ChainCache,
    assets:             list[str]  | None = None,
    near_days_options:  list[int]  | None = None,
    far_days_options:   list[int]  | None = None,
    min_oi_near:        float | None = None,
    min_oi_far:         float | None = None,
    min_iv_contango:    float | None = None,
    min_pop:            float | None = None,
    near_day_tolerance: int = 3,
    far_day_tolerance:  int = 7,
) -> list[CalendarCandidate]:
    """
    Scan the cache and return a ranked list of calendar spread candidates.

    Parameters
    ----------
    cache
        Populated ChainCache instance.
    assets
        Assets to scan (default: config.ASSETS).
    near_days_options
        Target near-leg DTE values to match (default: config.NEAR_DAYS_OPTIONS).
    far_days_options
        Target far-leg DTE values to match (default: config.FAR_DAYS_OPTIONS).
    min_oi_near / min_oi_far
        Minimum open interest per leg (default: config values).
    min_iv_contango
        Minimum near_iv - far_iv required (default: config.MIN_IV_CONTANGO).
    min_pop
        Minimum probability of profit (default: config.MIN_POP).
    near_day_tolerance / far_day_tolerance
        Acceptable DTE range around each target (±days).

    Returns
    -------
    list[CalendarCandidate]
        Candidates sorted by ev_score descending (best first).
    """
    assets            = [a.upper() for a in (assets            or config.ASSETS)]
    near_days_options = near_days_options or config.NEAR_DAYS_OPTIONS
    far_days_options  = far_days_options  or config.FAR_DAYS_OPTIONS
    min_oi_near       = min_oi_near       if min_oi_near       is not None else config.MIN_OI_NEAR
    min_oi_far        = min_oi_far        if min_oi_far        is not None else config.MIN_OI_FAR
    min_iv_contango   = min_iv_contango   if min_iv_contango   is not None else config.MIN_IV_CONTANGO
    min_pop           = min_pop           if min_pop           is not None else config.MIN_POP

    candidates: list[CalendarCandidate] = []

    for asset in assets:
        spot = cache.get_spot(asset)
        if spot is None or spot <= 0:
            logger.debug("No spot price for %s — skipping", asset)
            continue

        chain  = cache.get_chain(asset)
        groups = _group_chain(chain)

        logger.debug(
            "%s: %d instruments → %d (strike, type) groups",
            asset, len(chain), len(groups),
        )

        for (strike, opt_type), entries in groups.items():
            for near_target in near_days_options:
                near_matches = [
                    (dte, snap) for dte, snap in entries
                    if abs(dte - near_target) <= near_day_tolerance
                ]
                if not near_matches:
                    continue
                near_dte, near_snap = min(near_matches, key=lambda x: abs(x[0] - near_target))

                for far_target in far_days_options:
                    if far_target <= near_dte:
                        continue
                    far_matches = [
                        (dte, snap) for dte, snap in entries
                        if abs(dte - far_target) <= far_day_tolerance and dte > near_dte
                    ]
                    if not far_matches:
                        continue
                    far_dte, far_snap = min(far_matches, key=lambda x: abs(x[0] - far_target))

                    result = _eval_candidate(
                        asset, strike, opt_type,
                        near_dte, near_snap,
                        far_dte,  far_snap,
                        spot,
                        min_oi_near, min_oi_far,
                        min_iv_contango, min_pop,
                    )
                    if result is not None:
                        candidates.append(result)

    candidates.sort(key=lambda c: c.ev_score, reverse=True)
    logger.info("Scan complete: %d candidates across %s", len(candidates), assets)
    return candidates
