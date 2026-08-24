#!/usr/bin/env python3
"""
Explicit, confirmation-gated cancellation of specific Kraken order txids.

NOT a bulk/blind cleanup tool -- you must name the exact txids to cancel.
Each one is re-fetched live from OpenOrders and its full details printed
BEFORE anything is touched; nothing is cancelled until you type the
order count back to confirm. Meant to be used after reviewing
reconcile.py's output and deciding which specific orphaned orders are
genuinely dead -- pass exactly those txids, nothing more.

Usage:
    python3 cancel_orders.py TXID1 TXID2 TXID3 ...

A txid that's no longer open on Kraken by the time this runs (already
cancelled, or filled since you last checked) is reported and skipped --
never treated as a reason to abort the rest.
"""
import argparse
import sys

import config
from kraken_client import KrakenClient, KrakenAPIError


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("txids", nargs="+", help="exact Kraken order txid(s) to cancel")
    return p.parse_args()


def describe(txid, order):
    descr = order.get("descr", {})
    return (f"  {txid}\n"
            f"    {descr.get('type')} {descr.get('ordertype')}  "
            f"price={descr.get('price')}  vol={order.get('vol')}  "
            f"refid={order.get('refid')}\n"
            f"    {descr.get('order')}")


def main():
    args = parse_args()
    client = KrakenClient(config.KRAKEN_API_KEY, config.KRAKEN_API_SECRET, pair=config.PAIR)
    try:
        client.resolve_pair()
        open_orders = client.get_open_orders()
    except KrakenAPIError as e:
        print(f"Kraken API error fetching OpenOrders -- aborting (nothing changed): {e}")
        sys.exit(1)

    to_cancel = {}
    missing = []
    for txid in args.txids:
        if txid in open_orders:
            to_cancel[txid] = open_orders[txid]
        else:
            missing.append(txid)

    if missing:
        print("These txid(s) are NOT currently open on Kraken -- already cancelled/filled,")
        print("or a typo. Nothing will be attempted for them:")
        for txid in missing:
            print(f"  {txid}")
        print()

    if not to_cancel:
        print("Nothing to cancel.")
        return

    print(f"About to CANCEL {len(to_cancel)} live order(s):\n")
    for txid, order in to_cancel.items():
        print(describe(txid, order))
        print()

    confirm = input(f"Type {len(to_cancel)} to confirm cancelling exactly these orders, "
                     f"anything else to abort: ")
    if confirm.strip() != str(len(to_cancel)):
        print("Aborted -- nothing cancelled.")
        return

    print()
    for txid in to_cancel:
        try:
            client.cancel_order(txid)
            print(f"  cancelled {txid}")
        except KrakenAPIError as e:
            print(f"  FAILED to cancel {txid}: {e}")


if __name__ == "__main__":
    main()
