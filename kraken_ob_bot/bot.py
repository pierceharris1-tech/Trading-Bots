"""
Main loop: entry point for the BTC 3M Order Block bot on Kraken spot margin.

    python3 bot.py

Runs forever, one cycle every LOOP_SECONDS, until killed (Ctrl+C / SIGTERM).
Intended to run under systemd or `nohup ... &` on the EC2 box so it
survives SSH disconnects; that unit/process-manager setup is outside this
file's scope, added at Phase 7 (demo trading) once the loop itself is
verified stable.
"""
import os
import time
import logging
import traceback
from datetime import datetime, timezone

import pandas as pd

import config
import strategy
import risk
import state
import signal_log
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


def _try_open_signal(client, sig, equity, free_margin, margin_level, current_bar_time,
                      active_trades, balance_fetch_error, log_skip):
    """Attempt to size + open a trade for one signal against this cycle's
    account numbers. Returns (free_margin, opened, rate_limited) --
    free_margin unchanged unless opened is True.

    log_skip controls whether a failed attempt writes a signal_log
    'skipped' event: True for a signal's first-ever evaluation (in the
    new-signal loop below), False for a routine retry of an
    already-logged pending signal -- otherwise a signal stuck pending for
    dozens of cycles would produce dozens of near-identical skip lines
    and inflate signal_summary.py's counts. Its eventual resolution
    (opened, or expired_unfilled once its mitigation window closes) is
    what actually gets logged for a retried signal; Kraken API errors
    still always go to error_log regardless, since those are operational
    problems worth seeing regardless of log_skip."""
    if equity is None:
        if log_skip:
            signal_log.log_skipped(sig, "balance_fetch_failed",
                                    balance_fetch_error or "TradeBalance fetch failed")
        return free_margin, False, False
    if equity <= 0:
        if log_skip:
            log.info(f"skip signal {sig['ob_time']}: equity is 0 or unavailable from TradeBalance")
            signal_log.log_skipped(sig, "zero_equity", f"equity={equity}",
                                    equity=equity, free_margin=free_margin, margin_level=margin_level)
        return free_margin, False, False
    if not risk.check_margin_floor(margin_level, config.MARGIN_FLOOR_PCT):
        if log_skip:
            log.info(f"skip signal {sig['ob_time']}: margin level {margin_level} at/under floor")
            signal_log.log_skipped(sig, "margin_floor",
                                    f"margin_level={margin_level} floor={config.MARGIN_FLOOR_PCT}",
                                    equity=equity, free_margin=free_margin, margin_level=margin_level)
        return free_margin, False, False

    try:
        sl_price = (sig["ob_low"] if sig["direction"] == "bull" else sig["ob_high"])
        free_margin_before = free_margin
        sizes = risk.size_trade(equity, sig["entry_price"], sl_price, free_margin,
                                 risk_pct=config.RISK_PCT, leverage=config.LEVERAGE)

        trade = open_trade(client, sig, sizes, sl_price, current_bar_time)
        active_trades.append(trade)
        free_margin -= sizes["required_margin"]  # local running deduction, no re-query
        log.info(f"opened trade: {sig['direction']} entry={sig['entry_price']} "
                  f"size_a={sizes['size_a']:.6f} size_b={sizes['size_b']:.6f}")
        signal_log.log_opened(sig, sizes, equity=equity,
                               free_margin=free_margin_before, margin_level=margin_level)
        return free_margin, True, False

    except risk.SizingError as e:
        if log_skip:
            log.info(f"skip signal {sig['ob_time']}: {e}")
            signal_log.log_skipped(sig, getattr(e, "reason", "sizing_error"), str(e),
                                    equity=equity, free_margin=free_margin, margin_level=margin_level)
        return free_margin, False, False
    except KrakenAPIError as e:
        error_log.error(f"Kraken error opening trade for signal {sig['ob_time']}: {e}")
        rate_limited = "Rate limit" in str(e)
        if log_skip:
            reason = "rate_limited" if rate_limited else "kraken_api_error"
            signal_log.log_skipped(sig, reason, str(e),
                                    equity=equity, free_margin=free_margin, margin_level=margin_level)
        if rate_limited:
            log.info("rate limit hit -- stopping order placement for this cycle")
        return free_margin, False, rate_limited


