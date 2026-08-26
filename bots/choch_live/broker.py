from __future__ import annotations

from typing import Any

import pandas as pd

from bots.choch_live.config import ChochConfig
from bots.choch_live.models import AccountMetadata, InstrumentMetadata
from shared.daily_engine import select_latest_complete_candle
from shared.oanda_client import OandaClient


PRACTICE_URL = "https://api-fxpractice.oanda.com"


class ChochBroker:
    def __init__(self, config: ChochConfig) -> None:
        self.config = config
        self.client = OandaClient(
            base_url=config.settings.base_url,
            api_key=config.settings.oanda_api_key,
            account_id=config.account_id,
            bot_id=config.bot_id,
            timeframe=config.s2_timeframe,
        )
        self.account_metadata: AccountMetadata | None = None
        self.available_instruments: set[str] = set()
        self.instrument_metadata: dict[str, InstrumentMetadata] = {}

    def validate_practice_environment(self) -> None:
        if self.config.settings.oanda_env != "practice":
            raise RuntimeError("CHoCH requires OANDA_ENV=practice")
        if self.client.base_url.rstrip("/") != PRACTICE_URL:
            raise RuntimeError("CHoCH requires OANDA practice REST host")
        if self.config.allowed_oanda_env != "practice":
            raise RuntimeError("CHoCH config requires allowed_oanda_env=practice")

    def load_account_metadata(self) -> AccountMetadata:
        summary = self.client.get_account_summary()
        account = summary.get("account", {})
        account_id = str(account.get("id") or "")
        if account_id != self.config.expected_account_id:
            raise RuntimeError("OANDA account summary did not match expected CHoCH account")
        metadata = AccountMetadata(
            account_id=account_id,
            currency=str(account.get("currency", "")),
            last_transaction_id=(
                str(account.get("lastTransactionID"))
                if account.get("lastTransactionID") is not None
                else None
            ),
        )
        self.account_metadata = metadata
        return metadata

    def validate_expected_account(self) -> AccountMetadata:
        if self.config.account_id != self.config.expected_account_id:
            raise RuntimeError("CHoCH account_id and expected account ID differ")
        return self.load_account_metadata()

    def _instrument_metadata(self, *, key: str, item: dict[str, Any]) -> InstrumentMetadata:
        name = str(item.get("name") or "")
        required = {
            "marginRate": item.get("marginRate"),
            "minimumTradeSize": item.get("minimumTradeSize"),
            "tradeUnitsPrecision": item.get("tradeUnitsPrecision"),
            "pipLocation": item.get("pipLocation"),
            "displayPrecision": item.get("displayPrecision"),
            "type": item.get("type"),
        }
        missing = [field for field, value in required.items() if value is None or str(value) == ""]
        if missing:
            raise RuntimeError(f"OANDA instrument metadata missing for {name}: {missing}")
        margin_rate = float(required["marginRate"])
        if margin_rate <= 0:
            raise RuntimeError(f"OANDA instrument metadata invalid for {name}: marginRate must be positive")
        return InstrumentMetadata(
            key=key,
            name=name,
            margin_rate=margin_rate,
            minimum_trade_size=float(required["minimumTradeSize"]),
            trade_units_precision=int(required["tradeUnitsPrecision"]),
            pip_location=int(required["pipLocation"]),
            display_precision=int(required["displayPrecision"]),
            instrument_type=str(required["type"]),
        )

    def load_and_validate_instruments(self) -> dict[str, InstrumentMetadata]:
        data = self.client.get_account_instruments()
        by_name = {str(item.get("name")): item for item in data.get("instruments", []) if item.get("name")}
        available = set(by_name)
        self.available_instruments = available
        missing = [instrument.name for instrument in self.config.instruments if instrument.name not in available]
        if "EUR_USD" not in available:
            raise RuntimeError("OANDA account instruments missing required EUR_USD")
        if missing:
            raise RuntimeError(f"Configured CHoCH instruments not available in OANDA account: {missing}")
        self.instrument_metadata = {
            instrument.name: self._instrument_metadata(key=instrument.key, item=by_name[instrument.name])
            for instrument in self.config.instruments
        }
        return self.instrument_metadata

    def get_complete_candles(
        self,
        *,
        instrument: str,
        granularity: str,
        count: int,
        price: str = "BA",
    ) -> tuple[pd.Timestamp, dict[str, Any], dict[str, Any]]:
        candles = self.client.get_candles(instrument=instrument, granularity=granularity, count=count, price=price)
        decision = select_latest_complete_candle(candles.get("candles", []))
        if decision is None:
            raise RuntimeError(f"No complete {granularity} candle returned for {instrument}")
        latest, candle = decision
        return latest, candle, candles

    def get_pending_orders(self) -> dict[str, Any]:
        return self.client.get_pending_orders()

    def get_open_trades(self) -> dict[str, Any]:
        return self.client.get_open_trades()

    def get_recent_transactions_or_changes(self) -> dict[str, Any]:
        if self.account_metadata and self.account_metadata.last_transaction_id:
            return self.client.get_transactions_since(self.account_metadata.last_transaction_id)
        return {}

    def get_account_nav(self) -> float:
        return self.client.get_account_nav()

    def get_account_risk_state(self) -> dict[str, float]:
        return self.client.get_account_risk_state()

    def get_quote_to_account_rate(self, quote_currency: str) -> float:
        if not self.account_metadata or not self.account_metadata.currency:
            raise RuntimeError("Account metadata must be loaded before conversion lookup")
        account_currency = self.account_metadata.currency.upper()
        quote = quote_currency.upper()
        if quote == account_currency:
            return 1.0

        if not self.available_instruments:
            raise RuntimeError("Account instruments must be loaded before conversion lookup")

        direct_instrument = f"{quote}_{account_currency}"
        if direct_instrument in self.available_instruments:
            direct = self.client.get_price(direct_instrument)
            if direct is None:
                raise RuntimeError(f"No conversion price available for {direct_instrument}")
            # Use the higher side for risk conversion so GBP loss-at-stop is not understated.
            ask = float(direct["ask"])
            if ask <= 0:
                raise RuntimeError(f"Invalid conversion ask for {direct_instrument}")
            return ask

        inverse_instrument = f"{account_currency}_{quote}"
        if inverse_instrument in self.available_instruments:
            inverse = self.client.get_price(inverse_instrument)
            if inverse is None:
                raise RuntimeError(f"No conversion price available for {inverse_instrument}")
            # For ACCOUNT_QUOTE, 1 / bid is the conservative FROM->ACCOUNT conversion.
            bid = float(inverse["bid"])
            if bid <= 0:
                raise RuntimeError(f"Invalid conversion bid for {inverse_instrument}")
            return 1.0 / bid

        raise RuntimeError(f"No conversion price available for {quote}->{account_currency}")

    def place_order(self, *args, **kwargs):
        raise RuntimeError("Broker writes disabled by CHoCH safety gate")

    def replace_order(self, *args, **kwargs):
        raise RuntimeError("Broker writes disabled by CHoCH safety gate")

    def cancel_order(self, *args, **kwargs):
        raise RuntimeError("Broker writes disabled by CHoCH safety gate")

    def close_trade(self, *args, **kwargs):
        raise RuntimeError("Broker writes disabled by CHoCH safety gate")
