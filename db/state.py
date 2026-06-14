"""Database helpers for Calendar strategy trades."""
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from models import Calendar, get_session, STRATEGY_CALENDAR


def load_calendar_state(asset: str, session: Optional[Session] = None) -> dict:
    """
    Load calendar trading state for an asset from the database.

    Queries the Calendar table to reconstruct state from trade history.
    Returns a dict with keys: open, total_pnl, wins, losses, trades, broker.
    If no trades exist, returns a fresh default state.
    """
    close_session = session is None
    if session is None:
        session = get_session()

    try:
        trades = session.query(Calendar).filter_by(asset=asset).order_by(Calendar.date_open).all()

        if not trades:
            return {
                "open":      None,
                "total_pnl": 0.0,
                "wins":      0,
                "losses":    0,
                "trades":    0,
                "broker":    None,
            }

        # Calculate aggregate stats from trade history
        # Don't count "Far Leg Only" or "Near Leg Rolled" as closed (they're still open)
        closed_trades = [t for t in trades if t.result not in ("Open", "Far Leg Only", "Near Leg Rolled")]
        # Count wins: either "Win" in result or "Closed" with P&L >= 0
        wins = sum(
            1 for t in closed_trades
            if ("Win" in (t.result or "")) or (t.result == "Closed" and (t.pnl or 0.0) >= 0)
        )
        losses = len(closed_trades) - wins
        total_pnl = sum(t.pnl for t in closed_trades if t.pnl) or 0.0

        # Get open position from the most recent open trade if any
        # (includes "Open", "Far Leg Only", "Near Leg Rolled" statuses)
        open_position = None
        for trade in reversed(trades):
            if trade.result in ("Open", "Far Leg Only", "Near Leg Rolled"):
                open_position = {
                    "trade_id": trade.id,
                    "status": trade.result,
                    "asset": trade.asset,
                    "option_type": trade.option_type,
                    "strike": trade.strike,
                    "expiry_near": trade.expiry_near,
                    "expiry_far": trade.expiry_far,
                    "qty": trade.qty,
                    "net_debit": trade.net_debit or 0.0,
                    "spot_open": trade.spot_open,
                    "near_days": trade.near_days,
                    "far_days": trade.far_days,
                    "near_instrument": trade.near_instrument,
                    "far_instrument": trade.far_instrument,
                    "open_fees": trade.open_fees or 0.0,
                    "close_fees": trade.close_fees or 0.0,
                }
                break

        # Get broker from the most recent trade
        latest = trades[-1]

        return {
            "open":      open_position,
            "total_pnl": total_pnl,
            "wins":      wins,
            "losses":    losses,
            "trades":    len(closed_trades),
            "broker":    latest.broker,
        }
    finally:
        if close_session:
            session.close()


def save_calendar_state(asset: str, state: dict, session: Optional[Session] = None) -> None:
    """
    Persist calendar trading state to the database.

    Updates the most recent Calendar record with the current broker and result.
    If an open position is provided, creates a new record (used for opening positions).
    If no record exists, creates one with the provided state (used in tests).
    """
    close_session = session is None
    if session is None:
        session = get_session()

    try:
        row = session.query(Calendar).filter_by(asset=asset).order_by(Calendar.date_open.desc()).first()
        open_pos = state.get("open")

        if row and open_pos:
            # If we have a new open position and an existing record, create a new trade record
            trade = Calendar(
                asset=asset,
                option_type=open_pos.get("option_type", "Call"),
                strike=open_pos.get("strike", 0.0),
                expiry_near=open_pos.get("expiry_near", ""),
                expiry_far=open_pos.get("expiry_far", ""),
                near_days=open_pos.get("near_days", 7),
                far_days=open_pos.get("far_days", 30),
                qty=open_pos.get("qty", 1.0),
                date_open=date.today(),
                spot_open=open_pos.get("spot_open", 0.0),
                net_debit=open_pos.get("net_debit", 0.0),
                result="Open",
                broker=state.get("broker"),
                fees=0.0,
                open_fees=open_pos.get("open_fees", 0.0),
                close_fees=open_pos.get("close_fees", 0.0),
            )
            session.add(trade)
            session.commit()
        elif row and not open_pos and row.result == "Open":
            # Mark existing open position as closed (no result specified, default to Closed)
            row.result = "Closed"
            row.broker = state.get("broker")
            session.commit()
        elif row:
            # Update only broker if no new open position and row is not open
            row.broker = state.get("broker")
            session.commit()
        else:
            # Create a new record if none exists (for testing)
            trade = Calendar(
                asset=asset,
                option_type=open_pos.get("option_type") if open_pos else "Call",
                strike=open_pos.get("strike") if open_pos else 0.0,
                expiry_near=open_pos.get("expiry_near") if open_pos else "",
                expiry_far=open_pos.get("expiry_far") if open_pos else "",
                near_days=open_pos.get("near_days") if open_pos else 7,
                far_days=open_pos.get("far_days") if open_pos else 30,
                qty=open_pos.get("qty") if open_pos else 1.0,
                date_open=date.today(),
                spot_open=open_pos.get("spot_open") if open_pos else 0.0,
                net_debit=open_pos.get("net_debit") if open_pos else 0.0,
                result="Open" if open_pos else "Closed",
                broker=state.get("broker"),
                fees=0.0,
                open_fees=open_pos.get("open_fees", 0.0) if open_pos else 0.0,
                close_fees=open_pos.get("close_fees", 0.0) if open_pos else 0.0,
            )
            session.add(trade)
            session.commit()
    finally:
        if close_session:
            session.close()


