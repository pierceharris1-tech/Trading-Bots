from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


TIMEFRAME_MIN = 60
STRUCTURE_SIZE = 5
ATR_PERIOD = 14
FROZEN_MIN_DISPLACEMENT_ATR = 2.0
FROZEN_MIN_BROKEN_STRUCTURE_ATR = 1.0
FROZEN_MIN_CLOSE_THROUGH_ATR = 0.25
RETRACE = 0.618
PENDING_EXPIRY_BARS = 20
MAX_ENTRY_DISTANCE_ATR = 2.0
FROZEN_TP_R = 1.5
MAX_HOLD_BARS = 120


@dataclass(frozen=True)
class S1Result:
    client_order_key: str
    status: str
    event_time: pd.Timestamp
    direction: str
    origin: float
    event_atr: float
    event_extreme: float
    running_extreme: float
    entry_time: pd.Timestamp | None
    exit_time: pd.Timestamp | None
    entry: float | None
    stop: float | None
    target: float | None
    risk: float | None
    outcome: str | None
    net_r: float | None
    cancellation_reason: str | None
    payload: dict[str, Any]


def add_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.DataFrame:
    x = df.copy()
    prev = x["close"].shift(1)
    tr = pd.concat(
        [
            x["high"] - x["low"],
            (x["high"] - prev).abs(),
            (x["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    x["atr"] = tr.rolling(period, min_periods=period).mean()
    return x


def normalize_signal_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    rename = {
        "bid_open": "bidopen",
        "bid_high": "bidhigh",
        "bid_low": "bidlow",
        "bid_close": "bidclose",
        "ask_open": "askopen",
        "ask_high": "askhigh",
        "ask_low": "asklow",
        "ask_close": "askclose",
    }
    x = x.rename(columns={k: v for k, v in rename.items() if k in x.columns})
    if all(c in x.columns for c in ("bidopen", "bidhigh", "bidlow", "bidclose", "askopen", "askhigh", "asklow", "askclose")):
        x["open"] = (x["bidopen"] + x["askopen"]) / 2
        x["high"] = (x["bidhigh"] + x["askhigh"]) / 2
        x["low"] = (x["bidlow"] + x["asklow"]) / 2
        x["close"] = (x["bidclose"] + x["askclose"]) / 2
    return x.dropna(subset=["open", "high", "low", "close"]).sort_index()


def detect_structure_events(df: pd.DataFrame, size: int = STRUCTURE_SIZE) -> pd.DataFrame:
    x = add_atr(normalize_signal_ohlc(df), ATR_PERIOD)
    if len(x) <= size + 2:
        return pd.DataFrame()

    highs = x["high"].to_numpy(float)
    lows = x["low"].to_numpy(float)
    closes = x["close"].to_numpy(float)
    times = x.index
    atrs = x["atr"].to_numpy(float)
    h = x["high"]
    l = x["low"]
    new_leg_high = (h.shift(size) > h.rolling(size).max()).fillna(False).to_numpy(bool)
    new_leg_low = (l.shift(size) < l.rolling(size).min()).fillna(False).to_numpy(bool)

    bearish_leg = 0
    bullish_leg = 1
    bearish = -1
    bullish = 1
    leg = bearish_leg
    trend = 0
    pivot_high = None
    pivot_low = None
    pivot_high_crossed = True
    pivot_low_crossed = True
    events: list[dict[str, Any]] = []

    for i in range(size, len(x)):
        previous_leg = leg
        if new_leg_high[i]:
            leg = bearish_leg
        elif new_leg_low[i]:
            leg = bullish_leg

        if leg != previous_leg:
            src = i - size
            if leg == bullish_leg:
                pivot_low = {"price": float(lows[src]), "pivot_time": times[src], "available_time": times[i]}
                pivot_low_crossed = False
            else:
                pivot_high = {"price": float(highs[src]), "pivot_time": times[src], "available_time": times[i]}
                pivot_high_crossed = False

        previous_close = closes[i - 1]
        close = closes[i]

        if pivot_high is not None and not pivot_high_crossed:
            level = pivot_high["price"]
            if previous_close <= level and close > level:
                tag = "CHoCH" if trend == bearish else "BOS"
                trend = bullish
                pivot_high_crossed = True
                if pivot_low is not None and pivot_low["available_time"] <= times[i]:
                    origin = float(pivot_low["price"])
                    extreme = float(highs[i])
                    atr = float(atrs[i]) if not np.isnan(atrs[i]) else np.nan
                    events.append(
                        {
                            "event_time": times[i],
                            "bar_pos": i,
                            "type": tag,
                            "direction": "bullish",
                            "broken_level": level,
                            "origin_price": origin,
                            "event_extreme": extreme,
                            "atr": atr,
                            "break_strength_atr": (close - level) / atr if atr and atr > 0 else np.nan,
                        }
                    )

        if pivot_low is not None and not pivot_low_crossed:
            level = pivot_low["price"]
            if previous_close >= level and close < level:
                tag = "CHoCH" if trend == bullish else "BOS"
                trend = bearish
                pivot_low_crossed = True
                if pivot_high is not None and pivot_high["available_time"] <= times[i]:
                    origin = float(pivot_high["price"])
                    extreme = float(lows[i])
                    atr = float(atrs[i]) if not np.isnan(atrs[i]) else np.nan
                    events.append(
                        {
                            "event_time": times[i],
                            "bar_pos": i,
                            "type": tag,
                            "direction": "bearish",
                            "broken_level": level,
                            "origin_price": origin,
                            "event_extreme": extreme,
                            "atr": atr,
                            "break_strength_atr": (level - close) / atr if atr and atr > 0 else np.nan,
                        }
                    )

    ev = pd.DataFrame(events)
    if not ev.empty:
        ev["initial_displacement_atr"] = np.where(
            ev["direction"] == "bullish",
            (ev["event_extreme"] - ev["origin_price"]) / ev["atr"],
            (ev["origin_price"] - ev["event_extreme"]) / ev["atr"],
        )
        ev["broken_structure_size"] = (ev["broken_level"] - ev["origin_price"]).abs()
        ev["broken_structure_atr"] = ev["broken_structure_size"] / ev["atr"].replace(0, np.nan)
    return ev


def qualifying_choch_events(events: pd.DataFrame) -> pd.DataFrame:
    if events is None or events.empty:
        return pd.DataFrame()
    return events[
        (events["type"] == "CHoCH")
        & (events["broken_structure_atr"] >= FROZEN_MIN_BROKEN_STRUCTURE_ATR)
        & (events["break_strength_atr"] >= FROZEN_MIN_CLOSE_THROUGH_ATR)
    ].copy()


def build_supersession_maps(events: pd.DataFrame) -> dict[int, dict[str, Any]]:
    if events is None or events.empty:
        return {}
    e = events.sort_values("event_time").reset_index(drop=True)
    out: dict[int, dict[str, Any]] = {}
    for i, row in e.iterrows():
        next_choch = None
        next_opposite_bos = None
        for j in range(i + 1, len(e)):
            r = e.iloc[j]
            if next_choch is None and r["type"] == "CHoCH":
                next_choch = r["event_time"]
            if next_opposite_bos is None and r["type"] == "BOS" and r["direction"] != row["direction"]:
                next_opposite_bos = r["event_time"]
            if next_choch is not None and next_opposite_bos is not None:
                break
        out[int(row["bar_pos"])] = {"next_choch": next_choch, "next_opposite_bos": next_opposite_bos}
    return out


def retracement_price(origin: float, extreme: float, direction: str, retrace: float = RETRACE) -> float:
    if direction == "bullish":
        return extreme - retrace * (extreme - origin)
    return extreme + retrace * (origin - extreme)


def block_s1_entry(entry_time: pd.Timestamp) -> bool:
    t = pd.Timestamp(entry_time)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.weekday() == 4 and t.hour in {7, 8}


def pending_limit_filled(bar: pd.Series, direction: str, limit_price: float) -> bool:
    if direction == "bullish":
        if "asklow" in bar.index:
            return float(bar["asklow"]) <= limit_price
        return float(bar["low"]) <= limit_price
    if "bidhigh" in bar.index:
        return float(bar["bidhigh"]) >= limit_price
    return float(bar["high"]) >= limit_price


def executable_side(bar: pd.Series, direction: str) -> dict[str, float]:
    if direction == "bullish" and "bidlow" in bar.index:
        return {"high": float(bar["bidhigh"]), "low": float(bar["bidlow"]), "close": float(bar["bidclose"])}
    if direction == "bearish" and "asklow" in bar.index:
        return {"high": float(bar["askhigh"]), "low": float(bar["asklow"]), "close": float(bar["askclose"])}
    return {"high": float(bar["high"]), "low": float(bar["low"]), "close": float(bar["close"])}


def s1_price_at_r(entry: float, risk: float, direction: str, r_value: float) -> float:
    if direction == "bullish":
        return entry + r_value * risk
    return entry - r_value * risk


def deterministic_s1_key(instrument: str, event_time: pd.Timestamp, direction: str) -> str:
    compact_time = pd.Timestamp(event_time).tz_convert("UTC").strftime("%Y%m%dT%H%M%SZ")
    side = "LONG" if direction == "bullish" else "SHORT"
    return f"CHOCH-S1-{instrument.replace('_', '')}-{compact_time}-{side}"[:128]


def simulate_s1_event(df: pd.DataFrame, event: pd.Series, supersession_info: dict[str, Any]) -> S1Result | None:
    x = normalize_signal_ohlc(df)
    direction = str(event["direction"])
    event_pos = int(event["bar_pos"])
    event_time = pd.Timestamp(event["event_time"])
    origin = float(event["origin_price"])
    atr = float(event["atr"])
    if np.isnan(atr) or atr <= 0:
        return None
    running_extreme = float(event["event_extreme"])
    armed = False
    armed_pos = None
    limit_price = None
    timing_rejections: list[str] = []
    next_choch = supersession_info.get("next_choch")
    next_opposite_bos = supersession_info.get("next_opposite_bos")

    def displacement(extreme: float) -> float:
        if direction == "bullish":
            return (extreme - origin) / atr
        return (origin - extreme) / atr

    def cancel(status: str, armed_value: bool) -> S1Result:
        return S1Result(
            client_order_key=deterministic_s1_key(str(event.get("instrument", "UNKNOWN")), event_time, direction),
            status=status,
            event_time=event_time,
            direction=direction,
            origin=origin,
            event_atr=atr,
            event_extreme=float(event["event_extreme"]),
            running_extreme=running_extreme,
            entry_time=None,
            exit_time=None,
            entry=None,
            stop=origin,
            target=None,
            risk=None,
            outcome=None,
            net_r=None,
            cancellation_reason=status,
            payload={"armed": armed_value, "timing_rejections": timing_rejections},
        )

    if displacement(running_extreme) >= FROZEN_MIN_DISPLACEMENT_ATR:
        armed = True
        armed_pos = event_pos
        limit_price = retracement_price(origin, running_extreme, direction)

    search_end = min(len(x), event_pos + 1 + PENDING_EXPIRY_BARS + 100)
    for pos in range(event_pos + 1, search_end):
        t = pd.Timestamp(x.index[pos])
        bar = x.iloc[pos]
        if next_choch is not None and t >= pd.Timestamp(next_choch):
            return cancel("cancel_new_choch", armed)
        if next_opposite_bos is not None and t >= pd.Timestamp(next_opposite_bos):
            return cancel("cancel_opposite_bos", armed)

        if armed and limit_price is not None:
            if pos - int(armed_pos) > PENDING_EXPIRY_BARS:
                return cancel("cancel_expired", True)
            if pending_limit_filled(bar, direction, limit_price):
                if block_s1_entry(t):
                    timing_rejections.append(t.isoformat())
                else:
                    entry_price = float(limit_price)
                    stop = origin
                    risk = abs(entry_price - stop)
                    if risk <= 0:
                        return None
                    target = s1_price_at_r(entry_price, risk, direction, FROZEN_TP_R)
                    future = x.iloc[pos : min(len(x), pos + MAX_HOLD_BARS)]
                    outcome = None
                    exit_time = None
                    exit_price = None
                    for exit_t, exit_bar in future.iterrows():
                        side = executable_side(exit_bar, direction)
                        if direction == "bullish":
                            sl_hit = side["low"] <= stop
                            tp_hit = side["high"] >= target
                        else:
                            sl_hit = side["high"] >= stop
                            tp_hit = side["low"] <= target
                        if sl_hit:
                            outcome = "sl"
                            exit_time = pd.Timestamp(exit_t)
                            exit_price = stop
                            break
                        if tp_hit:
                            outcome = "tp"
                            exit_time = pd.Timestamp(exit_t)
                            exit_price = target
                            break
                    if outcome is None:
                        last = future.iloc[-1]
                        exit_time = pd.Timestamp(future.index[-1])
                        exit_price = executable_side(last, direction)["close"]
                        outcome = "timeout"
                    net_r = (exit_price - entry_price) / risk if direction == "bullish" else (entry_price - exit_price) / risk
                    return S1Result(
                        client_order_key=deterministic_s1_key(str(event.get("instrument", "UNKNOWN")), event_time, direction),
                        status="filled",
                        event_time=event_time,
                        direction=direction,
                        origin=origin,
                        event_atr=atr,
                        event_extreme=float(event["event_extreme"]),
                        running_extreme=running_extreme,
                        entry_time=t,
                        exit_time=exit_time,
                        entry=entry_price,
                        stop=stop,
                        target=target,
                        risk=risk,
                        outcome=outcome,
                        net_r=float(net_r),
                        cancellation_reason=None,
                        payload={"armed": True, "frozen_extreme": running_extreme, "timing_rejections": timing_rejections},
                    )

        if direction == "bullish" and float(bar["low"]) <= origin:
            return cancel("cancel_origin_break", armed)
        if direction == "bearish" and float(bar["high"]) >= origin:
            return cancel("cancel_origin_break", armed)

        new_extreme = False
        if direction == "bullish":
            e = float(bar["high"])
            if e > running_extreme:
                running_extreme = e
                new_extreme = True
        else:
            e = float(bar["low"])
            if e < running_extreme:
                running_extreme = e
                new_extreme = True
        current_disp = displacement(running_extreme)
        if not armed and current_disp >= FROZEN_MIN_DISPLACEMENT_ATR:
            armed = True
            armed_pos = pos
            limit_price = retracement_price(origin, running_extreme, direction)
        elif armed and new_extreme:
            limit_price = retracement_price(origin, running_extreme, direction)
        if armed and limit_price is not None:
            distance_atr = abs(float(bar["close"]) - limit_price) / atr
            if distance_atr > MAX_ENTRY_DISTANCE_ATR:
                return cancel("cancel_too_far", True)

    return cancel("unresolved", armed)


def simulate_s1(df: pd.DataFrame, instrument: str) -> list[S1Result]:
    x = normalize_signal_ohlc(df)
    events = detect_structure_events(x, STRUCTURE_SIZE)
    if events.empty:
        return []
    events["instrument"] = instrument
    supers = build_supersession_maps(events)
    rows = qualifying_choch_events(events)
    results: list[S1Result] = []
    for _, event in rows.iterrows():
        result = simulate_s1_event(x, event, supers.get(int(event["bar_pos"]), {}))
        if result is not None:
            results.append(result)
    return results
