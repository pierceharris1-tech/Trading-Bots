from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from bots.choch_live.s1_structure import FROZEN_TP_R, normalize_signal_ohlc


S2_TF_MINUTES = 10
S2_DECISION_BAR = 10
S2_CLOSE_THRESHOLD_R = 0.5750
S2_AGAINST_RATIO = 0.3809
S2_MIN_CONSECUTIVE = 3
S2_STOP_LOOKBACK = 2
S2_TARGET_R = 4.0
S2_MAX_HOLD_MINUTES = 90


@dataclass(frozen=True)
class S2Result:
    parent_s1_key: str
    status: str
    s1_tp_time: pd.Timestamp
    decision_time: pd.Timestamp | None
    original_direction: str
    s2_direction: str
    close_r: float | None
    against_ratio: float | None
    max_against_run: int
    lost_1p5: bool
    reclaimed_after_loss: bool
    entry: float | None
    stop: float | None
    target: float | None
    risk: float | None
    exit_time: pd.Timestamp | None
    outcome: str | None
    r: float | None
    payload: dict[str, Any]


def original_exec_ohlc(bar: pd.Series, original_direction: str) -> tuple[float, float, float, float]:
    if original_direction == "bullish" and "bidopen" in bar.index:
        return float(bar["bidopen"]), float(bar["bidhigh"]), float(bar["bidlow"]), float(bar["bidclose"])
    if original_direction == "bearish" and "askopen" in bar.index:
        return float(bar["askopen"]), float(bar["askhigh"]), float(bar["asklow"]), float(bar["askclose"])
    return float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])


def price_to_original_r(price: float, entry: float, risk: float, direction: str) -> float:
    if direction == "bullish":
        return (price - entry) / risk
    return (entry - price) / risk


def s2_direction_for_s1(original_direction: str) -> str:
    return "bearish" if original_direction == "bullish" else "bullish"


def build_s2_features(
    df: pd.DataFrame,
    *,
    s1_tp_time: pd.Timestamp,
    original_entry: float,
    original_risk: float,
    original_direction: str,
) -> pd.DataFrame:
    x = normalize_signal_ohlc(df)
    window = x[x.index > pd.Timestamp(s1_tp_time)]
    lost_1p5 = False
    reclaimed_after_loss = False
    against_run = 0
    max_against_run = 0
    against_count = 0
    rolling_adverse_prices: list[float] = []
    rows: list[dict[str, Any]] = []
    for bar_num, (signal_time, bar) in enumerate(window.iterrows(), start=1):
        o, h, l, c = original_exec_ohlc(bar, original_direction)
        open_r = price_to_original_r(o, original_entry, original_risk, original_direction)
        close_r = price_to_original_r(c, original_entry, original_risk, original_direction)
        closes_against = close_r < open_r
        if closes_against:
            against_count += 1
            against_run += 1
            max_against_run = max(max_against_run, against_run)
        else:
            against_run = 0
        if close_r < FROZEN_TP_R:
            lost_1p5 = True
        elif lost_1p5 and close_r >= FROZEN_TP_R:
            reclaimed_after_loss = True
        adverse_price = h if original_direction == "bullish" else l
        rolling_adverse_prices.append(adverse_price)
        rows.append(
            {
                "bar_num": int(bar_num),
                "decision_minutes": int(bar_num * S2_TF_MINUTES),
                "signal_time": pd.Timestamp(signal_time),
                "direction": original_direction,
                "signal_close": float(c),
                "signal_high": float(h),
                "signal_low": float(l),
                "open_r": float(open_r),
                "close_r": float(close_r),
                "against_ratio": float(against_count / bar_num),
                "max_against_run": int(max_against_run),
                "lost_1p5": bool(lost_1p5),
                "reclaimed_after_loss": bool(reclaimed_after_loss),
                "roll_adverse_2": float(
                    max(rolling_adverse_prices[-S2_STOP_LOOKBACK:])
                    if original_direction == "bullish"
                    else min(rolling_adverse_prices[-S2_STOP_LOOKBACK:])
                ),
            }
        )
    return pd.DataFrame(rows)


