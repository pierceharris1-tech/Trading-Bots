#!/usr/bin/env python3
"""
Explicit, confirmation-gated amendment of a single resting order's
trigger and/or limit price -- e.g. moving a stop-loss to breakeven.

Re-fetches the order live from OpenOrders and prints its current state
before doing anything; nothing is amended until you type 'amend' to
confirm.

Usage:
    python3 amend_order.py TXID --trigger-price 77689.8
    python3 amend_order.py TXID --limit-price 77689.8
    python3 amend_order.py TXID --trigger-price 77689.8 --limit-price 77689.9
"""
import argparse
import sys

import config
from kraken_client import KrakenClient, KrakenAPIError


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("txid", help="exact Kraken order txid to amend")
    p.add_argument("--trigger-price", type=float, default=None)
    p.add_argument("--limit-price", type=float, default=None)
    return p.parse_args()


def describe(txid, order):
    descr = order.get("descr", {})
    return (f"  {txid}\n"
            f"    {descr.get('type')} {descr.get('ordertype')}  "
            f"price={descr.get('price')}  price2={descr.get('price2')}  vol={order.get('vol')}\n"
            f"    {descr.get('order')}")


def main():
    args = parse_args()
    if args.trigger_price is None and args.limit_price is None:
        print("Nothing to do -- pass --trigger-price and/or --limit-price.")
        sys.exit(1)

    client = KrakenClient(config.KRAKEN_API_KEY, config.KRAKEN_API_SECRET, pair=config.PAIR)
    try:
        client.resolve_pair()
        open_orders = client.get_open_orders()
    except KrakenAPIError as e:
        print(f"Kraken API error fetching OpenOrders -- aborting (nothing changed): {e}")
        sys.exit(1)

    if args.txid not in open_orders:
        print(f"{args.txid} is not currently open on Kraken -- already filled/cancelled, or a typo.")
        return

    print("Current order:")
    print(describe(args.txid, open_orders[args.txid]))
    print()
    print("About to amend:")
    if args.trigger_price is not None:
        print(f"  trigger_price -> {args.trigger_price}")
    if args.limit_price is not None:
        print(f"  limit_price   -> {args.limit_price}")

    confirm = input("\nType 'amend' to confirm, anything else to abort: ")
    if confirm.strip() != "amend":
        print("Aborted -- nothing changed.")
        return

    try:
        resp = client.amend_order(args.txid, trigger_price=args.trigger_price,
                                   limit_price=args.limit_price)
        print(f"\nAmended. Kraken response: {resp}")
    except KrakenAPIError as e:
        print(f"\nFAILED to amend {args.txid}: {e}")


if __name__ == "__main__":
    main()