def run_cycle(client, active_trades, seen_ob_times, pending_signals):
    bars = fetch_candles(client)
    prepared = strategy.prepare_bars(bars)
    current_bar_time = prepared.index[-1]

    # 1. manage existing trades first -- fills/cancels take priority over
    #    opening new ones so margin figures used for sizing are current
    open_orders = client.get_open_orders()
    closed_orders = client.get_closed_orders()
    still_active = []
    for trade in active_trades:
        cancel_unfilled(client, trade, current_bar_time,
                         mitigation_wait_bars=strategy.WINNER["mitigation_wait"],
                         candle_minutes=config.CANDLE_MINUTES)
        done = manage_trade(client, trade, open_orders, closed_orders)
        if done:
            log.info(f"trade closed: {trade.direction} entry={trade.entry_price} "
                      f"a={trade.a['status']} b={trade.b['status']}")
        else:
            still_active.append(trade)
    active_trades = still_active

    # 2. look for new signals (already filtered to non-stale ones -- see
    #    strategy.get_new_signals' mitigation_wait check)
    signals = strategy.get_new_signals(prepared, seen_ob_times)

    equity = free_margin = margin_level = None
    balance_fetch_error = None
    if signals or pending_signals:
        # NOTE: TradeBalance is fetched ONCE per cycle, not once per signal --
        # equity/margin don't meaningfully change between signals a few
        # milliseconds apart within the same cycle, and a burst of several
        # OBs confirming off one price move (legitimate -- one strong move
        # can break multiple structure levels at once) used to mean one
        # TradeBalance call per signal, burning through Kraken's rate limit
        # for no reason. free_margin is tracked down locally as trades open
        # within this cycle (across BOTH the new-signal and pending-retry
        # passes below) so we don't overspend margin between the one real
        # balance check and however many signals get opened off it.
        try:
            # NOTE: risk is sized off account EQUITY (TradeBalance 'e'), not the raw
            # ZUSD cash balance -- Pierce's account holds its value across BTC, ATOM,
            # DOT etc, not fiat cash, so ZUSD alone reads as 0 and would silently
            # zero-size every trade. Equity is Kraken's own mark-to-market total
            # account value; free_margin ('mf') is the separate feasibility check.
            trade_bal = client.get_trade_balance()
            equity = float(trade_bal.get("e", 0))
            free_margin = float(trade_bal.get("mf", equity))
            # 'ml' is absent from Kraken's response when there are no open
            # margin positions -- confirmed against the real API during Phase 0
            # testing (came back missing, not zero). Treat missing/None as "no
            # positions open yet" -> nothing to be at risk from, floor check passes.
            ml_raw = trade_bal.get("ml")
            margin_level = float(ml_raw) if ml_raw is not None else 999.0
        except KrakenAPIError as e:
            error_log.error(f"Kraken error fetching TradeBalance this cycle: {e}")
            balance_fetch_error = str(e)

    rate_limited = False

    # 2a. brand-new signals get first shot at this cycle's capital -- an
    #     explicit choice (over retrying older pending signals first):
    #     freshest setups take priority. See "pending signal retry" below
    #     for signals that don't clear this pass.
    newly_pending = []
    for sig in signals:
        seen_ob_times.add(sig["ob_time"])
        if rate_limited:
            newly_pending.append(sig)
            continue
        free_margin, opened, hit_limit = _try_open_signal(
            client, sig, equity, free_margin, margin_level, current_bar_time,
            active_trades, balance_fetch_error, log_skip=True)
        rate_limited = rate_limited or hit_limit
        if not opened:
            newly_pending.append(sig)

    # 2b. retry every signal that was skipped on an EARLIER cycle and is
    #     still within its mitigation window (bos_time + mitigation_wait
    #     bars) -- e.g. a signal skipped for insufficient_capital gets
    #     retried here every cycle with whatever free_margin is left
    #     after 2a, so a TP fill freeing up margin can let a previously
    #     unaffordable-but-still-valid setup open on a later cycle,
    #     without ever double-counting or double-attempting a signal
    #     that was JUST added to newly_pending above this same cycle.
    #     Once the mitigation window closes the OB is genuinely gone --
    #     dropped here for good, logged as expired_unfilled so it's
    #     visible in signal_summary.py rather than silently vanishing.
    still_pending = []
    for sig in pending_signals:
        elapsed = strategy.bars_elapsed(sig["bos_time"], current_bar_time, config.CANDLE_MINUTES)
        if elapsed > strategy.WINNER["mitigation_wait"]:
            log.info(f"pending signal {sig['ob_time']} expired unfilled "
                      f"({elapsed:.0f} bars since BOS)")
            signal_log.log_skipped(sig, "expired_unfilled",
                                    f"{elapsed:.0f} bars since BOS > "
                                    f"{strategy.WINNER['mitigation_wait']} (mitigation window closed)")
            continue

        if equity is None or rate_limited:
            still_pending.append(sig)
            continue

        free_margin, opened, hit_limit = _try_open_signal(
            client, sig, equity, free_margin, margin_level, current_bar_time,
            active_trades, balance_fetch_error, log_skip=False)
        rate_limited = rate_limited or hit_limit
        if not opened:
            still_pending.append(sig)

    pending_signals = still_pending + newly_pending

    state.save_state(active_trades, seen_ob_times, pending_signals, path=config.STATE_PATH)
    return active_trades, seen_ob_times, pending_signals


