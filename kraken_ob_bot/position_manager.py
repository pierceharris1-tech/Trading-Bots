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
import logging
import time

import config
import strategy
from kraken_client import KrakenAPIError

R_BASE = 3
R_RUNNER = 20

# Propagates to bot.py's root logging config (FileHandler + StreamHandler
# on kraken-ob-bot-trades.log) via ordinary logger-name-hierarchy
# propagation -- no need to pass a logger through every call site.
_log = logging.getLogger("ob_bot.position_manager")


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

    VERIFIED 2026-08-24 (the refid-linkage question flagged below is
    resolved -- confirmed live via a manually-placed TP that Kraken's own
    order history showed correctly refid-linked back to its entry).

    FIXED 2026-08-24: the open_orders loop iterated `.values()` and then
    read `o.get('txid', None)` -- but Kraken's OpenOrders response nests
    each order under its txid as the DICT KEY (see kraken_client.
    get_open_orders: `result.get('open', {})` is `{txid: order}`), not as
    a field inside the order object itself. `.values()` throws the key
    away, so this always returned None for any SL still genuinely
    resting -- which is the NORMAL state right after an entry fills and
    before the TP/SL race even starts. Only an SL that had ALREADY fired
    (found via the second loop, which correctly used `.items()`) was ever
    discoverable. Consequence: leg['sl_txid'] never got set while
    protection was actually live, so manage_trade() could never detect
    that SL's eventual fill (sl_filled is hard-wired to None whenever
    sl_txid is None) -- a leg could sit at 'open_racing' forever with its
    TP never cancelled even after its real SL had already triggered on
    Kraken, and _move_runner_to_be() could never find a live SL to amend
    to breakeven at all. Very likely the actual mechanism behind most of
    the orphaned-order buildup found via reconcile.py today, not just the
    one incident that prompted this audit."""
    for txid, o in open_orders.items():
        if o.get("refid") == entry_txid:
            return txid
    for txid, o in closed_orders.items():
        if o.get("refid") == entry_txid:
            return txid
    return None


FAVORABLE_A_CLOSE = ("closed_tp", "closed_external")
TERMINAL = ("closed_tp", "closed_sl", "cancelled", "closed_external")


def _move_runner_to_be(client, trade, open_orders, closed_orders):
    """Standing invariant, re-checked every cycle rather than fired once
    at the moment of detection: if leg A has closed favourably and leg B
    is racing but not yet moved to breakeven, move it. Idempotent (guarded
    by sl_at_be) and self-healing -- it no longer matters WHICH cycle or
    WHICH code path resolved leg A, or whether leg B only became
    'open_racing' after leg A already resolved (an ordering gap the old
    inline version -- fired only from inside the tp_filled branch, once,
    for leg A -- could miss entirely; see 2026-08-24 postmortem below)."""
    if trade.a["status"] not in FAVORABLE_A_CLOSE:
        return
    if trade.b["status"] != "open_racing" or trade.b["sl_at_be"]:
        return
    if trade.b["sl_txid"] is None:
        trade.b["sl_txid"] = _find_sl_txid(open_orders, closed_orders, trade.b["entry_txid"])
    if not trade.b["sl_txid"]:
        return  # not discoverable yet -- retry next cycle
    try:
        client.amend_order(trade.b["sl_txid"], trigger_price=trade.entry_price)
        trade.b["sl_at_be"] = True
    except KrakenAPIError as e:
        _log.error(f"failed to move runner SL to breakeven (entry={trade.entry_price} "
                    f"sl_txid={trade.b['sl_txid']}): {e} -- will retry next cycle")


def manage_trade(client, trade, open_orders, closed_orders):
    """Call every cycle for every active trade. Advances each leg through
    its lifecycle and returns True if the trade is fully closed (both
    legs done) so the caller can drop it from active state.

    POSTMORTEM 2026-08-24: two bugs found together via a live incident.

    (1) The entry_open branch, on seeing an entry filled, unconditionally
    tried to place a NEW reduce_only TP order for that leg. If the
    position was already flat by the time the bot noticed -- either its
    own conditional SL fired before the bot's next 3-minute poll (a fast
    move can land both the entry fill and the SL fill inside one cycle),
    or Pierce closed it manually straight on the exchange (confirmed
    happening live) -- Kraken rejects a reduce_only order that would
    increase rather than reduce the position ("Reduce only: Cannot
    increase position"). That exception propagated all the way out of
    run_cycle() into bot.py's outer except, which logs one line and moves
    on WITHOUT ever reaching state.save_state() -- so the failing leg's
    status never advanced past entry_open, and every subsequent cycle
    (every 3 minutes, for ~2 hours before caught) retried the identical
    doomed placement. Fixed two ways: first, explicitly check whether the
    leg's own conditional SL already fired (definitely a loss, not a
    TP-shaped close) before ever attempting a TP placement. Second, wrap
    the placement attempt itself in try/except -- if Kraken still rejects
    it as reduce-only despite the SL not having fired, the most likely
    explanation left is an external close (the manual-TP case actually
    observed), so the leg is marked resolved rather than retried forever.
    Nothing here should ever again be able to silently freeze state.json.

    (2) Runner SL->breakeven only used to fire from inside this same
    entry_open->TP flow (leg A's own tp_filled detection). Because leg
    A's TP in the live incident was placed manually, outside the bot,
    leg A never passed through that exact code path at all -- it went
    entry_open -> closed_external via the new except-branch instead --
    so the BE-move trigger was structurally unreachable for this trade,
    even after fixing (1). Replaced the one-shot inline trigger with
    _move_runner_to_be(), a standing check re-run at the end of every
    manage_trade() call regardless of which path closed leg A or in what
    order the two legs were processed this cycle."""

    for leg_key, tp_price in (("a", trade.tp_a_price), ("b", trade.tp_b_price)):
        leg = trade.a if leg_key == "a" else trade.b

        if leg["status"] == "entry_open":
            filled = _is_filled(open_orders, closed_orders, leg["entry_txid"])
            if filled:
                sl_txid = _find_sl_txid(open_orders, closed_orders, leg["entry_txid"])
                sl_already_filled = _is_filled(open_orders, closed_orders, sl_txid) if sl_txid else None
                if sl_already_filled:
                    # own conditional stop already fired before the bot
                    # noticed the entry was even filled -- a loss, and
                    # nothing left to place a TP against.
                    leg["status"] = "closed_sl"
                    leg["sl_txid"] = sl_txid
                else:
                    leg["sl_txid"] = sl_txid
                    try:
                        _place_leg_tp(client, trade, leg_key, tp_price)
                    except KrakenAPIError as e:
                        _log.error(
                            f"TP placement rejected for {trade.direction} leg {leg_key} "
                            f"(entry={trade.entry_price}): {e} -- own SL hasn't fired, so "
                            f"treating as externally closed rather than retrying forever")
                        leg["status"] = "closed_external"
                        if leg["sl_txid"]:
                            try:
                                client.cancel_order(leg["sl_txid"])
                            except KrakenAPIError:
                                pass  # already gone/cancelled -- fine, nothing to do
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
                    try:
                        client.cancel_order(leg["sl_txid"])
                    except KrakenAPIError as e:
                        _log.error(f"failed to cancel SL {leg['sl_txid']} after TP fill: {e}")

            elif sl_filled:
                leg["status"] = "closed_sl"
                if leg["tp_txid"]:
                    try:
                        client.cancel_order(leg["tp_txid"])
                    except KrakenAPIError as e:
                        _log.error(f"failed to cancel TP {leg['tp_txid']} after SL fill: {e}")

    _move_runner_to_be(client, trade, open_orders, closed_orders)

    both_done = trade.a["status"] in TERMINAL and trade.b["status"] in TERMINAL
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
            try:
                client.cancel_order(leg["entry_txid"])
                leg["status"] = "cancelled"
                cancelled_any = True
            except KrakenAPIError as e:
                _log.error(f"failed to cancel unfilled entry {leg['entry_txid']}: {e} "
                           f"-- will retry next cycle")
    return cancelled_any
