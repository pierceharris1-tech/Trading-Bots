"""
JSON persistence for bot state -- active trades, seen OB signals, and the
candle buffer. Written every cycle so a crash/restart loses at most one
3-minute cycle of progress, not the whole trading history.
"""
import json
import os
from position_manager import Trade

STATE_PATH = os.environ.get("OB_BOT_STATE_PATH", "state.json")


def save_state(active_trades, seen_ob_times, path=STATE_PATH):
    payload = {
        "active_trades": [t.to_dict() for t in active_trades],
        "seen_ob_times": [str(t) for t in seen_ob_times],
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp_path, path)  # atomic on POSIX -- no half-written state.json on crash


def load_state(path=STATE_PATH):
    if not os.path.exists(path):
        return [], set()
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
        t.opened_from_bar_idx = d["opened_from_bar_idx"]
        t.a = d["a"]
        t.b = d["b"]
        trades.append(t)

    seen = set(payload.get("seen_ob_times", []))
    return trades, seen


def reconcile_on_startup(client, active_trades):
    """Call once at bot startup after load_state(). Cross-checks every
    tracked order txid against Kraken's live OpenOrders/ClosedOrders so a
    fill or cancellation that happened while the bot was down isn't missed.
    Reuses manage_trade's own fill-detection so there's one code path for
    'what happened to this order', not two that could disagree."""
    from position_manager import manage_trade

    open_orders = client.get_open_orders()
    closed_orders = client.get_closed_orders()

    still_active = []
    for trade in active_trades:
        done = manage_trade(client, trade, open_orders, closed_orders)
        if not done:
            still_active.append(trade)
    return still_active
