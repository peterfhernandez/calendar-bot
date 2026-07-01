"""SQLite state persistence for calendar spread trades."""
import os as _os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_AEST = ZoneInfo("Australia/Sydney")

# BOT_DB_PATH lets a separate bot instance (e.g. test mode) use its own DB
# without touching the paper-mode database.  Set via --db CLI flag or directly
# in the instance's .env file.
DB_PATH = Path(_os.environ.get("BOT_DB_PATH", str(Path(__file__).parent / "calendar_bot.db")))


@dataclass
class CalendarTrade:
    id: int
    asset: str
    option_type: str
    strike: float
    expiry_near: str
    expiry_far: str
    near_days: int
    far_days: int
    qty: float
    date_open: str
    spot_open: float
    near_prem: float
    far_prem: float
    net_debit: float
    fees: float
    open_fees: float
    close_fees: float
    result: str
    broker: Optional[str]
    notes: Optional[str]
    near_instrument: Optional[str]
    far_instrument: Optional[str]
    date_close: Optional[str]
    spot_close: Optional[float]
    pnl: Optional[float]
    ev_score: float = field(default=0.0)
    ev_score_initial: float = field(default=0.0)
    ev_score_at_roll: float = field(default=0.0)
    roll_pnl: float = field(default=0.0)


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Create the calendar_trades table if it does not exist."""
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calendar_trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                asset            TEXT    NOT NULL,
                option_type      TEXT    NOT NULL,
                strike           REAL    NOT NULL,
                expiry_near      TEXT    NOT NULL,
                expiry_far       TEXT    NOT NULL,
                near_days        INTEGER NOT NULL,
                far_days         INTEGER NOT NULL,
                qty              REAL    NOT NULL,
                date_open        TEXT    NOT NULL,
                spot_open        REAL    NOT NULL,
                near_prem        REAL    NOT NULL DEFAULT 0.0,
                far_prem         REAL    NOT NULL DEFAULT 0.0,
                net_debit        REAL    NOT NULL DEFAULT 0.0,
                fees             REAL    NOT NULL DEFAULT 0.0,
                open_fees        REAL    NOT NULL DEFAULT 0.0,
                close_fees       REAL    NOT NULL DEFAULT 0.0,
                result           TEXT    NOT NULL DEFAULT 'Open',
                broker           TEXT,
                notes            TEXT,
                near_instrument  TEXT,
                far_instrument   TEXT,
                date_close       TEXT,
                spot_close       REAL,
                pnl              REAL,
                ev_score         REAL    NOT NULL DEFAULT 0.0,
                ev_score_initial REAL    NOT NULL DEFAULT 0.0,
                ev_score_at_roll REAL    NOT NULL DEFAULT 0.0,
                roll_pnl         REAL    NOT NULL DEFAULT 0.0
            )
        """)
        # Migrations: add new columns to existing databases
        for col_name, col_type in [
            ("ev_score", "REAL NOT NULL DEFAULT 0.0"),
            ("ev_score_initial", "REAL NOT NULL DEFAULT 0.0"),
            ("ev_score_at_roll", "REAL NOT NULL DEFAULT 0.0"),
            ("roll_pnl", "REAL NOT NULL DEFAULT 0.0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE calendar_trades ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # column already exists


def _row_to_trade(row: sqlite3.Row) -> CalendarTrade:
    return CalendarTrade(
        id=row["id"],
        asset=row["asset"],
        option_type=row["option_type"],
        strike=row["strike"],
        expiry_near=row["expiry_near"],
        expiry_far=row["expiry_far"],
        near_days=row["near_days"],
        far_days=row["far_days"],
        qty=row["qty"],
        date_open=row["date_open"],
        spot_open=row["spot_open"],
        near_prem=row["near_prem"],
        far_prem=row["far_prem"],
        net_debit=row["net_debit"],
        fees=row["fees"],
        open_fees=row["open_fees"],
        close_fees=row["close_fees"],
        result=row["result"],
        broker=row["broker"],
        notes=row["notes"],
        near_instrument=row["near_instrument"],
        far_instrument=row["far_instrument"],
        date_close=row["date_close"],
        spot_close=row["spot_close"],
        pnl=row["pnl"],
        ev_score=row["ev_score"] if row["ev_score"] is not None else 0.0,
        ev_score_initial=row["ev_score_initial"] if row["ev_score_initial"] is not None else 0.0,
        ev_score_at_roll=row["ev_score_at_roll"] if row["ev_score_at_roll"] is not None else 0.0,
        roll_pnl=row["roll_pnl"] if row["roll_pnl"] is not None else 0.0,
    )


_OPEN_STATUSES = ("Open", "Far Leg Only", "Near Leg Rolled")


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
    ev_score: float = 0.0,
    ev_score_initial: float = 0.0,
    db_path: Path = DB_PATH,
) -> CalendarTrade:
    """Insert a new calendar trade record with result='Open'. Returns the persisted trade."""
    init_db(db_path)
    # If ev_score_initial is not provided, use ev_score (for backward compat)
    if ev_score_initial == 0.0:
        ev_score_initial = ev_score
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO calendar_trades
                (asset, option_type, strike, expiry_near, expiry_far,
                 near_days, far_days, qty, date_open, spot_open,
                 near_prem, far_prem, net_debit, fees, open_fees,
                 result, notes, broker, near_instrument, far_instrument, ev_score, ev_score_initial)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0.0,?,?,?,?,?,?,?,?)
            """,
            (
                asset, option_type, strike, expiry_near, expiry_far,
                near_days, far_days, qty, date_open.isoformat(), spot_open,
                near_prem, far_prem, net_debit, open_fees,
                "Open", notes, broker, near_instrument, far_instrument, ev_score, ev_score_initial,
            ),
        )
        row = conn.execute(
            "SELECT * FROM calendar_trades WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_trade(row)


def close_calendar_trade(
    trade_id: int,
    date_close: date,
    spot_close: float,
    pnl: float,
    result: str,
    notes: Optional[str] = None,
    close_fees: float = 0.0,
    db_path: Path = DB_PATH,
) -> CalendarTrade:
    """Update a trade record with close price, P&L, result, and close fees."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        trade = conn.execute(
            "SELECT * FROM calendar_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if not trade:
            raise ValueError(f"Calendar trade ID {trade_id} not found")

        conn.execute(
            """
            UPDATE calendar_trades
            SET date_close = ?, spot_close = ?, pnl = ?, result = ?,
                close_fees = ?, notes = COALESCE(?, notes)
            WHERE id = ?
            """,
            (date_close.isoformat(), spot_close, pnl, result, close_fees, notes, trade_id),
        )
        row = conn.execute(
            "SELECT * FROM calendar_trades WHERE id = ?", (trade_id,)
        ).fetchone()
    return _row_to_trade(row)


