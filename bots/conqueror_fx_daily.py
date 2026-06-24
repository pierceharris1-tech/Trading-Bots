from __future__ import annotations

import math
import time

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
from shared.indicators import atr, sma
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.risk import clamp_units, units_from_risk
from shared.state_store import get_equity, kv_set, trade_close_by_trade_id, trade_open
from shared.strategy_utils import candles_to_df, parse_order_ids, parse_realized_pl
from shared.time_utils import seconds_until_next_close

log = get_logger("bot.conqueror_fx_daily")


def _signal_triplet(df: pd.DataFrame, idx: int) -> tuple[float, float, float]:
    ma10 = df["ma10"].iloc[idx]
    ma10_prev10 = df["ma10"].iloc[idx - 10]
    c40 = df["close"].iloc[idx - 40]
    close_t = df["close"].iloc[idx]
    a = float(close_t - ma10)
    b = float(ma10 - ma10_prev10)
    c = float(close_t - c40)
    return a, b, c


def _all_aligned(side: int, a: float, b: float, c: float) -> bool:
    if side > 0:
        return a > 0 and b > 0 and c > 0
    return a < 0 and b < 0 and c < 0


def _close_trade(bot_id: str, client: OandaClient, pos: dict, now_ts: pd.Timestamp) -> None:
    trade_id = str(pos.get("trade_id", ""))
    if not trade_id or trade_id == "None":
        log.warning("Skipping close — trade_id is None for bot_id=%s", bot_id)
        return
    resp = client.close_trade(trade_id)
    realized = parse_realized_pl(resp)
    open_ts = pd.to_datetime(str(pos["open_time"]), utc=True)
    duration = int((now_ts - open_ts).total_seconds())
    trade_close_by_trade_id(
        bot_id=bot_id,
        trade_id=trade_id,
        close_time=now_ts.isoformat(),
        duration_seconds=max(0, duration),
        realized_pnl=realized,
    )


def _open_trade(
    *,
    bot_id: str,
    account_id: str,
    client: OandaClient,
    instrument: str,
    now_ts: pd.Timestamp,
    units: int,
    stop_px: float,
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


def _process_instrument(
    *,
    bot_id: str,
    account_id: str,
    client: OandaClient,
    instrument: str,
    bars: pd.DataFrame,
    cfg: dict,
) -> None:
    if len(bars) < 80:
        return
    bars = bars.copy()
    bars["ma10"] = sma(bars["close"], 10)
    bars["atr40"] = atr(bars, 40)
    t_idx = len(bars) - 1
    latest = bars.index[-1]
    if t_idx < 50:
        return

    a, b, c = _signal_triplet(bars, t_idx)
    long_signal = a > 0 and b > 0 and c > 0
    short_signal = a < 0 and b < 0 and c < 0
    key = f"pos_conqueror_{instrument}"
    pos = kv_json_get(bot_id, key)
    variant = str(cfg.get("variant", "I")).strip().upper()
    base_mult = 2.0 if variant == "I" else 12.0

    if pos is not None:
        side = int(pos["side"])
        atr_mult = float(pos["atr_mult"])
        entry_px = float(pos["entry_price"])
        atr_t = float(bars["atr40"].iloc[t_idx]) if not pd.isna(bars["atr40"].iloc[t_idx]) else math.nan
        if math.isnan(atr_t) or atr_t <= 0:
            return

        # Variant I stop tightening on adverse sign-flip events.
        if variant == "I":
            prev_a, prev_b, prev_c = _signal_triplet(bars, t_idx - 1)
            against_flip = False
            if side > 0:
                against_flip = ((prev_a > 0 and a < 0) or (prev_b > 0 and b < 0) or (prev_c > 0 and c < 0))
            else:
                against_flip = ((prev_a < 0 and a > 0) or (prev_b < 0 and b > 0) or (prev_c < 0 and c > 0))
            if against_flip:
                atr_mult *= 2.0 / 3.0
                pos["atr_mult"] = atr_mult

        stop_px = entry_px - side * (atr_mult * atr_t)
        low_t = float(bars["low"].iloc[t_idx])
        high_t = float(bars["high"].iloc[t_idx])
        stopped = (side > 0 and low_t <= stop_px) or (side < 0 and high_t >= stop_px)
        if stopped:
            aligned = _all_aligned(side, a, b, c)
            _close_trade(bot_id, client, pos, latest)
            kv_set(bot_id, key, "")
            if variant == "I" and aligned:
                # Wait for a flip away, then alignment again before re-entry.
                kv_json_set(bot_id, f"reentry_conqueror_{instrument}", {"side": side, "phase": "wait_flip"})
            return

        pos["stop_px"] = stop_px
        kv_json_set(bot_id, key, pos)
        return

    # flat and maybe blocked by re-entry wait state
    reentry = kv_json_get(bot_id, f"reentry_conqueror_{instrument}")
    if reentry is not None:
        side = int(reentry["side"])
        phase = str(reentry["phase"])
        aligned = _all_aligned(side, a, b, c)
        if phase == "wait_flip" and not aligned:
            reentry["phase"] = "wait_realign"
            kv_json_set(bot_id, f"reentry_conqueror_{instrument}", reentry)
            return
        if phase == "wait_realign" and aligned:
            kv_set(bot_id, f"reentry_conqueror_{instrument}", "")
        else:
            return

    side = 1 if long_signal else (-1 if short_signal else 0)
    if side == 0:
        return

    atr_t = float(bars["atr40"].iloc[t_idx]) if not pd.isna(bars["atr40"].iloc[t_idx]) else math.nan
    if math.isnan(atr_t) or atr_t <= 0:
        return
    entry_px = float(bars["close"].iloc[t_idx])
    stop_dist = base_mult * atr_t
    stop_px = entry_px - side * stop_dist
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

    placed = _open_trade(
        bot_id=bot_id,
        account_id=account_id,
        client=client,
        instrument=instrument,
        now_ts=latest,
        units=units,
        stop_px=stop_px,
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
            "atr_mult": base_mult,
            "stop_px": stop_px,
        },
    )


def main(cfg: dict | None = None) -> None:
    if cfg is None:
        cfg = {}
    settings = load_settings()
    bot_id = str(cfg.get("bot_id", "CONQUEROR_FX"))
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
                bars = candles_to_df(client.get_candles(instrument=instrument, granularity=timeframe, count=300))
                latest = bars.index[-1]
                key = last_processed_bar_key(instrument, timeframe, "_conqueror")
                if not should_process_candle_bar(
                    bot_id,
                    key,
                    latest,
                    last_processed_bar_time=last_processed_bar_by_instrument.get(instrument),
                ):
                    continue
                _process_instrument(
                    bot_id=bot_id,
                    account_id=account_id,
                    client=client,
                    instrument=instrument,
                    bars=bars,
                    cfg=cfg,
                )
                set_processed_candle_bar(bot_id, key, latest)
                last_processed_bar_by_instrument[instrument] = latest
        except Exception as e:
            log.exception("CONQUEROR_ERROR | bot_id=%s | account_id=%s | err=%s", bot_id, account_id, e)
            time.sleep(5)
