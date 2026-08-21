# shared/state_store.py
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parents[1] / "state" / "bot_state.db"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_kv (
            bot_id TEXT NOT NULL,
            k TEXT NOT NULL,
            v TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (bot_id, k)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            bot_id TEXT PRIMARY KEY,
            equity REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            instrument TEXT NOT NULL,
            account_id TEXT NOT NULL,
            open_time TEXT NOT NULL,
            close_time TEXT,
            duration_seconds INTEGER,
            realized_pnl REAL,
            order_id TEXT,
            trade_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    # Better concurrency behaviour
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    _ensure_schema(conn)
    return conn


def get_equity(bot_id: str, client=None) -> float:
    """
    Returns live account NAV from OANDA if client is provided.
    Falls back to DB value if no client given.
    Always use client= in live bots for accurate position sizing.
    """
    if client is not None:
        try:
            nav = client.get_account_nav()
            return nav
        except Exception as e:
            import logging
            logging.getLogger("shared.state_store").warning(
                f"get_equity: failed to fetch live NAV, falling back to DB | err={e}"
            )
    with _connect() as conn:
        row = conn.execute(
            "SELECT equity FROM bot_state WHERE bot_id = ?",
            (bot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"get_equity: no client provided and bot_id not found: {bot_id}"
            )
        return float(row["equity"])


def set_equity(bot_id: str, equity: float) -> None:
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE bot_state
               SET equity = ?,
                   updated_at = datetime('now')
             WHERE bot_id = ?
            """,
            (float(equity), bot_id),
        )
        if cur.rowcount != 1:
            raise KeyError(f"bot_id not found in bot_state: {bot_id}")


def add_pnl(bot_id: str, pnl: float) -> float:
    """
    Atomically: equity := equity + pnl
    Returns the new equity.
    """
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE;")  # lock for writers (safe compounding)
        row = conn.execute(
            "SELECT equity FROM bot_state WHERE bot_id = ?",
            (bot_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK;")
            raise KeyError(f"bot_id not found in bot_state: {bot_id}")

        new_eq = float(row["equity"]) + float(pnl)

        conn.execute(
            """
            UPDATE bot_state
               SET equity = ?,
                   updated_at = datetime('now')
             WHERE bot_id = ?
            """,
            (new_eq, bot_id),
        )
        conn.execute("COMMIT;")
        return new_eq

def kv_get(bot_id: str, key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT v FROM bot_kv WHERE bot_id = ? AND k = ?",
            (bot_id, key),
        ).fetchone()
        return None if row is None else str(row["v"])


def kv_set(bot_id: str, key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO bot_kv (bot_id, k, v, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(bot_id, k) DO UPDATE SET
                v = excluded.v,
                updated_at = datetime('now')
            """,
            (bot_id, key, str(value)),
        )


def trade_open(
    *,
    bot_id: str,
    instrument: str,
    account_id: str,
    open_time: str,
    order_id: str | None,
    trade_id: str | None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                bot_id, instrument, account_id, open_time, order_id, trade_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (bot_id, instrument, account_id, open_time, order_id, trade_id),
        )
        return int(cur.lastrowid)


def trade_close(
    *,
    trade_row_id: int,
    close_time: str,
    duration_seconds: int,
    realized_pnl: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE trades
               SET close_time = ?,
                   duration_seconds = ?,
                   realized_pnl = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (close_time, int(duration_seconds), float(realized_pnl), int(trade_row_id)),
        )


def trade_open_for_key(bot_id: str, instrument: str, trade_id: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM trades
             WHERE bot_id = ? AND instrument = ? AND trade_id = ? AND close_time IS NULL
             ORDER BY id DESC
             LIMIT 1
            """,
            (bot_id, instrument, trade_id),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])


def trade_open_rows(bot_id: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
             WHERE bot_id = ? AND close_time IS NULL
             ORDER BY id ASC
            """,
            (bot_id,),
        ).fetchall()
        return rows


def trade_close_by_trade_id(
    *,
    bot_id: str,
    trade_id: str,
    close_time: str,
    duration_seconds: int,
    realized_pnl: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE trades
               SET close_time = ?,
                   duration_seconds = ?,
                   realized_pnl = ?,
                   updated_at = datetime('now')
             WHERE bot_id = ? AND trade_id = ? AND close_time IS NULL
            """,
            (close_time, int(duration_seconds), float(realized_pnl), bot_id, trade_id),
        )