def list_assets_with_open_positions(db_path: Path = DB_PATH) -> list[str]:
    """Return distinct asset names that have at least one open position in the DB."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT asset FROM calendar_trades WHERE result IN ({','.join('?'*len(_OPEN_STATUSES))}) ORDER BY asset",
            _OPEN_STATUSES,
        ).fetchall()
    return [row["asset"] for row in rows]


def load_calendar_state(asset: str, db_path: Path = DB_PATH) -> dict:
    """
    Reconstruct trading state for an asset from trade history.

    Returns dict with keys: open_positions, total_pnl, wins, losses, trades, broker.
    ``open_positions`` is a list of all currently open position dicts (may be empty).
    """
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM calendar_trades WHERE asset = ? ORDER BY date_open",
            (asset,),
        ).fetchall()

    if not rows:
        return {"open_positions": [], "total_pnl": 0.0, "wins": 0, "losses": 0, "trades": 0, "broker": None}

    trades = [_row_to_trade(r) for r in rows]
    closed = [t for t in trades if t.result not in _OPEN_STATUSES]
    wins = sum(
        1 for t in closed
        if ("Win" in (t.result or "")) or (t.result == "Closed" and (t.pnl or 0.0) >= 0)
    )
    total_pnl = sum(t.pnl for t in closed if t.pnl is not None)

    open_positions = [
        {
            "trade_id":           trade.id,
            "status":             trade.result,
            "asset":              trade.asset,
            "option_type":        trade.option_type,
            "strike":             trade.strike,
            "expiry_near":        trade.expiry_near,
            "expiry_far":         trade.expiry_far,
            "qty":                trade.qty,
            "net_debit":          trade.net_debit,
            "spot_open":          trade.spot_open,
            "near_days":          trade.near_days,
            "far_days":           trade.far_days,
            "near_instrument":    trade.near_instrument,
            "far_instrument":     trade.far_instrument,
            "open_fees":          trade.open_fees,
            "close_fees":         trade.close_fees,
            "roll_pnl":           trade.roll_pnl,
            "ev_score":           trade.ev_score,
            "ev_score_initial":   trade.ev_score_initial,
            "ev_score_at_roll":   trade.ev_score_at_roll,
        }
        for trade in trades
        if trade.result in _OPEN_STATUSES
    ]

    return {
        "open_positions": open_positions,
        "total_pnl":      total_pnl,
        "wins":           wins,
        "losses":         len(closed) - wins,
        "trades":         len(closed),
        "broker":         trades[-1].broker,
    }


def update_near_leg(
    trade_id: int,
    new_near_instrument: str,
    new_expiry_near: str,
    roll_pnl: float = 0.0,
    ev_score_at_roll: float = 0.0,
    db_path: Path = DB_PATH,
) -> CalendarTrade:
    """Update a trade's near leg after a successful roll, including roll P&L and EV."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        trade = conn.execute(
            "SELECT * FROM calendar_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if not trade:
            raise ValueError(f"Calendar trade ID {trade_id} not found")
        conn.execute(
            """
            UPDATE calendar_trades
            SET near_instrument = ?, expiry_near = ?, result = 'Near Leg Rolled',
                roll_pnl = roll_pnl + ?, ev_score_at_roll = ?
            WHERE id = ?
            """,
            (new_near_instrument, new_expiry_near, roll_pnl, ev_score_at_roll, trade_id),
        )
        row = conn.execute(
            "SELECT * FROM calendar_trades WHERE id = ?", (trade_id,)
        ).fetchone()
    return _row_to_trade(row)


