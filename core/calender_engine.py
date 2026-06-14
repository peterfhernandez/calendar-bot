"""
strategies/calendar.py
======================
Calendar Spread paper trading simulator.

A calendar spread (time spread / horizontal spread) buys a longer-dated option
and sells a shorter-dated option at the same strike.  The maximum loss is the
net debit paid at entry; the maximum profit is achieved when the underlying is
near the strike at near-leg expiry (short leg expires worthless, long leg
retains maximum remaining time value).

Public API
----------
show_calendar_analysis(asset, spot, iv, days)
    Display a table of strike options with net debit, max profit, profit range,
    and estimated probability of profit.  Offers to draw an ASCII P&L chart.

calendar_paper_menu(asset, spot, iv, days)
    Interactive paper trading simulator for the calendar spread strategy.

Internal helpers
----------------
_spread_value(spot, ...)            Current mark value of the calendar spread
_pnl_at_near_expiry(spot_close, ...) P&L given a closing price at near expiry
_find_breakevens(...)               Numerically locate lower/upper breakeven prices
check_calendar_status(...)          Evaluate stop / take-profit / warn conditions
"""

from datetime import date
from types import SimpleNamespace

from config import (
    BUDGET_USD, RISK_FREE_RATE, OTM_LEVELS,
    CALENDAR_NEAR_DAYS, CALENDAR_FAR_DAYS, CALENDAR_STOP_PCT, DERIBIT_PAPER,
)
from database.calendar_db import (
    load_calendar_state,
    save_calendar_state,
    close_calendar_trade,
)
from access import DeribitClient
from market.pricing import bs_put, bs_call, prob_otm_put, prob_otm_call, round_strike, adjust_far_leg_price
from trading.executor import enter_trade, close_calendar_far_leg, roll_near_leg
from trading.fee_calculator import calculate_fee
from trading.expiry_handler import is_expired_worthless, handle_expires_worthless, handle_expires_itm
from strategies.calendar_analysis import calculate_roll_options, _days_remaining
from ui.display import (
    hdr, sub, inf, ok, warn,
    draw_calendar_zone,
    GR, RD, CY, YL, GY, WH, R,
)


# ── P&L helpers ───────────────────────────────────────────────────────────────

def _spread_value(
    spot: float,
    strike: float,
    T_near: float,
    T_far: float,
    r: float,
    iv: float,
    qty: float,
    option_type: str,
) -> float:
    """
    Current mark-to-market value of an open calendar spread.

    Returns (far_leg_value - near_leg_value) in USD.
    Positive when the far leg is worth more than the short near leg (normal).
    """
    if option_type == "Call":
        far_val  = bs_call(spot, strike, T_far,  r, iv) * qty
        near_val = bs_call(spot, strike, T_near, r, iv) * qty
    else:
        far_val  = bs_put(spot, strike, T_far,  r, iv) * qty
        near_val = bs_put(spot, strike, T_near, r, iv) * qty
    return far_val - near_val


def _pnl_at_near_expiry(
    spot_close: float,
    strike: float,
    near_days: int,
    far_days: int,
    r: float,
    iv: float,
    qty: float,
    net_debit: float,
    option_type: str,
) -> float:
    """
    Estimated P&L at near-leg expiry for a given closing spot price.

    Near leg is settled at intrinsic value (if ITM) or zero (if OTM) using shared expiry handler.
    Far leg is sold at Black-Scholes with the remaining time (far - near days).
    """
    T_remaining = max(far_days - near_days, 1) / 365.0

    # Use shared expiry handler for near leg settlement
    try:
        if is_expired_worthless(option_type, spot_close, strike):
            # Near leg expires worthless - we keep received premium, costs nothing
            near_cost = 0.0
        else:
            # Near leg expires ITM - use handler to get intrinsic value we owe
            near_result = handle_expires_itm(
                position_type=option_type,
                spot=spot_close,
                strike=strike,
                is_short=True,
                premium=0.0,  # We just need the intrinsic, not premium part
                qty=qty,
            )
            near_cost = near_result["total_intrinsic"]
    except Exception:
        # Fallback to original calculation
        if option_type == "Call":
            near_cost = max(spot_close - strike, 0) * qty
        else:
            near_cost = max(strike - spot_close, 0) * qty

    # Value far leg with remaining time
    if option_type == "Call":
        far_val = bs_call(spot_close, strike, T_remaining, r, iv) * qty
    else:
        far_val = bs_put(spot_close, strike, T_remaining, r, iv) * qty

    return far_val - near_cost - net_debit


