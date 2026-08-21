"""
Gold 5am Breakout Strategy
XAUUSD 5-min | London Open Trendline Break

Strategy rules:
- Lookback window: midnight → 4am UTC
- Trend filter: midnight price vs 4am price (must differ by > TREND_THRESHOLD)
- Trendline: rule of three on swing points (SWING_N=2, MIN_TOUCHES=4)
- Entry: first 5-min candle close that breaks the trendline (4am-6am window)
- SL: entry ± (1.5 × ATR)
- TP: entry ± (SL distance × 3)
- One trade per day maximum
"""

import time
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from shared.bot_config import require_account_id
from shared.daily_engine import reconcile_closed_trades
from shared.indicators import atr as _atr_series
from shared.config import load_settings
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.risk import units_from_risk, clamp_units
from shared.state_store import get_equity, kv_get, kv_set, trade_open, trade_close_by_trade_id
from shared.strategy_utils import candles_to_df

log = get_logger("bot.gold_5am_breakout")

# ── Strategy parameters ────────────────────────────────────────────────────
LOOKBACK_START_HOUR  = 0      # midnight UTC
ENTRY_WINDOW_START   = 4      # 4am UTC
ENTRY_WINDOW_END     = 6      # 6am UTC
TREND_THRESHOLD      = 1.0    # min $ move midnight→4am
MIN_TOUCHES          = 4      # rule of three minimum
MAX_TOUCHES          = 15     # flat line filter
TOUCH_TOLERANCE_ATR  = 0.2    # wick tolerance as ATR fraction
SWING_N              = 2      # swing point sensitivity
MIN_SLOPE_POINTS     = 0.5    # min trendline slope ($)
SL_ATR_MULTIPLE      = 1.5    # SL = entry ± (ATR × this)
TP_R_MULTIPLE        = 3.0    # TP = SL distance × this
ATR_PERIOD           = 14     # ATR lookback candles


# ── Helper functions ───────────────────────────────────────────────────────

def get_atr(candles: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """
    Wraps shared.indicators.atr() (Wilder's smoothing — the same formula every
    other bot uses) so ATR is calculated identically everywhere in this repo.
    Falls back to a simple mean of true ranges only when there isn't enough
    history yet for Wilder's EWM to warm up, matching this bot's original
    early-session behaviour.
    """
    series = _atr_series(candles, period)
    if not series.empty and not pd.isna(series.iloc[-1]):
        return float(series.iloc[-1])

    highs  = candles['high'].values
    lows   = candles['low'].values
    closes = candles['close'].values
    tr_list = []
    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        )
        tr_list.append(tr)
    if not tr_list:
        return 0.0
    return float(np.mean(tr_list[-period:]) if len(tr_list) >= period else np.mean(tr_list))


def get_trend_direction(df_day: pd.DataFrame) -> str | None:
    """
    Compare midnight close to 3:55am close.
    Returns 'up', 'down', or None if difference < threshold.
    """
    midnight = df_day.between_time("00:00", "00:05")
    pre_entry = df_day.between_time("03:55", "04:00")
    if midnight.empty or pre_entry.empty:
        return None
    diff = pre_entry.iloc[-1]['close'] - midnight.iloc[0]['close']
    if diff > TREND_THRESHOLD:
        return 'up'
    elif diff < -TREND_THRESHOLD:
        return 'down'
    return None


def get_line_price(p1: tuple, p2: tuple, x: int) -> float:
    x1, y1 = p1
    x2, y2 = p2
    if x2 == x1:
        return float(y1)
    slope = (y2 - y1) / (x2 - x1)
    return float(y1 + slope * (x - x1))


def find_swing_points(candles: pd.DataFrame, n: int, direction: str) -> list:
    points = []
    prices = candles['high'].values if direction == 'resistance' else candles['low'].values
    for i in range(n, len(prices) - n):
        window = prices[i-n : i+n+1]
        if direction == 'resistance' and prices[i] == max(window):
            points.append((i, float(prices[i])))
        elif direction == 'support' and prices[i] == min(window):
            points.append((i, float(prices[i])))
    return points


