from __future__ import annotations

from bots.choch_live.models import InstrumentMetadata
from shared.logging_utils import get_logger


def mask_account_id(account_id: str) -> str:
    parts = str(account_id or "").split("-")
    if len(parts) >= 4 and parts[-1]:
        return f"{parts[0]}-***-***-{parts[-1]}"
    if len(account_id) <= 4:
        return "***"
    return f"***{account_id[-4:]}"


def get_choch_logger():
    return get_logger("bot.choch_live")


def log_startup_validation_passed(
    *,
    environment: str,
    base_url: str,
    account_id: str,
    currency: str,
    instrument_count: int,
    configured_instruments: list[str],
    instrument_metadata: dict[str, InstrumentMetadata],
    trading_enabled: bool,
    dry_run: bool,
) -> None:
    log = get_choch_logger()
    log.info(
        "CHOCH startup validation passed | environment=%s | base_url=%s | account=%s | "
        "currency=%s | instrument_count=%s | configured_instruments=%s | "
        "trading_enabled=%s | dry_run=%s",
        environment,
        base_url,
        mask_account_id(account_id),
        currency,
        instrument_count,
        configured_instruments,
        str(trading_enabled).lower(),
        str(dry_run).lower(),
    )
    for metadata in instrument_metadata.values():
        log.info(
            "CHOCH instrument metadata | key=%s | instrument=%s | type=%s | marginRate=%s | "
            "effective_leverage=%.2f:1 | minimumTradeSize=%s | tradeUnitsPrecision=%s | "
            "pipLocation=%s | displayPrecision=%s",
            metadata.key,
            metadata.name,
            metadata.instrument_type,
            metadata.margin_rate,
            metadata.effective_leverage,
            metadata.minimum_trade_size,
            metadata.trade_units_precision,
            metadata.pip_location,
            metadata.display_precision,
        )
