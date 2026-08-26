from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from bots.choch_live.s1_structure import S1Result
from bots.choch_live.state import ChochStateStore


def filled_s1_result(*, key: str = "s1-key", outcome: str | None = None) -> S1Result:
    event_time = pd.Timestamp("2026-01-01T10:00:00Z")
    entry_time = pd.Timestamp("2026-01-01T11:00:00Z")
    return S1Result(
        client_order_key=key,
        status="filled",
        event_time=event_time,
        direction="bullish",
        origin=1.08,
        event_atr=0.01,
        event_extreme=1.11,
        running_extreme=1.11,
        entry_time=entry_time,
        exit_time=None,
        entry=1.1000,
        stop=1.0900,
        target=1.1150,
        risk=0.0100,
        outcome=outcome,
        net_r=None,
        cancellation_reason=None,
        payload={
            "shadow_position_size": {
                "nav": 100_000.0,
                "intended_risk_cash": 2_000.0,
                "actual_risk_cash": 1_997.0,
                "actual_risk_pct": 0.01997,
                "units": 249_625,
                "signed_units": 249_625.0,
                "margin_required": 9_153.0,
                "aggregate_cap_cash": 4_000.0,
                "existing_choch_risk_cash": 0.0,
                "resulting_aggregate_risk_cash": 1_997.0,
                "admitted": True,
            }
        },
    )


