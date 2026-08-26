from __future__ import annotations

import time
from typing import Any

import pandas as pd

from bots.choch_live.broker import ChochBroker
from bots.choch_live.config import ChochConfig
from bots.choch_live.logging import get_choch_logger
from bots.choch_live.models import EventType
from bots.choch_live.state import ChochStateStore
from shared.strategy_utils import bid_ask_candles_to_df
from shared.time_utils import seconds_until_next_close


class ChochEngine:
    def __init__(self, *, config: ChochConfig, broker: ChochBroker, state: ChochStateStore) -> None:
        self.config = config
        self.broker = broker
        self.state = state
        self.log = get_choch_logger()

    def reconcile_read_only(self) -> dict[str, Any]:
        pending = self.broker.get_pending_orders().get("orders", [])
        open_trades = self.broker.get_open_trades().get("trades", [])
        changes = self.broker.get_recent_transactions_or_changes()
        last_tx = None
        if self.broker.account_metadata:
            last_tx = self.broker.account_metadata.last_transaction_id

        local_pending = self.state.local_pending_order_keys(
            bot_id=self.config.bot_id, account_id=self.config.account_id
        )
        broker_pending = {
            str((order.get("clientExtensions") or {}).get("id") or order.get("id"))
            for order in pending
            if str(order.get("instrument", "")) in self.config.instrument_names
        }
        local_open = self.state.local_open_trade_ids(bot_id=self.config.bot_id, account_id=self.config.account_id)
        broker_open = {
            str(trade.get("id"))
            for trade in open_trades
            if str(trade.get("instrument", "")) in self.config.instrument_names
        }

        mismatches: list[str] = []
        for key in sorted(local_pending - broker_pending):
            mismatches.append(EventType.LOCAL_PENDING_BROKER_MISSING.value)
            self.log.warning("LOCAL_PENDING_BROKER_MISSING | client_order_key=%s", key)
        for key in sorted(broker_pending - local_pending):
            mismatches.append(EventType.BROKER_PENDING_LOCAL_MISSING.value)
            self.log.warning("BROKER_PENDING_LOCAL_MISSING | broker_key=%s", key)
        for trade_id in sorted(local_open - broker_open):
            mismatches.append(EventType.LOCAL_OPEN_BROKER_MISSING.value)
            self.log.warning("LOCAL_OPEN_BROKER_MISSING | trade_id=%s", trade_id)
        for trade_id in sorted(broker_open - local_open):
            mismatches.append(EventType.BROKER_OPEN_LOCAL_MISSING.value)
            self.log.warning("BROKER_OPEN_LOCAL_MISSING | trade_id=%s", trade_id)

        status = "ok" if not mismatches else "mismatch"
        self.state.upsert_reconciliation(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            last_transaction_id=last_tx,
            status=status,
        )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            event_type=EventType.RECONCILIATION_COMPLETE.value,
            payload={
                "status": status,
                "pending_orders": len(pending),
                "open_trades": len(open_trades),
                "mismatches": mismatches,
                "changes_keys": sorted(changes.keys()),
            },
        )
        return {"status": status, "mismatches": mismatches}

    def process_once(self) -> None:
        self.reconcile_read_only()
        for instrument in self.config.instrument_names:
            self._process_timeframe(instrument=instrument, granularity=self.config.s2_timeframe, count=300)
            self._process_timeframe(instrument=instrument, granularity=self.config.s1_timeframe, count=300)

    def _process_timeframe(self, *, instrument: str, granularity: str, count: int) -> None:
        latest, _candle, payload = self.broker.get_complete_candles(
            instrument=instrument,
            granularity=granularity,
            count=count,
            price="BA",
        )
        last = self.state.get_last_candle(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            granularity=granularity,
        )
        if last and latest <= pd.Timestamp(last):
            return
        df = bid_ask_candles_to_df(payload)
        latest_row = df.loc[latest]
        bid_ok = not pd.isna(latest_row.get("bid_close"))
        ask_ok = not pd.isna(latest_row.get("ask_close"))
        self.state.set_last_candle(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            granularity=granularity,
            candle_time=latest.isoformat(),
        )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            event_type=EventType.CANDLE_PROCESSED.value,
            payload={"granularity": granularity, "candle_time": latest.isoformat(), "bid": bid_ok, "ask": ask_ok},
        )
        self.log.info(
            "CHOCH_CANDLE_PROCESSED | instrument=%s | timeframe=%s | candle_time=%s | bid=%s | ask=%s",
            instrument,
            granularity,
            latest.isoformat(),
            bid_ok,
            ask_ok,
        )

    def run_forever(self) -> None:
        while True:
            try:
                time.sleep(seconds_until_next_close(self.config.s2_timeframe) + 1)
                self.process_once()
            except Exception as exc:
                self.log.exception("CHOCH_ENGINE_ERROR | err=%s", exc)
                time.sleep(5)
