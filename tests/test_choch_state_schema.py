from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bots.choch_live.state import ChochStateStore


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


if __name__ == "__main__":
    unittest.main()
