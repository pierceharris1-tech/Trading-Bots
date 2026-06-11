"""
position_manager.py
Tracks all copier-opened trades in SQLite.
Maps: telegram_msg_id → list of OANDA trade IDs
Handles SL moves, partial closes, full closes.
"""

import sqlite3
import logging
import json
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("/home/ubuntu/trading-bots/state/copier_positions.db")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_msg_id INTEGER NOT NULL,
                oanda_trade_id  TEXT NOT NULL,
                instrument      TEXT NOT NULL,
                direction       TEXT NOT NULL,
                entry_price     REAL,
                sl              REAL,
                tp              REAL,
                tp_label        TEXT,        -- "tp1", "tp2", "tp3"
                units_original  INTEGER,
                status          TEXT DEFAULT 'open',  -- open | closed
                opened_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at       DATETIME
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_msg_id ON positions(telegram_msg_id)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_instrument ON positions(instrument, status)
        """)


def _conn():
    return sqlite3.connect(DB_PATH)


def record_trade(telegram_msg_id: int, oanda_trade_id: str, instrument: str,
                 direction: str, entry_price: float, sl: float,
                 tp: Optional[float], tp_label: str, units: int):
    with _conn() as c:
        c.execute("""
            INSERT INTO positions
              (telegram_msg_id, oanda_trade_id, instrument, direction,
               entry_price, sl, tp, tp_label, units_original)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (telegram_msg_id, oanda_trade_id, instrument, direction,
              entry_price, sl, tp, tp_label, units))
    logger.info(f"Recorded trade {oanda_trade_id} | {instrument} {direction} | {tp_label}")


def mark_closed(oanda_trade_id: str):
    with _conn() as c:
        c.execute("""
            UPDATE positions SET status='closed', closed_at=CURRENT_TIMESTAMP
            WHERE oanda_trade_id=?
        """, (oanda_trade_id,))


def get_open_trades_for_message(telegram_msg_id: int) -> list[dict]:
    """Get all open OANDA trades linked to a specific Telegram message."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT * FROM positions
            WHERE telegram_msg_id=? AND status='open'
            ORDER BY tp_label
        """, (telegram_msg_id,)).fetchall()
    return [dict(r) for r in rows]


def get_open_trades_for_instrument(instrument: str) -> list[dict]:
    """Get all open trades for an instrument (most recent first)."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT * FROM positions
            WHERE instrument=? AND status='open'
            ORDER BY opened_at DESC
        """, (instrument,)).fetchall()
    return [dict(r) for r in rows]


def get_most_recent_open_trade(instrument: str) -> Optional[dict]:
    """Returns the single most recently opened trade for an instrument."""
    trades = get_open_trades_for_instrument(instrument)
    return trades[0] if trades else None


def get_all_open_trades() -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC
        """).fetchall()
    return [dict(r) for r in rows]