def _find_breakevens(
    spot: float,
    strike: float,
    near_days: int,
    far_days: int,
    r: float,
    iv: float,
    qty: float,
    net_debit: float,
    option_type: str,
    n_steps: int = 800,
) -> tuple[float, float]:
    """
    Numerically locate lower and upper breakeven prices at near-leg expiry.

    Scans spot * [0.50 … 1.50] for sign changes in P&L.
    Returns (be_lo, be_hi); both are 0.0 if no crossings are found.
    """
    lo = spot * 0.50
    hi = spot * 1.50
    step = (hi - lo) / n_steps
    prices = [lo + i * step for i in range(n_steps + 1)]
    pnls = [
        _pnl_at_near_expiry(p, strike, near_days, far_days, r, iv, qty, net_debit, option_type)
        for p in prices
    ]

    be_lo = be_hi = 0.0
    for i in range(len(pnls) - 1):
        if pnls[i] < 0 <= pnls[i + 1]:
            be_lo = prices[i]
        if pnls[i] >= 0 > pnls[i + 1]:
            be_hi = prices[i + 1]
    return be_lo, be_hi


# ── Status checker ────────────────────────────────────────────────────────────

def check_calendar_status(
    spot: float,
    iv: float,
    near_days_left: int,
    far_days_left: int,
    op: dict,
) -> tuple[str, float, float, str]:
    """
    Evaluate the current status of an open calendar spread position.

    Parameters
    ----------
    spot           : float  Current underlying price
    iv             : float  Current implied volatility (decimal)
    near_days_left : int    Days remaining until near-leg expiry
    far_days_left  : int    Days remaining until far-leg expiry
    op             : dict   Open position dict from state

    Returns
    -------
    tuple of (status, spread_val, pct_of_debit, message)
        status        : "ok" | "warn" | "stop" | "tp"
        spread_val    : float  Current spread mark value (USD)
        pct_of_debit  : float  spread_val / net_debit paid at entry
        message       : str    Human-readable status description
    """
    T_near = max(near_days_left / 365.0, 1 / 365.0)
    T_far  = max(far_days_left  / 365.0, 1 / 365.0)
    net_debit   = op["net_debit"]
    qty         = op["qty"]
    strike      = op["strike"]
    option_type = op["option_type"]

    sv  = _spread_value(spot, strike, T_near, T_far, RISK_FREE_RATE, iv, qty, option_type)
    pct = sv / net_debit if net_debit > 0 else 0.0
    pnl = sv - net_debit

    if pct <= CALENDAR_STOP_PCT:
        msg = (
            f"STOP  spread worth ${sv:.2f} ({pct*100:.0f}% of debit paid).  "
            f"Est. loss: ${abs(pnl):.2f}"
        )
        return "stop", sv, pct, msg

    if pct >= 1.50:
        msg = (
            f"TAKE-PROFIT  spread worth ${sv:.2f} ({pct*100:.0f}% of debit paid).  "
            f"Est. gain: ${pnl:.2f}"
        )
        return "tp", sv, pct, msg

    if pct <= 0.70:
        msg = (
            f"WARN  spread worth ${sv:.2f} ({pct*100:.0f}% of debit).  "
            f"Hard stop at {CALENDAR_STOP_PCT*100:.0f}%."
        )
        return "warn", sv, pct, msg

    msg = f"OK  {pct*100:.0f}% of debit  (stop at {CALENDAR_STOP_PCT*100:.0f}%)"
    return "ok", sv, pct, msg


# ── Strike analysis ───────────────────────────────────────────────────────────

