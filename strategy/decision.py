"""
strategy/decision.py
====================
Decision engine / state machine for the calendar spread bot.

Drives the full trade lifecycle autonomously:

    IDLE → SCAN → RANK → ENTER → MONITOR → {ROLL | CLOSE} → IDLE

The engine is designed to be called on a scheduler tick (not blocking). It
relies on an *executor* object that satisfies the ExecutorProtocol interface
for actual order placement. When no executor is provided a dry-run mode is
used that logs decisions without placing orders.

Public API
----------
DecisionEngine(cache, portfolio_value, executor=None, db_path=None, portfolio=None)
    Main engine class. Call scan_tick() on the scan interval and
    monitor_tick() on the monitor interval.

BotState
    Enum of all possible engine states.

DailyLossLimitError
    Raised (and caught internally) when the daily loss limit is breached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

import config
from alerts.notifier import Notifier
from core.calendar_engine import check_calendar_status
from core.fees import entry_fees, exit_fees, roll_fees as compute_roll_fees
from data.chain_cache import ChainCache
from db.state import (
    CalendarTrade,
    close_calendar_trade,
    create_calendar_trade,
    get_calendar_stats,
    list_assets_with_open_positions,
    load_calendar_state,
    mark_position_close_stuck,
    update_last_spread_value,
    update_near_leg,
    DB_PATH,
)
from portfolio.tracker import PortfolioTracker
from strategy.scanner import CalendarCandidate, scan
from strategy.sizer import size_candidate

logger = logging.getLogger(__name__)

# Days before near-leg expiry at which we consider rolling
_ROLL_TRIGGER_DAYS = 2


# ── Executor protocol ─────────────────────────────────────────────────────────

@runtime_checkable
class ExecutorProtocol(Protocol):
    """
    Interface the decision engine uses to place and close orders.

    Implement this for real Deribit execution (Phase 5). The default
    DryRunExecutor logs intent without placing orders.
    """

    def enter_spread(self, candidate: CalendarCandidate) -> dict | None:
        """
        Enter a calendar spread.

        Returns a fill dict on success::

            {
                "near_prem": float,   # near-leg fill price
                "far_prem":  float,   # far-leg fill price
                "net_debit": float,   # far_prem - near_prem
                "qty":       float,
            }

        Returns None if the order was rejected or timed out.
        """
        ...

    def close_spread(self, position: dict) -> float | None:
        """
        Close both legs of an open position.

        Returns the realised closing net credit (positive = profit over debit)
        or None on failure.
        """
        ...

    def roll_near_leg(self, position: dict, new_candidate: CalendarCandidate) -> bool:
        """
        Close the near leg and open a new near leg from *new_candidate*.

        Returns True on success.
        """
        ...


class DryRunExecutor:
    """No-op executor used when no real executor is wired up."""

    def enter_spread(self, candidate: CalendarCandidate) -> dict | None:
        logger.info(
            "[DRY-RUN] Would enter %s %s strike=%.0f  qty=%.1f  debit=%.4f",
            candidate.asset, candidate.option_type, candidate.strike,
            candidate.qty, candidate.net_debit,
        )
        return {
            "near_prem": candidate.near_bid,
            "far_prem":  candidate.far_ask,
            "net_debit": candidate.net_debit,
            "qty":       candidate.qty,
        }

    def close_spread(self, position: dict) -> float | None:
        logger.info(
            "[DRY-RUN] Would close trade_id=%s  asset=%s  strike=%.0f",
            position.get("trade_id"), position.get("asset"), position.get("strike", 0),
        )
        return position.get("net_debit", 0.0) * position.get("qty", 1.0)

    def roll_near_leg(self, position: dict, new_candidate: CalendarCandidate) -> bool:
        logger.info(
            "[DRY-RUN] Would roll near leg of trade_id=%s → %s",
            position.get("trade_id"), new_candidate.near_instrument,
        )
        return True


# ── State enum ────────────────────────────────────────────────────────────────

class BotState(Enum):
    IDLE    = "IDLE"
    SCAN    = "SCAN"
    RANK    = "RANK"
    ENTER   = "ENTER"
    MONITOR = "MONITOR"
    HALTED  = "HALTED"


# ── Internal exceptions ───────────────────────────────────────────────────────

class DailyLossLimitError(Exception):
    """Raised when today's realised loss exceeds DAILY_LOSS_LIMIT."""


# ── Engine ────────────────────────────────────────────────────────────────────

@dataclass
class EngineStatus:
    """Snapshot of engine state returned from tick methods."""
    state:          BotState
    open_positions: int
    daily_pnl:      float
    message:        str


