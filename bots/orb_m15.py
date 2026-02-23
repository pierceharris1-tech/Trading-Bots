import time
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from shared.bot_config import require_account_id
from shared.config import load_settings
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.time_utils import seconds_until_next_close, utc_now_iso
from shared.risk import units_from_risk, clamp_units
from shared.state_store import get_equity, kv_get, kv_set
from shared.strategy_utils import candles_to_df

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


def _today_session_start_utc(*, session_tz: str, start_hhmm: str) -> datetime:
    """
    Returns today's session start in UTC, based on the given timezone + hh:mm.
    """
    tz = ZoneInfo(session_tz)
    now_local = datetime.now(tz)
    hh, mm = [int(x) for x in start_hhmm.split(":")]
    start_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


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

    log.info(
        f"Starting ORB | bot_id={bot_id} | env={settings.oanda_env} | "
        f"instrument={settings.instrument} | tf={settings.granularity} | "
        f"tz={session_tz} start={session_start} | dry_run={settings.dry_run}"
    )

    client = OandaClient(
        base_url=settings.base_url,
        api_key=settings.oanda_api_key,
        account_id=account_id,
        bot_id=bot_id,
    )

    last_processed_candle: pd.Timestamp | None = None

    while True:
        try:
            sleep_s = seconds_until_next_close(settings.granularity)
            log.info(f"Heartbeat {utc_now_iso()} | sleeping {sleep_s}s until next close")
            time.sleep(sleep_s + 1)

            candles = client.get_candles(settings.instrument, settings.granularity, count=250)
            bars = candles_to_df(candles)

            latest = bars.index[-1]
            if last_processed_candle is not None and latest <= last_processed_candle:
                log.info("No new closed candle yet.")
                continue
            last_processed_candle = latest

            # Session timing anchored to the latest candle's trading day (in session tz)
            latest_candle_utc = latest
            session_start_utc = _session_start_for_candle_day_utc(
                candle_utc=latest_candle_utc,
                session_tz=session_tz,
                start_hhmm=session_start,
            )
            now_utc = datetime.now(timezone.utc)

            if now_utc < session_start_utc:
                log.info(
                    f"ORB_WAIT_SESSION | now={now_utc.isoformat()} | session_start_utc={session_start_utc.isoformat()}"
                )
                continue

            # Find opening range candle (first M15 close at/after session start)
            or_candle_time = _find_opening_range_candle(bars, session_start_utc=session_start_utc)
            if or_candle_time is None:
                log.info("ORB_WAIT_RANGE | no candle found after session start yet")
                continue

            # If we haven't even closed the OR candle yet, wait
            if latest < or_candle_time:
                log.info(f"ORB_WAIT_RANGE | latest={latest.isoformat()} < or_candle={or_candle_time.isoformat()}")
                continue

            or_high = float(bars.loc[or_candle_time, "high"])
            or_low = float(bars.loc[or_candle_time, "low"])
            _or_size = max(1e-9, or_high - or_low)  # kept for future filtering if you ever want it

            # One trade per session per day
            today_str = latest.tz_convert(ZoneInfo(session_tz)).date().isoformat()
            last_trade_date = kv_get(bot_id, "orb_last_trade_date")
            if last_trade_date == today_str:
                log.info(f"ORB_DONE_TODAY | date={today_str} | last_trade_date={last_trade_date}")
                continue

            # Breakout check on the latest closed candle (confirmation = close)
            last_close = float(bars.loc[latest, "close"])
            long_trigger = (last_close > (or_high + buffer_val))
            short_trigger = (last_close < (or_low - buffer_val))

            if confirm != "close":
                log.warning(f"confirm={confirm} not implemented yet; using close confirmation")

            if not long_trigger and not short_trigger:
                log.info(
                    f"ORB_NO_BREAKOUT | latest={latest.isoformat()} | close={last_close:.2f} | "
                    f"OR=[{or_low:.2f},{or_high:.2f}]"
                )
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
                log.info("ORB_BLOCK | invalid R (OR range is zero)")
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
                    log.info("ORB_BLOCK | invalid R0 (entry equals stop)")
                    continue

                tp_px = entry_px + direction * (r_mult * R0)


            # Position sizing using per-bot equity
            equity = float(get_equity(bot_id))
            units_mag, risk_cash, margin_req = units_from_risk(
                equity=equity,
                risk_pct=float(cfg.get("risk_pct", 0.01)),
                entry=entry_px,
                stop=stop_px,
                leverage=float(cfg.get("leverage", 20.0)),
            )
            units = clamp_units(direction * int(units_mag))

            log.info(
                f"ORB_SIGNAL | bot={bot_id} | side={side} | entry_model={entry_model} | latest={latest.isoformat()} | "
                f"OR_time={or_candle_time.isoformat()} OR=[{or_low:.2f},{or_high:.2f}] | "
                f"entry_ref={entry_px:.2f} sl={stop_px:.2f} tp={tp_px:.2f} | "
                f"units={units} equity={equity:.2f} risk_cash={risk_cash:.2f}"
            )

            if settings.dry_run:
                log.info("DRY_RUN | skipping order placement")
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
                log.info(f"ORDER_OK | type=LIMIT | resp_keys={list(resp.keys())}")
            else:
                resp = client.place_market_order(
                    units=int(units),
                    instrument=settings.instrument,
                    stop_loss=float(stop_px),
                    take_profit=float(tp_px),
                )
                log.info(f"ORDER_OK | type=MARKET | resp_keys={list(resp.keys())}")

            kv_set(bot_id, "orb_last_trade_date", today_str)

        except Exception as e:
            log.exception(f"ORB_ERROR | {e}")
            time.sleep(5)
