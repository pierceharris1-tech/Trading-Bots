from __future__ import annotations

import time

import pandas as pd

from shared.bot_config import cfg_instruments, require_account_id
from shared.config import load_settings
from shared.daily_engine import (
    kv_json_get,
    kv_json_set,
    last_processed_day_key,
    reconcile_closed_trades,
    set_processed_candle_day,
    should_process_candle_day,
)
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.risk import clamp_units, units_from_risk
from shared.state_store import get_equity, kv_set, trade_open, trade_open_for_key
from shared.strategy_utils import candles_to_df, parse_order_ids, pip_buffer_for_instrument
from shared.time_utils import seconds_until_next_close

log = get_logger("bot.slingshot_daily")


def _swing_highs(bars: pd.DataFrame) -> list[int]:
    out: list[int] = []
    for i in range(1, len(bars) - 1):
        if bars["high"].iloc[i] > bars["high"].iloc[i - 1] and bars["high"].iloc[i] > bars["high"].iloc[i + 1]:
            out.append(i)
    return out


def _swing_lows(bars: pd.DataFrame) -> list[int]:
    out: list[int] = []
    for i in range(1, len(bars) - 1):
        if bars["low"].iloc[i] < bars["low"].iloc[i - 1] and bars["low"].iloc[i] < bars["low"].iloc[i + 1]:
            out.append(i)
    return out


def _find_pattern(bars: pd.DataFrame) -> dict | None:
    highs = _swing_highs(bars)
    lows = _swing_lows(bars)
    if len(highs) < 2 or not lows:
        return None

    key_high_idx = None
    prev_high_idx = None
    for i in range(1, len(highs)):
        a = highs[i - 1]
        b = highs[i]
        if bars["high"].iloc[b] > bars["high"].iloc[a]:
            prev_high_idx = a
            key_high_idx = b
    if key_high_idx is None or prev_high_idx is None:
        return None

    # Slingshot high: later swing high that fails below key high.
    key_high_px = float(bars["high"].iloc[key_high_idx])
    sling_idx = None
    for i in highs:
        if i <= key_high_idx:
            continue
        if i - key_high_idx < 20:
            continue
        if i - key_high_idx < 3:
            continue
        if float(bars["high"].iloc[i]) < key_high_px:
            sling_idx = i
            break
    if sling_idx is None:
        return None

    lows_between = [i for i in lows if key_high_idx < i < sling_idx]
    if not lows_between:
        return None
    key_low_idx = lows_between[-1]
    key_low_px = float(bars["low"].iloc[key_low_idx])
    sling_high_px = float(bars["high"].iloc[sling_idx])
    if sling_high_px <= key_low_px:
        return None
    return {
        "key_low_idx": key_low_idx,
        "key_low_px": key_low_px,
        "sling_idx": sling_idx,
        "sling_high_px": sling_high_px,
        "target_dist": sling_high_px - key_low_px,
    }


def _sync_unlogged_open(bot_id: str, account_id: str, client: OandaClient, instrument: str) -> None:
    for tr in client.list_open_trades().get("trades", []):
        if str(tr.get("instrument")) != instrument:
            continue
        tid = str(tr.get("id"))
        if trade_open_for_key(bot_id, instrument, tid) is not None:
            continue
        trade_open(
            bot_id=bot_id,
            instrument=instrument,
            account_id=account_id,
            open_time=str(tr.get("openTime")),
            order_id=str(tr.get("id")),
            trade_id=tid,
        )