def show_calendar_analysis(
    asset: str,
    spot: float,
    iv: float,
    days: int                      = None,
    *,
    near_days: int | None          = None,
    far_days:  int | None          = None,
) -> None:
    """
    Display a table of calendar spread options with net debit, max profit,
    profit zone, and estimated probability of profit.

    The calendar has its own near/far horizons (independent of the global
    wheel/strangle expiry). Defaults come from
    ``config.CALENDAR_NEAR_DAYS`` (7) and ``config.CALENDAR_FAR_DAYS`` (30)
    and can be overridden per-call or via main-menu toggles [4] and [5].

    Parameters
    ----------
    asset     : str   Underlying asset symbol (e.g. "ETH")
    spot      : float Current spot price
    iv        : float Implied volatility (decimal)
    days      : int   Backwards-compatible alias for ``near_days``
    near_days : int   Short-leg horizon (overrides ``days`` if given)
    far_days  : int   Long-leg  horizon (defaults to CALENDAR_FAR_DAYS)
    """
    near_days = near_days if near_days is not None else (days if days is not None else CALENDAR_NEAR_DAYS)
    far_days  = far_days  if far_days  is not None else CALENDAR_FAR_DAYS
    days      = near_days  # internal name used throughout the function

    if days >= far_days:
        warn(
            f"Near expiry ({days}d) must be shorter than far expiry ({far_days}d). "
            f"Adjust horizons via main-menu options [4] / [5]."
        )
        return

    T_near = days     / 365.0
    T_far  = far_days / 365.0
    T_rem  = max(far_days - days, 1) / 365.0
    r      = RISK_FREE_RATE
    qty    = BUDGET_USD / spot

    hdr(f"Calendar Spread Analysis — {asset}  {days}d / {far_days}d expiry")
    inf(f"{asset} Spot",  f"${spot:,.2f}")
    inf("IV",             f"{iv * 100:.1f}%")
    inf("Near expiry",    f"{days} days   (short leg — sell this)")
    inf("Far expiry",     f"{far_days} days  (long leg — buy this)")
    inf("Budget",         f"${BUDGET_USD:.0f}   (net debit = max loss)")
    inf("Strategy edge",  "Theta decay differential + IV term-structure")

    best_call = best_put = None

    for option_type in ("Call", "Put"):
        sub(
            f"{option_type} Calendar Spreads  "
            f"{'(neutral-to-bullish)' if option_type == 'Call' else '(neutral-to-bearish)'}"
        )
        print(
            f"\n  {'OTM%':<7}{'Strike':<12}{'Near Prem':<12}{'Far Prem':<12}"
            f"{'Net Debit':<12}{'Max Profit':<13}{'Profit Range':<28}{'P(Profit)'}"
        )
        print(f"  {'─' * 108}")

        for otm in [0.00] + OTM_LEVELS:
            if option_type == "Call":
                K = round_strike(spot * (1 + otm), spot)
            else:
                K = round_strike(spot * (1 - otm), spot)

            if option_type == "Call":
                near_prem = bs_call(spot, K, T_near, r, iv) * qty
                far_prem_mid = bs_call(spot, K, T_far,  r, iv) * qty
                far_prem = adjust_far_leg_price(far_prem_mid / qty, far_days, is_buy=True) * qty
                max_far   = bs_call(K,    K, T_rem,  r, iv) * qty   # spot = K at near expiry
            else:
                near_prem = bs_put(spot, K, T_near, r, iv) * qty
                far_prem_mid = bs_put(spot, K, T_far,  r, iv) * qty
                far_prem = adjust_far_leg_price(far_prem_mid / qty, far_days, is_buy=True) * qty
                max_far   = bs_put(K,    K, T_rem,  r, iv) * qty

            # Account for fees on both legs (near and far)
            open_fee_near = calculate_fee(spot, near_prem / qty, asset) * qty
            open_fee_far = calculate_fee(spot, far_prem / qty, asset) * qty
            close_fee_est_near = calculate_fee(spot, 0.01, asset) * qty
            close_fee_est_far = calculate_fee(spot, 0.01, asset) * qty
            # For calendar: we receive near_prem (pay fee on it), pay far_prem (pay fee on it)
            net_debit  = (far_prem + open_fee_far + close_fee_est_far) - (near_prem - open_fee_near - close_fee_est_near)
            max_profit = max_far - net_debit

            be_lo, be_hi = _find_breakevens(spot, K, days, far_days, r, iv, qty, net_debit, option_type)

            if be_lo > 0 and be_hi > 0:
                profit_range = f"${be_lo:,.0f} – ${be_hi:,.0f}"
                p_lo = prob_otm_put (spot, be_lo, T_near, r, iv)
                p_hi = prob_otm_call(spot, be_hi, T_near, r, iv)
                pop  = max(0.0, (p_lo + p_hi - 1) * 100)
            else:
                profit_range = "—"
                pop = 0.0

            highlight = (otm == 0.00)
            c = GR if highlight else WH
            otm_str = "ATM" if otm == 0.00 else f"{otm*100:.0f}%"
            print(
                f"  {c}{otm_str:<7}${K:>8,.0f}   "
                f"${near_prem:>8.2f}   ${far_prem:>8.2f}   "
                f"${net_debit:>8.2f}   "
                f"${max_profit:>8.2f}   "
                f"{profit_range:<28}{pop:.0f}%{R}"
            )

            if otm == 0.00:
                if option_type == "Call":
                    best_call = (K, near_prem, far_prem, net_debit, max_profit, qty)
                else:
                    best_put  = (K, near_prem, far_prem, net_debit, max_profit, qty)

    print(f"\n  {GY}* Premiums via Black-Scholes  |  qty ≈ ${BUDGET_USD:.0f}/spot  |  Max loss = net debit{R}")
    print(f"  {GY}* Profit zone computed at near-leg expiry assuming unchanged IV{R}")
    inf("Best case",  "Spot pins the strike at near expiry — near expires worthless")
    inf("Worst case", "Spot moves far from strike — both legs have similar value")

    # ── Offer to show chart ───────────────────────────────────────────────────
    print()
    resp = input(
        f"  {YL}Show P&L chart?  [c]all / [p]ut / [n]o: {R}"
    ).strip().lower()
    if resp == "c" and best_call:
        K, np_, fp_, nd, mp, q2 = best_call
        draw_calendar_zone(spot, K, nd, q2, days, far_days, iv, "Call")
    elif resp == "p" and best_put:
        K, np_, fp_, nd, mp, q2 = best_put
        draw_calendar_zone(spot, K, nd, q2, days, far_days, iv, "Put")


