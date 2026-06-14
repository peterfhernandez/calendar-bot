"""
Ported from optionsStrategy/strategies/scanner.py
THIS MUST BE ADAPTED FOR THIS CALENDAR BOT, NOT JUST COPIED!
REMOVE UNUSED STRATEGIES
=====================
Cross-asset, cross-strategy trade recommendation scanner.

Scans every (asset, strategy, OTM level) combination, computes key
metrics for each candidate, including live liquidity data from Deribit,
and ranks them by two criteria:

  1. Highest probability of profit with a reasonable return
  2. Best annualised return regardless of probability

Generates exactly 3 propositions per instrument/strategy pair
(one per OTM level in config.OTM_LEVELS), consistent with the
analysis sections in wheel.py and strangle.py.

Public API
----------
run_scanner(active_spot, active_iv, active_asset, days)
    Fetch live data, compute all candidates, display ranked results.

Internal helpers
----------------
_fetch_liquidity(ticker, spot, days,      Fetch IV + liquidity for one strike
                 strike_round, otm, r)
_build_candidates(asset, spot, iv, days)  Build all candidates for one asset
_display_candidates(candidates)           Print the full candidate table
_display_ranked(label, candidates)        Print a ranked recommendation block
"""

from dataclasses import dataclass, field

import math

from database.scanner_db import save_scan_results
from database.wheel_db import load_wheel_state
from database.strangle_db import load_strangle_state
from database.calendar_db import load_calendar_state
from database.spread_db import load_spread_state
from config  import (
    SUPPORTED_ASSETS, TRADEABLE_ASSETS, BUDGET_USD, RISK_FREE_RATE, OTM_LEVELS,
    CALENDAR_NEAR_DAYS, CALENDAR_FAR_DAYS, CALENDAR_SPREADS, SPREAD_WIDTH_PCT, DERIBIT_PAPER,
)
from market.pricing import bs_put, bs_call, prob_otm_put, prob_otm_call
from ui.display import hdr, sub, inf, warn, ok, GR, RD, CY, YL, GY, WH, B, R
from market.market_data import get_spot_price, get_deribit_iv, _deribit_instrument, _fetch_order_book
from access import BrokerBase, DeribitClient
from trading.executor import enter_trade
from trading.fee_calculator import calculate_fee


def _has_open_position(asset: str, strategy: str) -> bool:
    """Check if there's an open position for the given asset and strategy."""
    try:
        if strategy in ("CSP", "CC"):
            state = load_wheel_state(asset)
        elif strategy == "Strangle":
            state = load_strangle_state(asset)
        elif strategy in ("Cal-C", "Cal-P"):
            state = load_calendar_state(asset)
        elif strategy in ("BPS", "BCS"):
            state = load_spread_state(asset)
        else:
            return False
        return bool(state.get("open"))
    except Exception:
        return False


# ── Minimum yield threshold for "high probability" ranking ───────────────────
MIN_YIELD_PCT = 20.0   # exclude candidates below this annualised yield

# ── Liquidity rating thresholds ───────────────────────────────────────────────
_OI_HIGH    = 1000.0   # open interest ≥ this → high liquidity
_OI_MED     = 100.0    # open interest ≥ this → medium liquidity
_VOL_HIGH   = 50000.0  # 24h volume USD ≥ this → high liquidity
_SPREAD_LOW = 0.02     # IV spread ≤ this → tight (liquid) market

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """A single tradeable proposition with all relevant metrics."""
    asset:        str
    strategy:     str    # "CSP", "CC", "Strangle", "Cal-C", "Cal-P"
    otm_pct:      float  # e.g. 0.10, 0.15, 0.20; 0.00 = ATM for calendars
    spot:         float
    iv:           float
    strike:       str    # formatted string e.g. "$1800" or "$1700/$2000"
    premium:      float  # total premium / net debit in USD
    yield_ann:    float  # annualised yield as a percentage
    prob_profit:  float  # probability of full profit (0–100)
    days:         int

    # Liquidity fields (None if Deribit fetch skipped or failed)
    open_interest: float = None
    volume_usd:    float = None
    iv_spread:     float = None   # ask_iv - bid_iv as decimal
    liquidity_tag: str   = ""     # "High", "Med", "Low", or ""

    # Strangle-only fields
    put_strike:   float = None
    call_strike:  float = None
    be_lo:        float = None
    be_hi:        float = None

    # Spread-only fields (BPS/BCS)
    short_strike: float = None
    long_strike:  float = None

    # Calendar-only fields
    far_days:     int   = None
    max_profit:   float = None


