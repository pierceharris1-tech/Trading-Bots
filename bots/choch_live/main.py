from __future__ import annotations

from bots.choch_live.broker import ChochBroker
from bots.choch_live.config import load_choch_config
from bots.choch_live.engine import ChochEngine
from bots.choch_live.logging import get_choch_logger, log_startup_validation_passed
from bots.choch_live.models import EventType
from bots.choch_live.state import ChochStateStore


def main(cfg: dict | None = None) -> None:
    raw_cfg = cfg or {}
    log = get_choch_logger()
    state = ChochStateStore()
    state.ensure_schema()

    try:
        config = load_choch_config(raw_cfg)
        broker = ChochBroker(config)
        broker.validate_practice_environment()
        account = broker.validate_expected_account()
        instrument_metadata = broker.load_and_validate_instruments()
        state.start_run(bot_id=config.bot_id, account_id=config.account_id, environment=config.settings.oanda_env)
        state.record_event(
            bot_id=config.bot_id,
            account_id=config.account_id,
            event_type=EventType.STARTUP_VALIDATION_PASSED.value,
        )
        log_startup_validation_passed(
            environment=config.settings.oanda_env,
            base_url=config.settings.base_url,
            account_id=config.account_id,
            currency=account.currency,
            instrument_count=len(instrument_metadata),
            configured_instruments=list(config.instrument_names),
            instrument_metadata=instrument_metadata,
            trading_enabled=config.trading_enabled,
            dry_run=config.dry_run,
        )
    except Exception as exc:
        bot_id = str(raw_cfg.get("bot_id", "UNKNOWN_BOT"))
        account_id = str(raw_cfg.get("account_id", "UNKNOWN_ACCOUNT"))
        state.record_event(
            bot_id=bot_id,
            account_id=account_id,
            event_type=EventType.STARTUP_VALIDATION_FAILED.value,
            payload={"error": str(exc)},
        )
        log.error("CHOCH_STARTUP_VALIDATION_FAILED | err=%s", exc)
        raise

    engine = ChochEngine(config=config, broker=broker, state=state)
    engine.reconcile_read_only()
    engine.run_forever()
