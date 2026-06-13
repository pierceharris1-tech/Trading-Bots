from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.config import load_settings


BASE_ENV = {
    "OANDA_API_KEY": "test-key-abc",
    "OANDA_ENV": "practice",
}


class TestLoadSettingsPractice(unittest.TestCase):
    def test_practice_env_hits_fxpractice_url(self):
        with patch.dict("os.environ", {**BASE_ENV, "OANDA_ENV": "practice"}, clear=True):
            s = load_settings()
        self.assertEqual(s.oanda_env, "practice")
        self.assertIn("fxpractice", s.base_url)
        self.assertNotIn("fxtrade", s.base_url)

    def test_live_env_hits_fxtrade_url(self):
        with patch.dict("os.environ", {**BASE_ENV, "OANDA_ENV": "live"}, clear=True):
            s = load_settings()
        self.assertEqual(s.oanda_env, "live")
        self.assertIn("fxtrade", s.base_url)
        self.assertNotIn("fxpractice", s.base_url)

    def test_missing_api_key_raises(self):
        with patch.dict("os.environ", {"OANDA_ENV": "practice"}, clear=True):
            with self.assertRaises(RuntimeError):
                load_settings()

    def test_invalid_oanda_env_raises(self):
        with patch.dict("os.environ", {**BASE_ENV, "OANDA_ENV": "staging"}, clear=True):
            with self.assertRaises(RuntimeError):
                load_settings()

    def test_dry_run_truthy_values(self):
        for val in ("1", "true", "True", "yes", "YES", "on"):
            with self.subTest(val=val):
                with patch.dict("os.environ", {**BASE_ENV, "DRY_RUN": val}, clear=True):
                    s = load_settings()
                self.assertTrue(s.dry_run, msg=f"DRY_RUN={val!r} should be truthy")

    def test_dry_run_falsy_values(self):
        for val in ("0", "false", "False", "no", "off"):
            with self.subTest(val=val):
                with patch.dict("os.environ", {**BASE_ENV, "DRY_RUN": val}, clear=True):
                    s = load_settings()
                self.assertFalse(s.dry_run, msg=f"DRY_RUN={val!r} should be falsy")

    def test_risk_pct_parsed(self):
        with patch.dict("os.environ", {**BASE_ENV, "RISK_PCT": "0.02"}, clear=True):
            s = load_settings()
        self.assertAlmostEqual(s.risk_pct, 0.02)

    def test_invalid_risk_pct_raises(self):
        with patch.dict("os.environ", {**BASE_ENV, "RISK_PCT": "notanumber"}, clear=True):
            with self.assertRaises(RuntimeError):
                load_settings()

    def test_defaults_are_safe(self):
        # With minimal env, dry_run defaults to True (safe) and env defaults to practice
        with patch.dict("os.environ", {"OANDA_API_KEY": "k"}, clear=True):
            s = load_settings()
        self.assertEqual(s.oanda_env, "practice")
        self.assertTrue(s.dry_run)

    def test_practice_and_live_urls_are_different(self):
        with patch.dict("os.environ", {**BASE_ENV, "OANDA_ENV": "practice"}, clear=True):
            practice = load_settings()
        with patch.dict("os.environ", {**BASE_ENV, "OANDA_ENV": "live"}, clear=True):
            live = load_settings()
        self.assertNotEqual(practice.base_url, live.base_url)


class TestServiceTierAccountSeparation(unittest.TestCase):
    """
    Verifies that the service files reference different env files, and that
    the practice account ID from bots.yaml (101-004-27287318-*) is never
    the live account (001-004-10576609-001).
    """

    def test_practice_service_loads_practice_env_file(self):
        import re
        from pathlib import Path

        svc = Path(__file__).resolve().parents[1] / "deploy" / "trading-bot-practice@.service"
        self.assertTrue(svc.exists(), "deploy/trading-bot-practice@.service missing from repo")
        content = svc.read_text()
        match = re.search(r"EnvironmentFile\s*=\s*(.+)", content)
        self.assertIsNotNone(match, "No EnvironmentFile directive found")
        env_file = match.group(1).strip()
        self.assertIn("practice", env_file,
                      f"Practice service should reference a practice env file, got: {env_file}")
        self.assertNotEqual(env_file, "/etc/qtp/oanda.env",
                            "Practice service must NOT load the live env file")

    def test_live_service_loads_live_env_file(self):
        import re
        from pathlib import Path

        svc = Path(__file__).resolve().parents[1] / "deploy" / "trading-bot@.service"
        self.assertTrue(svc.exists(), "deploy/trading-bot@.service missing from repo")
        content = svc.read_text()
        match = re.search(r"EnvironmentFile\s*=\s*(.+)", content)
        self.assertIsNotNone(match)
        env_file = match.group(1).strip()
        self.assertNotIn("practice", env_file,
                         "Live service should NOT reference the practice env file")

    def test_bots_yaml_practice_account_ids_never_live(self):
        import yaml
        from pathlib import Path

        LIVE_ACCOUNT = "001-004-10576609-001"
        cfg_path = Path(__file__).resolve().parents[1] / "config" / "bots.yaml"
        bots = yaml.safe_load(cfg_path.read_text()) or {}
        # Practice account IDs have prefix 101-004
        for bot_id, cfg in bots.items():
            account_id = cfg.get("account_id", "")
            if str(account_id).startswith("101-"):
                self.assertNotEqual(
                    str(account_id), LIVE_ACCOUNT,
                    f"Bot {bot_id} has a practice-prefix account ID equal to live account",
                )


if __name__ == "__main__":
    unittest.main()
