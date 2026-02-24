from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import pandas as pd

from shared.bot_config import cfg_instruments, cfg_timeframe, require_account_id
from shared.config import load_settings
from shared.daily_engine import (
    kv_json_get,
    kv_json_set,
    last_processed_bar_key,
    reconcile_closed_trades,
    set_processed_candle_bar,
    should_process_candle_bar,
)
from shared.indicators import adx, sma
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.risk import clamp_units, units_from_risk
from shared.state_store import get_equity, kv_set, trade_close_by_trade_id, trade_open, trade_open_for_key
from shared.strategy_utils import candles_to_df, parse_order_ids, parse_realized_pl
from shared.time_utils import seconds_until_next_close

log = get_logger("bot.channel_breakout_daily")


def _close_position(bot_id: str, client: OandaClient, state: dict, now_ts: pd.Timestamp) -> None:
    trade_id = str(state.get("trade_id", ""))
    if not trade_id:
        return
    close_resp = client.close_trade(trade_id)
    realized = parse_realized_pl(close_resp)
    open_ts = pd.to_datetime(str(state["open_time"]), utc=True)
    duration = int((now_ts - open_ts).total_seconds())
    trade_close_by_trade_id(
        bot_id=bot_id,
        trade_id=trade_id,
        close_time=now_ts.isoformat(),
        duration_seconds=max(0, duration),
        realized_pnl=realized,
    )


def _open_market(
    *,
    bot_id: str,
    instrument: str,
    account_id: str,
    client: OandaClient,
    units: int,
    stop_px: float,
    now_ts: pd.Timestamp,
) -> dict:
    resp = client.place_market_order(units=units, instrument=instrument, stop_loss=stop_px)
    order_id, trade_id = parse_order_ids(resp)
    trade_open(
        bot_id=bot_id,
        instrument=instrument,
        account_id=account_id,
        open_time=now_ts.isoformat(),
        order_id=order_id,
        trade_id=trade_id,
    )
    return {"order_id": order_id, "trade_id": trade_id}


def _can_take_long_hh55(hh55: pd.Series, t_idx: int) -> bool:
    # HH55[t-1] <= HH55[t-2] <= ... <= HH55[t-6]
    vals = [float(hh55.iloc[t_idx - i]) for i in range(1, 7)]
    return all(vals[i] <= vals[i + 1] for i in range(0, 5))