def _liquidity_tag(oi: float, vol_usd: float, iv_spread: float) -> str:
    """
    Derive a simple liquidity label from open interest, volume, and IV spread.

    High : OI ≥ 1000 AND spread ≤ 0.02
    Med  : OI ≥ 100  OR  vol_usd ≥ $50k
    Low  : everything else
    """
    if oi is None:
        return ""
    if oi >= _OI_HIGH and (iv_spread is None or iv_spread <= _SPREAD_LOW):
        return "High"
    if oi >= _OI_MED or (vol_usd and vol_usd >= _VOL_HIGH):
        return "Med"
    return "Low"


def _combine_liquidity_legs(*books) -> dict:
    """
    Combine the order books of two or more legs into a single
    "worst-of-legs" liquidity view.

    Used for multi-leg strategies (strangle, calendar) where the trade is
    only as fillable as its weakest leg. We take:

        open_interest : min   across legs   (the leg that has the fewest
                                              contracts open is the bottleneck)
        volume_usd    : min   across legs
        iv_spread     : max   across legs   (widest spread = least liquid)

    If any leg's book is missing, the combined result has empty fields and
    a blank liquidity_tag — better to be honest about uncertainty than to
    invent a rating.

    Returns
    -------
    dict with keys: open_interest, volume_usd, iv_spread, liquidity_tag
    """
    if not books or any(b is None for b in books):
        return {
            "open_interest": None,
            "volume_usd":    None,
            "iv_spread":     None,
            "liquidity_tag": "",
        }

    ois  = [b["open_interest"] for b in books if b.get("open_interest") is not None]
    vols = [b["volume_usd"]    for b in books if b.get("volume_usd")    is not None]
    spds = [b["iv_spread"]     for b in books if b.get("iv_spread")     is not None]

    worst_oi  = min(ois)  if ois  else None
    worst_vol = min(vols) if vols else None
    worst_spd = max(spds) if spds else None

    return {
        "open_interest": worst_oi,
        "volume_usd":    worst_vol,
        "iv_spread":     worst_spd,
        "liquidity_tag": _liquidity_tag(worst_oi, worst_vol, worst_spd),
    }
 

def _round_strike(price: float, strike_round: float) -> float:
    """Round a strike to the asset's configured strike increment."""
    rounded = round(price / strike_round) * strike_round
    return max(rounded, strike_round)


def _format_strike(strike: float, strike_round: float) -> str:
    """Format a strike string with the correct decimal precision for the asset."""
    decimals = 0 if strike_round >= 1 else int(round(-math.log10(strike_round)))
    return f"${strike:,.{decimals}f}"
 
 
 # ── Liquidity fetcher ─────────────────────────────────────────────────────────
 
def _fetch_liquidity(
    ticker:       str,
    spot:         float,
    days:         int,
    strike_round: int,
    otm:          float,
    side:         str,   # "put" or "call"
) -> dict | None:
    """
    Fetch IV and liquidity data from Deribit for one strike.

    Returns the order book dict from _fetch_order_book, or None on failure.
    For strangles both put and call are fetched; the put side is used for IV.

    Pass ``otm=0.0`` to fetch an at-the-money strike (used for calendar
    spreads).  ``days`` selects the expiry — calendars need to call this
    twice, once with the near-leg ``days`` and once with
    ``CALENDAR_FAR_DAYS``.
    """

    spot_adj = spot * (1 - otm) if side == "put" else spot * (1 + otm)
    opt_type = "P" if side == "put" else "C"

    instrument = _deribit_instrument(
        ticker       = ticker,
        spot         = spot_adj,
        days         = days,
        strike_round = strike_round,
        option_type  = opt_type,
    )
    return _fetch_order_book(instrument)


# ── Candidate builder ─────────────────────────────────────────────────────────

