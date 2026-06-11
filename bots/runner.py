import sys
import yaml
import importlib
from pathlib import Path

CONFIG_PATH = Path("/home/ubuntu/trading-bots/config/bots.yaml")

# ORB-only strategy map.
STRATEGY_MAP = {
    "orb_m15": "bots.orb_m15",
    "channel_breakout_daily": "bots.channel_breakout_daily",
    "conqueror_fx_daily": "bots.conqueror_fx_daily",
    "inside_days_daily": "bots.inside_days_daily",
    "slingshot_daily": "bots.slingshot_daily",
    "gold_5am_breakout": "bots.gold_5am_breakout",
}


def _default_timeframe_for_strategy(strategy: str) -> str:
    return "M15" if strategy == "orb_m15" else "D"


def main():
    if len(sys.argv) < 2:
        raise SystemExit("No bot_id provided. Usage: python -m bots.runner <BOT_ID>")

    bot_id = sys.argv[1]

    with open(CONFIG_PATH, "r") as f:
        bots = yaml.safe_load(f) or {}

    if bot_id not in bots:
        raise SystemExit(f"Unknown bot_id: {bot_id}")

    cfg = bots[bot_id]
    cfg = dict(cfg)
    cfg["bot_id"] = bot_id

    strategy = str(cfg.get("strategy", "")).strip()
    if strategy not in STRATEGY_MAP:
        raise SystemExit(f"Unknown strategy '{strategy}'. Options: {list(STRATEGY_MAP.keys())}")

    raw_timeframe = str(cfg.get("timeframe", "")).strip().upper()
    cfg["timeframe"] = raw_timeframe or _default_timeframe_for_strategy(strategy)

    mod_path = STRATEGY_MAP[strategy]
    mod = importlib.import_module(mod_path)

    if not hasattr(mod, "main"):
        raise SystemExit(f"Module {mod_path} has no main()")

    mod.main(cfg)


if __name__ == "__main__":
    main()