def simulate_s2_from_s1(df: pd.DataFrame, *, parent_s1_key: str, s1: Any) -> S2Result | None:
    if getattr(s1, "outcome", None) != "tp" or getattr(s1, "exit_time", None) is None:
        return None
    s1_tp_time = pd.Timestamp(s1.exit_time)
    original_entry = float(s1.entry)
    original_risk = float(s1.risk)
    original_direction = str(s1.direction)
    s2_direction = s2_direction_for_s1(original_direction)
    features = build_s2_features(
        df,
        s1_tp_time=s1_tp_time,
        original_entry=original_entry,
        original_risk=original_risk,
        original_direction=original_direction,
    )
    if len(features) < S2_DECISION_BAR:
        return S2Result(
            parent_s1_key=parent_s1_key,
            status="observing",
            s1_tp_time=s1_tp_time,
            decision_time=None,
            original_direction=original_direction,
            s2_direction=s2_direction,
            close_r=None,
            against_ratio=None,
            max_against_run=0,
            lost_1p5=False,
            reclaimed_after_loss=False,
            entry=None,
            stop=None,
            target=None,
            risk=None,
            exit_time=None,
            outcome=None,
            r=None,
            payload={"observed_bars": len(features)},
        )
    r = features.iloc[S2_DECISION_BAR - 1]
    qualified = (
        float(r["close_r"]) <= S2_CLOSE_THRESHOLD_R
        and float(r["against_ratio"]) >= S2_AGAINST_RATIO
        and int(r["max_against_run"]) >= S2_MIN_CONSECUTIVE
        and bool(r["lost_1p5"])
        and not bool(r["reclaimed_after_loss"])
    )
    base = {
        "parent_s1_key": parent_s1_key,
        "s1_tp_time": s1_tp_time,
        "decision_time": pd.Timestamp(r["signal_time"]),
        "original_direction": original_direction,
        "s2_direction": s2_direction,
        "close_r": float(r["close_r"]),
        "against_ratio": float(r["against_ratio"]),
        "max_against_run": int(r["max_against_run"]),
        "lost_1p5": bool(r["lost_1p5"]),
        "reclaimed_after_loss": bool(r["reclaimed_after_loss"]),
    }
    if not qualified:
        return S2Result(status="rejected", entry=None, stop=None, target=None, risk=None, exit_time=None, outcome=None, r=None, payload={}, **base)

    entry = float(r["signal_close"])
    stop = float(r["roll_adverse_2"])
    if original_direction == "bullish":
        risk = stop - entry
        target = entry - S2_TARGET_R * risk
    else:
        risk = entry - stop
        target = entry + S2_TARGET_R * risk
    if risk <= 0:
        return S2Result(status="rejected", entry=entry, stop=stop, target=None, risk=risk, exit_time=None, outcome=None, r=None, payload={"reason": "risk_non_positive"}, **base)

    x = normalize_signal_ohlc(df)
    pos = x.index.get_indexer([pd.Timestamp(r["signal_time"])])[0]
    max_hold_bars = max(1, int(np.ceil(S2_MAX_HOLD_MINUTES / S2_TF_MINUTES)))
    future = x.iloc[pos + 1 : min(len(x), pos + 1 + max_hold_bars)]
    if future.empty:
        return S2Result(status="qualified", entry=entry, stop=stop, target=target, risk=risk, exit_time=None, outcome=None, r=None, payload={}, **base)

    outcome = None
    exit_time = None
    exit_price = None
    for t, bar in future.iterrows():
        if original_direction == "bullish":
            high = float(bar["askhigh"]) if "askhigh" in bar.index else float(bar["high"])
            low = float(bar["asklow"]) if "asklow" in bar.index else float(bar["low"])
            stop_hit = high >= stop
            target_hit = low <= target
        else:
            high = float(bar["bidhigh"]) if "bidhigh" in bar.index else float(bar["high"])
            low = float(bar["bidlow"]) if "bidlow" in bar.index else float(bar["low"])
            stop_hit = low <= stop
            target_hit = high >= target
        if stop_hit:
            outcome = "stop"
            exit_time = pd.Timestamp(t)
            exit_price = stop
            break
        if target_hit:
            outcome = "target"
            exit_time = pd.Timestamp(t)
            exit_price = target
            break
    if outcome is None:
        last = future.iloc[-1]
        exit_time = pd.Timestamp(future.index[-1])
        if original_direction == "bullish":
            exit_price = float(last["askclose"]) if "askclose" in last.index else float(last["close"])
            pnl = entry - exit_price
        else:
            exit_price = float(last["bidclose"]) if "bidclose" in last.index else float(last["close"])
            pnl = exit_price - entry
        return S2Result(status="complete", entry=entry, stop=stop, target=target, risk=risk, exit_time=exit_time, outcome="timeout", r=float(pnl / risk), payload={}, **base)
    rr = -1.0 if outcome == "stop" else S2_TARGET_R
    return S2Result(status="complete", entry=entry, stop=stop, target=target, risk=risk, exit_time=exit_time, outcome=outcome, r=rr, payload={}, **base)