def _build_candidates(
    asset: str,
    spot:  float,
    iv:    float,
    days:  int,
    *,
    cal_near_days: int | None = None,
    cal_far_days:  int | None = None,
    progress_callback = None,
) -> list[Candidate]:
    """
    Build all (strategy, OTM level) candidates for one asset.

    Generates 3 propositions per strategy (one per OTM level),
    matching the analysis tables in show_strikes() and show_strangle_analysis().

    The wheel/strangle propositions use the global ``days`` near-expiry.
    Calendar propositions use their own dedicated near + far horizons,
    independent of ``days`` — defaults come from ``config.CALENDAR_SPREADS``
    and can be overridden per call.

    Parameters
    ----------
    progress_callback : callable or None
        Optional callback invoked as each candidate is created.
        Signature: progress_callback(asset, strategy, specifics, strike_str)
        Example: progress_callback("ETH", "CSP", "10%", "$1800")

    Returns a flat list of Candidate objects.
    """
    # Determine which calendar pairs to use
    # Use all configured pairs if no custom horizons provided, or if the provided values
    # match the defaults (meaning the user hasn't explicitly overridden them)
    if (
        (cal_near_days is None and cal_far_days is None)
        or (cal_near_days == CALENDAR_NEAR_DAYS and cal_far_days == CALENDAR_FAR_DAYS)
    ):
        calendar_pairs = CALENDAR_SPREADS
    else:
        calendar_pairs = [(cal_near_days, cal_far_days)]

    T   = days / 365.0
    r   = RISK_FREE_RATE
    cfg = SUPPORTED_ASSETS[asset]
    ticker       = cfg["deribit_ticker"]
    strike_round = cfg["strike_round"]
    candidates = []

    for otm in OTM_LEVELS:
        Kp = _round_strike(spot * (1 - otm), strike_round)
        Kc = _round_strike(spot * (1 + otm), strike_round)

        # Fetch liquidity for put and call strikes
        put_book  = _fetch_liquidity(ticker, spot, days, strike_round, otm, "put")
        call_book = _fetch_liquidity(ticker, spot, days, strike_round, otm, "call")

        # Use live IV from Deribit if available, otherwise use passed-in iv
        put_iv  = put_book["mark_iv"]  if put_book  else iv
        call_iv = call_book["mark_iv"] if call_book else iv

        # ── Cash-Secured Put ──────────────────────────────────────────────────
        qty_p = BUDGET_USD / Kp
        pp    = bs_put(spot, Kp, T, r, put_iv) * qty_p
        # Account for open and estimated close fees
        open_fee_p = calculate_fee(spot, pp / qty_p, asset) * qty_p
        close_fee_est_p = calculate_fee(spot, 0.01, asset) * qty_p  # Estimate close fee at ~1 cent per contract
        effective_pp = pp - open_fee_p - close_fee_est_p
        yld_p = (effective_pp / BUDGET_USD) * (365 / days) * 100 if effective_pp > 0 else 0.0
        pop_p = prob_otm_put(spot, Kp, T, r, put_iv) * 100
        oi_p  = put_book["open_interest"] if put_book else None
        vol_p = put_book["volume_usd"]    if put_book else None
        spd_p = put_book["iv_spread"]     if put_book else None
        cand_csp = Candidate(
            asset       = asset,
            strategy    = "CSP",
            otm_pct     = otm,
            spot        = spot,
            iv          = put_iv,
            strike      = _format_strike(Kp, strike_round),
            premium     = round(effective_pp, 2),
            yield_ann   = round(yld_p, 1),
            prob_profit = round(pop_p, 1),
            days        = days,
            open_interest = oi_p,
            volume_usd  = vol_p,
            iv_spread   = spd_p,
            liquidity_tag = _liquidity_tag(oi_p, vol_p, spd_p),
        )
        candidates.append(cand_csp)
        if progress_callback:
            progress_callback(asset, "CSP", f"{otm*100:.0f}%", cand_csp.strike)

        # ── Covered Call ──────────────────────────────────────────────────────
        qty_c = BUDGET_USD / spot
        cp    = bs_call(spot, Kc, T, r, call_iv) * qty_c
        # Account for open and estimated close fees
        open_fee_c = calculate_fee(spot, cp / qty_c, asset) * qty_c
        close_fee_est_c = calculate_fee(spot, 0.01, asset) * qty_c  # Estimate close fee
        effective_cp = cp - open_fee_c - close_fee_est_c
        yld_c = (effective_cp / BUDGET_USD) * (365 / days) * 100 if effective_cp > 0 else 0.0
        pop_c = prob_otm_call(spot, Kc, T, r, call_iv) * 100

        oi_c  = call_book["open_interest"] if call_book else None
        vol_c = call_book["volume_usd"]    if call_book else None
        spd_c = call_book["iv_spread"]     if call_book else None
        cand_cc = Candidate(
            asset       = asset,
            strategy    = "CC",
            otm_pct     = otm,
            spot        = spot,
            iv          = call_iv,
            strike      = _format_strike(Kc, strike_round),
            premium     = round(effective_cp, 2),
            yield_ann   = round(yld_c, 1),
            prob_profit = round(pop_c, 1),
            days        = days,
            open_interest = oi_c,
            volume_usd  = vol_c,
            iv_spread   = spd_c,
            liquidity_tag = _liquidity_tag(oi_c, vol_c, spd_c),
        )
        candidates.append(cand_cc)
        if progress_callback:
            progress_callback(asset, "CC", f"{otm*100:.0f}%", cand_cc.strike)

        # ── Short Strangle ────────────────────────────────────────────────────
        qty3          = BUDGET_USD / spot
        sp            = bs_put (spot, Kp, T, r, iv) * qty3
        sc            = bs_call(spot, Kc, T, r, iv) * qty3
        tot           = sp + sc
        # Account for open and estimated close fees for both legs
        open_fee_sp = calculate_fee(spot, sp / qty3, asset) * qty3
        open_fee_sc = calculate_fee(spot, sc / qty3, asset) * qty3
        close_fee_est_sp = calculate_fee(spot, 0.01, asset) * qty3
        close_fee_est_sc = calculate_fee(spot, 0.01, asset) * qty3
        effective_tot = tot - open_fee_sp - open_fee_sc - close_fee_est_sp - close_fee_est_sc
        yld3          = (effective_tot / BUDGET_USD) * (365 / days) * 100 if effective_tot > 0 else 0.0
        prem_per_unit = effective_tot / qty3 if qty3 else 0
        be_lo         = Kp - prem_per_unit
        be_hi         = Kc + prem_per_unit
        p_lo          = prob_otm_put (spot, Kp, T, r, iv)
        p_hi          = prob_otm_call(spot, Kc, T, r, iv)
        pop3          = max(0.0, (p_lo + p_hi - 1) * 100)

        # Worst-of-legs liquidity — strangle is only as fillable as its
        # weakest leg (reuse the put/call books fetched above).
        liq_str = _combine_liquidity_legs(put_book, call_book)
        cand_str = Candidate(
            asset       = asset,
            strategy    = "Strangle",
            otm_pct     = otm,
            spot        = spot,
            iv          = iv,
            strike      = f"{_format_strike(Kp, strike_round)}/{_format_strike(Kc, strike_round)}",
            premium     = round(effective_tot, 2),
            yield_ann   = round(yld3, 1),
            prob_profit = round(pop3, 1),
            days        = days,
            put_strike  = Kp,
            call_strike = Kc,
            be_lo       = round(be_lo, 0),
            be_hi       = round(be_hi, 0),
            open_interest = liq_str["open_interest"],
            volume_usd    = liq_str["volume_usd"],
            iv_spread     = liq_str["iv_spread"],
            liquidity_tag = liq_str["liquidity_tag"],
        )
        candidates.append(cand_str)
        if progress_callback:
            progress_callback(asset, "Strangle", f"{otm*100:.0f}%", cand_str.strike)

    # ── Calendar Spreads (ATM only — multiple near/far pairs per asset) ──────

    for cal_near, cal_far in calendar_pairs:
        if cal_near < cal_far:
            T_far  = cal_far / 365.0
            T_rem  = max(cal_far - cal_near, 1) / 365.0
            qty_c  = BUDGET_USD / spot
            K_atm  = _round_strike(spot, strike_round)

            for cal_type, bs_near, bs_far, side in (
                ("Cal-C", bs_call, bs_call, "call"),
                ("Cal-P", bs_put,  bs_put,  "put"),
            ):
                near_prem = bs_near(spot, K_atm, cal_near / 365.0, r, iv) * qty_c
                far_prem  = bs_far (spot, K_atm, T_far, r, iv) * qty_c
                net_debit = far_prem - near_prem

                # Account for fees on both legs (estimating close fees)
                open_fee_near = calculate_fee(spot, near_prem / qty_c, asset) * qty_c
                open_fee_far = calculate_fee(spot, far_prem / qty_c, asset) * qty_c
                close_fee_est_near = calculate_fee(spot, 0.01, asset) * qty_c
                close_fee_est_far = calculate_fee(spot, 0.01, asset) * qty_c
                # For calendar: we receive near_prem (pay fee on it), pay far_prem (pay fee on it)
                net_debit_with_fees = (far_prem + open_fee_far + close_fee_est_far) - (near_prem - open_fee_near - close_fee_est_near)

                # Max profit: spot pins the strike at near expiry
                bs_fn     = bs_call if cal_type == "Cal-C" else bs_put
                max_far   = bs_fn(K_atm, K_atm, T_rem, r, iv) * qty_c
                max_profit = max_far - net_debit_with_fees

                # Yield = max_profit / net_debit * annualised (based on near days)
                yld_cal = (max_profit / net_debit_with_fees * (365 / cal_near) * 100) if net_debit_with_fees > 0 else 0.0

                # Rough P(profit): probability spot stays within ±max_profit/net_debit band
                # Use numeric breakeven scan (fast pure-math function)
                from strategies.calendar import _find_breakevens as _cal_be
                be_lo_c, be_hi_c = _cal_be(
                    spot, K_atm, cal_near, cal_far, r, iv, qty_c, net_debit,
                    "Call" if cal_type == "Cal-C" else "Put",
                )
                if be_lo_c > 0 and be_hi_c > 0:
                    p_cal = max(0.0, (
                        prob_otm_put (spot, be_lo_c, cal_near / 365.0, r, iv) +
                        prob_otm_call(spot, be_hi_c, cal_near / 365.0, r, iv) - 1
                    ) * 100)
                else:
                    p_cal = 0.0

                # Worst-of-legs liquidity — fetch ATM near + far order books
                # for the appropriate option side.  A calendar is only as
                # fillable as the worse of its two legs.
                near_book = _fetch_liquidity(
                    ticker, spot, cal_near, strike_round, 0.0, side,
                )
                far_book  = _fetch_liquidity(
                    ticker, spot, cal_far, strike_round, 0.0, side,
                )
                liq_cal = _combine_liquidity_legs(near_book, far_book)

                cand_cal = Candidate(
                    asset       = asset,
                    strategy    = cal_type,
                    otm_pct     = 0.00,
                    spot        = spot,
                    iv          = iv,
                    strike      = f"{_format_strike(K_atm, strike_round)} ATM {cal_near}d/{cal_far}d",
                    premium     = round(net_debit_with_fees, 2),   # net debit = max loss (including fees)
                    yield_ann   = round(yld_cal, 1),
                    prob_profit = round(p_cal, 1),
                    days        = cal_near,
                    far_days    = cal_far,
                    max_profit  = round(max_profit, 2),
                    open_interest = liq_cal["open_interest"],
                    volume_usd    = liq_cal["volume_usd"],
                    iv_spread     = liq_cal["iv_spread"],
                    liquidity_tag = liq_cal["liquidity_tag"],
                )
                candidates.append(cand_cal)
                if progress_callback:
                    progress_callback(asset, cal_type, f"{cal_near}d/{cal_far}d ATM", cand_cal.strike)

    # ── Credit Spreads (BPS + BCS per OTM level) ─────────────────────────────
    for otm in OTM_LEVELS:
        for stype, bs_short, bs_long, side_short, side_long in (
            ("BPS", bs_put,  bs_put,  "put",  "put"),
            ("BCS", bs_call, bs_call, "call", "call"),
        ):
            width = SPREAD_WIDTH_PCT
            if stype == "BPS":
                short_k = _round_strike(spot * (1 - otm),         strike_round)
                long_k  = _round_strike(spot * (1 - otm - width), strike_round)
            else:
                short_k = _round_strike(spot * (1 + otm),         strike_round)
                long_k  = _round_strike(spot * (1 + otm + width), strike_round)

            qty_sp    = BUDGET_USD / spot
            short_p   = bs_short(spot, short_k, T, r, iv)
            long_p    = bs_long (spot, long_k,  T, r, iv)
            net_cr    = (short_p - long_p) * qty_sp

            # Account for fees on both legs (short and long)
            fee_short = calculate_fee(spot, short_p, asset)
            fee_long = calculate_fee(spot, long_p, asset)
            close_fee_est_short = calculate_fee(spot, 0.01, asset)
            close_fee_est_long = calculate_fee(spot, 0.01, asset)
            # Adjusted net credit: sell short (pay fee), buy long (pay fee and close fee)
            adjusted_net_cr = (short_p - fee_short - close_fee_est_short - long_p - fee_long - close_fee_est_long) * qty_sp

            max_ls    = abs(short_k - long_k) * qty_sp - adjusted_net_cr
            cap_req   = max_ls + adjusted_net_cr   # capital at risk = max_loss
            yld_sp    = (adjusted_net_cr / cap_req * (365 / days) * 100) if cap_req > 0 else 0.0

            if stype == "BPS":
                pop_sp = prob_otm_put(spot, short_k, T, r, iv) * 100
            else:
                pop_sp = prob_otm_call(spot, short_k, T, r, iv) * 100

            # Worst-of-legs liquidity
            short_book = _fetch_liquidity(ticker, spot, days, strike_round, otm, side_short)
            long_book  = _fetch_liquidity(
                ticker, spot, days, strike_round, otm + width, side_long
            )
            liq_sp = _combine_liquidity_legs(short_book, long_book)

            cand_sp = Candidate(
                asset         = asset,
                strategy      = stype,
                otm_pct       = otm,
                spot          = spot,
                iv            = iv,
                strike        = (
                    f"{_format_strike(short_k, strike_round)}"
                    f"/{_format_strike(long_k, strike_round)}"
                ),
                premium       = round(adjusted_net_cr, 2),
                yield_ann     = round(yld_sp, 1),
                prob_profit   = round(pop_sp, 1),
                days          = days,
                short_strike  = short_k,
                long_strike   = long_k,
                open_interest = liq_sp["open_interest"],
                volume_usd    = liq_sp["volume_usd"],
                iv_spread     = liq_sp["iv_spread"],
                liquidity_tag = liq_sp["liquidity_tag"],
            )
            candidates.append(cand_sp)
            if progress_callback:
                progress_callback(asset, stype, f"{otm*100:.0f}%", cand_sp.strike)

    return candidates