def _process_channel_breakout(
    *,
    bot_id: str,
    account_id: str,
    client: OandaClient,
    instrument: str,
    bars: pd.DataFrame,
    cfg: dict,
) -> None:
    latest = bars.index[-1]
    t_idx = len(bars) - 1
    if len(bars) < 80:
        return

    bars = bars.copy()
    bars["adx14"] = adx(bars, 14)
    bars["hh55"] = bars["high"].rolling(55).max().shift(1)
    bars["ll55"] = bars["low"].rolling(55).min().shift(1)
    bars["hh20"] = bars["high"].rolling(20).max().shift(1)
    bars["ll20"] = bars["low"].rolling(20).min().shift(1)

    key = f"pos_channel_breakout_{instrument}"
    pos = kv_json_get(bot_id, key)
    close_t = float(bars["close"].iloc[t_idx])
    adx_t = float(bars["adx14"].iloc[t_idx]) if not pd.isna(bars["adx14"].iloc[t_idx]) else math.nan
    adx_prev = float(bars["adx14"].iloc[t_idx - 1]) if not pd.isna(bars["adx14"].iloc[t_idx - 1]) else math.nan

    if pos is not None:
        side = int(pos["side"])
        breakout_level = float(pos.get("breakout_level", 0.0))
        rejection_left = int(pos.get("rejection_days_left", 0))
        risk_per_unit = float(pos.get("risk_per_unit", 1.0))
        deep_profit = bool(pos.get("deep_profit", False))

        if rejection_left > 0 and bool(cfg.get("rejection_rule", True)):
            ok = close_t > breakout_level if side > 0 else close_t < breakout_level
            if not ok:
                _close_position(bot_id, client, pos, latest)
                kv_set(bot_id, key, "")
                return
            pos["rejection_days_left"] = rejection_left - 1

        # update deep profit flag once unrealized >= 2R
        entry = float(pos["entry_price"])
        unrealized_r = (close_t - entry) / risk_per_unit if side > 0 else (entry - close_t) / risk_per_unit
        if unrealized_r >= 2.0:
            pos["deep_profit"] = True
            deep_profit = True

        # ADX profit exit
        if not math.isnan(adx_t) and not math.isnan(adx_prev) and adx_t > 40.0 and adx_t < adx_prev:
            _close_position(bot_id, client, pos, latest)
            kv_set(bot_id, key, "")
            return

        # 20-day channel exit after deep profit
        if deep_profit:
            ll20 = float(bars["ll20"].iloc[t_idx]) if not pd.isna(bars["ll20"].iloc[t_idx]) else math.nan
            hh20 = float(bars["hh20"].iloc[t_idx]) if not pd.isna(bars["hh20"].iloc[t_idx]) else math.nan
            if side > 0 and not math.isnan(ll20) and close_t < ll20:
                _close_position(bot_id, client, pos, latest)
                kv_set(bot_id, key, "")
                return
            if side < 0 and not math.isnan(hh20) and close_t > hh20:
                _close_position(bot_id, client, pos, latest)
                kv_set(bot_id, key, "")
                return

        # Re-trigger in same direction updates stop to breakout level for rejection window.
        hh55 = bars["hh55"].iloc[t_idx]
        ll55 = bars["ll55"].iloc[t_idx]
        if side > 0 and not pd.isna(hh55) and close_t > float(hh55) and bool(cfg.get("rejection_rule", True)):
            pos["breakout_level"] = float(hh55)
            pos["rejection_days_left"] = 2
        if side < 0 and not pd.isna(ll55) and close_t < float(ll55) and bool(cfg.get("rejection_rule", True)):
            pos["breakout_level"] = float(ll55)
            pos["rejection_days_left"] = 2

        kv_json_set(bot_id, key, pos)
        return

    # flat: evaluate entry
    hh55_t = bars["hh55"].iloc[t_idx]
    ll55_t = bars["ll55"].iloc[t_idx]
    if pd.isna(hh55_t) or pd.isna(ll55_t) or math.isnan(adx_t) or math.isnan(adx_prev):
        return
    if not (adx_t > adx_prev):
        return

    go_long = close_t > float(hh55_t)
    go_short = close_t < float(ll55_t)

    if go_long and t_idx >= 6:
        if not _can_take_long_hh55(bars["hh55"], t_idx):
            go_long = False

    if not go_long and not go_short:
        return

    side = 1 if go_long else -1
    breakout_level = float(hh55_t if go_long else ll55_t)
    stop_px = float(bars["low"].iloc[t_idx] if go_long else bars["high"].iloc[t_idx])
    entry_px = close_t
    equity = float(get_equity(bot_id))
    units_mag, _, _ = units_from_risk(
        equity=equity,
        risk_pct=0.01,
        entry=entry_px,
        stop=stop_px,
        leverage=float(cfg.get("leverage", 20.0)),
    )
    units = clamp_units(side * int(units_mag))
    if units == 0:
        return

    placed = _open_market(
        bot_id=bot_id,
        instrument=instrument,
        account_id=account_id,
        client=client,
        units=units,
        stop_px=stop_px,
        now_ts=latest,
    )
    kv_json_set(
        bot_id,
        key,
        {
            "trade_id": placed.get("trade_id"),
            "order_id": placed.get("order_id"),
            "open_time": latest.isoformat(),
            "side": side,
            "entry_price": entry_px,
            "stop_px": stop_px,
            "risk_per_unit": abs(entry_px - stop_px),
            "breakout_level": breakout_level,
            "rejection_days_left": 2 if bool(cfg.get("rejection_rule", True)) else 0,
            "deep_profit": False,
        },
    )


