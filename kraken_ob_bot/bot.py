"""
Main loop: entry point for the BTC 3M Order Block bot on Kraken spot margin.

    python3 bot.py

Runs forever, one cycle every LOOP_SECONDS, until killed (Ctrl+C / SIGTERM).
Intended to run under systemd or `nohup ... &` on the EC2 box so it
survives SSH disconnects; that unit/process-manager setup is outside this
file's scope, added at Phase 7 (demo trading) once the loop itself is
verified stable.
"""
import time
import logging
import traceback
from datetime import datetime, timezone

import pandas as pd

import config
import strategy
import risk
import state
from kraken_client import KrakenClient, KrakenAPIError
from position_manager import open_trade, manage_trade, cancel_unfilled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(config.TRADE_LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ob_bot")

error_log = logging.getLogger("ob_bot.errors")
error_log.addHandler(logging.FileHandler(config.ERROR_LOG_PATH))


def fetch_candles(client):
    """Kraken has no native 3-min OHLC interval -- pull 1-min bars and
    resample locally (strategy.resample_ohlc)."""
    raw = client.get_ohlc(interval=1)
    df = pd.DataFrame(raw).set_index("time")
    df.index = pd.to_datetime(df.index, unit="s")
    bars = strategy.resample_ohlc(df, freq=f"{config.CANDLE_MINUTES}min")
    return bars.tail(config.BUFFER_BARS)


def run_cycle(client, active_trades, seen_ob_times):
    bars = fetch_candles(client)
    prepared = strategy.prepare_bars(bars)
    current_bar_idx = len(prepared) - 1

    # 1. manage existing trades first -- fills/cancels take priority over
    #    opening new ones so margin figures used for sizing are current
    open_orders = client.get_open_orders()
    closed_orders = client.get_closed_orders()
    still_active = []
    for trade in active_trades:
        cancel_unfilled(client, trade, current_bar_idx, mitigation_wait=200)
        done = manage_trade(client, trade, open_orders, closed_orders)
        if done:
            log.info(f"trade closed: {trade.direction} entry={trade.entry_price} "
                      f"a={trade.a['status']} b={trade.b['status']}")
        else:
            still_active.append(trade)
    active_trades = still_active

    # 2. look for new signals
    signals = strategy.get_new_signals(prepared, seen_ob_times)
    for sig in signals:
        seen_ob_times.add(sig["ob_time"])
        try:
            balance = client.get_balance()
            usd_balance = float(balance.get("ZUSD", 0))
            trade_bal = client.get_trade_balance()
            free_margin = float(trade_bal.get("mf", usd_balance))
            margin_level = float(trade_bal.get("ml", 999))  # 'ml' absent/huge if no open positions

            if not risk.check_margin_floor(margin_level, config.MARGIN_FLOOR_PCT):
                log.info(f"skip signal {sig['ob_time']}: margin level {margin_level} at/under floor")
                continue

            sl_price = (sig["ob_low"] if sig["direction"] == "bull" else sig["ob_high"])
            sizes = risk.size_trade(usd_balance, sig["entry_price"], sl_price, free_margin,
                                     risk_pct=config.RISK_PCT, leverage=config.LEVERAGE)

            trade = open_trade(client, sig, sizes, sl_price, current_bar_idx)
            active_trades.append(trade)
            log.info(f"opened trade: {sig['direction']} entry={sig['entry_price']} "
                      f"size_a={sizes['size_a']:.6f} size_b={sizes['size_b']:.6f}")

        except risk.SizingError as e:
            log.info(f"skip signal {sig['ob_time']}: {e}")
        except KrakenAPIError as e:
            error_log.error(f"Kraken error opening trade for signal {sig['ob_time']}: {e}")

    state.save_state(active_trades, seen_ob_times, path=config.STATE_PATH)
    return active_trades, seen_ob_times


def main():
    client = KrakenClient(config.KRAKEN_API_KEY, config.KRAKEN_API_SECRET, pair=config.PAIR)
    client.resolve_pair()

    active_trades, seen_ob_times = state.load_state(path=config.STATE_PATH)
    if active_trades:
        log.info(f"restored {len(active_trades)} active trade(s) from state, reconciling...")
        active_trades = state.reconcile_on_startup(client, active_trades)
        log.info(f"{len(active_trades)} trade(s) still active after reconciliation")

    log.info("bot started")
    while True:
        cycle_start = time.time()
        try:
            active_trades, seen_ob_times = run_cycle(client, active_trades, seen_ob_times)
        except KrakenAPIError as e:
            error_log.error(f"Kraken API error this cycle: {e}")
        except Exception:
            error_log.error(f"unexpected error this cycle:\n{traceback.format_exc()}")

        elapsed = time.time() - cycle_start
        time.sleep(max(0, config.LOOP_SECONDS - elapsed))


if __name__ == "__main__":
    main()
