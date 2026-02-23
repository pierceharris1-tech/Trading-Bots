import os
from dataclasses import dataclass


def _getenv_required(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _truthy(val: str | None) -> bool:
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Settings:
    oanda_api_key: str
    oanda_env: str  # practice | live
    base_url: str

    instrument: str
    granularity: str
    dry_run: bool
    risk_pct: float


def load_settings() -> Settings:
    # Required
    api_key = _getenv_required("OANDA_API_KEY")

    env = os.getenv("OANDA_ENV", "practice").strip().lower()
    if env not in ("practice", "live"):
        raise RuntimeError("OANDA_ENV must be 'practice' or 'live'")

    base_url = (
        "https://api-fxpractice.oanda.com" if env == "practice" else "https://api-fxtrade.oanda.com"
    )

    # Bot defaults
    instrument = os.getenv("INSTRUMENT", "GBP_JPY").strip()
    granularity = os.getenv("GRANULARITY", "M15").strip().upper()
    dry_run = _truthy(os.getenv("DRY_RUN", "1"))

    try:
        risk_pct = float(os.getenv("RISK_PCT", "0.25"))
    except ValueError:
        raise RuntimeError("RISK_PCT must be a number")

    return Settings(
        oanda_api_key=api_key,
        oanda_env=env,
        base_url=base_url,
        instrument=instrument,
        granularity=granularity,
        dry_run=dry_run,
        risk_pct=risk_pct,
    )
