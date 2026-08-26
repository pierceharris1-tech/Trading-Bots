from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator
from typing import Any

CHOCH_DB_PATH = Path(__file__).resolve().parents[2] / "state" / "choch_state.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChochStateStore:
    def __init__(self, db_path=CHOCH_DB_PATH) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self.session() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS choch_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    environment TEXT NOT NULL,
                    version TEXT,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS choch_setups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    strategy_leg TEXT NOT NULL,
                    state TEXT NOT NULL,
                    direction TEXT,
                    signal_bar_time TEXT,
                    entry REAL,
                    stop REAL,
                    tp REAL,
                    r_value REAL,
                    parent_s1_trade_id TEXT,
                    s1_tp_time TEXT,
                    client_order_key TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    cancellation_reason TEXT,
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (bot_id, instrument, strategy_leg, client_order_key)
                );

                CREATE TABLE IF NOT EXISTS choch_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    strategy_leg TEXT NOT NULL,
                    client_order_key TEXT NOT NULL,
                    oanda_order_id TEXT,
                    state TEXT NOT NULL,
                    order_type TEXT,
                    price REAL,
                    units INTEGER,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (bot_id, instrument, strategy_leg, client_order_key)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_choch_orders_oanda_order_id
                    ON choch_orders(oanda_order_id)
                    WHERE oanda_order_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS choch_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    strategy_leg TEXT NOT NULL,
                    setup_id INTEGER,
                    client_order_key TEXT NOT NULL,
                    oanda_trade_id TEXT,
                    state TEXT NOT NULL,
                    entry_time TEXT,
                    exit_time TEXT,
                    entry_price REAL,
                    stop_price REAL,
                    tp_price REAL,
                    realized_r REAL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    is_simulated INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (bot_id, instrument, strategy_leg, client_order_key)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_choch_trades_oanda_trade_id
                    ON choch_trades(oanda_trade_id)
                    WHERE oanda_trade_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS choch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    instrument TEXT,
                    strategy_leg TEXT,
                    event_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    setup_id INTEGER,
                    order_id INTEGER,
                    trade_id INTEGER,
                    payload_json TEXT
                );

                CREATE TABLE IF NOT EXISTS choch_candle_progress (
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    granularity TEXT NOT NULL,
                    last_complete_candle_time TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (bot_id, account_id, instrument, granularity)
                );

                CREATE TABLE IF NOT EXISTS choch_reconciliation (
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    last_transaction_id TEXT,
                    last_reconciled_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (bot_id, account_id)
                );
                """
            )
            self._ensure_column(conn, "choch_setups", "is_simulated", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "choch_orders", "is_simulated", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "choch_trades", "is_simulated", "INTEGER NOT NULL DEFAULT 0")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def start_run(self, *, bot_id: str, account_id: str, environment: str, version: str = "phase1") -> int:
        with self.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO choch_runs (bot_id, account_id, started_at, environment, version, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (bot_id, account_id, utc_now_iso(), environment, version, "running"),
            )
            return int(cur.lastrowid)

    def get_last_candle(self, *, bot_id: str, account_id: str, instrument: str, granularity: str) -> str | None:
        with self.session() as conn:
            row = conn.execute(
                """
                SELECT last_complete_candle_time FROM choch_candle_progress
                WHERE bot_id = ? AND account_id = ? AND instrument = ? AND granularity = ?
                """,
                (bot_id, account_id, instrument, granularity),
            ).fetchone()
            return None if row is None else str(row["last_complete_candle_time"])

    def set_last_candle(self, *, bot_id: str, account_id: str, instrument: str, granularity: str, candle_time: str) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO choch_candle_progress (
                    bot_id, account_id, instrument, granularity, last_complete_candle_time, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, account_id, instrument, granularity) DO UPDATE SET
                    last_complete_candle_time = excluded.last_complete_candle_time,
                    updated_at = excluded.updated_at
                """,
                (bot_id, account_id, instrument, granularity, candle_time, utc_now_iso()),
            )

    def record_event(
        self,
        *,
        bot_id: str,
        account_id: str,
        event_type: str,
        instrument: str | None = None,
        strategy_leg: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO choch_events (
                    bot_id, account_id, instrument, strategy_leg, event_type, event_time, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bot_id,
                    account_id,
                    instrument,
                    strategy_leg,
                    event_type,
                    utc_now_iso(),
                    None if payload is None else json.dumps(payload, separators=(",", ":"), sort_keys=True),
                ),
            )

    def upsert_reconciliation(
        self,
        *,
        bot_id: str,
        account_id: str,
        last_transaction_id: str | None,
        status: str,
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO choch_reconciliation (
                    bot_id, account_id, last_transaction_id, last_reconciled_at, status
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, account_id) DO UPDATE SET
                    last_transaction_id = excluded.last_transaction_id,
                    last_reconciled_at = excluded.last_reconciled_at,
                    status = excluded.status
                """,
                (bot_id, account_id, last_transaction_id, utc_now_iso(), status),
            )

    def local_pending_order_keys(self, *, bot_id: str, account_id: str) -> set[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT client_order_key FROM choch_orders
                WHERE bot_id = ? AND account_id = ? AND state IN ('local_pending', 'broker_pending')
                  AND is_simulated = 0
                """,
                (bot_id, account_id),
            ).fetchall()
            return {str(row["client_order_key"]) for row in rows}

    def local_open_trade_ids(self, *, bot_id: str, account_id: str) -> set[str]:
        with self.session() as conn:
            rows = conn.execute(
                """
                SELECT oanda_trade_id FROM choch_trades
                WHERE bot_id = ? AND account_id = ? AND state IN ('local_open', 'broker_open')
                  AND oanda_trade_id IS NOT NULL
                  AND is_simulated = 0
                """,
                (bot_id, account_id),
            ).fetchall()
            return {str(row["oanda_trade_id"]) for row in rows}

    def upsert_simulated_s1_result(
        self,
        *,
        bot_id: str,
        account_id: str,
        instrument: str,
        result,
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO choch_setups (
                    bot_id, account_id, instrument, strategy_leg, state, direction,
                    signal_bar_time, entry, stop, tp, r_value, client_order_key,
                    cancellation_reason, is_simulated, updated_at
                )
                VALUES (?, ?, ?, 'S1', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                ON CONFLICT(bot_id, instrument, strategy_leg, client_order_key) DO UPDATE SET
                    state = excluded.state,
                    direction = excluded.direction,
                    entry = excluded.entry,
                    stop = excluded.stop,
                    tp = excluded.tp,
                    r_value = excluded.r_value,
                    cancellation_reason = excluded.cancellation_reason,
                    is_simulated = 1,
                    updated_at = datetime('now')
                """,
                (
                    bot_id,
                    account_id,
                    instrument,
                    str(result.status),
                    str(result.direction),
                    result.event_time.isoformat(),
                    result.entry,
                    result.stop,
                    result.target,
                    result.risk,
                    result.client_order_key,
                    result.cancellation_reason,
                ),
            )
            if result.entry is not None:
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        state, entry_time, exit_time, entry_price, stop_price, tp_price,
                        realized_r, is_simulated, updated_at
                    )
                    VALUES (?, ?, ?, 'S1', ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                    ON CONFLICT(bot_id, instrument, strategy_leg, client_order_key) DO UPDATE SET
                        state = excluded.state,
                        entry_time = excluded.entry_time,
                        exit_time = excluded.exit_time,
                        entry_price = excluded.entry_price,
                        stop_price = excluded.stop_price,
                        tp_price = excluded.tp_price,
                        realized_r = excluded.realized_r,
                        is_simulated = 1,
                        updated_at = datetime('now')
                    """,
                    (
                        bot_id,
                        account_id,
                        instrument,
                        result.client_order_key,
                        str(result.outcome or result.status),
                        result.entry_time.isoformat() if result.entry_time is not None else None,
                        result.exit_time.isoformat() if result.exit_time is not None else None,
                        result.entry,
                        result.stop,
                        result.target,
                        result.net_r,
                    ),
                )

    def upsert_simulated_s2_result(
        self,
        *,
        bot_id: str,
        account_id: str,
        instrument: str,
        result,
    ) -> None:
        key = f"{result.parent_s1_key}-S2"
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO choch_setups (
                    bot_id, account_id, instrument, strategy_leg, state, direction,
                    signal_bar_time, entry, stop, tp, r_value, parent_s1_trade_id,
                    s1_tp_time, client_order_key, is_simulated, updated_at
                )
                VALUES (?, ?, ?, 'S2', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                ON CONFLICT(bot_id, instrument, strategy_leg, client_order_key) DO UPDATE SET
                    state = excluded.state,
                    direction = excluded.direction,
                    signal_bar_time = excluded.signal_bar_time,
                    entry = excluded.entry,
                    stop = excluded.stop,
                    tp = excluded.tp,
                    r_value = excluded.r_value,
                    s1_tp_time = excluded.s1_tp_time,
                    is_simulated = 1,
                    updated_at = datetime('now')
                """,
                (
                    bot_id,
                    account_id,
                    instrument,
                    str(result.status),
                    str(result.s2_direction),
                    result.decision_time.isoformat() if result.decision_time is not None else None,
                    result.entry,
                    result.stop,
                    result.target,
                    result.risk,
                    result.parent_s1_key,
                    result.s1_tp_time.isoformat(),
                    key,
                ),
            )
            if result.entry is not None:
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        state, entry_time, exit_time, entry_price, stop_price, tp_price,
                        realized_r, is_simulated, updated_at
                    )
                    VALUES (?, ?, ?, 'S2', ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
                    ON CONFLICT(bot_id, instrument, strategy_leg, client_order_key) DO UPDATE SET
                        state = excluded.state,
                        entry_time = excluded.entry_time,
                        exit_time = excluded.exit_time,
                        entry_price = excluded.entry_price,
                        stop_price = excluded.stop_price,
                        tp_price = excluded.tp_price,
                        realized_r = excluded.realized_r,
                        is_simulated = 1,
                        updated_at = datetime('now')
                    """,
                    (
                        bot_id,
                        account_id,
                        instrument,
                        key,
                        str(result.outcome or result.status),
                        result.decision_time.isoformat() if result.decision_time is not None else None,
                        result.exit_time.isoformat() if result.exit_time is not None else None,
                        result.entry,
                        result.stop,
                        result.target,
                        result.r,
                    ),
                )
