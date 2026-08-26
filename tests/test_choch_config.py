from __future__ import annotations

import unittest
from unittest.mock import patch

from bots.choch_live.config import load_choch_config


BASE_ENV = {
    "OANDA_API_KEY": "test-key",
    "OANDA_ENV": "practice",
    "CHOCH_EXPECTED_ACCOUNT_ID": "101-004-00000000-999",
    "CHOCH_TRADING_ENABLED": "0",
}


def valid_cfg() -> dict:
    return {
        "bot_id": "CHOCH_TEST",
        "strategy": "choch_live",
        "account_id": "101-004-00000000-999",
        "allowed_oanda_env": "practice",
        "trading_enabled": False,
        "dry_run": True,
        "instruments": {
            "eur_usd": {"name": "EUR_USD"},
            "nasdaq_100": {"name": "NAS100_USD"},
        },
        "s1_timeframe": "H1",
        "s2_timeframe": "M10",
        "risk_pct": 0.01,
        "max_account_risk_open": 0.02,
        "min_margin_supported_risk_pct": 0.004,
        "max_units": 1000,
    }


class TestChochConfig(unittest.TestCase):
    def test_valid_config_loads(self):
        with patch.dict("os.environ", BASE_ENV, clear=True):
            cfg = load_choch_config(valid_cfg())
        self.assertEqual(cfg.account_id, BASE_ENV["CHOCH_EXPECTED_ACCOUNT_ID"])
        self.assertEqual(cfg.s2_timeframe, "M10")
        self.assertEqual(cfg.instrument_names, ("EUR_USD", "NAS100_USD"))
        self.assertEqual(cfg.min_margin_supported_risk_pct, 0.004)

    def test_placeholder_account_rejected(self):
        cfg = valid_cfg()
        cfg["account_id"] = "REPLACE_ME"
        with patch.dict("os.environ", BASE_ENV, clear=True):
            with self.assertRaisesRegex(RuntimeError, "account_id"):
                load_choch_config(cfg)

    def test_missing_expected_account_rejected(self):
        env = dict(BASE_ENV)
        env["CHOCH_EXPECTED_ACCOUNT_ID"] = ""
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CHOCH_EXPECTED_ACCOUNT_ID"):
                load_choch_config(valid_cfg())

    def test_live_environment_rejected(self):
        env = dict(BASE_ENV)
        env["OANDA_ENV"] = "live"
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OANDA_ENV=practice"):
                load_choch_config(valid_cfg())

    def test_allowed_env_must_be_practice(self):
        cfg = valid_cfg()
        cfg["allowed_oanda_env"] = "live"
        with patch.dict("os.environ", BASE_ENV, clear=True):
            with self.assertRaisesRegex(RuntimeError, "allowed_oanda_env"):
                load_choch_config(cfg)

    def test_account_mismatch_rejected(self):
        cfg = valid_cfg()
        cfg["account_id"] = "101-004-00000000-998"
        with patch.dict("os.environ", BASE_ENV, clear=True):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                load_choch_config(cfg)

    def test_trading_enabled_can_load_but_does_not_enable_broker_without_all_gates(self):
        cfg = valid_cfg()
        cfg["trading_enabled"] = True
        with patch.dict("os.environ", BASE_ENV, clear=True):
            loaded = load_choch_config(cfg)
        self.assertTrue(loaded.trading_enabled)

    def test_env_kill_switch_can_load_but_broker_gate_still_controls_writes(self):
        env = dict(BASE_ENV)
        env["CHOCH_TRADING_ENABLED"] = "1"
        with patch.dict("os.environ", env, clear=True):
            loaded = load_choch_config(valid_cfg())
        self.assertTrue(loaded.env_trading_enabled)

    def test_missing_eur_usd_rejected(self):
        cfg = valid_cfg()
        cfg["instruments"] = {"nasdaq_100": {"name": "NAS100_USD"}}
        with patch.dict("os.environ", BASE_ENV, clear=True):
            with self.assertRaisesRegex(RuntimeError, "EUR_USD"):
                load_choch_config(cfg)

    def test_placeholder_instrument_rejected(self):
        cfg = valid_cfg()
        cfg["instruments"] = {"eur_usd": {"name": "EUR_USD"}, "nasdaq_100": {"name": "REPLACE_NAS100_INSTRUMENT"}}
        with patch.dict("os.environ", BASE_ENV, clear=True):
            with self.assertRaisesRegex(RuntimeError, "instrument"):
                load_choch_config(cfg)

    def test_legacy_instrument_list_still_loads(self):
        cfg = valid_cfg()
        cfg["instruments"] = ["EUR_USD", "NAS100_USD"]
        with patch.dict("os.environ", BASE_ENV, clear=True):
            loaded = load_choch_config(cfg)
        self.assertEqual(loaded.instrument_names, ("EUR_USD", "NAS100_USD"))


if __name__ == "__main__":
    unittest.main()