class TestChochStateSchema(unittest.TestCase):
    def test_schema_creation_idempotent_and_progress_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            store.ensure_schema()
            store.set_last_candle(
                bot_id="BOT",
                account_id="acct",
                instrument="EUR_USD",
                granularity="M10",
                candle_time="2026-01-01T00:10:00+00:00",
            )
            self.assertEqual(
                store.get_last_candle(
                    bot_id="BOT",
                    account_id="acct",
                    instrument="EUR_USD",
                    granularity="M10",
                ),
                "2026-01-01T00:10:00+00:00",
            )

    def test_order_uniqueness_constraint(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = ChochStateStore(db_path)
            store.ensure_schema()
            with store.session() as conn:
                conn.execute(
                    """
                    INSERT INTO choch_orders (
                        bot_id, account_id, instrument, strategy_leg, client_order_key, state
                    )
                    VALUES ('BOT', 'acct', 'EUR_USD', 'S1', 'key-1', 'broker_pending')
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO choch_orders (
                            bot_id, account_id, instrument, strategy_leg, client_order_key, state
                        )
                        VALUES ('BOT', 'acct', 'EUR_USD', 'S1', 'key-1', 'broker_pending')
                        """
                    )

    def test_reconciliation_state_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            store.upsert_reconciliation(
                bot_id="BOT",
                account_id="acct",
                last_transaction_id="42",
                status="ok",
            )
            with store.session() as conn:
                row = conn.execute("SELECT * FROM choch_reconciliation").fetchone()
            self.assertEqual(row["last_transaction_id"], "42")
            self.assertEqual(row["status"], "ok")

    def test_reconciliation_queries_ignore_simulated_orders_and_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            with store.session() as conn:
                conn.execute(
                    """
                    INSERT INTO choch_orders (
                        bot_id, account_id, instrument, strategy_leg, client_order_key, state, is_simulated
                    )
                    VALUES ('BOT', 'acct', 'EUR_USD', 'S1', 'shadow-order', 'broker_pending', 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        oanda_trade_id, state, is_simulated
                    )
                    VALUES ('BOT', 'acct', 'EUR_USD', 'S1', 'shadow-trade', 'shadow-oanda-id', 'broker_open', 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO choch_orders (
                        bot_id, account_id, instrument, strategy_leg, client_order_key, state, is_simulated
                    )
                    VALUES ('BOT', 'acct', 'EUR_USD', 'S1', 'real-order', 'broker_pending', 0)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        oanda_trade_id, state, is_simulated
                    )
                    VALUES ('BOT', 'acct', 'EUR_USD', 'S1', 'real-trade', 'real-oanda-id', 'broker_open', 0)
                    """
                )

            self.assertEqual(store.local_pending_order_keys(bot_id="BOT", account_id="acct"), {"real-order"})
            self.assertEqual(store.local_open_trade_ids(bot_id="BOT", account_id="acct"), {"real-oanda-id"})

    def test_existing_phase1_schema_is_migrated_without_losing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE choch_setups (
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
                    client_order_key TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (bot_id, instrument, strategy_leg, client_order_key)
                );
                CREATE TABLE choch_orders (
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
                    UNIQUE (bot_id, instrument, strategy_leg, client_order_key)
                );
                CREATE TABLE choch_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    strategy_leg TEXT NOT NULL,
                    client_order_key TEXT NOT NULL,
                    oanda_trade_id TEXT,
                    state TEXT NOT NULL,
                    entry_time TEXT,
                    entry_price REAL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (bot_id, instrument, strategy_leg, client_order_key)
                );
                CREATE TABLE choch_events (
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
                CREATE TABLE choch_candle_progress (
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    granularity TEXT NOT NULL,
                    last_complete_candle_time TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (bot_id, account_id, instrument, granularity)
                );
                CREATE TABLE choch_reconciliation (
                    bot_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    last_transaction_id TEXT,
                    last_reconciled_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY (bot_id, account_id)
                );
                INSERT INTO choch_trades (
                    bot_id, account_id, instrument, strategy_leg, client_order_key,
                    state, entry_time, entry_price
                )
                VALUES ('BOT', 'acct', 'EUR_USD', 'S1', 'existing', 'filled',
                        '2026-01-01T00:00:00+00:00', 1.1);
                """
            )
            conn.commit()
            conn.close()

            store = ChochStateStore(db_path)
            store.ensure_schema()
            store.ensure_schema()

            with store.session() as conn:
                trade_cols = {row["name"] for row in conn.execute("PRAGMA table_info(choch_trades)").fetchall()}
                setup_cols = {row["name"] for row in conn.execute("PRAGMA table_info(choch_setups)").fetchall()}
                order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(choch_orders)").fetchall()}
                existing = conn.execute("SELECT entry_price FROM choch_trades WHERE client_order_key = 'existing'").fetchone()

            self.assertEqual(existing["entry_price"], 1.1)
            self.assertTrue({"actual_risk_cash", "actual_risk_pct", "margin_required", "realized_r", "is_simulated"} <= trade_cols)
            self.assertTrue({"r_value", "cancellation_reason", "is_simulated"} <= setup_cols)
            self.assertTrue({"actual_risk_cash", "actual_risk_pct", "margin_required", "is_simulated"} <= order_cols)

    def test_upsert_simulated_s1_result_persists_shadow_risk_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            result = filled_s1_result()

            store.upsert_simulated_s1_result(
                bot_id="BOT",
                account_id="acct",
                instrument="EUR_USD",
                result=result,
            )

            with store.session() as conn:
                setup = conn.execute("SELECT * FROM choch_setups WHERE client_order_key = ?", (result.client_order_key,)).fetchone()
                trade = conn.execute("SELECT * FROM choch_trades WHERE client_order_key = ?", (result.client_order_key,)).fetchone()

            self.assertEqual(setup["is_simulated"], 1)
            self.assertEqual(setup["state"], "filled")
            self.assertEqual(setup["client_order_key"], "s1-key")
            self.assertEqual(setup["signal_bar_time"], "2026-01-01T10:00:00+00:00")
            self.assertEqual(setup["entry"], 1.1)
            self.assertEqual(setup["stop"], 1.09)
            self.assertEqual(setup["tp"], 1.115)
            self.assertEqual(trade["is_simulated"], 1)
            self.assertEqual(trade["state"], "filled")
            self.assertEqual(trade["entry_time"], "2026-01-01T11:00:00+00:00")
            self.assertEqual(trade["entry_price"], 1.1)
            self.assertEqual(trade["stop_price"], 1.09)
            self.assertEqual(trade["tp_price"], 1.115)
            self.assertEqual(trade["units"], 249_625)
            self.assertEqual(trade["signed_units"], 249_625.0)
            self.assertEqual(trade["intended_risk_cash"], 2_000.0)
            self.assertEqual(trade["actual_risk_cash"], 1_997.0)
            self.assertEqual(trade["actual_risk_pct"], 0.01997)
            self.assertEqual(trade["margin_required"], 9_153.0)
            self.assertIsNotNone(trade["updated_at"])


if __name__ == "__main__":
    unittest.main()
