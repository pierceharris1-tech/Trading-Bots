from __future__ import annotations

"""
Inside Days Strategy — v2
=========================
Rules (backtested, spread-adjusted, 0.348R expectancy):
  - Inside bar: today's range completely inside yesterday's range
  - Entry: stop order above high (long) or below low (short)
  - SL: other side of inside bar range
  - TP: 3R (= 3 × inside bar range)
  - One trade at a time per instrument (no overlap)
  - No EOD leg close — single position runs until TP or SL

Live account: 001-004-10576609-001
Risk: 1% per trade
"""

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
from shared.logging_utils import get_logger
from shared.oanda_client import OandaClient
from shared.risk import clamp_units, units_from_risk
from shared.state_store import get_equity, kv_set, trade_open, trade_open_for_key
from shared.strategy_utils import candles_to_df, parse_order_ids, parse_realized_pl
from shared.time_utils import seconds_until_next_close

log = get_logger("bot.inside_days_daily")

# FCA retail leverage limits
LEVERAGE_MAP = {
    "EUR_USD": 30.0,
    "GBP_USD": 30.0,
    "USD_JPY": 30.0,
    "USD_CAD": 30.0,
    "EUR_GBP": 30.0,
    "AUD_USD": 20.0,
    "NZD_USD": 20.0,
    "XAU_USD": 20.0,
}



def _cancel_order_ids(client: OandaClient, order_ids: list[str]) -> None:
    """Cancel any pending orders by ID."""
    if not order_ids:
        return
    pending     = client.list_pending_orders().get("orders", [])
    pending_ids = {str(x.get("id")) for x in pending}
    for oid in order_ids:
        if oid in pending_ids:
            try:
                client.cancel_order(oid)
                log.info(f"CANCELLED_ORDER | order_id={oid}")
            except Exception as e:
                log.warning(f"CANCEL_FAILED | order_id={oid} | err={e}")


def _sync_open_trades(bot_id: str, account_id: str, client: OandaClient, instrument: str) -> None:
    """Log any open trades not yet recorded in state DB."""
    open_trades = client.list_open_trades().get("trades", [])
    for tr in open_trades:
        if str(tr.get("instrument")) != instrument:
            continue
        trade_id = str(tr.get("id"))
        if trade_open_for_key(bot_id, instrument, trade_id) is not None:
            continue
        trade_open(
            bot_id=bot_id,
            instrument=instrument,
            account_id=account_id,
            open_time=str(tr.get("openTime")),
            order_id=str(tr.get("id")),
            trade_id=trade_id,
        )


def _has_open_trade(client: OandaClient, instrument: str) -> bool:
    """Return True if there is any open trade on this instrument."""
    open_trades = client.list_open_trades().get("trades", [])
    return any(str(t.get("instrument")) == instrument for t in open_trades)


def _has_pending_orders(client: OandaClient, instrument: str) -> bool:
    """Return True if there are any pending stop orders on this instrument."""
    pending = client.list_pending_orders().get("orders", [])
    return any(str(o.get("instrument")) == instrument for o in pending)


