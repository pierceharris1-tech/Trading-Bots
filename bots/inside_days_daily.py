from __future__ import annotations

import time

import pandas as pd

from shared.bot_config import cfg_instruments, cfg_timeframe, require_account_id
from shared.config import load_settings
from shared.daily_engine import (
    emit_order_event,
    emit_processed_candle,
    kv_json_get,
    kv_json_set,
    last_processed_bar_key,
    reconcile_closed_trades,
    select_latest_complete_candle,
    set_processed_candle_bar,
    should_process_candle_bar,
)
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.risk import clamp_units, units_from_risk
from shared.state_store import get_equity, kv_set, trade_open, trade_open_for_key
from shared.strategy_utils import candles_to_df, parse_order_ids, parse_realized_pl, pip_buffer_for_instrument
from shared.time_utils import seconds_until_next_close

log = get_logger("bot.inside_days_daily")


def _tp_distance(instrument: str) -> float:
    if instrument == "XAU_USD":
        return 2.0
    return 20.0 * pip_buffer_for_instrument(instrument)


def _sync_unlogged_open_trades(bot_id: str, account_id: str, client: OandaClient, instrument: str) -> None:
    open_trades = client.list_open_trades().get("trades", [])
    for tr in open_trades:
        if str(tr.get("instrument")) != instrument:
            continue
        trade_id = str(tr.get("id"))
        if trade_open_for_key(bot_id, instrument, trade_id) is not None:
            continue
        trade_open(
            bot_id=bot_id,
            instrument=instrument,
            account_id=account_id,
            open_time=str(tr.get("openTime")),
            order_id=str(tr.get("id")),
            trade_id=trade_id,
        )


def _cancel_order_ids(client: OandaClient, order_ids: list[str]) -> None:
    if not order_ids:
        return
    pending = client.list_pending_orders().get("orders", [])
    pending_ids = {str(x.get("id")) for x in pending}
    for oid in order_ids:
        if oid in pending_ids:
            client.cancel_order(oid)


