import time
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from shared.bot_config import require_account_id
from shared.config import load_settings
from shared.daily_engine import (
    emit_order_event,
    emit_processed_candle,
    reconcile_closed_trades,
    select_latest_complete_candle,
)
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.time_utils import seconds_until_next_close
from shared.risk import units_from_risk, clamp_units
from shared.state_store import get_equity, kv_get, kv_set, trade_open
from shared.strategy_utils import candles_to_df, parse_order_ids

log = get_logger("bot.orb_m15")


def apply_cfg_overrides(settings, cfg: dict):
    updates = {}
    if cfg.get("instrument") is not None:
        updates["instrument"] = str(cfg["instrument"])
    if cfg.get("timeframe") is not None:
        updates["granularity"] = str(cfg["timeframe"])
    if cfg.get("dry_run") is not None:
        updates["dry_run"] = bool(cfg["dry_run"])
    safe_updates = {k: v for k, v in updates.items() if hasattr(settings, k)}
    return replace(settings, **safe_updates)


def _find_opening_range_candle(bars: pd.DataFrame, *, session_start_utc: datetime) -> pd.Timestamp | None:
    """
    Pick the first completed candle whose timestamp is >= session_start_utc.
    bars index is UTC.
    """
    idx = bars.index[bars.index >= pd.Timestamp(session_start_utc)]
    if len(idx) == 0:
        return None
    return idx[0]

