"""
Order placement, fill tracking, and the bot's own OCO emulation.

Kraken spot has no native bracket/OCO order (confirmed against Kraken's
docs during the build) -- close[] on AddOrder allows exactly one
conditional leg. So each leg (A and B) is managed as:

  1. Resting LIMIT entry, with close[ordertype]=stop-loss as the
     conditional close -- this activates automatically the moment the
     entry fills, so the stop protects the position even if the bot
     process is down.
  2. Once the entry is confirmed filled, the bot places a SEPARATE
     resting limit TP order for that leg's size.
  3. Every cycle, the bot checks both the TP order and the conditional
     SL order. Whichever fills first, the bot cancels the other -- this
     is the OCO emulation. A crash between "TP filled" and "SL cancelled"
     leaves a harmless orphaned SL order below/above an already-closed
     position; state.py reconciliation on restart cleans this up.

Runner (leg B): TP is fixed at R20 from the start. When leg A's TP
fills at R3, the bot calls AmendOrder to move B's conditional SL
trigger to breakeven -- no trailing stop (dropped per Pierce's decision
in favor of this simpler SL->BE + fixed R20 cap).
"""
import time

import config
import strategy

R_BASE = 3
R_RUNNER = 20


class Trade:
    """One OB signal's full lifecycle: entry -> TP/SL race -> (runner BE)."""

    def __init__(self, signal, sizes, sl_price, opened_from_time):
        self.direction = signal["direction"]
        self.ob_time = signal["ob_time"]
        self.entry_price = signal["entry_price"]
        self.sl_price = sl_price
        self.risk = abs(self.entry_price - self.sl_price)
        self.tp_a_price = (
            self.entry_price + R_BASE * self.risk if self.direction == "bull"
            else self.entry_price - R_BASE * self.risk
        )
        self.tp_b_price = (
            self.entry_price + R_RUNNER * self.risk if self.direction == "bull"
            else self.entry_price - R_RUNNER * self.risk
        )
        self.size_a = sizes["size_a"]
        self.size_b = sizes["size_b"]
        # timestamp (not a buffer bar-index -- see cancel_unfilled) used
        # for the mitigation-window cancel timeout
        self.opened_from_time = opened_from_time

        # order tracking, filled in by open_trade() / manage_trade()
        self.a = {"entry_txid": None, "tp_txid": None, "sl_txid": None, "status": "pending"}
        self.b = {"entry_txid": None, "tp_txid": None, "sl_txid": None,
                  "status": "pending", "sl_at_be": False}

    def to_dict(self):
        return {
            "direction": self.direction, "ob_time": str(self.ob_time),
            "entry_price": self.entry_price, "sl_price": self.sl_price,
            "tp_a_price": self.tp_a_price, "tp_b_price": self.tp_b_price,
            "size_a": self.size_a, "size_b": self.size_b,
            "opened_from_time": str(self.opened_from_time),
            "a": self.a, "b": self.b,
        }


def open_trade(client, signal, sizes, sl_price, opened_from_time):
    """Place the two resting limit entries (A + B), each with its own
    conditional SL close. Returns a Trade with entry txids filled in.

    FIXED 2026-08-23: kraken_client.add_order()'s leverage param defaults
    to 5 and neither call here was passing config.LEVERAGE explicitly, so
    every live order used 5x regardless of config -- risk.size_trade()'s
    required_margin math (which DOES use config.LEVERAGE) was computing
    against a leverage the real order never actually got, silently
    understating how much margin each trade really locked up. Caught by
    Pierce noticing a live position open at 5x right after setting
    config.LEVERAGE=10 and restarting."""
    trade = Trade(signal, sizes, sl_price, opened_from_time)
    side = "buy" if trade.direction == "bull" else "sell"

    if trade.size_a > 0:
        resp_a = client.add_order(side, trade.size_a, trade.entry_price, sl_price,
                                   leverage=config.LEVERAGE)
        trade.a["entry_txid"] = resp_a["txid"][0]
        trade.a["status"] = "entry_open"

    if trade.size_b > 0:
        resp_b = client.add_order(side, trade.size_b, trade.entry_price, sl_price,
                                   leverage=config.LEVERAGE)
        trade.b["entry_txid"] = resp_b["txid"][0]
        trade.b["status"] = "entry_open"

    return trade


def _is_filled(open_orders, closed_orders, txid):
    """Returns True if genuinely filled, False if genuinely
    cancelled/expired (never going to fill), or None if still resting/
    pending -- callers must only act on True/False and leave the order
    alone (retry next cycle) on None.

    FIXED 2026-08-23: previously returned False for BOTH "still resting,
    not yet filled" (txid in open_orders) AND "genuinely cancelled" (in
    closed_orders with a terminal non-closed status) -- manage_trade()
    treats any False as "cancelled" and drops the trade, so a perfectly
    live resting entry order that simply hadn't filled within a single
    3-minute cycle yet got wrongly declared dead and the trade lost from
    tracking, EVERY time an entry took more than one cycle to fill
    (normal for a resting limit order waiting for price to retrace to an
    OB edge -- not a rare edge case). Caught by Pierce comparing the
    bot's own log against Kraken's real order history: the bot declared
    a trade 'closed (cancelled)' at 16:29:09 UTC, but Kraken's records
    show both entry legs didn't actually fill until 16:35 -- six minutes
    later, after the bot had already stopped tracking it. That trade's
    TP was consequently never placed, since it never got another
    manage_trade() cycle to notice the eventual fill."""
    if txid in open_orders:
        return None  # still resting -- not filled, not cancelled, just waiting
    order = closed_orders.get(txid)
    if order is None:
        return None  # not found yet -- eventual-consistency lag, retry next cycle
    status = order.get("status")
    if status == "closed":
        return True
    if status in ("canceled", "expired"):
        return False
    return None  # some other/unknown terminal status -- don't assume, retry


