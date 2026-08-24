"""
JSON persistence for bot state -- active trades, seen OB signals, and the
candle buffer. Written every cycle so a crash/restart loses at most one
3-minute cycle of progress, not the whole trading history.
"""
import json
import logging
import os
import pandas as pd
from position_manager import Trade
from kraken_client import KrakenAPIError

_log = logging.getLogger("ob_bot.state")

STATE_PATH = os.environ.get("OB_BOT_STATE_PATH", "state.json")


def save_state(active_trades, seen_ob_times, pending_signals, path=STATE_PATH):
    payload = {
        "active_trades": [t.to_dict() for t in active_trades],
        "seen_ob_times": [str(t) for t in seen_ob_times],
        # signals that were evaluated and skipped (insufficient capital,
        # margin floor, etc) but whose OB hasn't been mitigated yet --
        # bot.py retries these every cycle until they either open or the
        # mitigation window closes. See bot._try_open_signal.
        "pending_signals": pending_signals,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp_path, path)  # atomic on POSIX -- no half-written state.json on crash


def load_state(path=STATE_PATH):
    if not os.path.exists(path):
        return [], set(), []
    with open(path) as f:
        payload = json.load(f)

    trades = []
    for d in payload.get("active_trades", []):
        t = Trade.__new__(Trade)  # rehydrate without re-running __init__'s pricing math
        t.direction = d["direction"]
        t.ob_time = d["ob_time"]
        t.entry_price = d["entry_price"]
        t.sl_price = d["sl_price"]
        t.tp_a_price = d["tp_a_price"]
        t.tp_b_price = d["tp_b_price"]
        t.size_a = d["size_a"]
        t.size_b = d["size_b"]
        # opened_from_time replaces the old opened_from_bar_idx (bar
        # positions don't survive across cycles -- see
        # position_manager.cancel_unfilled). Fall back to ob_time for a
        # state.json written by the pre-fix bot version, so an old file
        # doesn't crash load_state on restart.
        opened_from = d.get("opened_from_time", d.get("ob_time"))
        t.opened_from_time = pd.Timestamp(opened_from)
        t.a = d["a"]
        t.b = d["b"]
        trades.append(t)

    # ob_time is a pandas Timestamp everywhere it's compared (strategy.py's
    # `row["ob_time"] in seen_ob_times` check) -- saved to JSON as a plain
    # string, so it MUST be converted back to Timestamp here, or every
    # entry silently fails to match (different type -> different hash ->
    # `in` on the set always returns False) and the bot "forgets" every
    # signal it has ever seen the moment it restarts. Confirmed happening
    # live on 2026-08-23 before this fix: signals from hours earlier kept
    # reappearing as "new" after every restart.
    seen = set(pd.Timestamp(t) for t in payload.get("seen_ob_times", []))

    pending = []
    for d in payload.get("pending_signals", []):
        d = dict(d)
        d["ob_time"] = pd.Timestamp(d["ob_time"])
        if d.get("bos_time") is not None:
            d["bos_time"] = pd.Timestamp(d["bos_time"])
        pending.append(d)

    return trades, seen, pending


def reconcile_on_startup(client, active_trades):
    """Call once at bot startup after load_state(). Cross-checks every
    tracked order txid against Kraken's live OpenOrders/ClosedOrders so a
    fill or cancellation that happened while the bot was down isn't missed.
    Reuses manage_trade's own fill-detection so there's one code path for
    'what happened to this order', not two that could disagree.

    FIXED 2026-08-24: this loop had no per-trade try/except, unlike
    bot.run_cycle()'s equivalent loop (patched earlier the same day) --
    an exception from one stuck trade here, called directly from main()
    before the main loop's own try/except even starts, would crash the
    whole process on EVERY startup attempt, not just cost one cycle.
    Isolating it the same way run_cycle() already is."""
    from position_manager import manage_trade

    open_orders = client.get_open_orders()
    closed_orders = client.get_closed_orders()

    still_active = []
    for trade in active_trades:
        try:
            done = manage_trade(client, trade, open_orders, closed_orders)
        except KrakenAPIError as e:
            _log.error(f"Kraken error reconciling trade on startup (direction={trade.direction} "
                       f"entry={trade.entry_price}): {e} -- leaving active, retry next cycle")
            still_active.append(trade)
            continue
        if not done:
            still_active.append(trade)
    return still_active