def main(cfg: dict | None = None) -> None:
    if cfg is None:
        cfg = {}

    settings    = load_settings()
    bot_id      = str(cfg.get("bot_id", "INSIDE_DAYS"))
    account_id  = require_account_id(cfg)
    timeframe   = cfg_timeframe(cfg, default_timeframe="D")
    risk_pct    = float(cfg.get("risk_pct", 0.01))
    leverage    = float(cfg.get("leverage", 20.0))
    tp_r        = float(cfg.get("tp_r", 3.0))
    instruments = cfg_instruments(cfg, settings.instrument)

    client = OandaClient(
        base_url=settings.base_url,
        api_key=settings.oanda_api_key,
        account_id=account_id,
        bot_id=bot_id,
        timeframe=timeframe,
    )

    last_processed_bar_by_instrument: dict[str, pd.Timestamp] = {}

    log.info(
        f"Starting Inside Days v2 | bot_id={bot_id} | "
        f"instruments={instruments} | timeframe={timeframe} | "
        f"risk_pct={risk_pct} | tp_r={tp_r}R | "
        f"account={account_id} | dry_run={settings.dry_run}"
    )

    while True:
        try:
            sleep_s = seconds_until_next_close(timeframe)
            log.info(f"Heartbeat | sleeping {sleep_s}s until next {timeframe} close")
            time.sleep(sleep_s + 1)

            reconcile_closed_trades(bot_id, client)

            for instrument in instruments:
                try:
                    bars   = candles_to_df(client.get_candles(
                        instrument=instrument, granularity=timeframe, count=10))
                    latest = bars.index[-1]

                    process_key = last_processed_bar_key(instrument, timeframe, "_inside_v2")
                    if not should_process_candle_bar(
                        bot_id, process_key, latest,
                        last_processed_bar_time=last_processed_bar_by_instrument.get(instrument),
                    ):
                        continue

                    state_key = f"inside_v2_state_{instrument}"
                    state     = kv_json_get(bot_id, state_key)

                    # ── Step 1: sync any open trades into DB ───────────────
                    _sync_open_trades(bot_id, account_id, client, instrument)

                    # ── Step 2: check if pending orders were filled ────────
                    if state and state.get("mode") == "pending":
                        buy_entry  = float(state["buy_entry"])
                        sell_entry = float(state["sell_entry"])
                        high_t     = float(bars["high"].iloc[-1])
                        low_t      = float(bars["low"].iloc[-1])
                        up_hit     = high_t >= buy_entry
                        down_hit   = low_t  <= sell_entry

                        if up_hit and down_hit:
                            # Ambiguous — both sides hit same bar, cancel all
                            log.info(
                                f"AMBIGUOUS_BREAK | {instrument} | "
                                f"high={high_t} low={low_t} | "
                                f"buy_entry={buy_entry} sell_entry={sell_entry}"
                            )
                            all_orders = (
                                list(state.get("buy_orders", [])) +
                                list(state.get("sell_orders", []))
                            )
                            _cancel_order_ids(client, all_orders)
                            kv_set(bot_id, state_key, "")
                            state = None

                        elif up_hit or down_hit:
                            # One side triggered — cancel the other side
                            side     = 1 if up_hit else -1
                            side_str = "LONG" if side > 0 else "SHORT"
                            cancel   = (state.get("sell_orders", [])
                                        if side > 0
                                        else state.get("buy_orders", []))
                            _cancel_order_ids(client, list(cancel))
                            state["mode"] = "open"
                            state["side"] = side
                            kv_json_set(bot_id, state_key, state)
                            log.info(
                                f"ORDER_FILLED | {instrument} | "
                                f"side={side_str} | entry={buy_entry if side > 0 else sell_entry:.5f}"
                            )

                    # ── Step 3: check if open trade has closed ─────────────
                    state = kv_json_get(bot_id, state_key)
                    if state and state.get("mode") == "open":
                        if not _has_open_trade(client, instrument) and \
                           not _has_pending_orders(client, instrument):
                            log.info(f"TRADE_CLOSED | {instrument} | resetting state")
                            kv_set(bot_id, state_key, "")
                            state = None

                    # ── Step 4: look for new inside bar setup ──────────────
                    state = kv_json_get(bot_id, state_key)
                    if state is not None:
                        # Instrument is busy — skip
                        log.info(
                            f"INSTRUMENT_BUSY | {instrument} | "
                            f"mode={state.get('mode')} | skipping"
                        )
                        set_processed_candle_bar(bot_id, process_key, latest)
                        last_processed_bar_by_instrument[instrument] = latest
                        continue

                    if len(bars) < 2:
                        set_processed_candle_bar(bot_id, process_key, latest)
                        last_processed_bar_by_instrument[instrument] = latest
                        continue

                    # Current bar = latest completed daily bar
                    high_t    = float(bars["high"].iloc[-1])
                    low_t     = float(bars["low"].iloc[-1])
                    prev_high = float(bars["high"].iloc[-2])
                    prev_low  = float(bars["low"].iloc[-2])

                    inside = high_t <= prev_high and low_t >= prev_low

                    if not inside:
                        log.info(
                            f"NO_SETUP | {instrument} | "
                            f"bar=[{low_t:.5f},{high_t:.5f}] | "
                            f"prev=[{prev_low:.5f},{prev_high:.5f}]"
                        )
                        set_processed_candle_bar(bot_id, process_key, latest)
                        last_processed_bar_by_instrument[instrument] = latest
                        continue

                    # Inside bar confirmed — calculate levels
                    range_dist = high_t - low_t
                    if range_dist <= 0:
                        set_processed_candle_bar(bot_id, process_key, latest)
                        last_processed_bar_by_instrument[instrument] = latest
                        continue

                    buy_entry  = high_t   # stop order above inside bar high
                    sell_entry = low_t    # stop order below inside bar low
                    buy_sl     = low_t    # SL at other side
                    sell_sl    = high_t
                    buy_tp     = buy_entry  + tp_r * range_dist
                    sell_tp    = sell_entry - tp_r * range_dist

                    # Position sizing
                    equity = float(get_equity(bot_id, client=client))
                    # Get quote-to-GBP conversion rate for correct position sizing
                    quote_currency = instrument.split("_")[1]  # e.g. USD, JPY, GBP
                    if quote_currency == "GBP":
                        q2a_rate = 1.0
                    elif quote_currency == "JPY":
                        gbp_jpy = client.get_price("GBP_JPY")
                        q2a_rate = gbp_jpy["ask"] if gbp_jpy else 190.0
                    elif quote_currency == "USD":
                        gbp_usd = client.get_price("GBP_USD")
                        q2a_rate = gbp_usd["ask"] if gbp_usd else 1.27
                    else:
                        q2a_rate = 1.0

                    units_mag, risk_cash, _ = units_from_risk(
                        equity=equity,
                        risk_pct=risk_pct,
                        entry=buy_entry,
                        stop=buy_sl,
                        leverage=LEVERAGE_MAP.get(instrument, 20.0),
                        quote_to_account_rate=q2a_rate,
                    )
                    units = clamp_units(int(units_mag))
                    if units == 0:
                        log.warning(
                            f"ZERO_UNITS | {instrument} | "
                            f"equity={equity:.2f} range={range_dist:.5f}"
                        )
                        set_processed_candle_bar(bot_id, process_key, latest)
                        last_processed_bar_by_instrument[instrument] = latest
                        continue

                    log.info(
                        f"INSIDE_BAR | {instrument} | "
                        f"bar=[{low_t:.5f},{high_t:.5f}] | "
                        f"range={range_dist:.5f} | "
                        f"buy_entry={buy_entry:.5f} buy_tp={buy_tp:.5f} buy_sl={buy_sl:.5f} | "
                        f"sell_entry={sell_entry:.5f} sell_tp={sell_tp:.5f} sell_sl={sell_sl:.5f} | "
                        f"units=±{units} equity={equity:.2f} risk_cash={risk_cash:.2f}"
                    )

                    if settings.dry_run:
                        log.info(f"DRY_RUN | {instrument} | skipping order placement")
                        kv_json_set(bot_id, state_key, {
                            "mode":       "pending",
                            "inside_bar": latest.date().isoformat(),
                            "buy_entry":  buy_entry,
                            "sell_entry": sell_entry,
                            "buy_orders":  [],
                            "sell_orders": [],
                        })
                        set_processed_candle_bar(bot_id, process_key, latest)
                        last_processed_bar_by_instrument[instrument] = latest
                        continue

                    # Place stop orders — one long, one short
                    buy_resp  = client.place_stop_order(
                        units=units,
                        instrument=instrument,
                        price=buy_entry,
                        stop_loss=buy_sl,
                        take_profit=buy_tp,
                    )
                    sell_resp = client.place_stop_order(
                        units=-units,
                        instrument=instrument,
                        price=sell_entry,
                        stop_loss=sell_sl,
                        take_profit=sell_tp,
                    )

                    buy_order_id,  _ = parse_order_ids(buy_resp)
                    sell_order_id, _ = parse_order_ids(sell_resp)

                    log.info(
                        f"ORDERS_PLACED | {instrument} | "
                        f"buy_order={buy_order_id} sell_order={sell_order_id}"
                    )

                    kv_json_set(bot_id, state_key, {
                        "mode":        "pending",
                        "inside_bar":  latest.date().isoformat(),
                        "buy_entry":   buy_entry,
                        "sell_entry":  sell_entry,
                        "buy_orders":  [x for x in [buy_order_id]  if x],
                        "sell_orders": [x for x in [sell_order_id] if x],
                    })

                except Exception as e:
                    log.exception(
                        f"INSIDE_DAYS_INSTRUMENT_ERROR | "
                        f"bot_id={bot_id} | instrument={instrument} | err={e}"
                    )

                finally:
                    set_processed_candle_bar(bot_id, process_key, latest
                                             if 'latest' in dir() else pd.Timestamp.now())
                    last_processed_bar_by_instrument[instrument] = (
                        latest if 'latest' in dir() else pd.Timestamp.now())

        except Exception as e:
            log.exception(
                f"INSIDE_DAYS_ERROR | bot_id={bot_id} | account_id={account_id} | err={e}"
            )
            time.sleep(5)
