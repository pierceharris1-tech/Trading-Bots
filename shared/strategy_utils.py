from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


def candles_to_df(candles_json: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for c in candles_json.get("candles", []):
        if not c.get("complete"):
            continue
        rows.append(
            {
                "time": c["time"],
                "open": float(c["mid"]["o"]),
                "high": float(c["mid"]["h"]),
                "low": float(c["mid"]["l"]),
                "close": float(c["mid"]["c"]),
            }
        )
    if not rows:
        raise RuntimeError("No completed candles returned")
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


def bid_ask_candles_to_df(candles_json: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for c in candles_json.get("candles", []):
        if not c.get("complete"):
            continue
        row: dict[str, Any] = {"time": c["time"]}
        if c.get("bid") is not None:
            row.update(
                {
                    "bid_open": float(c["bid"]["o"]),
                    "bid_high": float(c["bid"]["h"]),
                    "bid_low": float(c["bid"]["l"]),
                    "bid_close": float(c["bid"]["c"]),
                }
            )
        if c.get("ask") is not None:
            row.update(
                {
                    "ask_open": float(c["ask"]["o"]),
                    "ask_high": float(c["ask"]["h"]),
                    "ask_low": float(c["ask"]["l"]),
                    "ask_close": float(c["ask"]["c"]),
                }
            )
        rows.append(row)
    if not rows:
        raise RuntimeError("No completed bid/ask candles returned")
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_order_ids(resp: dict[str, Any]) -> tuple[str | None, str | None]:
    order_id = None
    trade_id = None
    create_tx = resp.get("orderCreateTransaction") or {}
    fill_tx = resp.get("orderFillTransaction") or {}
    if create_tx.get("id") is not None:
        order_id = str(create_tx["id"])
    elif fill_tx.get("orderID") is not None:
        order_id = str(fill_tx["orderID"])

    opened = fill_tx.get("tradeOpened") or {}
    if opened.get("tradeID") is not None:
        trade_id = str(opened["tradeID"])
    elif fill_tx.get("tradeReduced", {}).get("tradeID") is not None:
        trade_id = str(fill_tx["tradeReduced"]["tradeID"])
    return order_id, trade_id


def parse_realized_pl(close_resp: dict[str, Any]) -> float:
    tx = close_resp.get("orderFillTransaction") or {}
    if tx.get("pl") is not None:
        return float(tx["pl"])
    reduced = tx.get("tradesClosed") or []
    if reduced:
        return float(sum(float(x.get("realizedPL", 0.0)) for x in reduced))
    return 0.0


def pip_buffer_for_instrument(instrument: str) -> float:
    if instrument == "XAU_USD":
        return 0.1
    if instrument.endswith("JPY"):
        return 0.01
    return 0.0001