def _session_start_for_candle_day_utc(*, candle_utc: pd.Timestamp, session_tz: str, start_hhmm: str) -> datetime:
    """
    Compute the session start (in UTC) for the *date of the provided candle*,
    interpreted in the session timezone.

    This avoids weekend/holiday issues where "today" has no candles.
    """
    tz = ZoneInfo(session_tz)
    candle_local = candle_utc.to_pydatetime().astimezone(tz)
    hh, mm = [int(x) for x in start_hhmm.split(":")]
    start_local = candle_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def main(cfg: dict | None = None) -> None:
    if cfg is None:
        cfg = {}

    settings = apply_cfg_overrides(load_settings(), cfg)
    bot_id = cfg.get("bot_id", "UNKNOWN_BOT")
    account_id = require_account_id(cfg)

    session_cfg = cfg.get("session") or {}
    breakout_cfg = cfg.get("breakout") or {}
    tp_cfg = cfg.get("tp") or {}
    stop_cfg = cfg.get("stop") or {}  # kept for compatibility, but not used for logic now

    entry_cfg = cfg.get("entry") or {}
    entry_mode = str(entry_cfg.get("mode", "market")).lower()
    tp_sl_mult = float(entry_cfg.get("tp_sl_mult", 2.0))

    session_tz = str(session_cfg.get("tz", "Europe/London"))
    session_start = str(session_cfg.get("start", "08:00"))
    range_minutes = int(session_cfg.get("range_minutes", 15))

    confirm = str(breakout_cfg.get("confirm", "close")).lower()
    buffer_val = float(breakout_cfg.get("buffer", 0.0))

    r_mult = float(tp_cfg.get("r_mult", 2.0))

    if settings.granularity.upper() != "M15":
        log.warning(f"ORB is designed for M15; got granularity={settings.granularity}")

    if range_minutes != 15:
        log.warning(f"Current implementation assumes 15-minute opening range; got range_minutes={range_minutes}")

    client = OandaClient(
        base_url=settings.base_url,
        api_key=settings.oanda_api_key,
        account_id=account_id,
        bot_id=bot_id,
        timeframe=str(cfg.get("timeframe", settings.granularity)).strip().upper(),
    )

    dedup_key = f"orb_last_processed_candle_{settings.instrument}_{settings.granularity}"

    while True:
        try:
            sleep_s = seconds_until_next_close(settings.granularity)
            time.sleep(sleep_s + 1)

            reconcile_closed_trades(bot_id, client)

            candles = client.get_candles(settings.instrument, settings.granularity, count=250)
            decision = select_latest_complete_candle(candles.get("candles", []))
            if decision is None:
                continue
            latest, _ = decision
            bars = candles_to_df(candles).loc[:latest]
            if bars.empty:
                continue
            last_processed_str = kv_get(bot_id, dedup_key)
            if last_processed_str and latest <= pd.Timestamp(last_processed_str):
                continue
            kv_set(bot_id, dedup_key, latest.isoformat())
            emit_processed_candle(bot_id, settings.granularity, settings.instrument, latest)

            # Session timing anchored to the latest candle's trading day (in session tz)
            latest_candle_utc = latest
            session_start_utc = _session_start_for_candle_day_utc(
                candle_utc=latest_candle_utc,
                session_tz=session_tz,
                start_hhmm=session_start,
            )
            now_utc = datetime.now(timezone.utc)

            if now_utc < session_start_utc:
                continue

            # Find opening range candle (first M15 close at/after session start)
            or_candle_time = _find_opening_range_candle(bars, session_start_utc=session_start_utc)
            if or_candle_time is None:
                continue

            # If we haven't even closed the OR candle yet, wait
            if latest < or_candle_time:
                continue

            or_high = float(bars.loc[or_candle_time, "high"])
            or_low = float(bars.loc[or_candle_time, "low"])
            _or_size = max(1e-9, or_high - or_low)  # kept for future filtering if you ever want it

            # One trade per session per day
            today_str = latest.tz_convert(ZoneInfo(session_tz)).date().isoformat()
            last_trade_date = kv_get(bot_id, "orb_last_trade_date")
            if last_trade_date == today_str:
                continue

            # Breakout check on the latest closed candle (confirmation = close)
            last_close = float(bars.loc[latest, "close"])
            long_trigger = (last_close > (or_high + buffer_val))
            short_trigger = (last_close < (or_low - buffer_val))

            if not long_trigger and not short_trigger:
                continue

            direction = 1 if long_trigger else -1
            side = "LONG" if direction == 1 else "SHORT"

            # Entry model: next candle open
            # Practically, because we run immediately after the close boundary, a MARKET order here is the "next open" execution.

            # ------------------------------------------------------------
            # Entry logic
            # ------------------------------------------------------------
            R = abs(or_high - or_low)
            if R <= 0:
                continue

            if entry_mode == "limit_or_edge":
                # Limit at the OR boundary in the breakout direction
                entry_model = "LIMIT_AT_OR_EDGE"
                entry_px = or_high if direction == 1 else or_low  # limit price

                # Symmetric TP/SL distances: tp_sl_mult * R from entry
                dist = tp_sl_mult * R
                stop_px = entry_px - direction * dist
                tp_px   = entry_px + direction * dist

            else:
                # Default: market near "next open" (executed right after close boundary)
                entry_model = "NEXT_OPEN_MARKET"
                entry_px = last_close  # reference price (live fill may differ)

                # Symmetric OR stops (legacy):
                stop_px = or_low if direction == 1 else or_high

                R0 = abs(entry_px - stop_px)
                if R0 <= 0:
                    continue

                tp_px = entry_px + direction * (r_mult * R0)


            # Position sizing using per-bot equity
            equity = float(get_equity(bot_id, client=client))
            units_mag, risk_cash, margin_req = units_from_risk(
                equity=equity,
                risk_pct=float(cfg.get("risk_pct", 0.01)),
                entry=entry_px,
                stop=stop_px,
                leverage=float(cfg.get("leverage", 20.0)),
            )
            units = clamp_units(direction * int(units_mag))

            if settings.dry_run:
                kv_set(bot_id, "orb_last_trade_date", today_str)
                continue

            # Live: place market order with attached SL/TP
            if entry_mode == "limit_or_edge":
                resp = client.place_limit_order(
                    units=int(units),
                    instrument=settings.instrument,
                    price=float(entry_px),
                    stop_loss=float(stop_px),
                    take_profit=float(tp_px),
                )
            else:
                resp = client.place_market_order(
                    units=int(units),
                    instrument=settings.instrument,
                    stop_loss=float(stop_px),
                    take_profit=float(tp_px),
                )

            order_id, trade_id = parse_order_ids(resp)
            emit_order_event(event="ORDER_PLACED", bot_id=str(bot_id), timeframe=settings.granularity, instrument=settings.instrument, ref_id=str(order_id or trade_id or ""))
            trade_open(
                bot_id=bot_id,
                instrument=settings.instrument,
                account_id=account_id,
                open_time=latest.isoformat(),
                order_id=order_id,
                trade_id=trade_id,
            )

            kv_set(bot_id, "orb_last_trade_date", today_str)

        except Exception as e:
            log.exception(f"ORB_ERROR | {e}")
            time.sleep(5)
