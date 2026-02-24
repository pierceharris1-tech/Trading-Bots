from __future__ import annotations

from shared.time_utils import granularity_to_seconds


def require_account_id(cfg: dict) -> str:
    account_id = str(cfg.get("account_id", "")).strip()
    if not account_id:
        bot_id = str(cfg.get("bot_id", "UNKNOWN_BOT"))
        raise RuntimeError(f"Missing required 'account_id' for bot_id={bot_id}")
    return account_id


def cfg_instruments(cfg: dict, default_instrument: str) -> list[str]:
    val = cfg.get("instruments")
    if isinstance(val, list):
        out = [str(x).strip() for x in val if str(x).strip()]
        if out:
            return out

    single = str(cfg.get("instrument", default_instrument)).strip()
    if not single:
        raise RuntimeError("No instrument configured")
    return [single]


def cfg_timeframe(cfg: dict, default_timeframe: str = "D") -> str:
    timeframe = str(cfg.get("timeframe", default_timeframe)).strip().upper()
    if not timeframe:
        timeframe = default_timeframe
    granularity_to_seconds(timeframe)
    return timeframe