def main(cfg: dict | None = None) -> None:
    if cfg is None:
        cfg = {}
    settings = load_settings()
    bot_id = str(cfg.get("bot_id", "INSIDE_DAYS"))
    account_id = require_account_id(cfg)
    timeframe = cfg_timeframe(cfg, default_timeframe="D")
    instruments = cfg_instruments(cfg, settings.instrument)
    client = OandaClient(
        base_url=settings.base_url,
        api_key=settings.oanda_api_key,
        account_id=account_id,
        bot_id=bot_id,
        timeframe=timeframe,
    )
    last_processed_bar_by_instrument: dict[str, pd.Timestamp] = {}

    while True:
        try:
            time.sleep(seconds_until_next_close(timeframe) + 1)
            reconcile_closed_trades(bot_id, client)
            for instrument in instruments:
                candles = client.get_candles(instrument=instrument, granularity=timeframe, count=200)
                decision = select_latest_complete_candle(candles.get("candles", []))
                if decision is None:
                    continue
                latest, _ = decision
                bars = candles_to_df(candles).loc[:latest]
                if bars.empty:
                    continue
                key = last_processed_bar_key(instrument, timeframe, "_inside")
                if not should_process_candle_bar(
                    bot_id,
                    key,
                    latest,
                    last_processed_bar_time=last_processed_bar_by_instrument.get(instrument),
                ):
                    continue

                state_key = f"inside_state_{instrument}"
                state = kv_json_get(bot_id, state_key)
                high_t = float(bars["high"].iloc[-1])
                low_t = float(bars["low"].iloc[-1])

                if state and state.get("mode") == "pending":
                    buy_entry = float(state["buy_entry"])
                    sell_entry = float(state["sell_entry"])
                    up_hit = high_t >= buy_entry
                    down_hit = low_t <= sell_entry
                    if up_hit and down_hit:
                        # Ambiguous range break day: clean up and reset.
                        _cancel_order_ids(client, list(state.get("buy_orders", [])) + list(state.get("sell_orders", [])))
                        kv_set(bot_id, state_key, "")
                        set_processed_candle_bar(bot_id, key, latest)
                        last_processed_bar_by_instrument[instrument] = latest
                        emit_processed_candle(bot_id, timeframe, instrument, latest)
                        continue
                    if up_hit or down_hit:
                        side = 1 if up_hit else -1
                        opp = state.get("sell_orders", []) if side > 0 else state.get("buy_orders", [])
                        _cancel_order_ids(client, list(opp))
                        state["mode"] = "open"
                        state["side"] = side
                        state["breakout_bar"] = latest.isoformat()
                        state["leg1_closed"] = False
                        kv_json_set(bot_id, state_key, state)

                _sync_unlogged_open_trades(bot_id, account_id, client, instrument)

                state = kv_json_get(bot_id, state_key)
                if state and state.get("mode") == "open":
                    open_trades = client.list_open_trades().get("trades", [])
                    side = int(state["side"])
                    side_open = [
                        x
                        for x in open_trades
                        if str(x.get("instrument")) == instrument and ((side > 0 and float(x.get("initialUnits", "0")) > 0) or (side < 0 and float(x.get("initialUnits", "0")) < 0))
                    ]
                    # Contract 1 closes on breakout bar close.
                    if (not bool(state.get("leg1_closed"))) and latest.isoformat() == str(state.get("breakout_bar")) and side_open:
                        tid = str(side_open[0].get("id"))
                        close_resp = client.close_trade(tid)
                        emit_order_event(event="ORDER_CLOSED", bot_id=bot_id, timeframe=timeframe, instrument=instrument, ref_id=tid)
                        _ = parse_realized_pl(close_resp)
                        state["leg1_closed"] = True
                        kv_json_set(bot_id, state_key, state)
                        open_trades = client.list_open_trades().get("trades", [])
                        side_open = [
                            x
                            for x in open_trades
                            if str(x.get("instrument")) == instrument and ((side > 0 and float(x.get("initialUnits", "0")) > 0) or (side < 0 and float(x.get("initialUnits", "0")) < 0))
                        ]
                    if not side_open:
                        kv_set(bot_id, state_key, "")

                state = kv_json_get(bot_id, state_key)
                if state is None:
                    if len(bars) >= 2:
                        prev_high = float(bars["high"].iloc[-2])
                        prev_low = float(bars["low"].iloc[-2])
                        inside = high_t <= prev_high and low_t >= prev_low
                        if inside:
                            buf = pip_buffer_for_instrument(instrument)
                            buy_entry = high_t + buf
                            sell_entry = low_t - buf
                            stop_dist = abs(buy_entry - sell_entry)
                            equity = float(get_equity(bot_id, client=client))
                            units_mag, _, _ = units_from_risk(
                                equity=equity,
                                risk_pct=0.01,
                                entry=buy_entry,
                                stop=sell_entry,
                                leverage=float(cfg.get("leverage", 20.0)),
                            )
                            total_units = clamp_units(int(units_mag))
                            leg_units = max(1, abs(total_units) // 2)
                            tp_dist = _tp_distance(instrument)

                            if bool(cfg.get("dry_run", True)):
                                log.info(
                                    "DRY_RUN | %s | would place inside-bar bracket units=%s buy_entry=%.5f sell_entry=%.5f",
                                    instrument, leg_units, buy_entry, sell_entry,
                                )
                                set_processed_candle_bar(bot_id, key, latest)
                                last_processed_bar_by_instrument[instrument] = latest
                                emit_processed_candle(bot_id, timeframe, instrument, latest)
                                continue

                            buy1 = client.place_stop_order(
                                units=leg_units,
                                instrument=instrument,
                                price=buy_entry,
                                stop_loss=sell_entry,
                            )
                            b1, _ = parse_order_ids(buy1)
                            emit_order_event(event="ORDER_PLACED", bot_id=bot_id, timeframe=timeframe, instrument=instrument, ref_id=str(b1 or ""))
                            buy2 = client.place_stop_order(
                                units=leg_units,
                                instrument=instrument,
                                price=buy_entry,
                                stop_loss=sell_entry,
                                take_profit=buy_entry + tp_dist,
                            )
                            b2, _ = parse_order_ids(buy2)
                            emit_order_event(event="ORDER_PLACED", bot_id=bot_id, timeframe=timeframe, instrument=instrument, ref_id=str(b2 or ""))
                            sell1 = client.place_stop_order(
                                units=-leg_units,
                                instrument=instrument,
                                price=sell_entry,
                                stop_loss=buy_entry,
                            )
                            s1, _ = parse_order_ids(sell1)
                            emit_order_event(event="ORDER_PLACED", bot_id=bot_id, timeframe=timeframe, instrument=instrument, ref_id=str(s1 or ""))
                            sell2 = client.place_stop_order(
                                units=-leg_units,
                                instrument=instrument,
                                price=sell_entry,
                                stop_loss=buy_entry,
                                take_profit=sell_entry - tp_dist,
                            )
                            s2, _ = parse_order_ids(sell2)
                            emit_order_event(event="ORDER_PLACED", bot_id=bot_id, timeframe=timeframe, instrument=instrument, ref_id=str(s2 or ""))
                            kv_json_set(
                                bot_id,
                                state_key,
                                {
                                    "mode": "pending",
                                    "inside_bar": latest.isoformat(),
                                    "buy_entry": buy_entry,
                                    "sell_entry": sell_entry,
                                    "buy_orders": [x for x in [b1, b2] if x],
                                    "sell_orders": [x for x in [s1, s2] if x],
                                },
                            )

                set_processed_candle_bar(bot_id, key, latest)
                last_processed_bar_by_instrument[instrument] = latest
                emit_processed_candle(bot_id, timeframe, instrument, latest)
        except Exception as e:
            log.exception("INSIDE_DAYS_ERROR | bot_id=%s | account_id=%s | err=%s", bot_id, account_id, e)
            time.sleep(5)
