from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from shared.oanda_client import OandaClient
from shared.state_store import (
    kv_get,
    kv_set,
    trade_close_by_trade_id,
    trade_open_rows,
)


def kv_json_get(bot_id: str, key: str) -> dict[str, Any] | None:
    raw = kv_get(bot_id, key)
    if not raw:
        return None
    return json.loads(raw)


def kv_json_set(bot_id: str, key: str, obj: dict[str, Any]) -> None:
    kv_set(bot_id, key, json.dumps(obj, separators=(",", ":")))


def select_latest_complete_candle(candles: list[dict[str, Any]]) -> tuple[pd.Timestamp, dict[str, Any]] | None:
    for candle in reversed(candles):
        if not isinstance(candle, dict):
            continue
        if candle.get("complete") is not True:
            continue
        ts_raw = candle.get("time")
        if not ts_raw:
            continue
        try:
            ts = pd.to_datetime(str(ts_raw), utc=True)
        except Exception:
            continue
        return ts, candle
    return None


def emit_processed_candle(bot_id: str, timeframe: str, instrument: str, candle_time: pd.Timestamp) -> None:
    print(
        f"PROCESSED_CANDLE bot={bot_id} tf={str(timeframe).upper()} inst={instrument} t={candle_time.isoformat()}",
        flush=True,
    )


def emit_order_event(
    *,
    event: str,
    bot_id: str,
    timeframe: str,
    instrument: str,
    ref_id: str = "",
) -> None:
    suffix = f" ref={ref_id}" if ref_id else ""
    print(
        f"{event} bot={bot_id} tf={str(timeframe).upper()} inst={instrument}{suffix}",
        flush=True,
    )


def last_processed_day_key(instrument: str, suffix: str = "") -> str:
    return f"daily_last_processed_{instrument}{suffix}"


def should_process_candle_day(bot_id: str, key: str, latest_day: pd.Timestamp) -> bool:
    last = kv_get(bot_id, key)
    day = latest_day.date().isoformat()
    return last != day


def set_processed_candle_day(bot_id: str, key: str, latest_day: pd.Timestamp) -> None:
    kv_set(bot_id, key, latest_day.date().isoformat())


def last_processed_bar_key(instrument: str, timeframe: str, suffix: str = "") -> str:
    tf = str(timeframe).strip().upper()
    return f"bar_last_processed_{tf}_{instrument}{suffix}"


def should_process_candle_bar(
    bot_id: str,
    key: str,
    latest_bar: pd.Timestamp,
    last_processed_bar_time: pd.Timestamp | None = None,
) -> bool:
    if last_processed_bar_time is not None and latest_bar <= last_processed_bar_time:
        return False
    return kv_get(bot_id, key) != latest_bar.isoformat()


def set_processed_candle_bar(bot_id: str, key: str, latest_bar: pd.Timestamp) -> None:
    kv_set(bot_id, key, latest_bar.isoformat())


def reconcile_closed_trades(bot_id: str, client: OandaClient) -> None:
    open_rows = trade_open_rows(bot_id)
    if not open_rows:
        return

    open_resp = client.list_open_trades()
    open_ids = {str(x.get("id")) for x in open_resp.get("trades", [])}

    for row in open_rows:
        trade_id = str(row["trade_id"] or "")
        if not trade_id or trade_id in open_ids:
            continue

        detail = client.get_trade(trade_id).get("trade", {})
        close_time = str(detail.get("closeTime", datetime.now(timezone.utc).isoformat()))
        realized = float(detail.get("realizedPL", 0.0))
        open_time = pd.to_datetime(str(row["open_time"]), utc=True)
        close_ts = pd.to_datetime(close_time, utc=True)
        dur = int((close_ts - open_time).total_seconds())
        trade_close_by_trade_id(
            bot_id=bot_id,
            trade_id=trade_id,
            close_time=close_ts.isoformat(),
            duration_seconds=max(0, dur),
            realized_pnl=realized,
        )
