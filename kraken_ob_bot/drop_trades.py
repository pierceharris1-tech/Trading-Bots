#!/usr/bin/env python3
"""
Explicit, confirmation-gated removal of specific trade(s) from
state.json's active_trades list.

For trades that are fully resolved on Kraken's side (every leg closed or
cancelled) but got stuck in active_trades because a crash mid-cycle
prevented state.save_state() from ever recording the resolution -- run
reconcile.py first and cross-check Kraken's own Orders/Trades/Positions
history to confirm a trade is really done before dropping it here.
Read-only against Kraken: this only ever rewrites state.json, never
touches an order or a position.

Usage:
    python3 drop_trades.py --drop "76387.9,2026-08-23 05:12:00"
    python3 drop_trades.py --drop "76387.9,2026-08-23 05:12:00" \\
                            --drop "78014.5,2026-08-23 21:33:00"

Each --drop identifies a trade by BOTH entry_price and ob_time together
(not just one), so a coincidental price match on a different trade can
never drop the wrong one. Copy both values straight from reconcile.py's
output or state.json to avoid transcription mistakes.
"""
import argparse

import config
import state


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--drop", action="append", required=True, metavar="ENTRY_PRICE,OB_TIME",
                    help="repeatable -- e.g. --drop \"76387.9,2026-08-23 05:12:00\"")
    p.add_argument("--state", default=config.STATE_PATH,
                    help=f"state.json path (default: {config.STATE_PATH})")
    return p.parse_args()


def main():
    args = parse_args()

    targets = []
    for raw in args.drop:
        if "," not in raw:
            print(f"Couldn't parse --drop value {raw!r} -- expected 'entry_price,ob_time'")
            return
        price_str, ob_time_str = raw.split(",", 1)
        try:
            targets.append((float(price_str.strip()), ob_time_str.strip()))
        except ValueError:
            print(f"Couldn't parse entry_price out of {raw!r}")
            return

    active_trades, seen_ob_times, pending_signals = state.load_state(path=args.state)

    def matches(t, price, ob_time):
        return abs(t.entry_price - price) < 1e-6 and str(t.ob_time) == ob_time

    to_drop, to_keep = [], []
    for t in active_trades:
        if any(matches(t, price, ob_time) for price, ob_time in targets):
            to_drop.append(t)
        else:
            to_keep.append(t)

    unmatched = [tgt for tgt in targets
                 if not any(matches(t, *tgt) for t in to_drop)]
    if unmatched:
        print("These --drop target(s) matched NO trade currently in active_trades --")
        print("check entry_price/ob_time against state.json. Nothing dropped for them:")
        for price, ob_time in unmatched:
            print(f"  entry_price={price}  ob_time={ob_time}")
        print()

    if not to_drop:
        print("Nothing matched -- state.json left unchanged.")
        return

    print(f"Currently {len(active_trades)} trade(s) in active_trades. "
          f"About to DROP {len(to_drop)}, KEEP {len(to_keep)}:\n")
    print("DROPPING:")
    for t in to_drop:
        print(f"  {t.direction} entry={t.entry_price} ob_time={t.ob_time} "
              f"a={t.a['status']} b={t.b['status']}")
    if to_keep:
        print("\nKEEPING:")
        for t in to_keep:
            print(f"  {t.direction} entry={t.entry_price} ob_time={t.ob_time} "
                  f"a={t.a['status']} b={t.b['status']}")

    confirm = input(f"\nType {len(to_drop)} to confirm dropping exactly these trades "
                     f"from {args.state}, anything else to abort: ")
    if confirm.strip() != str(len(to_drop)):
        print("Aborted -- state.json left unchanged.")
        return

    state.save_state(to_keep, seen_ob_times, pending_signals, path=args.state)
    print(f"\nDone -- {args.state} now has {len(to_keep)} active trade(s).")


if __name__ == "__main__":
    main()
