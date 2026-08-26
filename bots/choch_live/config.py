from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from bots.choch_live.models import InstrumentConfig
from shared.config import Settings, load_settings
from shared.time_utils import granularity_to_seconds


PLACEHOLDERS = {"", "REPLACE_ME", "REPLACE_NAS100_INSTRUMENT", "NONE", "NULL"}


@dataclass(frozen=True)
class ChochConfig:
    bot_id: str
    account_id: str
    expected_account_id: str
    allowed_oanda_env: str
    trading_enabled: bool
    env_trading_enabled: bool
    dry_run: bool
    instruments: tuple[InstrumentConfig, ...]
    s1_timeframe: str
    s2_timeframe: str
    risk_pct: float
    max_account_risk_open: float
    min_margin_supported_risk_pct: float
    max_units: int
    settings: Settings

    @property
    def instrument_names(self) -> tuple[str, ...]:
        return tuple(instrument.name for instrument in self.instruments)


def _bool_cfg(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_truthy(key: str) -> bool:
    return str(os.getenv(key, "0")).strip().lower() in {"1", "true", "yes", "y", "on"}


def _require_non_placeholder(value: str, name: str) -> str:
    cleaned = str(value or "").strip()
    if cleaned in PLACEHOLDERS or cleaned.startswith("REPLACE_"):
        raise RuntimeError(f"CHoCH config invalid: {name} must be configured")
    return cleaned


def _parse_instruments(raw: Any) -> tuple[InstrumentConfig, ...]:
    if isinstance(raw, dict) and raw:
        parsed: list[InstrumentConfig] = []
        for key, value in raw.items():
            alias = _require_non_placeholder(str(key), "instrument key")
            max_units = None
            if isinstance(value, dict):
                name = _require_non_placeholder(str(value.get("name", "")), f"instrument {alias}.name")
                if value.get("max_units") is not None:
                    max_units = int(value["max_units"])
                    if max_units <= 0:
                        raise RuntimeError(f"CHoCH config invalid: instrument {alias}.max_units must be positive")
            else:
                name = _require_non_placeholder(str(value), f"instrument {alias}")
            parsed.append(InstrumentConfig(key=alias, name=name, max_units=max_units))
        return tuple(parsed)

    if isinstance(raw, list) and raw:
        return tuple(
            InstrumentConfig(
                key=_require_non_placeholder(str(x), "instrument"),
                name=_require_non_placeholder(str(x), "instrument"),
            )
            for x in raw
        )

    raise RuntimeError("CHoCH config invalid: instruments must be a non-empty mapping or list")


def load_choch_config(cfg: dict[str, Any]) -> ChochConfig:
    settings = load_settings()
    bot_id = _require_non_placeholder(str(cfg.get("bot_id", "")), "bot_id")
    account_id = _require_non_placeholder(str(cfg.get("account_id", "")), "account_id")
    expected_account_id = _require_non_placeholder(
        os.getenv("CHOCH_EXPECTED_ACCOUNT_ID", ""), "CHOCH_EXPECTED_ACCOUNT_ID"
    )

    allowed_env = str(cfg.get("allowed_oanda_env", "")).strip().lower()
    if allowed_env != "practice":
        raise RuntimeError("CHoCH Phase 1 requires allowed_oanda_env=practice")
    if settings.oanda_env != "practice":
        raise RuntimeError("CHoCH Phase 1 refuses to start unless OANDA_ENV=practice")
    if settings.base_url.rstrip("/") != "https://api-fxpractice.oanda.com":
        raise RuntimeError("CHoCH Phase 1 requires OANDA practice REST host")
    if account_id != expected_account_id:
        raise RuntimeError("CHoCH configured account_id does not match CHOCH_EXPECTED_ACCOUNT_ID")

    trading_enabled = _bool_cfg(cfg.get("trading_enabled"), default=False)
    env_trading_enabled = _env_truthy("CHOCH_TRADING_ENABLED")

    dry_run = _bool_cfg(cfg.get("dry_run"), default=True)

    instruments = _parse_instruments(cfg.get("instruments"))
    instrument_names = tuple(instrument.name for instrument in instruments)
    if "EUR_USD" not in instrument_names:
        raise RuntimeError("CHoCH config invalid: instruments must include EUR_USD")

    s1_timeframe = str(cfg.get("s1_timeframe", "")).strip().upper()
    s2_timeframe = str(cfg.get("s2_timeframe", "")).strip().upper()
    if s1_timeframe != "H1":
        raise RuntimeError("CHoCH config invalid: s1_timeframe must be H1")
    if s2_timeframe != "M10":
        raise RuntimeError("CHoCH config invalid: s2_timeframe must be M10")
    granularity_to_seconds(s1_timeframe)
    granularity_to_seconds(s2_timeframe)

    risk_pct = float(cfg.get("risk_pct", 0.01))
    max_account_risk_open = float(cfg.get("max_account_risk_open", 0.02))
    min_margin_supported_risk_pct = float(cfg.get("min_margin_supported_risk_pct", 0.004))
    max_units = int(cfg.get("max_units", 1000))
    if not (0 < risk_pct <= 0.05):
        raise RuntimeError("CHoCH config invalid: risk_pct must be > 0 and <= 0.05")
    if not (0 < max_account_risk_open <= 0.20):
        raise RuntimeError("CHoCH config invalid: max_account_risk_open must be > 0 and <= 0.20")
    if not (0 < min_margin_supported_risk_pct <= risk_pct):
        raise RuntimeError(
            "CHoCH config invalid: min_margin_supported_risk_pct must be > 0 and <= risk_pct"
        )
    if max_units <= 0:
        raise RuntimeError("CHoCH config invalid: max_units must be positive")
    for instrument in instruments:
        if instrument.max_units is None:
            continue
        if instrument.max_units <= 0:
            raise RuntimeError(f"CHoCH config invalid: instrument {instrument.key}.max_units must be positive")

    return ChochConfig(
        bot_id=bot_id,
        account_id=account_id,
        expected_account_id=expected_account_id,
        allowed_oanda_env=allowed_env,
        trading_enabled=trading_enabled,
        env_trading_enabled=env_trading_enabled,
        dry_run=dry_run,
        instruments=instruments,
        s1_timeframe=s1_timeframe,
        s2_timeframe=s2_timeframe,
        risk_pct=risk_pct,
        max_account_risk_open=max_account_risk_open,
        min_margin_supported_risk_pct=min_margin_supported_risk_pct,
        max_units=max_units,
        settings=settings,
    )