def main(cfg: dict | None = None) -> None:
    if cfg is None:
        cfg = {}
    settings = load_settings()
    bot_id = str(cfg.get("bot_id", "SLINGSHOT"))
    account_id = require_account_id(cfg)
    instruments = cfg_instruments(cfg, settings.instrument)
    client = OandaClient(
        base_url=settings.base_url,
        api_key=settings.oanda_api_key,
        account_id=account_id,
        bot_id=bot_id,
    )

    while True:
        try:
            time.sleep(seconds_until_next_close("D") + 1)
            reconcile_closed_trades(bot_id, client)
            for instrument in instruments:
                bars = candles_to_df(client.get_candles(instrument=instrument, granularity="D", count=500))
                latest = bars.index[-1]
                key = last_processed_day_key(instrument, "_slingshot")
                if not should_process_candle_day(bot_id, key, latest):
                    continue

                state_key = f"slingshot_state_{instrument}"
                state = kv_json_get(bot_id, state_key)
                _sync_unlogged_open(bot_id, account_id, client, instrument)

                if state and state.get("mode") == "pending":
                    entry = float(state["entry"])
                    if float(bars["low"].iloc[-1]) <= entry:
                        state["mode"] = "open"
                        state["next_multiple"] = 1
                        state["remaining_frac"] = 1.0
                        kv_json_set(bot_id, state_key, state)

                state = kv_json_get(bot_id, state_key)
                if state and state.get("mode") == "open":
                    open_trades = [
                        x for x in client.list_open_trades().get("trades", []) if str(x.get("instrument")) == instrument and float(x.get("initialUnits", "0")) < 0
                    ]
                    if not open_trades:
                        kv_set(bot_id, state_key, "")
                        set_processed_candle_day(bot_id, key, latest)
                        continue

                    trade_id = str(open_trades[0].get("id"))
                    current_units = abs(int(float(open_trades[0].get("currentUnits", "0"))))
                    target_dist = float(state["target_dist"])
                    entry = float(state["entry"])
                    stop_px = float(state["stop"])
                    low_t = float(bars["low"].iloc[-1])
                    high_t = float(bars["high"].iloc[-1])

                    # local stop management
                    if high_t >= stop_px:
                        client.close_trade(trade_id)
                        kv_set(bot_id, state_key, "")
                        set_processed_candle_day(bot_id, key, latest)
                        continue

                    # first target and subsequent multiples
                    multiple = int(state.get("next_multiple", 1))
                    target_px = entry - (multiple * target_dist)
                    if low_t <= target_px and current_units > 0:
                        to_close = max(1, current_units // 2)
                        client.close_trade(trade_id, units=str(to_close))
                        state["next_multiple"] = multiple + 1
                        # protect 25% profits after first target
                        if multiple == 1:
                            state["stop"] = entry - 0.25 * target_dist
                        kv_json_set(bot_id, state_key, state)

                    # trail at 50% of target distance for shorts
                    trail = float(bars["close"].iloc[-1]) + 0.5 * target_dist
                    state = kv_json_get(bot_id, state_key) or state
                    state["stop"] = min(float(state.get("stop", stop_px)), trail)
                    kv_json_set(bot_id, state_key, state)

                state = kv_json_get(bot_id, state_key)
                if state is None:
                    pattern = _find_pattern(bars)
                    if pattern is not None:
                        buf = pip_buffer_for_instrument(instrument)
                        entry = float(pattern["key_low_px"] - buf)
                        stop_px = float(pattern["sling_high_px"])
                        equity = float(get_equity(bot_id))
                        units_mag, _, _ = units_from_risk(
                            equity=equity,
                            risk_pct=0.01,
                            entry=entry,
                            stop=stop_px,
                            leverage=float(cfg.get("leverage", 20.0)),
                        )
                        units = clamp_units(-int(units_mag))
                        if units != 0:
                            resp = client.place_stop_order(
                                units=units,
                                instrument=instrument,
                                price=entry,
                                stop_loss=stop_px,
                            )
                            order_id, _ = parse_order_ids(resp)
                            kv_json_set(
                                bot_id,
                                state_key,
                                {
                                    "mode": "pending",
                                    "order_id": order_id,
                                    "entry": entry,
                                    "stop": stop_px,
                                    "target_dist": float(pattern["target_dist"]),
                                },
                            )

                set_processed_candle_day(bot_id, key, latest)
        except Exception as e:
            log.exception("SLINGSHOT_ERROR | bot_id=%s | account_id=%s | err=%s", bot_id, account_id, e)
            time.sleep(5)