def seed_seen_signals(client):
    """Called once on a true cold start (no state.json yet). Marks every
    OB currently sitting in the buffer as 'seen' WITHOUT opening trades for
    any of them -- the bot should only act on setups from this point
    forward, never retroactively fire off a backlog of already-stale
    historical signals the moment it first starts (that backlog is exactly
    what caused the rate-limit flood on first launch)."""
    bars = fetch_candles(client)
    prepared = strategy.prepare_bars(bars)
    bull_df, bear_df = strategy.find_order_blocks(prepared)
    seen = set(bull_df["ob_time"]).union(set(bear_df["ob_time"]))
    log.info(f"cold start: seeding {len(seen)} pre-existing OB(s) as already-seen "
              f"(not tradeable) -- bot will only act on signals from here forward")
    return seen


def main():
    client = KrakenClient(config.KRAKEN_API_KEY, config.KRAKEN_API_SECRET, pair=config.PAIR)
    client.resolve_pair()

    is_cold_start = not os.path.exists(config.STATE_PATH)
    active_trades, seen_ob_times, pending_signals = state.load_state(path=config.STATE_PATH)
    if is_cold_start:
        seen_ob_times = seed_seen_signals(client)
        pending_signals = []
        state.save_state(active_trades, seen_ob_times, pending_signals, path=config.STATE_PATH)

    if active_trades:
        log.info(f"restored {len(active_trades)} active trade(s) from state, reconciling...")
        active_trades = state.reconcile_on_startup(client, active_trades)
        log.info(f"{len(active_trades)} trade(s) still active after reconciliation")

    log.info("bot started")
    while True:
        cycle_start = time.time()
        try:
            active_trades, seen_ob_times, pending_signals = run_cycle(
                client, active_trades, seen_ob_times, pending_signals)
        except KrakenAPIError as e:
            error_log.error(f"Kraken API error this cycle: {e}")
        except Exception:
            error_log.error(f"unexpected error this cycle:\n{traceback.format_exc()}")

        elapsed = time.time() - cycle_start
        time.sleep(max(0, config.LOOP_SECONDS - elapsed))


if __name__ == "__main__":
    main()