# ── Paper trading simulator ───────────────────────────────────────────────────

def calendar_paper_menu(
    asset: str,
    spot: float,
    iv: float,
    days: int                      = None,
    *,
    near_days: int | None          = None,
    far_days:  int | None          = None,
) -> None:
    """
    Interactive paper trading simulator for the calendar spread strategy.

    Manages state across sessions via the SQLite database.
    Supports opening, monitoring, closing at near expiry, and early-closing.

    Calendar horizons default to ``config.CALENDAR_NEAR_DAYS`` (7d short
    leg) and ``config.CALENDAR_FAR_DAYS`` (30d long leg). The main menu
    [4] / [5] toggles cycle through ``CALENDAR_NEAR_OPTIONS`` and
    ``CALENDAR_FAR_OPTIONS`` and pass the selected values in here.

    Parameters
    ----------
    asset     : str              Underlying asset symbol
    spot      : float            Current spot price
    iv        : float            Implied volatility (decimal)
    days      : int              Backwards-compatible alias for ``near_days``
    near_days : int              Short-leg horizon (overrides ``days``)
    far_days  : int              Long-leg horizon
    """
    near_days = near_days if near_days is not None else (days if days is not None else CALENDAR_NEAR_DAYS)
    far_days  = far_days  if far_days  is not None else CALENDAR_FAR_DAYS
    days      = near_days

    if days >= far_days:
        warn(
            f"Near expiry ({days}d) ≥ far expiry ({far_days}d).  "
            f"Adjust horizons via main-menu options [4] / [5]."
        )
        return

    T_near = days     / 365.0
    T_far  = far_days / 365.0
    r      = RISK_FREE_RATE
    qty    = BUDGET_USD / spot
    s      = load_calendar_state(asset)

    hdr(f"Calendar Spread — {asset} Paper Trading  ({days}d / {far_days}d)")

    total = s["wins"] + s["losses"]
    inf("Trades completed", str(total))
    inf("Wins / Losses",    f"{s['wins']} / {s['losses']}")
    inf("Win Rate",         f"{s['wins'] / total * 100:.1f}%" if total else "N/A")
    inf("Total P&L",        f"${s['total_pnl']:.2f}")

    # ── Show open position details ────────────────────────────────────────────
    if s["open"]:
        op          = s["open"]
        K           = op["strike"]
        opt_type    = op["option_type"]
        net_debit   = op["net_debit"]
        q           = op["qty"]
        near_exp    = op["expiry_near"]
        far_exp     = op["expiry_far"]

        from automation.monitor import _days_remaining
        near_left = _days_remaining(near_exp)
        far_left  = _days_remaining(far_exp)

        T_n = max(near_left / 365.0, 1 / 365.0)
        T_f = max(far_left  / 365.0, 1 / 365.0)

        sv   = _spread_value(spot, K, T_n, T_f, r, iv, q, opt_type)
        pnl  = sv - net_debit
        col  = GR if pnl >= 0 else RD

        status, sv2, pct, st_msg = check_calendar_status(spot, iv, near_left, far_left, op)

        sub(f"Open {opt_type} Calendar — Strike ${K:,.0f}")
        inf("  Near expiry",    f"{near_exp}  ({near_left}d left)")
        inf("  Far expiry",     f"{far_exp}  ({far_left}d left)")
        inf("  Net Debit Paid", f"${net_debit:.2f}  (max loss)")
        inf("  Spread Value Now", f"${sv:.2f}  ({pct*100:.0f}% of debit)")
        inf("  Unrealised P&L", f"{col}${pnl:.2f}{R}")

        # Status bar
        bar_w   = 36
        fill    = min(pct / 2.0, 1.0)   # scale 0 → 200% of debit
        filled  = int(fill * bar_w)
        tp_pos  = int((1.50 / 2.0) * bar_w)
        stop_pos = int((CALENDAR_STOP_PCT / 2.0) * bar_w)
        bar = ""
        for i in range(bar_w):
            if i < filled:
                if i >= tp_pos:    bar += f"{GR}█{R}"
                elif i >= bar_w // 2: bar += f"{YL}█{R}"
                else:              bar += f"{RD}█{R}"
            elif i == tp_pos:      bar += f"{GR}|{R}"
            elif i == stop_pos:    bar += f"{RD}|{R}"
            else:                  bar += f"{GY}░{R}"

        print(f"\n  {WH}Spread Value Monitor{R}  {GY}[stop={CALENDAR_STOP_PCT*100:.0f}%  tp=150%]{R}")
        print(f"  {bar}  {WH}{pct*100:.0f}% of debit{R}")

        if status == "stop":
            print(f"\n  {RD}⛔  {st_msg}{R}")
            print(f"  {RD}Consider closing — you have lost ≥50% of the debit paid.{R}")
        elif status == "tp":
            print(f"\n  {GR}🎯  {st_msg}{R}")
            print(f"  {GR}Consider closing to lock in this profit.{R}")
        elif status == "warn":
            print(f"\n  {YL}⚠   {st_msg}{R}")
        else:
            print(f"  {GY}✓   {st_msg}{R}")

    # ── Menu ──────────────────────────────────────────────────────────────────
    print(f"""
  {CY}[1]{R}  Open new calendar spread
  {CY}[2]{R}  Show P&L chart  {GY}(open position){R}
  {CY}[3]{R}  Close at near-leg expiry
  {CY}[4]{R}  Close NOW at current price  {RD}(early exit / stop-loss){R}
  {CY}[5]{R}  Back
""")
    choice = input(f"  {YL}Choice: {R}").strip()

    # [1] Open new calendar spread
    if choice == "1":
        if s["open"]:
            warn("Close the existing position before opening a new one.")
            return

        sub("Suggested ATM calendar spread")
        K_sug = round_strike(spot, spot)

        print(f"""
  {GY}Option type:{R}
  {CY}[1]{R}  Call  {GY}(neutral to bullish — profits if spot stays near or rises slightly){R}
  {CY}[2]{R}  Put   {GY}(neutral to bearish — profits if spot stays near or falls slightly){R}
""")
        ot_choice = input(f"  {YL}Type [1=Call / 2=Put, Enter for Call]: {R}").strip()
        option_type = "Put" if ot_choice == "2" else "Call"

        if option_type == "Call":
            near_prem_sug = bs_call(spot, K_sug, T_near, r, iv) * qty
            far_prem_mid = bs_call(spot, K_sug, T_far,  r, iv) * qty
            far_prem_sug = adjust_far_leg_price(far_prem_mid / qty, far_days, is_buy=True) * qty
        else:
            near_prem_sug = bs_put(spot, K_sug, T_near, r, iv) * qty
            far_prem_mid = bs_put(spot, K_sug, T_far,  r, iv) * qty
            far_prem_sug = adjust_far_leg_price(far_prem_mid / qty, far_days, is_buy=True) * qty

        nd_sug = far_prem_sug - near_prem_sug
        inf(f"  Suggested strike (ATM)", f"${K_sug:,.0f}")
        inf(f"  Near leg ({days}d) premium",  f"${near_prem_sug:.2f}  ← you receive this")
        inf(f"  Far leg  ({far_days}d) premium", f"${far_prem_sug:.2f}  ← you pay this")
        inf(f"  Net debit (max loss)",     f"${nd_sug:.2f}")

        K = float(input(f"\n  Strike [Enter for ${K_sug:,.0f}]: $") or K_sug)

        if option_type == "Call":
            near_prem = bs_call(spot, K, T_near, r, iv) * qty
            far_prem_mid = bs_call(spot, K, T_far,  r, iv) * qty
            far_prem = adjust_far_leg_price(far_prem_mid / qty, far_days, is_buy=True) * qty
        else:
            near_prem = bs_put(spot, K, T_near, r, iv) * qty
            far_prem_mid = bs_put(spot, K, T_far,  r, iv) * qty
            far_prem = adjust_far_leg_price(far_prem_mid / qty, far_days, is_buy=True) * qty

        net_debit = far_prem - near_prem

        strategy = "Cal-C" if option_type == "Call" else "Cal-P"
        c = SimpleNamespace(
            strategy=strategy,
            asset=asset,
            spot=spot,
            iv=iv,
            days=days,
            strike=str(K),
            far_days=far_days,
            prob_profit=0,
            yield_ann=0,
        )

        # Validate that the asset has options available on the broker
        broker = DeribitClient(paper=DERIBIT_PAPER)
        if not broker.asset_has_options(asset):
            warn(f"No {asset} options available on Deribit. Try BTC or ETH instead.")
            return

        enter_trade(c)
        s  = load_calendar_state(asset)
        op = s["open"]

        ok(
            f"Calendar opened: {option_type} ${op['strike']:,.4g}  "
            f"{days}d/{far_days}d  |  Net debit: ${op['net_debit']:.2f}"
        )
        draw_calendar_zone(spot, op["strike"], op["net_debit"], op["qty"], days, far_days, iv, option_type)

    # [2] Show P&L chart
    elif choice == "2":
        if not s["open"]:
            warn("No open position.")
            return
        op = s["open"]
        draw_calendar_zone(
            spot, op["strike"], op["net_debit"], op["qty"],
            op["near_days"], op["far_days"], iv, op["option_type"],
        )

    # [3] Close at near-leg expiry
    elif choice == "3":
        if not s["open"]:
            warn("No open position.")
            return
        op          = s["open"]
        K           = op["strike"]
        opt_type    = op["option_type"]
        net_debit   = op["net_debit"]
        q           = op["qty"]

        spot_close = float(
            input(f"  {asset} price at near-leg expiry [~${spot:,.0f}]: $") or spot
        )
        pnl = _pnl_at_near_expiry(
            spot_close, K, op["near_days"], op["far_days"],
            r, iv, q, net_debit, opt_type,
        )

        if pnl >= 0:
            result = "Win";  s["wins"]   += 1
            ok(f"Profitable!  P&L: +${pnl:.2f}")
        else:
            result = "Loss"; s["losses"] += 1
            warn(f"Loss.  P&L: ${pnl:.2f}  "
                 f"({'spot moved away from strike' if abs(spot_close - K) > K * 0.10 else 'narrow miss'})")

        s["total_pnl"] += pnl

        trade_id = op.get("trade_id")
        if trade_id:
            close_calendar_trade(
                trade_id=trade_id,
                date_close=date.today(),
                spot_close=spot_close,
                pnl=round(pnl, 4),
                result=result,
                notes=f"{asset} closed at near expiry, spot ${spot_close:,.0f}",
            )

        s["open"] = None
        save_calendar_state(asset, s)

    # [4] Early close at current mark
    elif choice == "4":
        if not s["open"]:
            warn("No open position.")
            return
        op          = s["open"]
        K           = op["strike"]
        opt_type    = op["option_type"]
        net_debit   = op["net_debit"]
        q           = op["qty"]

        from automation.monitor import _days_remaining
        near_left = _days_remaining(op["expiry_near"])
        far_left  = _days_remaining(op["expiry_far"])

        T_n = max(near_left / 365.0, 1 / 365.0)
        T_f = max(far_left  / 365.0, 1 / 365.0)
        sv  = _spread_value(spot, K, T_n, T_f, r, iv, q, opt_type)
        pnl = sv - net_debit
        pct = sv / net_debit if net_debit > 0 else 0.0

        status, _, _, st_msg = check_calendar_status(spot, iv, near_left, far_left, op)

        sub("Early Close — Mark-to-Market")
        inf("  Net debit paid",          f"${net_debit:.2f}")
        inf("  Spread value now",        f"${sv:.2f}  ({pct*100:.0f}% of debit)")
        col = GR if pnl >= 0 else RD
        inf("  Net P&L if closed now",   f"{col}${pnl:.2f}{R}")

        if status == "stop":
            print(f"\n  {RD}⛔  Stop-loss — {st_msg}{R}")
        elif status == "tp":
            print(f"\n  {GR}🎯  Take-profit — {st_msg}{R}")
        elif status == "warn":
            print(f"\n  {YL}⚠   {st_msg}{R}")
        else:
            print(f"\n  {GY}Position within normal range — early close is optional.{R}")

        confirm = input(f"\n  {YL}Confirm close at current price? (y/n): {R}").strip().lower()
        if confirm != "y":
            print("  Cancelled.")
            return

        if pnl >= 0:
            result = "Win";  s["wins"]   += 1
        else:
            tag = "Stop" if status == "stop" else "Early"
            result = f"Loss ({tag})";  s["losses"] += 1

        s["total_pnl"] += pnl
        note = (
            f"Stop at {pct*100:.0f}% of debit — loss ${abs(pnl):.2f}"
            if status == "stop"
            else f"Early close at {pct*100:.0f}% of debit — P&L ${pnl:.2f}"
        )
        ok(f"Closed.  P&L: {'+'if pnl>=0 else ''}${pnl:.2f}")

        trade_id = op.get("trade_id")
        if trade_id:
            close_calendar_trade(
                trade_id=trade_id,
                date_close=date.today(),
                spot_close=spot,
                pnl=round(pnl, 4),
                result=result,
                notes=f"{asset} {note}",
            )

        s["open"] = None
        save_calendar_state(asset, s)