class DecisionEngine:
    """
    Autonomous decision engine for calendar spread trading.

    Parameters
    ----------
    cache
        Populated ChainCache providing live option chain data.
    portfolio_value
        Initial portfolio value in USD.  When a PortfolioTracker is attached
        this is updated automatically on each scan_tick from the live account.
    executor
        Object implementing ExecutorProtocol. Defaults to DryRunExecutor.
    db_path
        Path to the SQLite database (defaults to db/calendar_bot.db).
    daily_loss_limit
        USD threshold for the daily halt (defaults to config.DAILY_LOSS_LIMIT).
    portfolio
        Optional PortfolioTracker.  When supplied, scan_tick() calls
        portfolio.refresh() before sizing so that available_cash reflects
        the live Deribit account balance.
    """

    def __init__(
        self,
        cache:            ChainCache,
        portfolio_value:  float,
        executor:         ExecutorProtocol | None = None,
        db_path:          Path | None = None,
        daily_loss_limit: float | None = None,
        portfolio:        PortfolioTracker | None = None,
        notifier:         Notifier | None = None,
    ) -> None:
        self._cache           = cache
        self._portfolio_value = portfolio_value
        self._executor        = executor or DryRunExecutor()
        self._db_path         = db_path or DB_PATH
        self._daily_loss_limit = (
            daily_loss_limit if daily_loss_limit is not None
            else config.DAILY_LOSS_LIMIT
        )
        self._portfolio       = portfolio
        self._notifier        = notifier
        self._state           = BotState.IDLE
        self._today_pnl: float = 0.0       # realised P&L; accumulates from closed positions
        self._session_pnl: float = 0.0     # realised P&L since bot start (never resets)
        self._unrealized_pnl: float = 0.0  # MTM P&L of currently-held positions; refreshed each monitor tick
        self._fees_paid_today: float = 0.0  # cumulative fees paid today (entry + exit + roll)
        self._just_entered: set[int] = set()    # trade IDs entered in the current scan tick; skipped by the immediately-following monitor tick
        self._rolled_this_tick: set[int] = set()  # trade IDs rolled in the current monitor tick; prevents double-roll within one pass
        self._close_roll_failures: dict[int, int] = {}  # trade_id → attempt count; prevents unbounded close/roll retry loops
        self._notified_stuck: set[int] = set()  # trade IDs already notified about being stuck; prevents spam
        self._paused: bool = False
        self._start_time: datetime = datetime.now(timezone.utc)

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def state(self) -> BotState:
        return self._state

    @property
    def portfolio_value(self) -> float:
        return self._portfolio_value

    @portfolio_value.setter
    def portfolio_value(self, value: float) -> None:
        self._portfolio_value = value

    @property
    def portfolio(self) -> PortfolioTracker | None:
        """The attached PortfolioTracker, or None if not configured."""
        return self._portfolio

    @property
    def fees_paid_today(self) -> float:
        """Cumulative fees paid today in USD (entry + exit + roll)."""
        return self._fees_paid_today

    @property
    def paused(self) -> bool:
        """True when scan and monitor ticks are suspended via pause()."""
        return self._paused

    @property
    def start_time(self) -> datetime:
        """UTC datetime when this engine instance was created."""
        return self._start_time

    @property
    def session_pnl(self) -> float:
        """Total realised P&L accumulated since this engine instance started."""
        return self._session_pnl

    def pause(self) -> None:
        """Suspend scan_tick and monitor_tick. Feed and portfolio tracker remain active."""
        self._paused = True
        logger.warning("DecisionEngine paused — scanning and monitoring suspended.")

    def resume(self) -> None:
        """Resume scan_tick and monitor_tick after a pause."""
        self._paused = False
        logger.info("DecisionEngine resumed — normal scanning and monitoring restarted.")

    # ── Public tick methods ───────────────────────────────────────────────────

    def scan_tick(self) -> EngineStatus:
        """
        Execute one scan cycle: IDLE → SCAN → RANK → ENTER → (back to IDLE).

        Safe to call on a timer even if the bot is already MONITOR-ing — it
        will still look for new entry opportunities while existing positions
        are open (up to MAX_POSITIONS).

        Returns
        -------
        EngineStatus
            Current engine snapshot after the tick.
        """
        if self._paused:
            return self._status("Bot is paused — use /start_bot to resume.")

        if self._state is BotState.HALTED:
            return self._status("Bot is halted — daily loss limit breached.")

        try:
            self._check_daily_loss_limit()
        except DailyLossLimitError as exc:
            return self._status(str(exc))

        # ── Portfolio refresh (before sizing) ─────────────────────────────────
        if self._portfolio is not None:
            try:
                state = self._portfolio.refresh()
                if state.available_cash > 0:
                    self._portfolio_value = (
                        config.PORTFOLIO_OVERRIDE
                        if config.PORTFOLIO_OVERRIDE is not None
                        else state.available_cash
                    )
                    logger.debug(
                        "Portfolio refreshed: available_cash=$%.2f equity=$%.2f",
                        state.available_cash, state.equity_usd,
                    )
                if state.available_cash == 0 and self._portfolio._client_id:
                    # Credentials set but cash is zero — log and skip entry
                    logger.info(
                        "SCAN skipped: available_cash=0 (portfolio refresh returned zero funds)"
                    )
                    self._state = BotState.IDLE
                    return self._status("Scan skipped: insufficient available cash.")
            except Exception as exc:
                logger.warning("Portfolio refresh error in scan_tick: %s — continuing with cached value", exc)

        open_positions = self._load_all_open_positions()

        # ── Drain mode: skip entry entirely (drain_and_new still allows entry) ──
        if config.DRAIN_MODE and not config.DRAIN_AND_NEW_MODE:
            self._state = BotState.IDLE if not open_positions else BotState.MONITOR
            logger.info("DRAIN MODE — scan_tick skipped (no new entries). Open: %d", len(open_positions))
            return self._status("Drain mode: no new entries.", open_positions)

        # ── SCAN ──────────────────────────────────────────────────────────────
        self._state = BotState.SCAN
        logger.info("SCAN tick started. Open positions: %d", len(open_positions))

        candidates = scan(self._cache)
        if not candidates:
            logger.info("SCAN: no candidates found — returning to IDLE")
            self._state = BotState.IDLE
            return self._status("Scan complete: no candidates.", open_positions)

        logger.info("SCAN: %d candidates found", len(candidates))

        # ── RANK + ENTER ──────────────────────────────────────────────────────
        self._state = BotState.RANK
        entered = 0

        for candidate in candidates:
            if candidate.ev_score < config.MIN_EV:
                logger.debug(
                    "RANK skip: EV %.4f below MIN_EV %.4f for %s %s strike=%.0f",
                    candidate.ev_score, config.MIN_EV, candidate.asset, candidate.option_type, candidate.strike,
                )
                continue

            size = size_candidate(candidate, self._portfolio_value, open_positions)
            if size.qty <= 0:
                logger.debug("RANK skip: %s", size.reason)
                continue

            candidate.qty = size.qty

            gate_reason = self._check_liquidity_gate(candidate)
            if gate_reason:
                logger.debug(
                    "LIQUIDITY GATE skip: %s for %s %s strike=%.0f",
                    gate_reason, candidate.asset, candidate.option_type, candidate.strike,
                )
                continue

            logger.info(
                "RANK approved: %s %s strike=%.0f  qty=%.1f  ev=%.4f",
                candidate.asset, candidate.option_type, candidate.strike,
                candidate.qty, candidate.ev_score,
            )

            self._state = BotState.ENTER
            trade = self._enter(candidate, open_positions)
            if trade is not None:
                open_positions.append(_trade_to_position(trade))
                self._just_entered.add(trade.id)
                entered += 1

            # Re-check limit after every entry
            try:
                self._check_daily_loss_limit()
            except DailyLossLimitError as exc:
                return self._status(str(exc), open_positions)

            # Stop scanning if max positions reached
            if len(open_positions) >= config.MAX_POSITIONS:
                break

        self._state = BotState.IDLE if not open_positions else BotState.MONITOR
        msg = f"Scan done. Entered {entered} trade(s). Open: {len(open_positions)}."
        return self._status(msg, open_positions)

    def monitor_tick(self) -> EngineStatus:
        """
        Execute one monitor cycle: check all open positions for stop/TP/roll.

        Returns
        -------
        EngineStatus
            Current engine snapshot after the tick.
        """
        if self._paused:
            return self._status("Bot is paused — use /start_bot to resume.")

        if self._state is BotState.HALTED:
            return self._status("Bot is halted — daily loss limit breached.")

        try:
            self._check_daily_loss_limit()
        except DailyLossLimitError as exc:
            return self._status(str(exc))

        open_positions = self._load_all_open_positions()
        if not open_positions:
            self._state = BotState.IDLE
            self._just_entered.clear()
            self._rolled_this_tick.clear()
            self._close_roll_failures.clear()  # no open positions, clear all failure counters
            return self._status("Monitor: no open positions.", [])

        self._state = BotState.MONITOR
        actions: list[str] = []
        skipped_no_iv = 0
        unrealized_pnl = 0.0

        for pos in list(open_positions):
            action, unr = self._monitor_position(pos)
            if action == "__NO_IV__":
                skipped_no_iv += 1
            elif action:
                actions.append(action)
            else:
                unrealized_pnl += unr

        self._unrealized_pnl = unrealized_pnl

        # Clear per-tick guard sets so the next tick evaluates all positions fresh.
        self._just_entered.clear()
        self._rolled_this_tick.clear()

        # Refresh after actions
        open_positions = self._load_all_open_positions()

        # Clean up stale failure counters for closed positions
        open_trade_ids = {p.get("trade_id") for p in open_positions if p.get("trade_id")}
        closed_trades = set(self._close_roll_failures.keys()) - open_trade_ids
        for trade_id in closed_trades:
            self._close_roll_failures.pop(trade_id, None)
        self._state = BotState.IDLE if not open_positions else BotState.MONITOR

        if skipped_no_iv and not actions:
            summary = f"{skipped_no_iv} position(s) skipped — no IV data"
        elif skipped_no_iv:
            summary = "; ".join(actions) + f"; {skipped_no_iv} skipped (no IV)"
        else:
            summary = "; ".join(actions) if actions else "All positions OK."

        return self._status(f"Monitor: {summary}", open_positions)

    # ── Liquidity gate ────────────────────────────────────────────────────────

    def _check_liquidity_gate(self, candidate: CalendarCandidate) -> str | None:
        """
        Fine liquidity gate applied just before order submission.

        Three checks:
        1. Per-leg bid/ask spread must be <= MAX_LEG_SPREAD_PCT of mid.
           Wide spreads signal thin books and inflate entry cost.
        2. Net entry debit must be <= spread_mid * (1 + MAX_ENTRY_PREMIUM).
           This catches cases where the bid/ask friction on two legs combines
           to make the trade start deeply underwater (e.g. paying $60 for a
           spread whose mid is $40).
        3. Both legs must have bid_size >= MIN_LEG_BID_SIZE and
           ask_size >= MIN_LEG_ASK_SIZE — ensures there is real size to hit/lift.
           (Only checked when the cache provides non-zero size data.)

        Returns a rejection reason string, or None if the candidate passes.
        """
        # Resolve per-asset thresholds (SOL uses wider limits than BTC/ETH).
        max_leg_spread_pct = config.asset_config(candidate.asset, "MAX_LEG_SPREAD_PCT")
        max_entry_premium  = config.asset_config(candidate.asset, "MAX_ENTRY_PREMIUM")

        near_mid = (candidate.near_bid + candidate.near_ask) / 2 if (candidate.near_bid > 0 and candidate.near_ask > 0) else 0.0
        far_mid  = (candidate.far_bid  + candidate.far_ask)  / 2 if (candidate.far_bid  > 0 and candidate.far_ask  > 0) else 0.0

        if near_mid > 0:
            near_spread_pct = (candidate.near_ask - candidate.near_bid) / near_mid
            if near_spread_pct > max_leg_spread_pct:
                return (
                    f"near-leg spread {near_spread_pct:.1%} > MAX_LEG_SPREAD_PCT "
                    f"{max_leg_spread_pct:.1%}"
                )

        if far_mid > 0:
            far_spread_pct = (candidate.far_ask - candidate.far_bid) / far_mid
            if far_spread_pct > max_leg_spread_pct:
                return (
                    f"far-leg spread {far_spread_pct:.1%} > MAX_LEG_SPREAD_PCT "
                    f"{max_leg_spread_pct:.1%}"
                )

        spread_mid = far_mid - near_mid
        if spread_mid > 0:
            premium = (candidate.net_debit - spread_mid) / spread_mid
            if premium > max_entry_premium:
                return (
                    f"entry premium {premium:.1%} > MAX_ENTRY_PREMIUM "
                    f"{max_entry_premium:.1%} "
                    f"(debit={candidate.net_debit:.4f}, spread_mid={spread_mid:.4f})"
                )

        # ── Bid/ask size check (requires live cache snapshot) ─────────────────
        near_snap = self._cache.get(candidate.near_instrument)
        far_snap  = self._cache.get(candidate.far_instrument)

        if near_snap is not None and near_snap.bid_size > 0:
            if near_snap.bid_size < config.MIN_LEG_BID_SIZE:
                return (
                    f"near-leg bid_size {near_snap.bid_size:.1f} < MIN_LEG_BID_SIZE "
                    f"{config.MIN_LEG_BID_SIZE}"
                )
        if near_snap is not None and near_snap.ask_size > 0:
            if near_snap.ask_size < config.MIN_LEG_ASK_SIZE:
                return (
                    f"near-leg ask_size {near_snap.ask_size:.1f} < MIN_LEG_ASK_SIZE "
                    f"{config.MIN_LEG_ASK_SIZE}"
                )
        if far_snap is not None and far_snap.bid_size > 0:
            if far_snap.bid_size < config.MIN_LEG_BID_SIZE:
                return (
                    f"far-leg bid_size {far_snap.bid_size:.1f} < MIN_LEG_BID_SIZE "
                    f"{config.MIN_LEG_BID_SIZE}"
                )
        if far_snap is not None and far_snap.ask_size > 0:
            if far_snap.ask_size < config.MIN_LEG_ASK_SIZE:
                return (
                    f"far-leg ask_size {far_snap.ask_size:.1f} < MIN_LEG_ASK_SIZE "
                    f"{config.MIN_LEG_ASK_SIZE}"
                )

        return None

    # ── Entry logic ───────────────────────────────────────────────────────────

    def _enter(
        self,
        candidate: CalendarCandidate,
        open_positions: list[dict],
    ) -> CalendarTrade | None:
        """Attempt to enter a spread. Returns the CalendarTrade record or None."""
        fill = self._executor.enter_spread(candidate)
        if fill is None:
            logger.warning(
                "ENTER rejected by executor: %s %s strike=%.0f",
                candidate.asset, candidate.option_type, candidate.strike,
            )
            return None

        expiry_near = _instrument_expiry_label(candidate.near_instrument)
        expiry_far  = _instrument_expiry_label(candidate.far_instrument)

        # Compute entry fees (use fill prices if available, else candidate prices).
        via_combo = fill.get("via_combo", False)
        try:
            open_fees_usd = entry_fees(
                candidate.asset,
                candidate.spot,
                fill["qty"],
                near_price=fill.get("near_prem", candidate.near_bid),
                far_price=fill.get("far_prem",  candidate.far_ask),
                via_combo=via_combo,
            )
        except Exception:
            open_fees_usd = 0.0
        self._fees_paid_today += open_fees_usd

        trade = create_calendar_trade(
            asset=candidate.asset,
            date_open=date.today(),
            option_type=candidate.option_type,
            strike=candidate.strike,
            expiry_near=expiry_near,
            expiry_far=expiry_far,
            near_days=candidate.near_days,
            far_days=candidate.far_days,
            qty=fill["qty"],
            spot_open=candidate.spot,
            near_prem=fill["near_prem"],
            far_prem=fill["far_prem"],
            net_debit=fill["net_debit"],
            broker=config.TRADING_MODE,
            near_instrument=candidate.near_instrument,
            far_instrument=candidate.far_instrument,
            open_fees=open_fees_usd,
            ev_score=candidate.ev_score,
            ev_score_initial=candidate.ev_score,
            db_path=self._db_path,
        )
        logger.info(
            "ENTER filled: trade_id=%d  %s %s strike=%.0f  qty=%.1f  debit=%.4f  fees=%.2f  ev=%.4f",
            trade.id, trade.asset, trade.option_type, trade.strike,
            trade.qty, trade.net_debit, open_fees_usd, candidate.ev_score,
        )
        if self._notifier:
            try:
                self._notifier.notify_entry(
                    trade.id, trade.asset, trade.option_type,
                    trade.strike, trade.qty, trade.net_debit,
                )
                logger.info("Entry notification queued for trade_id=%s", trade.id)
            except Exception as exc:
                logger.error("⚠️  NOTIFICATION FAILED on entry of trade_id=%s: %s", trade.id, exc)
        return trade

    # ── Monitor logic ─────────────────────────────────────────────────────────

    def _monitor_position(self, pos: dict) -> tuple[str | None, float]:
        """
        Check a single open position. Closes, rolls, or logs OK.

        Returns (action, unrealized_pnl) where:
          - action is a short description string, None if OK (no action taken),
            or "__NO_IV__" if the check was skipped due to missing IV data.
          - unrealized_pnl is the mark-to-market P&L contribution for this
            position (non-zero only when action is None, i.e. position held).
        """
        asset       = pos["asset"]
        strike      = pos["strike"]
        option_type = pos.get("option_type", "Call")
        trade_id    = pos["trade_id"]

        # Skip positions entered in this same scan tick — the B-S spread value
        # computed from cache IV can differ substantially from the actual fill
        # price, causing spurious TP/stop signals the moment the trade is booked.
        if trade_id in self._just_entered:
            logger.debug(
                "trade_id=%d grace skip — entered this scan tick, deferring first check",
                trade_id,
            )
            return None, 0.0

        # Fix 4: skip if already rolled this monitor tick (belt-and-suspenders guard
        # against the roll loop bug — the DB update in _try_roll is the primary fix).
        if trade_id in self._rolled_this_tick:
            logger.debug(
                "trade_id=%d already rolled this tick — deferring re-evaluation",
                trade_id,
            )
            return None, 0.0

        spot = self._cache.get_spot(asset)
        if spot is None or spot <= 0:
            logger.warning("No spot for %s — skipping monitor of trade %d", asset, trade_id)
            return "__NO_IV__", 0.0

        # Determine remaining days from today
        near_days_left, far_days_left = _days_left(pos)
        if near_days_left <= 0:
            # Near leg has expired — close the full position.
            # Apply retry limiting: cap at 3 failed attempts, then force-close to prevent stuck positions.
            trade_id = pos["trade_id"]
            failure_count = self._close_roll_failures.get(trade_id, 0)
            if failure_count >= 3:
                logger.error(
                    "trade_id=%d near leg expired but close failed %d times — halting retries, force-closing",
                    trade_id, failure_count,
                )
                # When a normal close fails due to expired near leg (Deribit rejects orders on
                # expired instruments), bypass the executor and mark the position as closed
                # in the DB with a force-close note. This breaks the retry loop.
                near_instr = pos.get("near_instrument", "")
                logger.warning(
                    "trade_id=%d near leg (%s) is expired/untradeable; position marked as closed to prevent retry loop",
                    trade_id, near_instr,
                )
                # Record force-close without calling executor (which would fail anyway)
                try:
                    close_calendar_trade(
                        trade_id=trade_id,
                        date_close=date.today(),
                        spot_close=spot,
                        pnl=0.0,  # unknown P&L; position is stuck
                        result="Loss (Force Close)",
                        notes="Near leg expired — force closed (Deribit untradeable)",
                        close_fees=0.0,
                        db_path=self._db_path,
                    )
                    self._close_roll_failures.pop(trade_id, None)
                    return f"trade_id={trade_id} force closed (expired near leg)", 0.0
                except Exception as exc:
                    logger.error("Force close of trade_id=%d failed: %s", trade_id, exc)
                    return f"trade_id={trade_id} force close FAILED: {exc}", 0.0

            close_msg = self._close_position(pos, spot, "Near leg expired")
            if "FAILED" in close_msg:
                # Close failed; increment counter and retry next tick
                self._close_roll_failures[trade_id] = failure_count + 1
            else:
                # Close succeeded; clear counter
                self._close_roll_failures.pop(trade_id, None)
            return close_msg, 0.0

        # Get current IV from cache (use the far instrument as representative)
        iv = self._get_iv(pos)
        if iv is None or iv <= 0:
            logger.warning("No IV for trade %d — skipping status check", trade_id)
            return "__NO_IV__", 0.0

        market_sv = self._get_market_spread_value(pos)
        if market_sv is None:
            logger.debug("trade_id=%d  no market prices for legs — falling back to B-S", trade_id)
        status, sv, pct, msg = check_calendar_status(
            spot, iv, near_days_left, far_days_left, pos, market_sv=market_sv
        )
        logger.info(
            "trade_id=%d  %s  [%s %s %s→%s]%s",
            trade_id, msg,
            pos.get("asset", "?"),
            int(pos.get("strike", 0)),
            pos.get("expiry_near", "?"),
            pos.get("expiry_far", "?"),
            "" if market_sv is not None else "  [B-S fallback]",
        )

        # Store the last known spread value for fallback when cache is stale
        if sv is not None:
            try:
                update_last_spread_value(trade_id, sv, db_path=self._db_path)
            except Exception as exc:
                logger.debug("Failed to update last_spread_value for trade_id=%d: %s", trade_id, exc)

        if status == "stop":
            failure_count = self._close_roll_failures.get(trade_id, 0)
            if failure_count >= 3:
                logger.error(
                    "trade_id=%d stop-loss close failed %d times — marking as stuck for manual intervention",
                    trade_id, failure_count,
                )
                # Mark as stuck instead of force-closing
                mark_position_close_stuck(
                    trade_id=trade_id,
                    error_reason=f"Stop-loss close failed after {failure_count} attempts — position needs manual close on Deribit",
                    intended_close_reason="stop-loss",
                    db_path=self._db_path,
                )
                self._close_roll_failures.pop(trade_id, None)

                # Notify user about stuck position (only once, not every monitor tick)
                if trade_id not in self._notified_stuck and self._notifier:
                    try:
                        self._notifier.notify_close_stuck(
                            trade_id=trade_id,
                            asset=pos.get("asset", ""),
                            strike=pos.get("strike", 0.0),
                            reason="Stop-loss trigger",
                            error=f"Close failed after {failure_count} attempts",
                        )
                        self._notified_stuck.add(trade_id)
                    except Exception as exc:
                        logger.error("Failed to notify about stuck position: %s", exc)

                return f"trade_id={trade_id} marked as close_stuck (stop-loss)", 0.0

            result = self._close_position(pos, spot, f"Stop-loss ({pct*100:.0f}% of debit)", sv)
            if "FAILED" in result:
                self._close_roll_failures[trade_id] = failure_count + 1
            else:
                self._close_roll_failures.pop(trade_id, None)
            return result, 0.0

        if status == "tp":
            failure_count = self._close_roll_failures.get(trade_id, 0)
            if failure_count >= 3:
                logger.error(
                    "trade_id=%d take-profit close failed %d times — marking as stuck for manual intervention",
                    trade_id, failure_count,
                )
                # Mark as stuck instead of force-closing
                mark_position_close_stuck(
                    trade_id=trade_id,
                    error_reason=f"Take-profit close failed after {failure_count} attempts — position needs manual close on Deribit",
                    intended_close_reason="take-profit",
                    db_path=self._db_path,
                )
                self._close_roll_failures.pop(trade_id, None)

                # Notify user about stuck position (only once, not every monitor tick)
                if trade_id not in self._notified_stuck and self._notifier:
                    try:
                        self._notifier.notify_close_stuck(
                            trade_id=trade_id,
                            asset=pos.get("asset", ""),
                            strike=pos.get("strike", 0.0),
                            reason="Take-profit trigger",
                            error=f"Close failed after {failure_count} attempts",
                        )
                        self._notified_stuck.add(trade_id)
                    except Exception as exc:
                        logger.error("Failed to notify about stuck position: %s", exc)

                return f"trade_id={trade_id} marked as close_stuck (take-profit)", 0.0

            result = self._close_position(pos, spot, f"Take-profit ({pct*100:.0f}% of debit)", sv)
            if "FAILED" in result:
                self._close_roll_failures[trade_id] = failure_count + 1
            else:
                self._close_roll_failures.pop(trade_id, None)
            return result, 0.0

        # Roll trigger: near leg approaching expiry and no exit signal yet.
        # In drain mode, skip rolling and close instead.
        if near_days_left <= _ROLL_TRIGGER_DAYS:
            if config.DRAIN_MODE or config.DRAIN_AND_NEW_MODE:
                logger.info(
                    "DRAIN MODE — trade_id=%d near leg expires in %d day(s), closing instead of rolling",
                    trade_id, near_days_left,
                )
                return self._close_position(pos, spot, "Drain mode — closing instead of rolling"), 0.0

            # Cap roll/close retry attempts per position — prevents unbounded retry loops
            failure_count = self._close_roll_failures.get(trade_id, 0)
            if failure_count >= 3:
                logger.error(
                    "trade_id=%d roll/close failed %d times — halting retries, closing position",
                    trade_id, failure_count,
                )
                return self._close_position(pos, spot, "Roll/close retry limit exceeded — closing"), 0.0

            rolled = self._try_roll(pos, spot)
            if rolled:
                # Clear failure counter on successful roll
                self._close_roll_failures.pop(trade_id, None)
                return f"trade_id={trade_id} rolled near leg", 0.0

            # If roll fails, increment counter and close cleanly
            self._close_roll_failures[trade_id] = failure_count + 1
            return self._close_position(pos, spot, "Roll failed — closing"), 0.0

        # Position held — compute unrealized P&L vs entry cost (debit + open fees).
        # sv is already qty-weighted (spread_value multiplies by qty internally),
        # so total debit is net_debit * qty — do NOT multiply the difference by qty again.
        # open_fees are already paid so they are included in the cost basis immediately.
        unrealized = (
            sv
            - pos.get("net_debit", 0.0) * pos.get("qty", 1.0)
            - pos.get("open_fees", 0.0)
        )
        return None, unrealized

    def _close_position(
        self,
        pos: dict,
        spot: float,
        reason: str,
        spread_value: float | None = None,
    ) -> str:
        """
        Close a position and record it in the database.

        Parameters
        ----------
        spread_value
            Current mark-to-market spread value per unit, as returned by
            check_calendar_status.  When provided this is used to compute P&L
            directly (more accurate than relying on the executor's return value,
            which in dry-run mode just echoes the entry debit).  Falls back to
            the executor return value when None (e.g. expiry or roll-fail close).
        """
        trade_id = pos["trade_id"]
        net_debit = pos.get("net_debit", 0.0)
        qty = pos.get("qty", 1.0)
        asset = pos.get("asset", "BTC")

        close_credit = self._executor.close_spread(pos)
        if close_credit is None:
            logger.error("Executor failed to close trade_id=%d", trade_id)
            return f"trade_id={trade_id} close FAILED"

        if spread_value is not None:
            # spread_value is already qty-weighted; net_debit is per-unit
            gross_pnl = spread_value - net_debit * qty
        else:
            gross_pnl = close_credit - net_debit * qty

        # Compute exit fees for fee-inclusive net P&L logging.
        try:
            near_price = pos.get("near_prem", 0.0) or 0.0
            far_price  = pos.get("far_prem",  0.0) or 0.0
            close_fees_usd = exit_fees(asset, spot, qty, near_price, far_price)
        except Exception:
            close_fees_usd = 0.0
        self._fees_paid_today += close_fees_usd

        open_fees_usd = pos.get("open_fees", 0.0) or 0.0
        roll_pnl_total = pos.get("roll_pnl", 0.0) or 0.0
        net_pnl = gross_pnl + roll_pnl_total - open_fees_usd - close_fees_usd  # include roll profit, deduct all fees

        pnl = net_pnl  # stored as true net P&L (entry + roll + exit fees already deducted)
        result = "Win (Auto TP)" if pnl >= 0 else "Loss (Auto Stop)"
        if "Take-profit" in reason:
            result = "Win (Auto TP)"
        elif "Stop-loss" in reason:
            result = "Loss (Auto Stop)"

        close_calendar_trade(
            trade_id=trade_id,
            date_close=date.today(),
            spot_close=spot,
            pnl=pnl,
            result=result,
            notes=reason,
            close_fees=close_fees_usd,
            db_path=self._db_path,
        )
        self._today_pnl += pnl
        self._session_pnl += pnl
        logger.info(
            "CLOSE trade_id=%d  gross_pnl=%.2f  roll_pnl=%.2f  close_fees=%.2f  net_pnl=%.2f  "
            "open_fees=%.2f  total_fees=%.2f  ev_initial=%.4f  reason=%s  daily_pnl=%.2f",
            trade_id, gross_pnl, roll_pnl_total, close_fees_usd, net_pnl,
            open_fees_usd, open_fees_usd + close_fees_usd,
            pos.get("ev_score_initial", 0.0),
            reason, self._today_pnl,
        )
        if self._notifier:
            try:
                asset  = pos.get("asset", "")
                strike = pos.get("strike", 0.0)
                notification_type = "take-profit" if "Take-profit" in reason else "stop-loss" if "Stop-loss" in reason else "close"
                if "Take-profit" in reason:
                    self._notifier.notify_take_profit(trade_id, asset, strike, pnl)
                elif "Stop-loss" in reason:
                    self._notifier.notify_stop(trade_id, asset, strike, pnl)
                else:
                    self._notifier.notify_close(trade_id, asset, strike, pnl, reason)
                logger.info("Notification queued for position close: type=%s trade_id=%s", notification_type, trade_id)
            except Exception as exc:
                logger.error("⚠️  NOTIFICATION FAILED on close of trade_id=%s: %s", trade_id, exc)

        # Clear any accumulated roll/close failure count for this position
        self._close_roll_failures.pop(trade_id, None)
        return f"trade_id={trade_id} {result} pnl={pnl:+.2f} ({reason})"

    def _try_roll(self, pos: dict, spot: float) -> bool:
        """
        Attempt to roll the near leg to a new expiry.

        Scans for a fresh near candidate on the same asset/strike/type and asks
        the executor to roll if one is found. Validates the new candidate meets
        all entry criteria and recalculates EV before rolling.
        """
        candidates = scan(self._cache, assets=[pos["asset"]])
        target_strike = pos["strike"]
        opt_type      = pos.get("option_type", "Call")
        matches = [
            c for c in candidates
            if c.strike == target_strike and c.option_type == opt_type
        ]
        if not matches:
            logger.info(
                "Roll: no matching candidate for asset=%s strike=%.0f",
                pos["asset"], target_strike,
            )
            return False

        new_candidate = matches[0]
        new_candidate.qty = pos.get("qty", 1.0)

        # Fix 5: skip if the scanner returned the same near instrument we already hold —
        # this means no later expiry is available yet; rolling would be a no-op.
        if new_candidate.near_instrument == pos.get("near_instrument"):
            logger.info(
                "Roll: new near instrument (%s) is the same as current — skipping",
                new_candidate.near_instrument,
            )
            return False

        # Validate new candidate passes liquidity gate (entry criteria)
        rejection_reason = self._check_liquidity_gate(new_candidate)
        if rejection_reason:
            logger.info(
                "Roll: candidate rejected by liquidity gate — %s (trade_id=%d)",
                rejection_reason, pos.get("trade_id"),
            )
            return False

        # Roll fee gate: only roll if the expected theta gain exceeds the roll cost.
        # Theta gain ≈ premium collected on the new near leg (new_near_bid × qty).
        # If the roll fees eat up most or all of the gain, close instead.
        qty_pos = pos.get("qty", 1.0)
        try:
            near_price_now = pos.get("near_prem", new_candidate.near_bid) or new_candidate.near_bid
            roll_cost = compute_roll_fees(
                pos.get("asset", new_candidate.asset),
                spot,
                qty_pos,
                near_price=near_price_now,
                new_near_price=new_candidate.near_bid,
            )
            theta_gain = new_candidate.near_bid * qty_pos
            if theta_gain <= roll_cost:
                logger.info(
                    "Roll: fee gate blocked — theta_gain=%.4f <= roll_cost=%.4f  "
                    "(trade_id=%d) — closing instead",
                    theta_gain, roll_cost, pos.get("trade_id"),
                )
                return False
            logger.debug(
                "Roll: fee gate passed — theta_gain=%.4f  roll_cost=%.4f",
                theta_gain, roll_cost,
            )
        except Exception as exc:
            logger.debug("Roll fee gate skipped (error: %s)", exc)

        success = self._executor.roll_near_leg(pos, new_candidate)
        if success:
            trade_id        = pos["trade_id"]
            new_near_instr  = new_candidate.near_instrument
            new_expiry_near = _instrument_expiry_label(new_near_instr)

            # Calculate roll P&L: what we originally sold the near leg for minus what we're buying it back at
            old_near_sell_price = pos.get("near_prem", 0.0) or 0.0
            roll_pnl_realized = (old_near_sell_price - new_candidate.near_bid) * qty_pos
            self._today_pnl += roll_pnl_realized
            self._session_pnl += roll_pnl_realized

            # Track roll fees
            try:
                roll_fee_usd = compute_roll_fees(
                    pos.get("asset", new_candidate.asset),
                    spot,
                    qty_pos,
                    near_price=near_price_now,
                    new_near_price=new_candidate.near_bid,
                )
                self._fees_paid_today += roll_fee_usd
            except Exception:
                roll_fee_usd = 0.0

            logger.info(
                "ROLL trade_id=%d  → new near=%s  roll_pnl=%.2f  roll_fees=%.2f  "
                "ev_new=%.4f  daily_pnl=%.2f",
                trade_id, new_near_instr, roll_pnl_realized, roll_fee_usd,
                new_candidate.ev_score, self._today_pnl,
            )

            # Fix 1: persist the new near leg to the DB with roll P&L and new EV.
            try:
                update_near_leg(
                    trade_id, new_near_instr, new_expiry_near,
                    roll_pnl=roll_pnl_realized,
                    ev_score_at_roll=new_candidate.ev_score,
                    db_path=self._db_path,
                )
            except Exception as exc:
                logger.error("Roll: failed to update near leg in DB for trade_id=%d: %s", trade_id, exc)

            # Fix 2: update the in-memory dict so this tick's logic sees the new expiry.
            pos["near_instrument"] = new_near_instr
            pos["expiry_near"]     = new_expiry_near
            pos["roll_pnl"] = roll_pnl_realized  # track in position dict

            # Fix 4: mark as rolled so the same tick won't evaluate this position again.
            self._rolled_this_tick.add(trade_id)

            if self._notifier:
                try:
                    self._notifier.notify_roll(
                        trade_id, pos.get("asset", ""),
                        pos.get("strike", 0.0), new_near_instr,
                    )
                    logger.info("Roll notification queued for trade_id=%s", trade_id)
                except Exception as exc:
                    logger.error("⚠️  NOTIFICATION FAILED on roll of trade_id=%s: %s", trade_id, exc)
        return success

    # ── Daily loss limit ──────────────────────────────────────────────────────

    def _check_daily_loss_limit(self) -> None:
        """Halt the bot if today's realised losses exceed the configured limit."""
        if self._today_pnl <= -abs(self._daily_loss_limit):
            self._state = BotState.HALTED
            msg = (
                f"HALTED: daily loss limit breached  "
                f"(today_pnl={self._today_pnl:.2f}, limit={-self._daily_loss_limit:.2f})"
            )
            logger.critical(msg)
            if self._notifier:
                try:
                    self._notifier.notify_daily_limit(self._today_pnl)
                except Exception as exc:
                    logger.warning("notify_daily_limit failed: %s", exc)
            raise DailyLossLimitError(msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_all_open_positions(self) -> list[dict]:
        """Return all open positions across all assets — configured or legacy.

        Iterates the union of config.ASSETS and any asset currently holding
        an open position in the DB, so positions entered when an asset was
        configured (e.g. BTC) continue to be monitored even after it is
        removed from ASSETS.
        """
        assets = set(config.ASSETS) | set(
            list_assets_with_open_positions(db_path=self._db_path)
        )
        positions: list[dict] = []
        for asset in sorted(assets):
            state = load_calendar_state(asset, db_path=self._db_path)
            positions.extend(state["open_positions"])
        return positions

    def _get_iv(self, pos: dict) -> float | None:
        """Get current IV for the far leg instrument from the cache."""
        far_instrument = pos.get("far_instrument")
        if not far_instrument:
            return None
        chain = self._cache.get_chain(pos["asset"])
        for snap in chain:
            if snap.instrument == far_instrument:
                return snap.mark_iv if snap.mark_iv > 0 else None
        return None

    def _get_market_spread_value(self, pos: dict) -> float | None:
        """
        Compute current spread value from live market mid-prices in the cache.

        Returns (far_mid - near_mid) * qty, or None if either leg is missing.
        This is far more reliable than Black-Scholes with a uniform IV, which
        can diverge badly from market prices for options away from ATM or when
        there is significant IV skew across the term structure.
        """
        near_instrument = pos.get("near_instrument")
        far_instrument  = pos.get("far_instrument")
        if not near_instrument or not far_instrument:
            return None

        chain = self._cache.get_chain(pos["asset"])
        near_mid = far_mid = None
        for snap in chain:
            if snap.instrument == near_instrument:
                if snap.bid > 0 and snap.ask > 0:
                    near_mid = (snap.bid + snap.ask) / 2
                elif snap.mark_price > 0:
                    near_mid = snap.mark_price
            elif snap.instrument == far_instrument:
                if snap.bid > 0 and snap.ask > 0:
                    far_mid = (snap.bid + snap.ask) / 2
                elif snap.mark_price > 0:
                    far_mid = snap.mark_price

        if near_mid is None or far_mid is None:
            return None
        # A calendar spread cannot be worth less than zero — clamp guards against
        # stale/inverted market data producing a negative spread value that, when
        # multiplied by a large quantity, generates an enormous phantom loss.
        return max(0.0, far_mid - near_mid) * pos.get("qty", 1.0)

    def _status(
        self,
        message: str,
        open_positions: list[dict] | None = None,
    ) -> EngineStatus:
        return EngineStatus(
            state=self._state,
            open_positions=len(open_positions) if open_positions is not None else 0,
            daily_pnl=self._today_pnl + self._unrealized_pnl,
            message=message,
        )


# ── Helper functions ──────────────────────────────────────────────────────────

def _trade_to_position(trade: CalendarTrade) -> dict:
    """Convert a CalendarTrade to the position dict format used internally."""
    return {
        "trade_id":        trade.id,
        "status":          trade.result,
        "asset":           trade.asset,
        "option_type":     trade.option_type,
        "strike":          trade.strike,
        "expiry_near":     trade.expiry_near,
        "expiry_far":      trade.expiry_far,
        "qty":             trade.qty,
        "net_debit":       trade.net_debit,
        "spot_open":       trade.spot_open,
        "near_days":       trade.near_days,
        "far_days":        trade.far_days,
        "near_instrument": trade.near_instrument,
        "far_instrument":  trade.far_instrument,
        "open_fees":       trade.open_fees,
        "close_fees":      trade.close_fees,
    }


def _days_left(pos: dict) -> tuple[int, int]:
    """
    Calculate remaining days for the near and far legs.

    Uses expiry label strings (e.g. "27JUN25") stored at entry time combined
    with today's date. Falls back to stored near_days/far_days if parsing fails.
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    _FMT = "%d%b%y"

    def _parse(label: str) -> int | None:
        try:
            dt = datetime.strptime(label.upper(), _FMT).replace(
                hour=8, tzinfo=timezone.utc
            )
            return max(int((dt - today).days), 0)
        except (ValueError, TypeError):
            return None

    near = _parse(pos.get("expiry_near", ""))
    far  = _parse(pos.get("expiry_far",  ""))
    return (
        near if near is not None else int(pos.get("near_days", 7)),
        far  if far  is not None else int(pos.get("far_days",  30)),
    )


def _instrument_expiry_label(instrument: str) -> str:
    """Extract the expiry label from a Deribit instrument name, e.g. 'BTC-27JUN25-100000-C' → '27JUN25'."""
    parts = instrument.split("-")
    return parts[1] if len(parts) >= 2 else ""
