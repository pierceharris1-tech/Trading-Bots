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


if __name__ == "__main__":
    unittest.main()
