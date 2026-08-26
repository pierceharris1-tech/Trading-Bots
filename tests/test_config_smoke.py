from __future__ import annotations

import importlib
import unittest
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

from shared.time_utils import granularity_to_seconds

STRATEGY_MAP = {
    "orb_m15": "bots.orb_m15",
    "channel_breakout_daily": "bots.channel_breakout_daily",
    "conqueror_fx_daily": "bots.conqueror_fx_daily",
    "inside_days_daily": "bots.inside_days_daily",
    "slingshot_daily": "bots.slingshot_daily",
    "gold_5am_breakout": "bots.gold_5am_breakout",
    "choch_live": "bots.choch_live.main",
}


class TestConfigSmoke(unittest.TestCase):
    def test_bots_yaml_timeframes_and_strategy_imports(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML is not installed")
        cfg_path = Path(__file__).resolve().parents[1] / "config" / "bots.yaml"
        with cfg_path.open("r", encoding="utf-8") as f:
            bots = yaml.safe_load(f) or {}

        self.assertIsInstance(bots, dict)
        self.assertTrue(bots)

        for bot_id, cfg in bots.items():
            self.assertIsInstance(cfg, dict, msg=f"Invalid config for bot_id={bot_id}")
            strategy = str(cfg.get("strategy", "")).strip()
            self.assertIn(strategy, STRATEGY_MAP, msg=f"Unknown strategy for bot_id={bot_id}: {strategy}")

            timeframe = str(cfg.get("timeframe", "M15" if strategy == "orb_m15" else "D")).strip().upper()
            granularity_to_seconds(timeframe)

            mod = importlib.import_module(STRATEGY_MAP[strategy])
            self.assertTrue(hasattr(mod, "main"), msg=f"Strategy module missing main(): {STRATEGY_MAP[strategy]}")


if __name__ == "__main__":
    unittest.main()
