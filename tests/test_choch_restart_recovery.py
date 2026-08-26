from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bots.choch_live.engine import ChochEngine
from bots.choch_live.config import ChochConfig
from bots.choch_live.models import AccountMetadata, InstrumentConfig
from bots.choch_live.state import ChochStateStore
from shared.config import Settings


def config() -> ChochConfig:
    settings = Settings(
        oanda_api_key="k",
        oanda_env="practice",
        base_url="https://api-fxpractice.oanda.com",
        instrument="EUR_USD",
        granularity="M10",
        dry_run=True,
        risk_pct=0.01,
    )
    return ChochConfig(
        bot_id="CHOCH_TEST",
        account_id="101-004-00000000-999",
        expected_account_id="101-004-00000000-999",
        allowed_oanda_env="practice",
        trading_enabled=False,
        env_trading_enabled=False,
        dry_run=True,
        instruments=(
            InstrumentConfig(key="eur_usd", name="EUR_USD"),
            InstrumentConfig(key="nasdaq_100", name="NAS100_USD"),
        ),
        s1_timeframe="H1",
        s2_timeframe="M10",
        risk_pct=0.01,
        max_account_risk_open=0.02,
        min_margin_supported_risk_pct=0.004,
        max_units=1000,
        settings=settings,
    )


class FakeBroker:
    def __init__(self):
        self.account_metadata = AccountMetadata(
            account_id="101-004-00000000-999",
            currency="GBP",
            last_transaction_id="42",
        )
        self.write_calls = 0

    def get_pending_orders(self):
        return {
            "orders": [
                {
                    "id": "broker-order-1",
                    "instrument": "EUR_USD",
                    "clientExtensions": {"id": "broker-key-1"},
                }
            ]
        }

    def get_open_trades(self):
        return {"trades": [{"id": "broker-trade-1", "instrument": "EUR_USD"}]}

    def get_recent_transactions_or_changes(self):
        return {"changes": [], "state": {}}

    def cancel_order(self, *args, **kwargs):
        self.write_calls += 1


class TestChochRestartRecovery(unittest.TestCase):
    def test_reconciliation_logs_mismatches_without_broker_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            with store.session() as conn:
                conn.execute(
                    """
                    INSERT INTO choch_orders (
                        bot_id, account_id, instrument, strategy_leg, client_order_key, state
                    )
                    VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1', 'local-key-1', 'broker_pending')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key, oanda_trade_id, state
                    )
                    VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1', 'trade-key-1', 'local-trade-1', 'broker_open')
                    """
                )

            broker = FakeBroker()
            engine = ChochEngine(config=config(), broker=broker, state=store)
            result = engine.reconcile_read_only()

            self.assertEqual(result["status"], "mismatch")
            self.assertIn("LOCAL_PENDING_BROKER_MISSING", result["mismatches"])
            self.assertIn("BROKER_PENDING_LOCAL_MISSING", result["mismatches"])
            self.assertIn("LOCAL_OPEN_BROKER_MISSING", result["mismatches"])
            self.assertIn("BROKER_OPEN_LOCAL_MISSING", result["mismatches"])
            self.assertEqual(broker.write_calls, 0)


if __name__ == "__main__":
    unittest.main()