def create_calendar_trade(
    asset: str,
    date_open: date,
    option_type: str,
    strike: float,
    expiry_near: str,
    expiry_far: str,
    near_days: int,
    far_days: int,
    qty: float,
    spot_open: float,
    near_prem: float,
    far_prem: float,
    net_debit: float,
    notes: Optional[str] = None,
    broker: Optional[str] = None,
    near_instrument: Optional[str] = None,
    far_instrument: Optional[str] = None,
    open_fees: float = 0.0,
    session: Optional[Session] = None,
) -> Calendar:
    """Create and insert a Calendar trade record. Returns the persisted Calendar."""
    close_session = session is None
    if session is None:
        session = get_session()

    try:
        trade = Calendar(
            asset=asset,
            option_type=option_type,
            strike=strike,
            expiry_near=expiry_near,
            expiry_far=expiry_far,
            near_days=near_days,
            far_days=far_days,
            qty=qty,
            date_open=date_open,
            spot_open=spot_open,
            near_prem=near_prem,
            far_prem=far_prem,
            net_debit=net_debit,
            fees=0.0,
            open_fees=open_fees,
            result="Open",
            notes=notes,
            broker=broker,
            near_instrument=near_instrument,
            far_instrument=far_instrument,
        )
        session.add(trade)
        session.commit()
        session.refresh(trade)
        return trade
    finally:
        if close_session:
            session.close()


def close_calendar_trade(
    trade_id: int,
    date_close: date,
    spot_close: float,
    pnl: float,
    result: str,
    notes: Optional[str] = None,
    close_fees: float = 0.0,
    session: Optional[Session] = None,
) -> Calendar:
    """Close a Calendar trade by updating its close price, P&L, result, and close fees."""
    close_session = session is None
    if session is None:
        session = get_session()

    try:
        trade = session.get(Calendar, trade_id)
        if not trade:
            raise ValueError(f"Calendar trade ID {trade_id} not found")

        trade.date_close = date_close
        trade.spot_close = spot_close
        trade.pnl        = pnl
        trade.result     = result
        trade.close_fees = close_fees
        if notes:
            trade.notes = notes

        session.commit()
        session.refresh(trade)
        return trade
    finally:
        if close_session:
            session.close()


def get_calendar_stats(asset: Optional[str] = None, session: Optional[Session] = None) -> dict:
    """
    Get performance statistics for closed calendar trades.

    Returns dict with: trades, wins, losses, win_rate, total_pnl, avg_pnl.
    """
    close_session = session is None
    if session is None:
        session = get_session()

    try:
        query = session.query(Calendar).filter(
            Calendar.result.in_(["Win", "Loss", "Closed", "Win (Auto TP)", "Loss (Auto Stop)", "Loss (Stop)", "Loss (Early)"])
        )
        if asset:
            query = query.filter(Calendar.asset == asset)

        trades = query.all()
        if not trades:
            return {
                "trades":    0,
                "wins":      0,
                "losses":    0,
                "win_rate":  0.0,
                "total_pnl": 0.0,
                "avg_pnl":   0.0,
            }

        # Count wins: either "Win" in result or "Closed" with P&L >= 0
        wins = sum(
            1 for t in trades
            if ("Win" in (t.result or "")) or (t.result == "Closed" and (t.pnl or 0.0) >= 0)
        )
        losses    = len(trades) - wins
        pnls      = [t.pnl for t in trades if t.pnl is not None]
        total_pnl = sum(pnls) if pnls else 0.0

        return {
            "trades":    len(trades),
            "wins":      wins,
            "losses":    losses,
            "win_rate":  (wins / len(trades) * 100) if trades else 0.0,
            "total_pnl": total_pnl,
            "avg_pnl":   (total_pnl / len(pnls)) if pnls else 0.0,
        }
    finally:
        if close_session:
            session.close()
