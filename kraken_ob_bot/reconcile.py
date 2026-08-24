#!/usr/bin/env python3
"""
Read-only audit: compares what the bot's state.json thinks is currently
tracked against what Kraken's OpenOrders actually shows, and flags any
order sitting live on Kraken that state.json has no record of at all --
i.e. an orphan. Meant to be run ad hoc, any time, the same way you'd run
signal_summary.py -- it never places, amends, or cancels anything.

Why this exists: before the 2026-08-23 _is_filled() fix (see
position_manager.py), the bot could lose track of a trade whose entry
took more than one cycle to fill, drop it from state.json, and never
place its TP -- but the entry's conditional SL (and, once the entry
actually filled on Kraken's side minutes later, a TP the bot never knew
to place) can keep resting on Kraken indefinitely with nothing in
state.json pointing at it. This script finds exactly that gap.

Usage:
    python3 reconcile.py                      # human-readable report
    python3 reconcile.py --state /path/to/state.json

Needs live Kraken API credentials (KRAKEN_API_KEY / KRAKEN_API_SECRET)
to call OpenOrders -- on the EC2 box those live in /etc/qtp/kraken.env,
readable only by the bot's systemd service user, so run this with sudo
and the env sourced, e.g.:

    sudo -E env $(grep -v '^#' /etc/qtp/kraken.env | xargs) python3 reconcile.py

(same credential-access pattern used for the earlier ad-hoc diagnostics).

Read-only guarantee: this script only ever calls get_open_orders() and
resolve_pair() -- both read-only Kraken endpoints. It never calls
add_order / cancel_order / amend_order. If you decide something flagged
here should actually be cancelled, do that by hand in the Kraken UI (or
ask for a separate, explicit cancel script) -- this tool's whole point
is to give you the facts first, before anything gets touched.
"""
import argparse

import config
import state
from kraken_client import KrakenClient, KrakenAPIError


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default=config.STATE_PATH,
                    help=f"state.json path (default: {config.STATE_PATH})")
    return p.parse_args()


def tracked_txids(active_trades, pending_signals):
    """Every order txid state.json currently knows about, tagged with
    where it came from (trade direction/entry price + leg + slot) so a
    match can be explained, not just confirmed. pending_signals never
    have txids -- they're signals that were evaluated and skipped/
    retried, not orders ever placed on Kraken -- so they contribute
    nothing here, but a summary count is still surfaced in the report
    since a stuck-forever pending signal is itself worth a human glance."""
    tracked = {}
    for t in active_trades:
        label = f"{t.direction} entry={t.entry_price} ob_time={t.ob_time}"
        for leg_key in ("a", "b"):
            leg = getattr(t, leg_key)
            for slot in ("entry_txid", "tp_txid", "sl_txid"):
                txid = leg.get(slot)
                if txid:
                    tracked[txid] = f"{label} leg={leg_key} {slot} status={leg['status']}"
    return tracked


def describe_order(txid, order):
    descr = order.get("descr", {})
    return (
        f"  {txid}\n"
        f"    side/type:  {descr.get('type')} {descr.get('ordertype')}\n"
        f"    pair:       {descr.get('pair')}\n"
        f"    price:      {descr.get('price')}"
        + (f"  (close trigger {descr.get('price2')})" if descr.get("price2") not in (None, "0") else "")
        + f"\n"
        f"    volume:     {order.get('vol')}  (executed: {order.get('vol_exec')})\n"
        f"    status:     {order.get('status')}\n"
        f"    opened:     {order.get('opentm')}\n"
        f"    refid:      {order.get('refid')}\n"
        f"    userref:    {order.get('userref')}\n"
        f"    descr:      {descr.get('order')}\n"
    )


def main():
    args = parse_args()

    active_trades, seen_ob_times, pending_signals = state.load_state(path=args.state)
    tracked = tracked_txids(active_trades, pending_signals)

    client = KrakenClient(config.KRAKEN_API_KEY, config.KRAKEN_API_SECRET, pair=config.PAIR)
    try:
        client.resolve_pair()
        open_orders = client.get_open_orders()
    except KrakenAPIError as e:
        print(f"Kraken API error fetching OpenOrders -- aborting (nothing changed): {e}")
        return

    print("=" * 60)
    print("Kraken OB Bot -- State Reconciliation (read-only)")
    print("=" * 60)
    print(f"State file:            {args.state}")
    print(f"Active trades tracked:  {len(active_trades)}")
    print(f"Tracked order txids:    {len(tracked)}")
    print(f"Pending signals:        {len(pending_signals)} "
          "(no live orders -- these are evaluated-but-not-yet-opened signals)")
    print(f"Open orders on Kraken:  {len(open_orders)}")
    print()

    orphans = {txid: o for txid, o in open_orders.items() if txid not in tracked}
    matched = {txid: o for txid, o in open_orders.items() if txid in tracked}

    if matched:
        print("-" * 60)
        print(f"TRACKED -- {len(matched)} order(s) on Kraken that state.json accounts for:")
        print("-" * 60)
        for txid, order in matched.items():
            print(f"  {txid}  <-  {tracked[txid]}")
        print()

    if orphans:
        print("-" * 60)
        print(f"ORPHANED -- {len(orphans)} order(s) open on Kraken with NO matching "
              "entry in state.json:")
        print("-" * 60)
        print("These are candidates for leftover debris from trades the bot lost")
        print("track of before the entry-fill-detection fix -- verify against Kraken's")
        print("own Orders/Trades history before cancelling anything by hand.\n")
        for txid, order in orphans.items():
            print(describe_order(txid, order))
    else:
        print("No orphaned orders found -- every open order on Kraken is accounted")
        print("for in state.json.")

    # Sanity check the other direction too: a txid state.json thinks is
    # still live but that Kraken's OpenOrders no longer has at all (not
    # even in a terminal state visible here) would mean state.json itself
    # is stale -- surfaced so it's not silently missed.
    missing = [txid for txid in tracked if txid not in open_orders]
    if missing:
        print()
        print("-" * 60)
        print(f"NOTE -- {len(missing)} txid(s) state.json tracks as active but that are "
              "no longer in Kraken's OpenOrders:")
        print("-" * 60)
        print("Expected if they already filled/cancelled and the bot just hasn't run its")
        print("next manage_trade() cycle yet -- only worth investigating if it persists")
        print("across multiple runs of this script.")
        for txid in missing:
            print(f"  {txid}  <-  {tracked[txid]}")


if __name__ == "__main__":
    main()