def _process_reversal_days(
    *,
    bot_id: str,
    account_id: str,
    client: OandaClient,
    instrument: str,
    bars: pd.DataFrame,
    cfg: dict,
) -> None:
    if len(bars) < 15:
        return
    latest = bars.index[-1]
    t_idx = len(bars) - 1
    bars = bars.copy()
    bars["ma7"] = sma(bars["close"], 7)
    key = f"pos_reversal_days_{instrument}"
    pos = kv_json_get(bot_id, key)

    if pos is not None:
        side = int(pos["side"])
        stop_px = float(pos["stop_px"])
        entry_bar = pd.to_datetime(str(pos["entry_bar"]), utc=True)
        low_t = float(bars["low"].iloc[t_idx])
        high_t = float(bars["high"].iloc[t_idx])

        stopped = (side > 0 and low_t <= stop_px) or (side < 0 and high_t >= stop_px)
        if stopped:
            _close_position(bot_id, client, pos, latest)
            kv_set(bot_id, key, "")
            # lemonade flip
            flip_side = -side
            flip_stop = high_t if flip_side < 0 else low_t
            entry_px = float(bars["close"].iloc[t_idx])
            equity = float(get_equity(bot_id))
            units_mag, _, _ = units_from_risk(
                equity=equity,
                risk_pct=0.01,
                entry=entry_px,
                stop=flip_stop,
                leverage=float(cfg.get("leverage", 20.0)),
            )
            units = clamp_units(flip_side * int(units_mag))
            if units != 0:
                placed = _open_market(
                    bot_id=bot_id,
                    instrument=instrument,
                    account_id=account_id,
                    client=client,
                    units=units,
                    stop_px=flip_stop,
                    now_ts=latest,
                )
                kv_json_set(
                    bot_id,
                    key,
                    {
                        "trade_id": placed.get("trade_id"),
                        "order_id": placed.get("order_id"),
                        "open_time": latest.isoformat(),
                        "entry_bar": latest.isoformat(),
                        "side": flip_side,
                        "entry_price": entry_px,
                        "stop_px": flip_stop,
                    },
                )
            return

        # close on t+1 close
        if latest > entry_bar:
            _close_position(bot_id, client, pos, latest)
            kv_set(bot_id, key, "")
        return

    # flat: detect reversal
    low_t = float(bars["low"].iloc[t_idx])
    low_prev = float(bars["low"].iloc[t_idx - 1])
    high_t = float(bars["high"].iloc[t_idx])
    high_prev = float(bars["high"].iloc[t_idx - 1])
    close_t = float(bars["close"].iloc[t_idx])
    close_prev = float(bars["close"].iloc[t_idx - 1])
    ma_t = bars["ma7"].iloc[t_idx]
    ma_prev = bars["ma7"].iloc[t_idx - 1]
    if pd.isna(ma_t) or pd.isna(ma_prev):
        return

    bull = low_t < low_prev and close_t > close_prev and ma_t > ma_prev
    bear = high_t > high_prev and close_t < close_prev and ma_t < ma_prev
    if not bull and not bear:
        return

    side = 1 if bull else -1
    stop_px = low_t if bull else high_t
    equity = float(get_equity(bot_id))
    units_mag, _, _ = units_from_risk(
        equity=equity,
        risk_pct=0.01,
        entry=close_t,
        stop=stop_px,
        leverage=float(cfg.get("leverage", 20.0)),
    )
    units = clamp_units(side * int(units_mag))
    if units == 0:
        return

    placed = _open_market(
        bot_id=bot_id,
        instrument=instrument,
        account_id=account_id,
        client=client,
        units=units,
        stop_px=stop_px,
        now_ts=latest,
    )
    kv_json_set(
        bot_id,
        key,
        {
            "trade_id": placed.get("trade_id"),
            "order_id": placed.get("order_id"),
            "open_time": latest.isoformat(),
            "entry_bar": latest.isoformat(),
            "side": side,
            "entry_price": close_t,
            "stop_px": stop_px,
        },
    )


def main(cfg: dict | None = None) -> None:
    if cfg is None:
        cfg = {}
    settings = load_settings()
    bot_id = str(cfg.get("bot_id", "CHANNEL_BREAKOUT"))
    account_id = require_account_id(cfg)
    timeframe = cfg_timeframe(cfg, default_timeframe="D")
    sub_strategy = str(cfg.get("sub_strategy", "channel_breakout")).strip().lower()
    if sub_strategy not in {"channel_breakout", "reversal_days"}:
        raise RuntimeError(f"Unsupported sub_strategy={sub_strategy}")

    client = OandaClient(
        base_url=settings.base_url,
        api_key=settings.oanda_api_key,
        account_id=account_id,
        bot_id=bot_id,
        timeframe=timeframe,
    )
    instruments = cfg_instruments(cfg, settings.instrument)
    last_processed_bar_by_instrument: dict[str, pd.Timestamp] = {}

    while True:
        try:
            time.sleep(seconds_until_next_close(timeframe) + 1)
            reconcile_closed_trades(bot_id, client)
            for instrument in instruments:
                candles = client.get_candles(instrument=instrument, granularity=timeframe, count=400)
                bars = candles_to_df(candles)
                latest = bars.index[-1]
                process_key = last_processed_bar_key(instrument, timeframe, f"_{sub_strategy}")
                if not should_process_candle_bar(
                    bot_id,
                    process_key,
                    latest,
                    last_processed_bar_time=last_processed_bar_by_instrument.get(instrument),
                ):
                    continue

                if sub_strategy == "reversal_days":
                    _process_reversal_days(
                        bot_id=bot_id,
                        account_id=account_id,
                        client=client,
                        instrument=instrument,
                        bars=bars,
                        cfg=cfg,
                    )
                else:
                    _process_channel_breakout(
                        bot_id=bot_id,
                        account_id=account_id,
                        client=client,
                        instrument=instrument,
                        bars=bars,
                        cfg=cfg,
                    )
                set_processed_candle_bar(bot_id, process_key, latest)
                last_processed_bar_by_instrument[instrument] = latest
        except Exception as e:
            log.exception("CHANNEL_BREAKOUT_ERROR | bot_id=%s | account_id=%s | err=%s", bot_id, account_id, e)
            time.sleep(5)
