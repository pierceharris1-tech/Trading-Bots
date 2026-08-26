from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pandas as pd

from bots.choch_live.broker import ChochBroker
from bots.choch_live.config import ChochConfig
from bots.choch_live.logging import get_choch_logger
from bots.choch_live.models import EventType
from bots.choch_live.risk import (
    assess_aggregate_risk_admission,
    assess_margin_feasibility,
    quote_currency,
    size_shadow_position,
)
from bots.choch_live.s1_structure import S1Result, simulate_s1
from bots.choch_live.s2_continuation import S2Result, simulate_s2_from_s1
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
            if result.entry is not None:
                filled_s1 += 1
            if s2_df is None:
                continue
            s2_result = simulate_s2_from_s1(s2_df, parent_s1_key=result.client_order_key, s1=result)
            if s2_result is not None:
                self._persist_shadow_s2(instrument=instrument, result=s2_result)
                s2_count += 1
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
            feasibility = assess_margin_feasibility(
                account_nav=nav,
                margin_available=margin_available,
                entry=result.entry,
                metadata=metadata,
                quote_to_account_rate=conversion,
                risk_per_unit=size.risk_per_unit,
                margin_required=size.margin_required,
            )
            margin_ok = feasibility.full_target_risk_feasible
            existing_risk = self.state.aggregate_open_risk_cash(
                bot_id=self.config.bot_id,
                account_id=self.config.account_id,
                is_simulated=True,
            )
            admission = assess_aggregate_risk_admission(
                nav=nav,
                max_account_risk_open=self.config.max_account_risk_open,
                existing_risk_cash=existing_risk,
                candidate_intended_risk_cash=size.intended_risk_cash,
                candidate_actual_risk_cash=size.actual_risk_cash,
            )
            admitted = admission.admitted and margin_ok
            rejection_reason = admission.rejection_reason
            if not margin_ok:
                rejection_reason = "margin_insufficient"
            self.log.info(
                "CHOCH_RISK_ADMISSION | instrument=%s | strategy_leg=%s | nav=%s | aggregate_cap_pct=%s "
                "| aggregate_cap_cash=%s | existing_choch_risk_cash=%s | candidate_intended_risk_cash=%s "
                "| candidate_actual_risk_cash=%s | resulting_aggregate_risk_cash=%s | margin_required=%s "
                "| margin_available=%s | margin_required_pct_of_nav=%s | desired_units_before_caps=%s "
                "| final_units=%s | full_target_risk_feasible=%s | max_margin_supported_units=%s "
                "| max_margin_supported_risk_cash=%s | max_margin_supported_risk_pct=%s | margin_ok=%s "
                "| admitted=%s | rejection_reason=%s",
                instrument,
                strategy_leg,
                nav,
                self.config.max_account_risk_open,
                admission.aggregate_cap_cash,
                admission.existing_risk_cash,
                size.intended_risk_cash,
                size.actual_risk_cash,
                admission.resulting_aggregate_risk_cash,
                size.margin_required,
                margin_available,
                feasibility.margin_required_pct_of_nav,
                size.desired_units_before_caps,
                size.units,
                feasibility.full_target_risk_feasible,
                feasibility.max_margin_supported_units,
                feasibility.max_margin_supported_risk_cash,
                feasibility.max_margin_supported_risk_pct,
                margin_ok,
                admitted,
                rejection_reason,
            )
            return {
                "nav": nav,
                "intended_risk_pct": self.config.risk_pct,
                "risk_cash": size.risk_cash,
                "intended_risk_cash": size.intended_risk_cash,
                "desired_units_before_caps": size.desired_units_before_caps,
                "actual_risk_cash": size.actual_risk_cash,
                "actual_risk_pct": size.actual_risk_pct,
                "risk_per_unit": size.risk_per_unit,
                "units": size.units,
                "final_units": size.units,
                "signed_units": size.signed_units,
                "margin_required": size.margin_required,
                "margin_available": margin_available,
                "margin_required_pct_of_nav": feasibility.margin_required_pct_of_nav,
                "margin_ok": margin_ok,
                "full_target_risk_feasible": feasibility.full_target_risk_feasible,
                "max_margin_supported_units": feasibility.max_margin_supported_units,
                "max_margin_supported_risk_cash": feasibility.max_margin_supported_risk_cash,
                "max_margin_supported_risk_pct": feasibility.max_margin_supported_risk_pct,
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