def validate_trendline(
    candles: pd.DataFrame,
    p1: tuple, p2: tuple,
    direction: str,
    atr: float,
) -> tuple[bool, int, list]:
    tolerance   = atr * TOUCH_TOLERANCE_ATR
    touch_count = 0
    touch_indices = []
    x1, _ = p1

    for i in range(x1, len(candles)):
        line_price = get_line_price(p1, p2, i)
        candle     = candles.iloc[i]

        if direction == 'resistance':
            if candle['close'] > line_price:
                return False, 0, []
            if candle['high'] >= (line_price - tolerance) and candle['close'] < line_price:
                touch_count += 1
                touch_indices.append(i)
        else:
            if candle['close'] < line_price:
                return False, 0, []
            if candle['low'] <= (line_price + tolerance) and candle['close'] > line_price:
                touch_count += 1
                touch_indices.append(i)

    return touch_count >= MIN_TOUCHES, touch_count, touch_indices


def find_best_trendline(
    candles: pd.DataFrame,
    swing_points: list,
    direction: str,
    atr: float,
) -> tuple | None:
    best         = None
    best_touches = 0

    for i in range(len(swing_points)):
        for j in range(i + 1, len(swing_points)):
            p1 = swing_points[i]
            p2 = swing_points[j]

            if direction == 'resistance' and p2[1] >= p1[1]:
                continue
            if direction == 'support' and p2[1] <= p1[1]:
                continue
            if abs(p2[1] - p1[1]) < MIN_SLOPE_POINTS:
                continue

            is_valid, touches, touch_indices = validate_trendline(
                candles, p1, p2, direction, atr
            )

            if touches > MAX_TOUCHES:
                continue

            if is_valid and touches > best_touches:
                best_touches = touches
                best = (p1, p2, touches, touch_indices)

    return best


def find_break_candle(
    entry_window: pd.DataFrame,
    lookback_len: int,
    p1: tuple, p2: tuple,
    tl_direction: str,
) -> tuple | None:
    """
    Scan entry window candles for first close beyond trendline.
    Returns (timestamp, candle_series) or None.
    """
    for k, (ts, candle) in enumerate(entry_window.iterrows()):
        x          = lookback_len + k
        line_price = get_line_price(p1, p2, x)

        if tl_direction == 'resistance' and candle['close'] > line_price:
            return ts, candle
        elif tl_direction == 'support' and candle['close'] < line_price:
            return ts, candle

    return None


# ── Main bot loop ──────────────────────────────────────────────────────────


def get_daily_trend(daily_bars) -> str | None:
    """
    Check last 3 completed daily closes for consecutive direction.
    Returns 'up', 'down', or None.
    """
    # Drop last bar — may be incomplete (today)
    completed = daily_bars.iloc[:-1]
    if len(completed) < 3:
        return None
    closes    = completed['close'].values[-3:]
    up_days   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
    down_days = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
    if up_days >= 2:   return 'up'
    if down_days >= 2: return 'down'
    return None