def get_open_trades(db_path: Path = DB_PATH) -> list[CalendarTrade]:
    """Return all currently open calendar trades as CalendarTrade objects."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM calendar_trades WHERE result IN ({','.join('?'*len(_OPEN_STATUSES))}) ORDER BY date_open",
            _OPEN_STATUSES,
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_trades_opened_today(db_path: Path = DB_PATH) -> list[CalendarTrade]:
    """Return trades opened since midnight UTC today."""
    init_db(db_path)
    today_str = datetime.now(timezone.utc).date().isoformat()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM calendar_trades WHERE date_open >= ? ORDER BY date_open",
            (today_str,),
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_trades_closed_today(db_path: Path = DB_PATH) -> list[CalendarTrade]:
    """Return trades closed since midnight UTC today (any non-open result)."""
    init_db(db_path)
    today_str = datetime.now(timezone.utc).date().isoformat()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM calendar_trades WHERE date_close >= ? AND result NOT IN ({','.join('?'*len(_OPEN_STATUSES))}) ORDER BY date_close",
            (today_str, *_OPEN_STATUSES),
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_trades_opened_today_aest(db_path: Path = DB_PATH) -> list[CalendarTrade]:
    """Return trades opened since midnight AEST today."""
    init_db(db_path)
    today_str = datetime.now(_AEST).date().isoformat()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM calendar_trades WHERE date_open >= ? ORDER BY date_open",
            (today_str,),
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_trades_closed_today_aest(db_path: Path = DB_PATH) -> list[CalendarTrade]:
    """Return trades closed since midnight AEST today (any non-open result)."""
    init_db(db_path)
    today_str = datetime.now(_AEST).date().isoformat()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM calendar_trades WHERE date_close >= ? AND result NOT IN ({','.join('?'*len(_OPEN_STATUSES))}) ORDER BY date_close",
            (today_str, *_OPEN_STATUSES),
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_trades_opened_since(since: datetime, db_path: Path = DB_PATH) -> list[CalendarTrade]:
    """Return trades opened on or after `since` (UTC datetime)."""
    init_db(db_path)
    since_str = since.date().isoformat()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM calendar_trades WHERE date_open >= ? ORDER BY date_open",
            (since_str,),
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_trades_closed_since(since: datetime, db_path: Path = DB_PATH) -> list[CalendarTrade]:
    """Return trades closed on or after `since` (UTC datetime, any non-open result)."""
    init_db(db_path)
    since_str = since.date().isoformat()
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM calendar_trades WHERE date_close >= ? AND result NOT IN ({','.join('?'*len(_OPEN_STATUSES))}) ORDER BY date_close",
            (since_str, *_OPEN_STATUSES),
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_calendar_stats(asset: Optional[str] = None, db_path: Path = DB_PATH) -> dict:
    """
    Aggregate performance statistics for closed calendar trades.

    Returns dict with: trades, wins, losses, win_rate, total_pnl, avg_pnl.
    """
    init_db(db_path)
    closed_results = (
        "Win", "Loss", "Closed",
        "Win (Auto TP)", "Loss (Auto Stop)", "Loss (Stop)", "Loss (Early)",
    )
    placeholders = ",".join("?" * len(closed_results))
    sql = f"SELECT * FROM calendar_trades WHERE result IN ({placeholders})"
    params: list = list(closed_results)
    if asset:
        sql += " AND asset = ?"
        params.append(asset)

    with get_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}

    trades = [_row_to_trade(r) for r in rows]
    wins = sum(
        1 for t in trades
        if ("Win" in (t.result or "")) or (t.result == "Closed" and (t.pnl or 0.0) >= 0)
    )
    pnls = [t.pnl for t in trades if t.pnl is not None]
    total_pnl = sum(pnls) if pnls else 0.0

    return {
        "trades":    len(trades),
        "wins":      wins,
        "losses":    len(trades) - wins,
        "win_rate":  wins / len(trades) * 100,
        "total_pnl": total_pnl,
        "avg_pnl":   total_pnl / len(pnls) if pnls else 0.0,
    }
