#!/usr/bin/env python3
"""
Read-only: dumps Kraken's raw TradesHistory fills, unprocessed and with NO
matching/attribution logic -- just every fill in a time window, in
chronological order, with every field. Use this to manually eyeball what
actually happened during a period with multiple overlapping trades, where
risk_check.py's automated entry/close matching can get confused (partial
fills, several same-direction trades close together in price/time).

Usage:
    python3 raw_fills.py                       # last 24h of fills
    python3 raw_fills.py --hours 6              # last 6 hours
    python3 raw_fills.py --since "2026-08-24T10:00:00"  # explicit UTC start

Entirely read-only -- calls TradesHistory (and OpenOrders/ClosedOrders for
context) only, never places or touches an order.
"""
import argparse
from datetime import datetime, timedelta, timezone

import config
from kraken_client import KrakenClient, KrakenAPIError


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=float, default=24.0,
                    help="how far back to look (default 24h) -- ignored if --since given")
    p.add_argument("--since", default=None,
                    help="explicit UTC start, e.g. 2026-08-24T10:00:00")
    return p.parse_args()


def main():
    args = parse_args()
    if args.since:
        start_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    else:
        start_dt = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    start_ts = start_dt.timestamp()

    client = KrakenClient(config.KRAKEN_API_KEY, config.KRAKEN_API_SECRET, pair=config.PAIR)
    try:
        client.resolve_pair()
        raw = client.get_trades_history(start=int(start_ts))
    except KrakenAPIError as e:
        print(f"Kraken API error fetching TradesHistory: {e}")
        return

    fills = []
    for txid, f in raw.items():
        fills.append({
            "txid": txid,
            "ordertxid": f.get("ordertxid", "?"),
            "time": float(f["time"]),
            "type": f["type"],
            "ordertype": f.get("ordertype", "?"),
            "price": float(f["price"]),
            "vol": float(f["vol"]),
            "cost": float(f["cost"]),
            "fee": float(f["fee"]),
        })
    fills.sort(key=lambda f: f["time"])

    print("=" * 100)
    print(f"Kraken OB Bot -- Raw fills since {start_dt.isoformat()} (read-only, unprocessed)")
    print("=" * 100)
    if not fills:
        print("No fills in this window.")
        return

    print(f"{'time (UTC)':<20} {'ordertxid':<10} {'side':<5} {'ordertype':<10} "
          f"{'price':>10} {'vol':>12} {'cost':>10} {'fee':>8}")
    print("-" * 100)
    for f in fills:
        t = datetime.fromtimestamp(f["time"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        print(f"{t:<20} {f['ordertxid'][:10]:<10} {f['type']:<5} {f['ordertype']:<10} "
              f"{f['price']:>10.1f} {f['vol']:>12.6f} {f['cost']:>10.2f} {f['fee']:>8.3f}")

    print("-" * 100)
    print(f"{len(fills)} fill(s) total.")
    print()
    print("Cross-check with OpenOrders/ClosedOrders below to see which txid each")
    print("ordertxid belongs to (entry vs conditional SL vs separately-placed TP),")
    print("and refid links a conditional SL order back to the entry order it")
    print("came from.")
    print()

    try:
        open_orders = client.get_open_orders()
        closed_orders = client.get_closed_orders(start=int(start_ts))
    except KrakenAPIError as e:
        print(f"(could not fetch Open/ClosedOrders for context: {e})")
        return

    print("=" * 100)
    print("OPEN ORDERS (still resting)")
    print("=" * 100)
    if not open_orders:
        print("(none)")
    for txid, o in open_orders.items():
        descr = o.get("descr", {})
        print(f"  {txid}  refid={o.get('refid')}  status={o.get('status')}  "
              f"type={descr.get('ordertype')}  {descr.get('order', '')}")

    print()
    print("=" * 100)
    print(f"CLOSED ORDERS since {start_dt.isoformat()}")
    print("=" * 100)
    closed_in_window = {txid: o for txid, o in closed_orders.items()
                         if float(o.get("closetm", 0) or 0) >= start_ts}
    if not closed_in_window:
        print("(none)")
    for txid, o in sorted(closed_in_window.items(), key=lambda kv: float(kv[1].get("closetm", 0) or 0)):
        descr = o.get("descr", {})
        t = datetime.fromtimestamp(float(o.get("closetm", 0)), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        print(f"  {t}  {txid}  refid={o.get('refid')}  status={o.get('status')}  "
              f"type={descr.get('ordertype')}  {descr.get('order', '')}")


if __name__ == "__main__":
    main()