def _place_leg_tp(client, trade, leg_key, tp_price):
    leg = trade.a if leg_key == "a" else trade.b
    size = trade.size_a if leg_key == "a" else trade.size_b
    close_side = "sell" if trade.direction == "bull" else "buy"
    resp = client.add_order(close_side, size, tp_price, ordertype="limit", reduce_only=True)
    leg["tp_txid"] = resp["txid"][0]
    leg["status"] = "open_racing"


def _find_sl_txid(open_orders, closed_orders, entry_txid):
    """The conditional close Kraken auto-creates from an entry's close[]
    is a separate order whose `refid` points back at the entry's txid.
    Search both open and closed so it's still findable after it fires.
    UNVERIFIED: this refid linkage is documented behavior but the exact
    shape (does it appear in OpenOrders before or only after triggering?
    is refid definitely the entry txid and not something else?) needs
    confirming against a real demo account in Phase 1/7 before this is
    trusted with real capital -- flagged rather than assumed."""
    for o in open_orders.values():
        if o.get("refid") == entry_txid:
            return o.get("txid", None)
    for txid, o in closed_orders.items():
        if o.get("refid") == entry_txid:
            return txid
    return None


def manage_trade(client, trade, open_orders, closed_orders):
    """Call every cycle for every active trade. Advances each leg through
    its lifecycle and returns True if the trade is fully closed (both
    legs done) so the caller can drop it from active state."""

    for leg_key, tp_price in (("a", trade.tp_a_price), ("b", trade.tp_b_price)):
        leg = trade.a if leg_key == "a" else trade.b

        if leg["status"] == "entry_open":
            filled = _is_filled(open_orders, closed_orders, leg["entry_txid"])
            if filled:
                _place_leg_tp(client, trade, leg_key, tp_price)
            elif filled is False:
                leg["status"] = "cancelled"

        elif leg["status"] == "open_racing":
            if leg["sl_txid"] is None:
                leg["sl_txid"] = _find_sl_txid(open_orders, closed_orders, leg["entry_txid"])

            tp_filled = _is_filled(open_orders, closed_orders, leg["tp_txid"])
            sl_filled = (
                _is_filled(open_orders, closed_orders, leg["sl_txid"])
                if leg["sl_txid"] else None
            )

            if tp_filled:
                leg["status"] = "closed_tp"
                if leg["sl_txid"]:
                    client.cancel_order(leg["sl_txid"])
                if leg_key == "a" and trade.b["status"] == "open_racing" and not trade.b["sl_at_be"]:
                    # Position A hit R3 -> move runner's SL to breakeven
                    if trade.b["sl_txid"] is None:
                        trade.b["sl_txid"] = _find_sl_txid(open_orders, closed_orders,
                                                            trade.b["entry_txid"])
                    if trade.b["sl_txid"]:
                        client.amend_order(trade.b["sl_txid"], trigger_price=trade.entry_price)
                        trade.b["sl_at_be"] = True

            elif sl_filled:
                leg["status"] = "closed_sl"
                if leg["tp_txid"]:
                    client.cancel_order(leg["tp_txid"])

    both_done = trade.a["status"] in ("closed_tp", "closed_sl", "cancelled") and \
        trade.b["status"] in ("closed_tp", "closed_sl", "cancelled")
    return both_done


def cancel_unfilled(client, trade, current_bar_time, mitigation_wait_bars=200, candle_minutes=3):
    """Cancel entries that never filled within the OB's mitigation window.

    FIXED 2026-08-23: originally compared trade.opened_from_bar_idx (a
    position within the rolling candle buffer, captured once at open
    time) against a freshly-computed current_bar_idx from a LATER
    cycle's buffer. Because bot.fetch_candles keeps a fixed-size sliding
    window (.tail(BUFFER_BARS)), a bar's position inside that window
    shifts every cycle even though its timestamp doesn't -- once the
    buffer was full, current_bar_idx was ~constant (~BUFFER_BARS-1)
    every cycle, so the subtraction never grew and this timeout likely
    never fired in practice. Now compares real timestamps instead, via
    strategy.bars_elapsed, which stays correct across cycles."""
    elapsed_bars = strategy.bars_elapsed(trade.opened_from_time, current_bar_time, candle_minutes)
    if elapsed_bars <= mitigation_wait_bars:
        return False
    cancelled_any = False
    for leg in (trade.a, trade.b):
        if leg["status"] == "entry_open" and leg["entry_txid"]:
            client.cancel_order(leg["entry_txid"])
            leg["status"] = "cancelled"
            cancelled_any = True
    return cancelled_any
