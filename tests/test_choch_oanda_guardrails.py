from __future__ import annotations

import unittest
from dataclasses import replace

from bots.choch_live.broker import ChochBroker
from bots.choch_live.config import ChochConfig
from bots.choch_live.models import InstrumentConfig
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
        max_units=1000,
        settings=settings,
    )


class FakeClient:
    def __init__(self, *, account_id="101-004-00000000-999", instruments=None):
        self.base_url = "https://api-fxpractice.oanda.com"
        self._account_id = account_id
        self._instruments = instruments or ["EUR_USD", "NAS100_USD"]

    def get_account_summary(self):
        return {"account": {"id": self._account_id, "currency": "GBP", "lastTransactionID": "42"}}

    def get_account_instruments(self):
        defaults = {
            "EUR_USD": {
                "name": "EUR_USD",
                "marginRate": "0.03333333333333",
                "minimumTradeSize": "1",
                "tradeUnitsPrecision": 0,
                "pipLocation": -4,
                "displayPrecision": 5,
                "type": "CURRENCY",
            },
            "NAS100_USD": {
                "name": "NAS100_USD",
                "marginRate": "0.05",
                "minimumTradeSize": "0.1",
                "tradeUnitsPrecision": 1,
                "pipLocation": -1,
                "displayPrecision": 1,
                "type": "CFD",
            },
            "GBP_USD": {
                "name": "GBP_USD",
                "marginRate": "0.03333333333333",
                "minimumTradeSize": "1",
                "tradeUnitsPrecision": 0,
                "pipLocation": -4,
                "displayPrecision": 5,
                "type": "CURRENCY",
            },
        }
        items = [defaults[name] if isinstance(name, str) else name for name in self._instruments]
        return {"instruments": items}


class TestChochOandaGuardrails(unittest.TestCase):
    def test_practice_environment_accepted(self):
        broker = ChochBroker(config())
        broker.validate_practice_environment()

    def test_wrong_base_url_rejected(self):
        cfg = replace(config(), settings=replace(config().settings, base_url="https://api-fxtrade.oanda.com"))
        broker = ChochBroker(cfg)
        with self.assertRaisesRegex(RuntimeError, "practice REST host"):
            broker.validate_practice_environment()

    def test_account_summary_mismatch_rejected(self):
        broker = ChochBroker(config())
        broker.client = FakeClient(account_id="101-004-00000000-998")
        with self.assertRaisesRegex(RuntimeError, "did not match"):
            broker.validate_expected_account()

    def test_account_summary_match_accepted(self):
        broker = ChochBroker(config())
        broker.client = FakeClient()
        metadata = broker.validate_expected_account()
        self.assertEqual(metadata.currency, "GBP")
        self.assertEqual(metadata.last_transaction_id, "42")

    def test_missing_eur_usd_rejected(self):
        broker = ChochBroker(config())
        broker.client = FakeClient(instruments=["NAS100_USD"])
        with self.assertRaisesRegex(RuntimeError, "EUR_USD"):
            broker.load_and_validate_instruments()

    def test_configured_nas_missing_rejected(self):
        broker = ChochBroker(config())
        broker.client = FakeClient(instruments=["EUR_USD"])
        with self.assertRaisesRegex(RuntimeError, "not available"):
            broker.load_and_validate_instruments()

    def test_valid_instruments_accepted(self):
        broker = ChochBroker(config())
        broker.client = FakeClient(instruments=["EUR_USD", "NAS100_USD", "GBP_USD"])
        metadata = broker.load_and_validate_instruments()
        self.assertIn("NAS100_USD", metadata)
        self.assertAlmostEqual(metadata["EUR_USD"].effective_leverage, 30.0, places=6)
        self.assertAlmostEqual(metadata["NAS100_USD"].effective_leverage, 20.0, places=6)

    def test_missing_required_metadata_rejected(self):
        broker = ChochBroker(config())
        broker.client = FakeClient(
            instruments=[
                {
                    "name": "EUR_USD",
                    "minimumTradeSize": "1",
                    "tradeUnitsPrecision": 0,
                    "pipLocation": -4,
                    "displayPrecision": 5,
                    "type": "CURRENCY",
                },
                "NAS100_USD",
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "metadata missing"):
            broker.load_and_validate_instruments()

    def test_phase1_write_methods_raise(self):
        broker = ChochBroker(config())
        for method_name in ("place_order", "replace_order", "cancel_order", "close_trade"):
            with self.subTest(method=method_name):
                with self.assertRaisesRegex(RuntimeError, "Broker writes disabled"):
                    getattr(broker, method_name)()


if __name__ == "__main__":
    unittest.main()
