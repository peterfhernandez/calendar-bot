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
DecisionEngine(cache, portfolio_value, executor=None, db_path=None)
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
from core.calendar_engine import check_calendar_status
from data.chain_cache import ChainCache
from db.state import (
    CalendarTrade,
    close_calendar_trade,
    create_calendar_trade,
    get_calendar_stats,
    load_calendar_state,
    DB_PATH,
)
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
        Current total portfolio value in USD (caller should refresh this).
    executor
        Object implementing ExecutorProtocol. Defaults to DryRunExecutor.
    db_path
        Path to the SQLite database (defaults to db/calendar_bot.db).
    daily_loss_limit
        USD threshold for the daily halt (defaults to config.DAILY_LOSS_LIMIT).
    """

    def __init__(
        self,
        cache:           ChainCache,
        portfolio_value: float,
        executor:        ExecutorProtocol | None = None,
        db_path:         Path | None = None,
        daily_loss_limit: float | None = None,
    ) -> None:
        self._cache           = cache
        self._portfolio_value = portfolio_value
        self._executor        = executor or DryRunExecutor()
        self._db_path         = db_path or DB_PATH
        self._daily_loss_limit = (
            daily_loss_limit if daily_loss_limit is not None
            else config.DAILY_LOSS_LIMIT
        )
        self._state           = BotState.IDLE
        self._today_pnl: float = 0.0       # realised P&L; accumulates from closed positions
        self._unrealized_pnl: float = 0.0  # MTM P&L of currently-held positions; refreshed each monitor tick

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
        if self._state is BotState.HALTED:
            return self._status("Bot is halted — daily loss limit breached.")

        try:
            self._check_daily_loss_limit()
        except DailyLossLimitError as exc:
            return self._status(str(exc))

        open_positions = self._load_all_open_positions()

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
                    "RANK skip: negative EV (%.4f) for %s %s strike=%.0f",
                    candidate.ev_score, candidate.asset, candidate.option_type, candidate.strike,
                )
                continue

            size = size_candidate(candidate, self._portfolio_value, open_positions)
            if size.qty <= 0:
                logger.debug("RANK skip: %s", size.reason)
                continue

            candidate.qty = size.qty
            logger.info(
                "RANK approved: %s %s strike=%.0f  qty=%.1f  ev=%.4f",
                candidate.asset, candidate.option_type, candidate.strike,
                candidate.qty, candidate.ev_score,
            )

            self._state = BotState.ENTER
            trade = self._enter(candidate, open_positions)
            if trade is not None:
                open_positions.append(_trade_to_position(trade))
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
        if self._state is BotState.HALTED:
            return self._status("Bot is halted — daily loss limit breached.")

        try:
            self._check_daily_loss_limit()
        except DailyLossLimitError as exc:
            return self._status(str(exc))

        open_positions = self._load_all_open_positions()
        if not open_positions:
            self._state = BotState.IDLE
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

        # Refresh after actions
        open_positions = self._load_all_open_positions()
        self._state = BotState.IDLE if not open_positions else BotState.MONITOR

        if skipped_no_iv and not actions:
            summary = f"{skipped_no_iv} position(s) skipped — no IV data"
        elif skipped_no_iv:
            summary = "; ".join(actions) + f"; {skipped_no_iv} skipped (no IV)"
        else:
            summary = "; ".join(actions) if actions else "All positions OK."

        return self._status(f"Monitor: {summary}", open_positions)

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
            broker="paper" if config.DERIBIT_PAPER else "live",
            near_instrument=candidate.near_instrument,
            far_instrument=candidate.far_instrument,
            db_path=self._db_path,
        )
        logger.info(
            "ENTER filled: trade_id=%d  %s %s strike=%.0f  qty=%.1f  debit=%.4f",
            trade.id, trade.asset, trade.option_type, trade.strike,
            trade.qty, trade.net_debit,
        )
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

        spot = self._cache.get_spot(asset)
        if spot is None or spot <= 0:
            logger.warning("No spot for %s — skipping monitor of trade %d", asset, trade_id)
            return "__NO_IV__", 0.0

        # Determine remaining days from today
        near_days_left, far_days_left = _days_left(pos)
        if near_days_left <= 0:
            # Near leg has expired — close the full position
            logger.info("trade_id=%d near leg expired, closing", trade_id)
            return self._close_position(pos, spot, "Near leg expired"), 0.0

        # Get current IV from cache (use the far instrument as representative)
        iv = self._get_iv(pos)
        if iv is None or iv <= 0:
            logger.warning("No IV for trade %d — skipping status check", trade_id)
            return "__NO_IV__", 0.0

        status, sv, pct, msg = check_calendar_status(spot, iv, near_days_left, far_days_left, pos)
        logger.info("trade_id=%d  %s", trade_id, msg)

        if status == "stop":
            return self._close_position(pos, spot, f"Stop-loss ({pct*100:.0f}% of debit)"), 0.0

        if status == "tp":
            return self._close_position(pos, spot, f"Take-profit ({pct*100:.0f}% of debit)"), 0.0

        # Roll trigger: near leg approaching expiry and no exit signal yet
        if near_days_left <= _ROLL_TRIGGER_DAYS:
            rolled = self._try_roll(pos, spot)
            if rolled:
                return f"trade_id={trade_id} rolled near leg", 0.0
            # If roll fails, close cleanly
            return self._close_position(pos, spot, "Roll failed — closing"), 0.0

        # Position held — compute unrealized P&L vs entry debit
        unrealized = (sv - pos.get("net_debit", 0.0)) * pos.get("qty", 1.0)
        return None, unrealized

    def _close_position(self, pos: dict, spot: float, reason: str) -> str:
        """Close a position and record it in the database."""
        trade_id = pos["trade_id"]
        net_debit = pos.get("net_debit", 0.0)

        close_credit = self._executor.close_spread(pos)
        if close_credit is None:
            logger.error("Executor failed to close trade_id=%d", trade_id)
            return f"trade_id={trade_id} close FAILED"

        pnl = close_credit - net_debit * pos.get("qty", 1.0)
        result = "Win (Auto TP)" if pnl >= 0 else "Loss (Auto Stop)"
        # Override label to reflect close reason when it's explicit
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
            db_path=self._db_path,
        )
        self._today_pnl += pnl
        logger.info(
            "CLOSE trade_id=%d  pnl=%.2f  reason=%s  daily_pnl=%.2f",
            trade_id, pnl, reason, self._today_pnl,
        )
        return f"trade_id={trade_id} {result} pnl={pnl:+.2f} ({reason})"

    def _try_roll(self, pos: dict, spot: float) -> bool:
        """
        Attempt to roll the near leg to a new expiry.

        Scans for a fresh near candidate on the same asset/strike/type and asks
        the executor to roll if one is found.
        """
        candidates = scan(self._cache, assets=[pos["asset"]])
        # Find a candidate matching the same strike and option type
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
        success = self._executor.roll_near_leg(pos, new_candidate)
        if success:
            logger.info(
                "ROLL trade_id=%d  → new near=%s",
                pos["trade_id"], new_candidate.near_instrument,
            )
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
            raise DailyLossLimitError(msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_all_open_positions(self) -> list[dict]:
        """Return all open positions across all configured assets."""
        positions: list[dict] = []
        for asset in config.ASSETS:
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