# ── Display helpers ───────────────────────────────────────────────────────────


def _liq_colour(tag: str) -> str:
    """Return ANSI colour for a liquidity tag."""
    return GR if tag == "High" else YL if tag == "Med" else RD if tag == "Low" else GY
 
 
def _fmt_oi(oi: float) -> str:
    """Format open interest compactly."""
    if oi is None: return "  —"
    if oi >= 1000: return f"{oi/1000:.1f}k"
    return f"{oi:.0f}"
 
 
def _fmt_vol(vol: float) -> str:
    """Format 24h volume compactly."""
    if vol is None: return "  —"
    if vol >= 1_000_000: return f"${vol/1_000_000:.1f}M"
    if vol >= 1_000:     return f"${vol/1_000:.0f}k"
    return f"${vol:.0f}"
 
 
def _fmt_spread(spd: float) -> str:
    """Format IV spread as percentage points."""
    if spd is None: return " —"
    return f"{spd*100:.1f}pp"


def _display_candidates(candidates: list[Candidate], days: int) -> None:
    """Print the full candidate table grouped by asset."""

    current_asset = None
    for c in candidates:
        if c.asset != current_asset:
            current_asset = c.asset
            sub(f"{c.asset}  spot=${c.spot:,.2f}   IV={c.iv*100:.0f}%   {days}d")
            print(
                f"\n  {'Strategy':<12}{'OTM%':<7}{'Strike(s)':<22}"
                f"{'Prem/Debit':<11}{'Yld/yr':<9}{'P(Prof)':<10}"
                f"{'OI':<8}{'Vol24h':<10}{'Spread':<9}{'Liq'}"
            )
            print(f"  {'─' * 102}")

        lc  = _liq_colour(c.liquidity_tag)
        pc  = GR if c.prob_profit >= 70 else YL if c.prob_profit >= 55 else WH
        is_cal = c.strategy.startswith("Cal")
        otm_str = "ATM" if c.otm_pct == 0.00 else f"{c.otm_pct*100:.0f}%"
        # For calendars, append max-profit note after premium
        prem_str = f"${c.premium:<7.2f}"
        if is_cal and c.max_profit is not None:
            prem_str = f"${c.premium:.2f}{'':2}"   # debit
        print(
            f"  {WH}{c.strategy:<12}{otm_str:<7}"
            f"{c.strike:<22}{prem_str}"
            f"{'':2}{c.yield_ann:>6.1f}%/yr  "
            f"{pc}{c.prob_profit:>5.1f}%{WH}     "
            f"{_fmt_oi(c.open_interest):<8}"
            f"{_fmt_vol(c.volume_usd):<10}"
            f"{_fmt_spread(c.iv_spread):<9}"
            f"{lc}{c.liquidity_tag or '—'}{R}"
            + (f"  {GY}max profit ${c.max_profit:.2f}{R}" if is_cal and c.max_profit else "")
        )
    print(f"\n  {GY}OI = open interest | Spread = bid/ask IV spread (tighter = more liquid){R}")
    print(f"  {GY}Premiums/debits estimated via Black-Scholes | Budget: ${BUDGET_USD:.0f}{R}")
    print(f"  {GY}Cal = calendar spread: Prem/Debit = net debit paid (= max loss){R}")