def main(cfg: dict | None = None) -> None:
    if cfg is None:
        cfg = {}

    settings    = load_settings()
    bot_id      = cfg.get("bot_id", "GOLD_5AM_BREAKOUT")
    account_id  = require_account_id(cfg)
    instrument  = str(cfg.get("instrument", "XAU_USD"))
    dry_run     = bool(cfg.get("dry_run", True))  # default to dry_run for safety
    risk_pct    = float(cfg.get("risk_pct", 0.01))

    log.info(
        f"Starting Gold 5am Breakout | bot_id={bot_id} | "
        f"instrument={instrument} | dry_run={dry_run} | risk_pct={risk_pct}"
    )

    client = OandaClient(
        base_url=settings.base_url,
        api_key=settings.oanda_api_key,
        account_id=account_id,
        bot_id=bot_id,
        timeframe="M5",
    )

    last_processed_candle = None

    while True:
        try:
            # Sleep until next 5-min candle close
            now_utc   = datetime.now(timezone.utc)
            seconds   = 60 - now_utc.second + (4 - now_utc.minute % 5) * 60
            if seconds > 300:
                seconds -= 300
            log.info(f"Heartbeat {now_utc.isoformat()} | sleeping {seconds}s until next 5min close")
            time.sleep(seconds + 2)

            reconcile_closed_trades(bot_id, client)

            # ── Fetch latest 5-min candles ─────────────────────────────────
            # We need from midnight to 6am — that's max 72 candles
            # Fetch 100 to be safe
            candles = client.get_candles(instrument, "M5", count=100)
            # Fetch daily candles for trend filter (count=5 is well within limits)
            daily_candles = client.get_candles(instrument, "D", count=5)
            daily_bars    = candles_to_df(daily_candles)
            if daily_bars.index.tzinfo is not None:
                daily_bars.index = daily_bars.index.tz_convert(None)
            bars    = candles_to_df(candles)

            # Make index timezone-naive for consistent comparisons
            if bars.index.tzinfo is not None:
                bars.index = bars.index.tz_convert(None)

            latest = bars.index[-1]

            # Skip if no new candle
            if last_processed_candle is not None and latest <= last_processed_candle:
                log.info("No new closed candle yet.")
                continue
            last_processed_candle = latest

            now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)

            # ── Only run logic during entry window ─────────────────────────
            current_hour   = latest.hour
            current_minute = latest.minute
            current_time   = current_hour + current_minute / 60

            if not (ENTRY_WINDOW_START <= current_time <= ENTRY_WINDOW_END):
                log.info(
                    f"OUTSIDE_WINDOW | current={latest.isoformat()} | "
                    f"window={ENTRY_WINDOW_START}:00-{ENTRY_WINDOW_END}:00 UTC"
                )
                continue

            # ── One trade per day ──────────────────────────────────────────
            today_str = latest.date().isoformat()
            last_trade_date = kv_get(bot_id, "last_trade_date")
            if last_trade_date == today_str:
                log.info(f"DONE_TODAY | date={today_str}")
                continue

            # ── Build today's data ─────────────────────────────────────────
            today_mask = bars.index.date == latest.date()
            df_day     = bars[today_mask]

            if len(df_day) < 10:
                log.info(f"NOT_ENOUGH_CANDLES | count={len(df_day)}")
                continue

            # ── Trend filter ───────────────────────────────────────────────
            # ── Daily trend filter ────────────────────────────────────────
            daily_trend = get_daily_trend(daily_bars)
            if daily_trend is None:
                log.info("DAILY_TREND_NONE | no consecutive daily trend, skipping")
                continue

            trend = get_trend_direction(df_day)
            if trend is None:
                log.info("NO_CLEAR_TREND | skipping")
                continue

            # Uptrend → look for support break downward (short)
            # Downtrend → look for resistance break upward (long)
            tl_direction = 'support' if trend == 'up' else 'resistance'
            direction_if_break = 'short' if tl_direction == 'support' else 'long'

            # Only trade if intraday signal matches daily trend direction
            if daily_trend == 'up' and direction_if_break != 'long':
                log.info(f"TREND_FILTER | daily=up signal=short skipping")
                continue
            if daily_trend == 'down' and direction_if_break != 'short':
                log.info(f"TREND_FILTER | daily=down signal=long skipping")
                continue
            log.info(f"TREND_MATCH | daily={daily_trend} signal={direction_if_break}")

            # ── Build lookback window (midnight → 4am) ─────────────────────
            lookback = df_day.between_time(
                f"{LOOKBACK_START_HOUR:02d}:00",
                f"{ENTRY_WINDOW_START:02d}:00"
            )

            if len(lookback) < (SWING_N * 2 + 3):
                log.info(f"NOT_ENOUGH_LOOKBACK | count={len(lookback)}")
                continue

            # ── ATR ────────────────────────────────────────────────────────
            atr = get_atr(lookback)
            if atr == 0:
                log.info("ATR_ZERO | skipping")
                continue

            # ── Find trendline ─────────────────────────────────────────────
            swing_pts = find_swing_points(lookback, SWING_N, tl_direction)
            if len(swing_pts) < 2:
                log.info(f"NOT_ENOUGH_SWINGS | count={len(swing_pts)}")
                continue

            trendline = find_best_trendline(lookback, swing_pts, tl_direction, atr)
            if trendline is None:
                log.info("NO_VALID_TRENDLINE | skipping")
                continue

            p1, p2, touch_count, _ = trendline

            # ── Scan entry window for break ────────────────────────────────
            entry_window = df_day.between_time(
                f"{ENTRY_WINDOW_START:02d}:00",
                f"{ENTRY_WINDOW_END:02d}:00"
            )

            if entry_window.empty:
                log.info("ENTRY_WINDOW_EMPTY | skipping")
                continue

            # Only check candles up to and including the latest closed candle
            entry_window = entry_window[entry_window.index <= latest]

            lookback_len = len(lookback)
            result = find_break_candle(
                entry_window, lookback_len, p1, p2, tl_direction
            )

            if result is None:
                log.info(
                    f"NO_BREAK | latest={latest.isoformat()} | "
                    f"tl={tl_direction} | touches={touch_count} | ATR={atr:.3f}"
                )
                continue

            break_time, break_candle = result

            # Only act on the most recent candle's break — not historical ones
            if break_time != latest:
                log.info(
                    f"BREAK_NOT_LATEST | break={break_time.isoformat()} | "
                    f"latest={latest.isoformat()} | already passed"
                )
                continue

            # ── Calculate entry, SL, TP ────────────────────────────────────
            entry_price = float(break_candle['close'])
            sl_dist     = atr * SL_ATR_MULTIPLE

            if tl_direction == 'resistance':
                # Broke above resistance → long
                sl        = entry_price - sl_dist
                tp        = entry_price + (sl_dist * TP_R_MULTIPLE)
                direction = 1   # long
                side      = "LONG"
            else:
                # Broke below support → short
                sl        = entry_price + sl_dist
                tp        = entry_price - (sl_dist * TP_R_MULTIPLE)
                direction = -1  # short
                side      = "SHORT"

            # ── Position sizing ────────────────────────────────────────────
            equity = float(get_equity(bot_id, client=client))
            units_mag, risk_cash, margin_req = units_from_risk(
                equity=equity,
                risk_pct=risk_pct,
                entry=entry_price,
                stop=sl,
                leverage=float(cfg.get("leverage", 20.0)),
            )
            units = clamp_units(direction * int(units_mag))

            if units == 0:
                log.info(f"UNITS_ZERO | equity={equity:.2f} | skipping")
                continue

            log.info(
                f"SIGNAL | bot={bot_id} | side={side} | tl={tl_direction} | "
                f"touches={touch_count} | ATR={atr:.3f} | "
                f"entry={entry_price:.3f} | sl={sl:.3f} | tp={tp:.3f} | "
                f"sl_dist={sl_dist:.3f} | units={units} | "
                f"equity={equity:.2f} | risk_cash={risk_cash:.2f}"
            )

            if dry_run:
                log.info(
                    f"DRY_RUN | would place {side} {abs(units)} units | "
                    f"entry={entry_price:.3f} sl={sl:.3f} tp={tp:.3f}"
                )
                kv_set(bot_id, "last_trade_date", today_str)
                continue

            # ── Place order ────────────────────────────────────────────────
            resp = client.place_market_order(
                units=int(units),
                instrument=instrument,
                stop_loss=round(sl, 2),
                take_profit=round(tp, 2),
            )

            order_id = str(resp.get("orderCreateTransaction", {}).get("id", ""))
            trade_id = str(
                resp.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID", "")
            )

            log.info(
                f"ORDER_OK | side={side} | units={units} | "
                f"order_id={order_id} | trade_id={trade_id}"
            )

            # Record in database
            trade_open(
                bot_id=bot_id,
                instrument=instrument,
                account_id=account_id,
                open_time=break_time.isoformat(),
                order_id=order_id,
                trade_id=trade_id,
            )

            kv_set(bot_id, "last_trade_date", today_str)
            kv_set(bot_id, "last_trade_id", trade_id)

        except Exception as e:
            log.exception(f"ERROR | {e}")
            time.sleep(30)
