from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pandas as pd

from bots.choch_live.broker import ChochBroker
from bots.choch_live.config import ChochConfig
from bots.choch_live.logging import get_choch_logger
from bots.choch_live.models import EventType
from bots.choch_live.risk import (
    assess_aggregate_risk_admission,
    constrain_shadow_position_to_margin,
    quote_currency,
    size_shadow_position,
)
from bots.choch_live.s1_structure import S1Result, simulate_s1
from bots.choch_live.s2_continuation import S2_DECISION_BAR, S2_MAX_HOLD_MINUTES, S2_TF_MINUTES, S2Result, simulate_s2_from_s1
from bots.choch_live.state import ChochStateStore
from shared.strategy_utils import bid_ask_candles_to_df
from shared.time_utils import seconds_until_next_close


class ChochEngine:
    def __init__(self, *, config: ChochConfig, broker: ChochBroker, state: ChochStateStore) -> None:
        self.config = config
        self.broker = broker
        self.state = state
        self.log = get_choch_logger()
        self._latest_complete_candle: dict[tuple[str, str], pd.Timestamp] = {}
        self._reconciliation_ok = False
        self._submission_block_reason: str | None = "reconciliation_not_run"

    def reconcile_read_only(self) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        processed_tx: list[str] = []
        last_tx = self._last_processed_transaction_id()
        try:
            if last_tx is not None and hasattr(self.broker, "get_transactions_since"):
                changes = self.broker.get_transactions_since(last_tx)
            else:
                changes = self.broker.get_recent_transactions_or_changes()
            transactions = self._ordered_transactions(changes)
            for tx in transactions:
                self._process_transaction(tx)
                tx_id = self._transaction_id(tx)
                if tx_id is not None:
                    processed_tx.append(tx_id)
            if processed_tx:
                last_tx = processed_tx[-1]
            if last_tx is None and self.broker.account_metadata:
                last_tx = self.broker.account_metadata.last_transaction_id
            self.state.upsert_reconciliation(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                last_transaction_id=last_tx,
                status="transactions_ok",
            )
        except Exception as exc:
            self._reconciliation_ok = False
            self._submission_block_reason = "transaction_reconciliation_failed"
            self.log.exception("CHOCH_RECONCILIATION_FAILED | err=%s", exc)
            self.state.upsert_reconciliation(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                last_transaction_id=last_tx,
                status="failed",
            )
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                event_type="CHOCH_RECONCILIATION_FAILED",
                payload={"error": str(exc)},
            )
            return {"status": "failed", "mismatches": [], "processed_transactions": processed_tx}

        pending = self.broker.get_pending_orders().get("orders", [])
        open_trades = self.broker.get_open_trades().get("trades", [])

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

        unprotected = self._verify_real_protection(open_trades)
        if unprotected:
            mismatches.append("CHOCH_UNPROTECTED_TRADE")
        status = "ok" if not mismatches else "mismatch"
        self._reconciliation_ok = status == "ok"
        self._submission_block_reason = None if self._reconciliation_ok else "broker_local_state_mismatch"
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
                "processed_transactions": processed_tx,
                "changes_keys": sorted(changes.keys()),
            },
        )
        return {"status": status, "mismatches": mismatches, "processed_transactions": processed_tx}

    def process_once(self) -> None:
        self.reconcile_read_only()
        for instrument in self.config.instrument_names:
            try:
                s2_df = self._process_timeframe(instrument=instrument, granularity=self.config.s2_timeframe, count=1200)
                s1_df = self._process_timeframe(instrument=instrument, granularity=self.config.s1_timeframe, count=500)
                if s1_df is not None:
                    self._process_shadow_strategy(instrument=instrument, s1_df=s1_df, s2_df=s2_df)
            except Exception as exc:
                self.log.exception("CHOCH_INSTRUMENT_ERROR | instrument=%s | err=%s", instrument, exc)
                self.state.record_event(
                    bot_id=self.config.bot_id,
                    account_id=self.config.account_id,
                    instrument=instrument,
                    event_type=EventType.CHOCH_INSTRUMENT_ERROR.value,
                    payload={"error": str(exc)},
                )

    def _process_timeframe(self, *, instrument: str, granularity: str, count: int) -> pd.DataFrame | None:
        latest, _candle, payload = self.broker.get_complete_candles(
            instrument=instrument,
            granularity=granularity,
            count=count,
            price="BA",
        )
        df = bid_ask_candles_to_df(payload)
        if df.empty:
            return None
        last = self.state.get_last_candle(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            granularity=granularity,
        )
        if last and latest <= pd.Timestamp(last):
            return df
        if self.state.get_execution_watermark(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            granularity=granularity,
        ) is None:
            self.state.set_execution_watermark(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=instrument,
                granularity=granularity,
                candle_time=latest.isoformat(),
            )
        self._latest_complete_candle[(instrument, granularity)] = latest
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
        return df

    def _process_shadow_strategy(
        self,
        *,
        instrument: str,
        s1_df: pd.DataFrame,
        s2_df: pd.DataFrame | None,
    ) -> None:
        s1_results = simulate_s1(s1_df, instrument=instrument)
        s2_count = 0
        filled_s1 = 0
        for result in s1_results:
            self._persist_shadow_s1(instrument=instrument, result=result)
            if result.entry is None:
                self._maybe_cancel_s1(instrument=instrument, result=result)
            else:
                self._maybe_execute_s1(instrument=instrument, result=result)
            if result.entry is not None:
                filled_s1 += 1
            if s2_df is None:
                continue
            s2_result = simulate_s2_from_s1(s2_df, parent_s1_key=result.client_order_key, s1=result)
            if s2_result is not None:
                self._persist_shadow_s2(instrument=instrument, result=s2_result)
                s2_count += 1
        if s2_df is not None:
            self._process_real_s2_observations(instrument=instrument, s2_df=s2_df)
        latest = self._latest_complete_candle.get((instrument, self.config.s1_timeframe))
        if latest is not None:
            self.state.set_execution_watermark(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=instrument,
                granularity=self.config.s1_timeframe,
                candle_time=latest.isoformat(),
            )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            event_type="CHOCH_SHADOW_STRATEGY_PASS",
            payload={
                "s1_results": len(s1_results),
                "s1_filled": filled_s1,
                "s2_results": s2_count,
                "dry_run": self.config.dry_run,
                "trading_enabled": self.config.trading_enabled,
                "env_trading_enabled": self.config.env_trading_enabled,
            },
        )
        self.log.info(
            "CHOCH_SHADOW_STRATEGY_PASS | instrument=%s | s1_results=%s | s1_filled=%s | s2_results=%s | dry_run=%s",
            instrument,
            len(s1_results),
            filled_s1,
            s2_count,
            self.config.dry_run,
        )

    def _persist_shadow_s1(self, *, instrument: str, result: S1Result) -> None:
        payload = dict(result.payload)
        if result.entry is not None and result.stop is not None:
            sizing = self._estimate_shadow_sizing(instrument=instrument, result=result)
            if sizing is not None:
                payload["shadow_position_size"] = sizing
                self.state.record_event(
                    bot_id=self.config.bot_id,
                    account_id=self.config.account_id,
                    instrument=instrument,
                    strategy_leg="S1",
                    event_type="CHOCH_SHADOW_POSITION_SIZE",
                    payload={"client_order_key": result.client_order_key, **sizing},
                )
                result = S1Result(
                    client_order_key=result.client_order_key,
                    status=result.status,
                    event_time=result.event_time,
                    direction=result.direction,
                    origin=result.origin,
                    event_atr=result.event_atr,
                    event_extreme=result.event_extreme,
                    running_extreme=result.running_extreme,
                    entry_time=result.entry_time,
                    exit_time=result.exit_time,
                    entry=result.entry,
                    stop=result.stop,
                    target=result.target,
                    risk=result.risk,
                    outcome=result.outcome,
                    net_r=result.net_r,
                    cancellation_reason=result.cancellation_reason,
                    payload=payload,
                )
        self.state.upsert_simulated_s1_result(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            result=result,
        )

    def _maybe_execute_s1(self, *, instrument: str, result: S1Result) -> None:
        if not self.broker.broker_writes_enabled():
            return
        if not self._can_submit_new_order(instrument=instrument, strategy_leg="S1", client_order_key=result.client_order_key):
            return
        if result.entry is None or result.stop is None or result.target is None or result.risk is None:
            return
        if self._is_replay_s1(instrument=instrument, result=result):
            return

        existing_order = self.state.get_real_order_by_client_key(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            client_order_key=result.client_order_key,
        )
        existing_trade = self.state.get_real_trade_by_client_key(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
                client_order_key=result.client_order_key,
        )
        if existing_trade is not None:
            return

        sizing = self._estimate_shadow_sizing(
            instrument=instrument,
            result=result,
            strategy_leg="S1",
            is_simulated_ledger=False,
        )
        if sizing is None or not sizing["admitted"] or sizing["final_units"] <= 0:
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=instrument,
                strategy_leg="S1",
                event_type="CHOCH_REAL_ORDER_REJECTED",
                payload={"client_order_key": result.client_order_key, "sizing": sizing},
            )
            return

        if existing_order is not None:
            if str(existing_order["state"]) not in {"local_pending", "broker_pending"}:
                return
            if not self._real_order_needs_replace(existing_order, result=result, sizing=sizing):
                return
            order_id = existing_order["oanda_order_id"]
            if order_id is None:
                response = self.broker.submit_limit_order_idempotent(
                    client_order_id=result.client_order_key,
                    instrument=instrument,
                    signed_units=sizing["signed_units"],
                    price=result.entry,
                    stop_loss=result.stop,
                    take_profit=result.target,
                )
            else:
                response = self.broker.replace_order(
                    order_id=str(order_id),
                    client_order_id=result.client_order_key,
                    instrument=instrument,
                    signed_units=sizing["signed_units"],
                    price=result.entry,
                    stop_loss=result.stop,
                    take_profit=result.target,
                )
            new_order_id = self._extract_oanda_order_id(response) or str(order_id or "")
            self.state.upsert_real_order(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=instrument,
                strategy_leg="S1",
                client_order_key=result.client_order_key,
                oanda_order_id=new_order_id,
                state="broker_pending",
                order_type="LIMIT",
                price=result.entry,
                stop_price=result.stop,
                units=sizing["final_units"],
                signed_units=sizing["signed_units"],
                intended_risk_cash=sizing["intended_risk_cash"],
                actual_risk_cash=sizing["actual_risk_cash"],
                actual_risk_pct=sizing["actual_risk_pct"],
                margin_required=sizing["margin_required"],
                tp_price=result.target,
            )
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=instrument,
                strategy_leg="S1",
                event_type="CHOCH_REAL_ORDER_REPLACED",
                payload={"client_order_key": result.client_order_key, "oanda_order_id": new_order_id, "sizing": sizing},
            )
            return

        self.state.upsert_real_setup_intent(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S1",
            client_order_key=result.client_order_key,
            state="pending_order_intended",
            direction=result.direction,
            signal_bar_time=result.event_time.isoformat(),
            entry=result.entry,
            stop=result.stop,
            tp=result.target,
            r_value=result.risk,
        )
        self.state.upsert_real_order(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S1",
            client_order_key=result.client_order_key,
            oanda_order_id=None,
            state="local_pending",
            order_type="LIMIT",
            price=result.entry,
            stop_price=result.stop,
            units=sizing["final_units"],
            signed_units=sizing["signed_units"],
            intended_risk_cash=sizing["intended_risk_cash"],
            actual_risk_cash=sizing["actual_risk_cash"],
            actual_risk_pct=sizing["actual_risk_pct"],
            margin_required=sizing["margin_required"],
            tp_price=result.target,
        )
        response = self.broker.submit_limit_order_idempotent(
            client_order_id=result.client_order_key,
            instrument=instrument,
            signed_units=sizing["signed_units"],
            price=result.entry,
            stop_loss=result.stop,
            take_profit=result.target,
        )
        order_id = self._extract_oanda_order_id(response)
        self.state.upsert_real_order(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S1",
            client_order_key=result.client_order_key,
            oanda_order_id=order_id,
            state="broker_pending",
            order_type="LIMIT",
            price=result.entry,
            stop_price=result.stop,
            units=sizing["final_units"],
            signed_units=sizing["signed_units"],
            intended_risk_cash=sizing["intended_risk_cash"],
            actual_risk_cash=sizing["actual_risk_cash"],
            actual_risk_pct=sizing["actual_risk_pct"],
            margin_required=sizing["margin_required"],
            tp_price=result.target,
        )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S1",
            event_type="CHOCH_REAL_ORDER_SUBMITTED",
            payload={"client_order_key": result.client_order_key, "oanda_order_id": order_id, "sizing": sizing},
        )

    def _maybe_cancel_s1(self, *, instrument: str, result: S1Result) -> None:
        if not self.broker.broker_writes_enabled():
            return
        if self._is_replay_s1(instrument=instrument, result=result):
            return
        existing_order = self.state.get_real_order_by_client_key(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            client_order_key=result.client_order_key,
        )
        if existing_order is None or str(existing_order["state"]) not in {"local_pending", "broker_pending"}:
            return
        order_id = existing_order["oanda_order_id"]
        if order_id is not None:
            response = self.broker.cancel_order(str(order_id))
            tx = response.get("orderCancelTransaction") or {}
            cancel_transaction_id = str(tx.get("id")) if tx.get("id") is not None else None
        else:
            cancel_transaction_id = None
        self.state.mark_real_order_cancelled(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            client_order_key=result.client_order_key,
            cancel_transaction_id=cancel_transaction_id,
        )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S1",
            event_type="CHOCH_REAL_ORDER_CANCELLED",
            payload={
                "client_order_key": result.client_order_key,
                "oanda_order_id": order_id,
                "cancel_transaction_id": cancel_transaction_id,
                "reason": result.cancellation_reason or result.status,
            },
        )

    def _last_processed_transaction_id(self) -> str | None:
        row = self.state.get_reconciliation(bot_id=self.config.bot_id, account_id=self.config.account_id)
        if row is not None and row["last_transaction_id"] is not None:
            return str(row["last_transaction_id"])
        if self.broker.account_metadata and self.broker.account_metadata.last_transaction_id:
            return str(self.broker.account_metadata.last_transaction_id)
        return None

    def _ordered_transactions(self, changes: dict[str, Any]) -> list[dict[str, Any]]:
        raw: list[Any] = []
        nested = changes.get("changes")
        if isinstance(nested, dict):
            raw.extend(nested.get("transactions") or [])
        elif isinstance(nested, list):
            raw.extend(nested)
        raw.extend(changes.get("transactions") or [])
        for key in (
            "orderCreateTransaction",
            "orderCancelTransaction",
            "orderCancelRejectTransaction",
            "orderFillTransaction",
        ):
            value = changes.get(key)
            if isinstance(value, dict):
                raw.append(value)

        def sort_key(tx: dict[str, Any]) -> int:
            tx_id = self._transaction_id(tx)
            try:
                return int(tx_id or 0)
            except ValueError:
                return 0

        return sorted((tx for tx in raw if isinstance(tx, dict)), key=sort_key)

    def _transaction_id(self, tx: dict[str, Any]) -> str | None:
        return str(tx["id"]) if tx.get("id") is not None else None

    def _process_transaction(self, tx: dict[str, Any]) -> None:
        tx_type = str(tx.get("type") or "")
        if tx_type in {"ORDER_CREATE", "LIMIT_ORDER", "MARKET_ORDER"}:
            self._process_order_create_transaction(tx)
        elif tx_type == "ORDER_CANCEL":
            self._process_order_cancel_transaction(tx)
        elif tx_type == "ORDER_FILL":
            self._process_order_fill_transaction(tx)
        elif tx_type in {"STOP_LOSS_ORDER", "TAKE_PROFIT_ORDER"}:
            self._process_protective_order_transaction(tx)
        elif tx_type == "ORDER_CANCEL_REJECT":
            self._process_order_cancel_reject_transaction(tx)
        elif tx_type == "TRADE_CLIENT_EXTENSIONS_MODIFY":
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                event_type="CHOCH_TRADE_CLIENT_EXTENSIONS_MODIFY_SEEN",
                payload={"transaction_id": self._transaction_id(tx)},
            )

    def _process_order_create_transaction(self, tx: dict[str, Any]) -> None:
        key = self._transaction_client_key(tx)
        if key is None or not self._is_choch_client_key(key):
            return
        order = self.state.get_real_order_by_client_key(
            bot_id=self.config.bot_id, account_id=self.config.account_id, client_order_key=key
        )
        if order is None:
            return
        self.state.upsert_real_order(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=str(order["instrument"]),
            strategy_leg=str(order["strategy_leg"]),
            client_order_key=key,
            oanda_order_id=self._order_id_from_transaction(tx),
            state=str(order["state"]),
            order_type=str(order["order_type"] or tx.get("type") or ""),
            price=float(order["price"]),
            stop_price=float(order["stop_price"]),
            units=float(order["units"]),
            signed_units=float(order["signed_units"]),
            intended_risk_cash=float(order["intended_risk_cash"]),
            actual_risk_cash=float(order["actual_risk_cash"]),
            actual_risk_pct=float(order["actual_risk_pct"]),
            margin_required=float(order["margin_required"]),
            tp_price=float(order["tp_price"]) if order["tp_price"] is not None else None,
        )

    def _process_order_cancel_transaction(self, tx: dict[str, Any]) -> None:
        order_id = str(tx.get("orderID") or tx.get("id") or "")
        if not order_id:
            return
        order = self.state.get_real_order_by_oanda_order_id(
            bot_id=self.config.bot_id, account_id=self.config.account_id, oanda_order_id=order_id
        )
        if order is None:
            return
        self.state.mark_real_order_cancelled(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            client_order_key=str(order["client_order_key"]),
            cancel_transaction_id=self._transaction_id(tx),
        )

    def _process_order_cancel_reject_transaction(self, tx: dict[str, Any]) -> None:
        order_id = str(tx.get("orderID") or "")
        if not order_id:
            return
        order = self.state.get_real_order_by_oanda_order_id(
            bot_id=self.config.bot_id, account_id=self.config.account_id, oanda_order_id=order_id
        )
        if order is None:
            return
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=str(order["instrument"]),
            strategy_leg=str(order["strategy_leg"]),
            event_type="CHOCH_REAL_ORDER_CANCEL_REJECTED",
            payload={
                "client_order_key": str(order["client_order_key"]),
                "oanda_order_id": order_id,
                "transaction_id": self._transaction_id(tx),
                "reason": tx.get("reason"),
            },
        )

    def _process_protective_order_transaction(self, tx: dict[str, Any]) -> None:
        trade_id = str(tx.get("tradeID") or "")
        if not trade_id:
            return
        trade = self.state.get_real_trade_by_oanda_trade_id(
            bot_id=self.config.bot_id, account_id=self.config.account_id, oanda_trade_id=trade_id
        )
        if trade is None:
            return
        tx_type = str(tx.get("type") or "")
        self.state.update_real_trade_protection(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            client_order_key=str(trade["client_order_key"]),
            stop_loss_order_id=self._transaction_id(tx) if tx_type == "STOP_LOSS_ORDER" else None,
            take_profit_order_id=self._transaction_id(tx) if tx_type == "TAKE_PROFIT_ORDER" else None,
        )

    def _process_order_fill_transaction(self, tx: dict[str, Any]) -> None:
        if self._is_trade_close_fill(tx):
            self._process_trade_close_fill(tx)
            return
        key = self._transaction_client_key(tx)
        order = None
        if key is not None and self._is_choch_client_key(key):
            order = self.state.get_real_order_by_client_key(
                bot_id=self.config.bot_id, account_id=self.config.account_id, client_order_key=key
            )
        if order is None and tx.get("orderID") is not None:
            order = self.state.get_real_order_by_oanda_order_id(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                oanda_order_id=str(tx["orderID"]),
            )
        if order is None:
            return
        client_key = str(order["client_order_key"])
        trade_id = self._opened_trade_id(tx)
        self.state.mark_real_order_filled(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            client_order_key=client_key,
            fill_transaction_id=str(self._transaction_id(tx) or ""),
        )
        self.state.upsert_real_trade(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=str(order["instrument"]),
            strategy_leg=str(order["strategy_leg"]),
            client_order_key=client_key,
            oanda_trade_id=trade_id,
            state="broker_open",
            entry_time=str(tx.get("time") or ""),
            entry_price=float(tx.get("price") or order["price"]),
            stop_price=float(order["stop_price"]) if order["stop_price"] is not None else None,
            tp_price=self._take_profit_price_from_fill(tx) or (
                float(order["tp_price"]) if order["tp_price"] is not None else None
            ),
            units=abs(float(tx.get("units") or order["units"])),
            signed_units=float(tx.get("units") or order["signed_units"]),
            intended_risk_cash=float(order["intended_risk_cash"]) if order["intended_risk_cash"] is not None else None,
            actual_risk_cash=float(order["actual_risk_cash"]) if order["actual_risk_cash"] is not None else None,
            actual_risk_pct=float(order["actual_risk_pct"]) if order["actual_risk_pct"] is not None else None,
            margin_required=float(order["margin_required"]) if order["margin_required"] is not None else None,
            fill_transaction_id=self._transaction_id(tx),
        )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=str(order["instrument"]),
            strategy_leg=str(order["strategy_leg"]),
            event_type="CHOCH_REAL_ORDER_FILLED",
            payload={"client_order_key": client_key, "oanda_trade_id": trade_id, "transaction_id": self._transaction_id(tx)},
        )

    def _process_trade_close_fill(self, tx: dict[str, Any]) -> None:
        for item in self._closed_trade_items(tx):
            trade_id = str(item.get("tradeID") or item.get("id") or "")
            if not trade_id:
                continue
            trade = self.state.get_real_trade_by_oanda_trade_id(
                bot_id=self.config.bot_id, account_id=self.config.account_id, oanda_trade_id=trade_id
            )
            if trade is None:
                continue
            realized_pl = self._float_or_none(item.get("realizedPL") or tx.get("pl"))
            risk_cash = self._float_or_none(trade["actual_risk_cash"])
            realized_r = (realized_pl / risk_cash) if realized_pl is not None and risk_cash and risk_cash > 0 else None
            close_reason = str(tx.get("reason") or "")
            state = "other_close"
            if close_reason == "TAKE_PROFIT_ORDER":
                state = "tp"
            elif close_reason == "STOP_LOSS_ORDER":
                state = "sl"
            self.state.close_real_trade(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                client_order_key=str(trade["client_order_key"]),
                state=state,
                exit_time=str(tx.get("time") or ""),
                close_transaction_id=str(self._transaction_id(tx) or ""),
                realized_pl=realized_pl,
                realized_r=realized_r,
            )
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=str(trade["instrument"]),
                strategy_leg=str(trade["strategy_leg"]),
                event_type="CHOCH_REAL_TRADE_CLOSED",
                payload={
                    "client_order_key": str(trade["client_order_key"]),
                    "oanda_trade_id": trade_id,
                    "state": state,
                    "realized_pl": realized_pl,
                    "realized_r": realized_r,
                    "transaction_id": self._transaction_id(tx),
                },
            )
            if str(trade["strategy_leg"]) == "S1" and state == "tp":
                self._arm_real_s2_from_s1_tp(trade=trade, close_time=str(tx.get("time") or ""))

    def _transaction_client_key(self, tx: dict[str, Any]) -> str | None:
        candidates = [
            (tx.get("clientExtensions") or {}).get("id") if isinstance(tx.get("clientExtensions"), dict) else None,
            (tx.get("tradeClientExtensions") or {}).get("id") if isinstance(tx.get("tradeClientExtensions"), dict) else None,
            (tx.get("tradeOpened") or {}).get("clientExtensions", {}).get("id") if isinstance(tx.get("tradeOpened"), dict) else None,
            tx.get("clientOrderID"),
        ]
        return next((str(x) for x in candidates if x), None)

    def _is_choch_client_key(self, key: str) -> bool:
        return key.startswith("CHOCH") or "-CHOCH-" in key or key.startswith("s1-") or key.endswith("-S2")

    def _order_id_from_transaction(self, tx: dict[str, Any]) -> str | None:
        return str(tx.get("orderID") or tx.get("id")) if tx.get("orderID") is not None or tx.get("id") is not None else None

    def _opened_trade_id(self, tx: dict[str, Any]) -> str | None:
        opened = tx.get("tradeOpened") or {}
        if isinstance(opened, dict):
            return str(opened.get("tradeID") or opened.get("id")) if opened.get("tradeID") or opened.get("id") else None
        return None

    def _closed_trade_items(self, tx: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key in ("tradesClosed", "tradesReduced"):
            value = tx.get(key)
            if isinstance(value, list):
                items.extend(x for x in value if isinstance(x, dict))
        closed = tx.get("tradeClosed")
        if isinstance(closed, dict):
            items.append(closed)
        return items

    def _is_trade_close_fill(self, tx: dict[str, Any]) -> bool:
        return bool(self._closed_trade_items(tx)) or str(tx.get("reason") or "") in {
            "TAKE_PROFIT_ORDER",
            "STOP_LOSS_ORDER",
            "MARKET_ORDER_TRADE_CLOSE",
        }

    def _take_profit_price_from_fill(self, tx: dict[str, Any]) -> float | None:
        tp = tx.get("takeProfitOnFill") or {}
        if isinstance(tp, dict) and tp.get("price") is not None:
            return float(tp["price"])
        return None

    def _float_or_none(self, value: Any) -> float | None:
        if value is None or value == "":
            return None
        return float(value)

    def _arm_real_s2_from_s1_tp(self, *, trade, close_time: str) -> None:
        tp_time = pd.Timestamp(close_time)
        expected = tp_time + pd.Timedelta(minutes=S2_DECISION_BAR * S2_TF_MINUTES)
        key = f"{trade['client_order_key']}-S2"
        direction = "bearish" if float(trade["signed_units"] or 0) > 0 else "bullish"
        entry = float(trade["entry_price"])
        stop = float(trade["stop_price"])
        risk = abs(entry - stop)
        self.state.upsert_real_s2_observation(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=str(trade["instrument"]),
            client_order_key=key,
            parent_s1_trade_id=str(trade["oanda_trade_id"] or trade["client_order_key"]),
            direction=direction,
            s1_tp_time=close_time,
            observation_start_time=close_time,
            expected_decision_time=expected.isoformat(),
            entry=entry,
            stop=stop,
            tp=float(trade["tp_price"]) if trade["tp_price"] is not None else entry,
            r_value=risk,
        )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=str(trade["instrument"]),
            strategy_leg="S2",
            event_type="CHOCH_REAL_S2_ARMED",
            payload={
                "client_order_key": key,
                "parent_s1_trade_id": str(trade["oanda_trade_id"] or trade["client_order_key"]),
                "s1_tp_time": close_time,
                "expected_decision_time": expected.isoformat(),
            },
        )

    def _process_real_s2_observations(self, *, instrument: str, s2_df: pd.DataFrame) -> None:
        for setup in self.state.observing_real_s2_setups(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
        ):
            key = str(setup["client_order_key"])
            existing_order = self.state.get_real_order_by_client_key(
                bot_id=self.config.bot_id, account_id=self.config.account_id, client_order_key=key
            )
            existing_trade = self.state.get_real_trade_by_client_key(
                bot_id=self.config.bot_id, account_id=self.config.account_id, client_order_key=key
            )
            if existing_order is not None or existing_trade is not None:
                continue
            original_direction = "bullish" if str(setup["direction"]) == "bearish" else "bearish"
            s1 = SimpleNamespace(
                outcome="tp",
                exit_time=pd.Timestamp(setup["s1_tp_time"]),
                entry=float(setup["entry"]),
                risk=float(setup["r_value"]),
                direction=original_direction,
            )
            result = simulate_s2_from_s1(s2_df, parent_s1_key=str(setup["parent_s1_trade_id"]), s1=s1)
            if result is None or result.decision_time is None:
                continue
            latest = self._latest_complete_candle.get((instrument, self.config.s2_timeframe))
            if latest is None and not s2_df.empty:
                latest = pd.Timestamp(s2_df.index[-1])
            if latest is not None and pd.Timestamp(result.decision_time) < pd.Timestamp(latest):
                self.state.update_real_setup_state(
                    bot_id=self.config.bot_id,
                    account_id=self.config.account_id,
                    strategy_leg="S2",
                    client_order_key=key,
                    state="missed_downtime",
                    cancellation_reason="decision_time_missed",
                )
                self.state.record_event(
                    bot_id=self.config.bot_id,
                    account_id=self.config.account_id,
                    instrument=instrument,
                    strategy_leg="S2",
                    event_type="CHOCH_REAL_S2_MISSED_DOWNTIME",
                    payload={
                        "client_order_key": key,
                        "decision_time": result.decision_time.isoformat(),
                        "latest_complete_candle": latest.isoformat(),
                    },
                )
                continue
            if result.entry is None or result.stop is None or result.target is None or result.risk is None:
                self.state.update_real_setup_state(
                    bot_id=self.config.bot_id,
                    account_id=self.config.account_id,
                    strategy_leg="S2",
                    client_order_key=key,
                    state="rejected",
                )
                self.state.record_event(
                    bot_id=self.config.bot_id,
                    account_id=self.config.account_id,
                    instrument=instrument,
                    strategy_leg="S2",
                    event_type="CHOCH_REAL_S2_REJECTED",
                    payload={"client_order_key": key, "decision_time": result.decision_time.isoformat()},
                )
                continue
            self._maybe_execute_s2(instrument=instrument, client_order_key=key, result=result)
        self._maybe_close_expired_s2_trades()

    def _maybe_execute_s2(self, *, instrument: str, client_order_key: str, result: S2Result) -> None:
        if not self.broker.broker_writes_enabled():
            return
        if not self._can_submit_new_order(instrument=instrument, strategy_leg="S2", client_order_key=client_order_key):
            return
        sizing = self._estimate_shadow_sizing(
            instrument=instrument,
            result=result,
            direction=result.s2_direction,
            strategy_leg="S2",
            is_simulated_ledger=False,
        )
        if sizing is None or not sizing["admitted"] or sizing["final_units"] <= 0:
            self.state.update_real_setup_state(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                strategy_leg="S2",
                client_order_key=client_order_key,
                state="rejected",
                cancellation_reason="risk_admission_failed",
            )
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=instrument,
                strategy_leg="S2",
                event_type="CHOCH_REAL_S2_REJECTED",
                payload={"client_order_key": client_order_key, "sizing": sizing},
            )
            return
        self.state.update_real_setup_state(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            strategy_leg="S2",
            client_order_key=client_order_key,
            state="qualified",
        )
        self.state.upsert_real_order(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S2",
            client_order_key=client_order_key,
            oanda_order_id=None,
            state="local_pending",
            order_type="MARKET",
            price=result.entry,
            stop_price=result.stop,
            units=sizing["final_units"],
            signed_units=sizing["signed_units"],
            intended_risk_cash=sizing["intended_risk_cash"],
            actual_risk_cash=sizing["actual_risk_cash"],
            actual_risk_pct=sizing["actual_risk_pct"],
            margin_required=sizing["margin_required"],
            tp_price=result.target,
        )
        response = self.broker.submit_market_order_idempotent(
            client_order_id=client_order_key,
            instrument=instrument,
            signed_units=sizing["signed_units"],
            stop_loss=result.stop,
            take_profit=result.target,
        )
        order_id = self._extract_oanda_order_id(response)
        self.state.upsert_real_order(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S2",
            client_order_key=client_order_key,
            oanda_order_id=order_id,
            state="broker_pending",
            order_type="MARKET",
            price=result.entry,
            stop_price=result.stop,
            units=sizing["final_units"],
            signed_units=sizing["signed_units"],
            intended_risk_cash=sizing["intended_risk_cash"],
            actual_risk_cash=sizing["actual_risk_cash"],
            actual_risk_pct=sizing["actual_risk_pct"],
            margin_required=sizing["margin_required"],
            tp_price=result.target,
        )
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S2",
            event_type="CHOCH_REAL_S2_MARKET_ORDER_SUBMITTED",
            payload={"client_order_key": client_order_key, "oanda_order_id": order_id, "sizing": sizing},
        )

    def _maybe_close_expired_s2_trades(self) -> None:
        if not self.broker.broker_writes_enabled():
            return
        now = pd.Timestamp.utcnow()
        for trade in self.state.active_real_trades(bot_id=self.config.bot_id, account_id=self.config.account_id):
            if str(trade["strategy_leg"]) != "S2" or trade["entry_time"] is None or trade["oanda_trade_id"] is None:
                continue
            expires = pd.Timestamp(trade["entry_time"]) + pd.Timedelta(minutes=S2_MAX_HOLD_MINUTES)
            if now < expires:
                continue
            response = self.broker.close_trade(str(trade["oanda_trade_id"]))
            tx = response.get("orderFillTransaction") or response.get("longOrderFillTransaction") or response.get("shortOrderFillTransaction") or {}
            close_tx = str(tx.get("id") or "")
            realized_pl = self._float_or_none(tx.get("pl"))
            risk_cash = self._float_or_none(trade["actual_risk_cash"])
            realized_r = (realized_pl / risk_cash) if realized_pl is not None and risk_cash and risk_cash > 0 else None
            self.state.close_real_trade(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                client_order_key=str(trade["client_order_key"]),
                state="timeout",
                exit_time=str(tx.get("time") or pd.Timestamp.utcnow().isoformat()),
                close_transaction_id=close_tx,
                realized_pl=realized_pl,
                realized_r=realized_r,
            )
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=str(trade["instrument"]),
                strategy_leg="S2",
                event_type="CHOCH_REAL_S2_FORCED_CLOSE",
                payload={"client_order_key": str(trade["client_order_key"]), "close_transaction_id": close_tx},
            )

    def _can_submit_new_order(self, *, instrument: str, strategy_leg: str, client_order_key: str) -> bool:
        if self._reconciliation_ok:
            return True
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg=strategy_leg,
            event_type="CHOCH_REAL_ORDER_BLOCKED_FAIL_CLOSED",
            payload={"client_order_key": client_order_key, "reason": self._submission_block_reason},
        )
        self.log.warning(
            "CHOCH_REAL_ORDER_BLOCKED_FAIL_CLOSED | instrument=%s | strategy_leg=%s | client_order_key=%s | reason=%s",
            instrument,
            strategy_leg,
            client_order_key,
            self._submission_block_reason,
        )
        return False

    def _verify_real_protection(self, open_trades: list[dict[str, Any]]) -> list[str]:
        by_id = {str(trade.get("id")): trade for trade in open_trades if trade.get("id") is not None}
        unprotected: list[str] = []
        for trade in self.state.active_real_trades(bot_id=self.config.bot_id, account_id=self.config.account_id):
            trade_id = str(trade["oanda_trade_id"] or "")
            if not trade_id:
                continue
            broker_trade = by_id.get(trade_id)
            if broker_trade is None:
                continue
            has_stop = bool(broker_trade.get("stopLossOrder") or broker_trade.get("stopLossOrderID") or trade["stop_loss_order_id"])
            has_tp = bool(broker_trade.get("takeProfitOrder") or broker_trade.get("takeProfitOrderID") or trade["take_profit_order_id"])
            if has_stop and has_tp:
                continue
            key = str(trade["client_order_key"])
            unprotected.append(key)
            self.log.critical(
                "CHOCH_UNPROTECTED_TRADE | instrument=%s | strategy_leg=%s | client_order_key=%s | oanda_trade_id=%s",
                trade["instrument"],
                trade["strategy_leg"],
                key,
                trade_id,
            )
            self.state.record_event(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                instrument=str(trade["instrument"]),
                strategy_leg=str(trade["strategy_leg"]),
                event_type="CHOCH_UNPROTECTED_TRADE",
                payload={"client_order_key": key, "oanda_trade_id": trade_id},
            )
        return unprotected

    def _is_replay_s1(self, *, instrument: str, result: S1Result) -> bool:
        watermark = self.state.get_execution_watermark(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            granularity=self.config.s1_timeframe,
        )
        if watermark is not None and pd.Timestamp(result.event_time) >= pd.Timestamp(watermark):
            return False
        self.state.record_event(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            strategy_leg="S1",
            event_type="CHOCH_EXECUTION_SKIPPED_REPLAY",
            payload={
                "client_order_key": result.client_order_key,
                "event_time": result.event_time.isoformat(),
                "execution_watermark": watermark,
            },
        )
        return True

    def _real_order_needs_replace(self, existing_order, *, result: S1Result, sizing: dict[str, float]) -> bool:
        checks = (
            ("price", result.entry),
            ("stop_price", result.stop),
            ("units", sizing["final_units"]),
            ("signed_units", sizing["signed_units"]),
        )
        for column, expected in checks:
            if existing_order[column] is None or abs(float(existing_order[column]) - float(expected)) > 1e-9:
                return True
        return False

    def _extract_oanda_order_id(self, response: dict[str, Any]) -> str | None:
        if "order" in response:
            order = response.get("order") or {}
            return str(order.get("id")) if order.get("id") is not None else None
        tx = response.get("orderCreateTransaction") or {}
        if tx.get("id") is not None:
            return str(tx["id"])
        cancel_replace = response.get("orderCancelTransaction") or {}
        if cancel_replace.get("id") is not None:
            return str(cancel_replace["id"])
        return None

    def _persist_shadow_s2(self, *, instrument: str, result: S2Result) -> None:
        payload = dict(result.payload)
        if result.entry is not None and result.stop is not None:
            sizing = self._estimate_shadow_sizing(
                instrument=instrument,
                result=result,
                direction=result.s2_direction,
                strategy_leg="S2",
            )
            if sizing is not None:
                payload["shadow_position_size"] = sizing
                self.state.record_event(
                    bot_id=self.config.bot_id,
                    account_id=self.config.account_id,
                    instrument=instrument,
                    strategy_leg="S2",
                    event_type="CHOCH_SHADOW_POSITION_SIZE",
                    payload={"client_order_key": f"{result.parent_s1_key}-S2", **sizing},
                )
                result = replace(result, payload=payload)
        self.state.upsert_simulated_s2_result(
            bot_id=self.config.bot_id,
            account_id=self.config.account_id,
            instrument=instrument,
            result=result,
        )

    def _estimate_shadow_sizing(
        self,
        *,
        instrument: str,
        result: S1Result | S2Result,
        direction: str | None = None,
        strategy_leg: str = "S1",
        is_simulated_ledger: bool = True,
    ) -> dict[str, float] | None:
        metadata = self.broker.instrument_metadata.get(instrument)
        if metadata is None or result.entry is None or result.stop is None:
            return None
        try:
            account_state = self.broker.get_account_risk_state()
            nav = float(account_state["nav"])
            margin_available = float(account_state["margin_available"])
            conversion = self.broker.get_quote_to_account_rate(quote_currency(instrument))
            instrument_cfg = next((x for x in self.config.instruments if x.name == instrument), None)
            max_units = instrument_cfg.max_units if instrument_cfg and instrument_cfg.max_units else self.config.max_units
            size = size_shadow_position(
                account_nav=nav,
                risk_pct=self.config.risk_pct,
                entry=result.entry,
                stop=result.stop,
                direction=direction or result.direction,
                metadata=metadata,
                quote_to_account_rate=conversion,
                max_units=max_units,
            )
            constrained_size = constrain_shadow_position_to_margin(
                account_nav=nav,
                margin_available=margin_available,
                entry=result.entry,
                direction=direction or result.direction,
                metadata=metadata,
                quote_to_account_rate=conversion,
                max_units=max_units,
                min_margin_supported_risk_pct=self.config.min_margin_supported_risk_pct,
                full_size=size,
            )
            existing_risk = self.state.aggregate_open_risk_cash(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                is_simulated=is_simulated_ledger,
            )
            admission = assess_aggregate_risk_admission(
                nav=nav,
                max_account_risk_open=self.config.max_account_risk_open,
                existing_risk_cash=existing_risk,
                candidate_intended_risk_cash=constrained_size.intended_risk_cash,
                candidate_actual_risk_cash=constrained_size.actual_risk_cash,
            )
            admitted = constrained_size.margin_floor_ok and admission.admitted
            rejection_reason = admission.rejection_reason
            if not constrained_size.margin_floor_ok:
                rejection_reason = "margin_supported_risk_below_floor"
            self.log.info(
                "CHOCH_RISK_ADMISSION | instrument=%s | strategy_leg=%s | nav=%s | aggregate_cap_pct=%s "
                "| aggregate_cap_cash=%s | intended_risk_pct=%s | intended_risk_cash=%s "
                "| desired_units_before_caps=%s | full_size_units=%s | full_size_margin_required=%s "
                "| full_target_risk_feasible=%s | margin_fallback_used=%s | margin_supported_units=%s "
                "| final_units=%s | actual_risk_cash=%s | actual_risk_pct=%s "
                "| min_margin_supported_risk_pct=%s | margin_required=%s | margin_available=%s "
                "| margin_required_pct_of_nav=%s | existing_choch_risk_cash=%s "
                "| resulting_aggregate_risk_cash=%s | admitted=%s | rejection_reason=%s",
                instrument,
                strategy_leg,
                nav,
                self.config.max_account_risk_open,
                admission.aggregate_cap_cash,
                self.config.risk_pct,
                constrained_size.intended_risk_cash,
                constrained_size.desired_units_before_caps,
                constrained_size.full_size_units,
                constrained_size.full_size_margin_required,
                constrained_size.full_target_risk_feasible,
                constrained_size.margin_fallback_used,
                constrained_size.margin_supported_units,
                constrained_size.final_units,
                constrained_size.actual_risk_cash,
                constrained_size.actual_risk_pct,
                self.config.min_margin_supported_risk_pct,
                constrained_size.margin_required,
                margin_available,
                constrained_size.margin_required_pct_of_nav,
                admission.existing_risk_cash,
                admission.resulting_aggregate_risk_cash,
                admitted,
                rejection_reason,
            )
            return {
                "nav": nav,
                "intended_risk_pct": self.config.risk_pct,
                "risk_cash": constrained_size.intended_risk_cash,
                "intended_risk_cash": constrained_size.intended_risk_cash,
                "desired_units_before_caps": constrained_size.desired_units_before_caps,
                "full_size_units": constrained_size.full_size_units,
                "full_size_margin_required": constrained_size.full_size_margin_required,
                "full_target_risk_feasible": constrained_size.full_target_risk_feasible,
                "margin_fallback_used": constrained_size.margin_fallback_used,
                "margin_supported_units": constrained_size.margin_supported_units,
                "actual_risk_cash": constrained_size.actual_risk_cash,
                "actual_risk_pct": constrained_size.actual_risk_pct,
                "risk_per_unit": constrained_size.risk_per_unit,
                "units": constrained_size.final_units,
                "final_units": constrained_size.final_units,
                "signed_units": constrained_size.signed_units,
                "margin_required": constrained_size.margin_required,
                "margin_available": margin_available,
                "margin_required_pct_of_nav": constrained_size.margin_required_pct_of_nav,
                "margin_ok": constrained_size.full_target_risk_feasible,
                "max_margin_supported_units": constrained_size.max_margin_supported_units,
                "max_margin_supported_risk_cash": constrained_size.max_margin_supported_risk_cash,
                "max_margin_supported_risk_pct": constrained_size.max_margin_supported_risk_pct,
                "min_margin_supported_risk_pct": self.config.min_margin_supported_risk_pct,
                "margin_rate": metadata.margin_rate,
                "effective_leverage": metadata.effective_leverage,
                "quote_to_account_rate": conversion,
                "max_units": float(max_units),
                "aggregate_cap_pct": self.config.max_account_risk_open,
                "aggregate_cap_cash": admission.aggregate_cap_cash,
                "existing_choch_risk_cash": admission.existing_risk_cash,
                "resulting_aggregate_risk_cash": admission.resulting_aggregate_risk_cash,
                "admitted": admitted,
                "rejection_reason": rejection_reason,
            }
        except Exception as exc:
            self.log.warning(
                "CHOCH_SHADOW_SIZING_SKIPPED | instrument=%s | client_order_key=%s | err=%s",
                instrument,
                result.client_order_key if strategy_leg == "S1" else f"{result.parent_s1_key}-S2",
                exc,
            )
            return None

    def run_forever(self) -> None:
        while True:
            try:
                time.sleep(seconds_until_next_close(self.config.s2_timeframe) + 1)
                self.process_once()
            except Exception as exc:
                self.log.exception("CHOCH_ENGINE_ERROR | err=%s", exc)
                time.sleep(5)