def _display_ranked(
    rank_label: str,
    candidates: list[Candidate],
    limit: int = 5,
) -> None:
    """Print a ranked recommendation block with medal positions."""
    medals = ["🥇", "🥈", "🥉", "  4.", "  5."]
    sub(rank_label)
    print(
        f"\n  {'':4}{'Asset':<6}{'Strat':<12}{'OTM%':<6}{'Strike(s)':<20}"
        f"{'Prem':<9}{'Yld/yr':<9}{'P(Prof)':<10}{'IV':<7}{'OI':<8}{'Vol24h':<10}{'Liq'}"
    )
    print(f"  {'─' * 105}")
 
    for i, c in enumerate(candidates[:limit]):
        medal  = medals[i] if i < len(medals) else f"  {i+1}."
        colour = GR if i == 0 else YL if i == 1 else WH
        lc     = _liq_colour(c.liquidity_tag)
        print(
            f"  {medal} {colour}{c.asset:<6}{c.strategy:<12}"
            f"{c.otm_pct*100:.0f}%{'':3}{c.strike:<20}"
            f"${c.premium:<7.2f}{c.yield_ann:>6.1f}%/yr  "
            f"{c.prob_profit:>5.1f}%     {c.iv*100:.0f}%{'':4}"
            f"{_fmt_oi(c.open_interest):<8}"
            f"{_fmt_vol(c.volume_usd):<10}"
            f"{lc}{c.liquidity_tag or '—'}{R}"
        )