def handle_far_leg_only_menu(
    asset: str,
    spot: float,
    iv: float,
    broker: DeribitClient | None = None,
) -> None:
    """
    Interactive menu for "Far Leg Only" positions (after near leg expiration).

    Presents options to:
    1. Close the far leg at current market price
    2. Keep the position open for continued monitoring
    3. (Future) Roll to a new near leg

    Parameters
    ----------
    asset   : str             Underlying asset symbol (ETH, BTC, etc.)
    spot    : float           Current spot price
    iv      : float           Implied volatility (decimal)
    broker  : DeribitClient   Broker for executing closes (optional for paper trading)
    """
    from strategies.calendar_analysis import analyze_calendar_far_leg, display_calendar_analysis

    s = load_calendar_state(asset)
    if not s.get("open"):
        warn("No open position.")
        return

    op = s["open"]
    status = op.get("status", "Open")
    if status != "Far Leg Only":
        warn(f"Position is {status}, not Far Leg Only. Use regular close option.")
        return

    K = op["strike"]
    opt_type = op["option_type"]
    net_debit = op["net_debit"]
    qty = op["qty"]
    expiry_far = op.get("expiry_far", "")
    trade_id = op.get("trade_id")

    hdr(f"Far Leg Only — {asset} {opt_type} ${K:,.0f}")

    # Analyze and display far leg
    analysis = analyze_calendar_far_leg(
        asset=asset,
        strike=K,
        option_type=opt_type,
        expiry_far=expiry_far,
        qty=qty,
        net_debit=net_debit,
        paper=DERIBIT_PAPER,
    )

    if analysis:
        display_calendar_analysis(analysis, asset, K, opt_type)
    else:
        warn("Could not fetch far-leg analysis from Deribit. Manual review needed.")

    # ── Menu ──────────────────────────────────────────────────────────────────
    print(f"""
  {CY}[1]{R}  Close far leg at current market price
  {CY}[2]{R}  Keep position open
  {CY}[3]{R}  Roll to a new near leg
  {CY}[4]{R}  Back
""")
    choice = input(f"  {YL}Choice: {R}").strip()

    # [1] Close far leg
    if choice == "1":
        if broker is None:
            broker = DeribitClient(paper=DERIBIT_PAPER)

        try:
            far_order = close_calendar_far_leg(op, broker, spot)
            ok(f"Far leg closed: {far_order.order_id}")

            # Record the close in the database
            if trade_id:
                pnl_estimate = (op["far_instrument"] and analysis.current_pnl) or 0.0
                close_calendar_trade(
                    trade_id=trade_id,
                    date_close=date.today(),
                    spot_close=spot,
                    pnl=round(pnl_estimate, 4) if analysis else 0.0,
                    result="Closed",
                    notes=f"Far leg closed at ${spot:,.0f}. Position fully closed (both legs).",
                )

            s["open"] = None
            save_calendar_state(asset, s)
        except Exception as e:
            warn(f"Failed to close far leg: {e}")

    # [2] Keep position open
    elif choice == "2":
        ok("Position kept open for continued monitoring.")

    # [3] Roll to a new near leg
    elif choice == "3":
        # Calculate roll options with PoP and expected profit
        current_far_pnl = (analysis.current_pnl if analysis else 0.0)
        expiry_far_days = _days_remaining(op.get("expiry_far", ""))

        roll_options = calculate_roll_options(
            strike=K,
            option_type=opt_type,
            spot=spot,
            iv=iv,
            qty=qty,
            current_far_pnl=current_far_pnl,
            expiry_far_days=expiry_far_days,
        )

        if not roll_options:
            warn("No suitable roll options available (near leg would not expire before far leg).")
            return

        print(f"\n  {CY}Roll Options (New Near Legs){R}")
        for i, opt in enumerate(roll_options, 1):
            expiry_str = opt.expiry_date.strftime("%d-%b-%Y")
            pop_pct = opt.probability_profit * 100
            pnl_str = f"+${opt.expected_pnl:.2f}" if opt.expected_pnl >= 0 else f"-${abs(opt.expected_pnl):.2f}"
            pnl_color = GR if opt.expected_pnl >= 0 else RD
            print(f"    [{i}]  {opt.days}d near leg (expires {expiry_str})")
            print(f"         Premium: ${opt.estimated_premium:.2f}  |  PoP: {pop_pct:.0f}%  |  Expected P&L: {pnl_color}{pnl_str}{R}")
            print(f"         {opt.justification}")
        print(f"    [4]  Back")
        roll_choice = input(f"  {YL}Select expiry: {R}").strip()

        roll_idx = int(roll_choice) - 1 if roll_choice.isdigit() else -1
        if roll_idx < 0 or roll_idx >= len(roll_options):
            print("  Cancelled.")
            return

        selected_roll = roll_options[roll_idx]
        roll_days = selected_roll.days

        if broker is None:
            broker = DeribitClient(paper=DERIBIT_PAPER)

        try:
            near_order = roll_near_leg(op, roll_days, broker, spot, iv)
            ok(f"Near leg rolled ({roll_days}d): {near_order.order_id}")
            print(f"     Expected P&L at expiry: {pnl_color}{pnl_str}{R}")

            # State is updated by roll_near_leg
            s = load_calendar_state(asset)
            save_calendar_state(asset, s)
        except Exception as e:
            warn(f"Failed to roll near leg: {e}")

    # [4] Back
    elif choice == "4":
        print("  Cancelled.")
        return