# ── Public API ────────────────────────────────────────────────────────────────
def set_min_yield(value: float) -> None:
    """Update the minimum yield filter for the current session."""
    global MIN_YIELD_PCT
    MIN_YIELD_PCT = value

def _report_progress(asset: str, strategy: str, specifics: str, strike: str) -> None:
    """Display progress for a candidate being evaluated."""
    print(f"    {CY}{asset:3s}  {strategy:10s}  {specifics:12s}  {strike}{R}")

def run_scanner(
    active_spot:  float,
    active_iv:    float,
    active_asset: str,
    days:         int,
    *,
    cal_near_days: int | None = None,
    cal_far_days:  int | None = None,
    broker:        BrokerBase | None = None,
) -> list[Candidate]:
    """
    Scan every (asset, strategy, OTM level) combination and display
    ranked trade recommendations including live liquidity data.

    After ranking, the user may optionally enter one of the top candidates
    directly via the executor (using the supplied broker).

    Parameters
    ----------
    active_spot  : float        Current spot for the active asset
    active_iv    : float        Current IV for the active asset
    active_asset : str          Currently selected asset
    days         : int          Days to expiry
    broker       : BrokerBase   Adapter used when entering a selected trade.
                                Defaults to DeribitClient(paper=DERIBIT_PAPER).

    Returns
    -------
    list[Candidate]  All candidates generated (empty list if none).
    """
    if broker is None:
        broker = DeribitClient(paper=DERIBIT_PAPER)

    hdr("Trade Recommendation Scanner")
    print(f"  {GY}Fetching live data and liquidity for all candidates...{R}")
    print(f"  {GY}(This may take a few seconds — fetching order books per strike){R}\n")

    all_candidates: list[Candidate] = []

    for asset in TRADEABLE_ASSETS:
        if asset == active_asset:
            spot = active_spot
            iv   = active_iv
        else:
            spot = get_spot_price(asset)
            if not spot:
                warn(f"Could not fetch {asset} price — skipping")
                continue
            iv = get_deribit_iv(asset, spot, days)
            if not iv:
                warn(f"Could not fetch {asset} IV — using fallback")
                iv = active_iv

        expected_count = len(OTM_LEVELS)*5 + len(CALENDAR_SPREADS)*2
        ok(
            f"{asset}: spot=${spot:,.2f}  IV={iv*100:.0f}%  "
            f"— evaluating {expected_count} candidates"
        )
        all_candidates.extend(
            _build_candidates(
                asset, spot, iv, days,
                cal_near_days=cal_near_days,
                cal_far_days=cal_far_days,
                progress_callback=_report_progress,
            )
        )
        print()

    if not all_candidates:
        warn("No candidates generated — check your connection.")
        return []

    # ── Persist to SQLite ─────────────────────────────────────────────────────
    try:
        save_scan_results(all_candidates)
    except Exception as exc:  # pragma: no cover
        warn(f"Could not save scan results to database: {exc}")

    # ── Full candidate table ──────────────────────────────────────────────────
    print()
    hdr("All Candidates")
    _display_candidates(all_candidates, days)

    # ── Ranking 1: Highest probability with reasonable return ─────────────────
    print()
    hdr("Ranked Recommendations")
    liquid    = [c for c in all_candidates if c.liquidity_tag]
    qualified = [c for c in liquid if c.yield_ann >= MIN_YIELD_PCT]
    by_prob   = sorted(qualified, key=lambda c: c.prob_profit, reverse=True)
    _display_ranked(
        rank_label = f"① Highest Probability  {GY}(yield ≥ {MIN_YIELD_PCT:.0f}%/yr){R}",
        candidates = by_prob,
    )

    # ── Ranking 2: Best annualised return ─────────────────────────────────────
    print()
    by_yield = sorted(liquid, key=lambda c: c.yield_ann, reverse=True)
    _display_ranked(
        rank_label = "② Best Return",
        candidates = by_yield,
    )

    print(f"""
  {GY}Strategy key:
  CSP      = Cash-Secured Put    (wheel leg 1)
  CC       = Covered Call        (wheel leg 2)
  Strangle = Short Strangle      (simultaneous OTM put + call)
  Cal-C    = Call Calendar Spread  (buy far call, sell near call — same strike)
  Cal-P    = Put  Calendar Spread  (buy far put,  sell near put  — same strike)
             For Cal: premium = net debit (= max loss)  |  max profit shown separately
  BPS      = Bull Put Spread     (sell OTM put, buy further OTM put — credit received)
  BCS      = Bear Call Spread    (sell OTM call, buy further OTM call — credit received)
             For BPS/BCS: premium = net credit  |  strike = short/long

  Liquidity key:
  High = OI ≥ 1,000 contracts and tight IV spread
  Med  = OI ≥ 100 contracts or 24h volume ≥ $50k
  Low  = thin market — wider spreads, harder to fill
  For multi-leg trades (Strangle, Cal-C, Cal-P, BPS, BCS) the liquidity tag is
  the WORST of the legs (lowest OI, lowest volume, widest spread) — a
  trade is only as fillable as its weakest leg.

  {YL}⚠ Strangles carry unlimited loss potential on the call side.
  {YL}⚠ Calendar spreads carry limited risk (net debit) but require two expiries.
  {YL}⚠ Credit spreads (BPS/BCS) cap both profit and loss — max loss = spread width minus credit.
  {YL}⚠ All figures are Black-Scholes estimates — not financial advice.{R}
""")
    warn(f"Min yield filter for ranking ①: {MIN_YIELD_PCT:.0f}%/yr  "
         f"— adjust MIN_YIELD_PCT in scanner.py to change this.")

    # ── Optional interactive trade entry ──────────────────────────────────────
    top = by_prob[:5] or by_yield[:5]
    if top:
        print(f"\n  {CY}Enter a trade from the top recommendations?{R}")
        print(f"  Type 1–{len(top)} to enter that candidate, or press Enter to skip.")
        raw = input(f"  {YL}Choice: {R}").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(top):
                pick = top[idx]
                if _has_open_position(pick.asset, pick.strategy):
                    warn(
                        f"Cannot enter {pick.strategy} on {pick.asset} — "
                        f"position already open. Close it first."
                    )
                else:
                    try:
                        enter_trade(pick, broker=broker)
                        ok(
                            f"Entered {pick.strategy} on {pick.asset}  "
                            f"strike {pick.strike}  prem ${pick.premium:.2f}  "
                            f"via {broker.broker_name}"
                        )
                    except Exception as exc:
                        warn(f"Trade entry failed: {exc}")
            else:
                warn(f"Invalid choice — enter a number between 1 and {len(top)}.")

    return all_candidates