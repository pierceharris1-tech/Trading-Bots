#!/usr/bin/env python3
"""
Read-only investigation: reconstructs REALIZED $ P&L per trade directly
from Kraken's own fill-level TradesHistory (not the UI's P&L/P&L% display,
whose exact formula isn't something this reconciles against), and compares
it to the equity the bot itself recorded (via signal_log) at the moment
each trade opened -- to answer, with real numbers rather than a screenshot
read, "what fraction of equity did this trade actually risk/lose".

Matching approach (best-effort, flags anything ambiguous rather than
guessing): for each 'opened' event in the signal log, find the two entry
fills (by side + price-proximity + size-proximity, closest in time at/after
the open event), then find each leg's closing fill (opposite side, same
size, occurring after the entry fill). Realized $ for a leg = the signed
cash flow of its entry fill plus its closing fill (accounts for fees on
both sides). A leg whose closing fill can't be found yet is still open --
reported as such, not treated as zero.

Usage:
    python3 risk_check.py                  # every 'opened' event in the log
    python3 risk_check.py --days 1          # only the last N days

Entirely read-only -- calls TradesHistory only, never places or touches
an order.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone

import config
from kraken_client import KrakenClient, KrakenAPIError

PRICE_TOL = 0.01   # 1% -- entry fills can be a few ticks off signal_log's recorded entry_price
SIZE_TOL = 0.02    # 2% -- rounding between risk.size_trade()'s float and the actual filled volume


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=float, default=None,
                    help="only consider 'opened' events from the last N days")
    p.add_argument("--signal-log", default=config.SIGNAL_LOG_PATH)
    return p.parse_args()


def load_opened_events(path, cutoff=None):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("decision") != "opened":
                continue
            if cutoff is not None:
                try:
                    ts = datetime.fromisoformat(rec["ts"])
                except (KeyError, ValueError):
                    continue
                if ts < cutoff:
                    continue
            events.append(rec)
    events.sort(key=lambda r: r["ts"])
    return events


def load_fills(client):
    raw = client.get_trades_history()
    fills = []
    for txid, f in raw.items():
        fills.append({
            "txid": txid,
            "time": float(f["time"]),
            "type": f["type"],          # 'buy' or 'sell'
            "price": float(f["price"]),
            "vol": float(f["vol"]),
            "cost": float(f["cost"]),
            "fee": float(f["fee"]),
        })
    fills.sort(key=lambda f: f["time"])
    return fills


def cashflow(fill):
    sign = 1 if fill["type"] == "sell" else -1
    return sign * fill["cost"] - fill["fee"]


def find_entry_fill(fills, used, side, price, size, after_ts):
    best, best_score = None, None
    for f in fills:
        if f["txid"] in used or f["type"] != side:
            continue
        if f["time"] < after_ts - 5:  # allow tiny clock slack, not much
            continue
        if abs(f["price"] - price) / price > PRICE_TOL:
            continue
        if abs(f["vol"] - size) / size > SIZE_TOL:
            continue
        score = f["time"]  # earliest matching fill after the signal
        if best is None or score < best_score:
            best, best_score = f, score
    return best


def find_close_fill(fills, used, side, size, after_ts):
    best, best_score = None, None
    for f in fills:
        if f["txid"] in used or f["type"] != side:
            continue
        if f["time"] < after_ts:
            continue
        if abs(f["vol"] - size) / size > SIZE_TOL:
            continue
        score = f["time"]
        if best is None or score < best_score:
            best, best_score = f, score
    return best


def analyze_leg(fills, used, direction, entry_price, size, open_ts, label):
    if size <= 0:
        return None
    entry_side = "buy" if direction == "bull" else "sell"
    close_side = "sell" if direction == "bull" else "buy"

    entry_fill = find_entry_fill(fills, used, entry_side, entry_price, size, open_ts)
    if entry_fill is None:
        return {"label": label, "status": "entry not found (never filled, or outside match tolerance)"}
    used.add(entry_fill["txid"])

    close_fill = find_close_fill(fills, used, close_side, entry_fill["vol"], entry_fill["time"])
    if close_fill is None:
        return {"label": label, "status": "still open (no matching close fill found)",
                "entry_fill": entry_fill}
    used.add(close_fill["txid"])

    realized = cashflow(entry_fill) + cashflow(close_fill)
    return {"label": label, "status": "closed", "realized": realized,
            "entry_fill": entry_fill, "close_fill": close_fill}


def main():
    args = parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days) if args.days else None

    try:
        events = load_opened_events(args.signal_log, cutoff)
    except FileNotFoundError:
        print(f"No signal log found at {args.signal_log}")
        return
    if not events:
        print("No 'opened' events found in that window.")
        return

    client = KrakenClient(config.KRAKEN_API_KEY, config.KRAKEN_API_SECRET, pair=config.PAIR)
    try:
        client.resolve_pair()
        fills = load_fills(client)
    except KrakenAPIError as e:
        print(f"Kraken API error fetching TradesHistory: {e}")
        return

    used = set()
    print("=" * 78)
    print("Kraken OB Bot -- Realized P&L vs Recorded Equity (read-only)")
    print("=" * 78)
    print(f"{'opened':<20} {'dir':<5} {'entry':>10} {'equity':>9} {'2% risk':>9} "
          f"{'realized':>10} {'% of equity':>12}")
    print("-" * 78)

    totals_realized = 0.0
    totals_intended = 0.0
    unresolved = []
    slippage_rows = []  # (ts, direction, label, entry_px, close_px, actual_dist, implied_dist, realized)

    for e in events:
        ts, direction, entry_price = e["ts"], e["direction"], e["entry_price"]
        equity = e.get("equity")
        size_a, size_b = e.get("size_a") or 0.0, e.get("size_b") or 0.0
        open_ts = datetime.fromisoformat(ts).timestamp()

        leg_results = [
            analyze_leg(fills, used, direction, entry_price, size_a, open_ts, "A"),
            analyze_leg(fills, used, direction, entry_price, size_b, open_ts, "B"),
        ]

        # implied per-BTC stop distance this trade WOULD have needed to make
        # the 2%-of-equity sizing math work out, using the ACTUAL filled
        # volume (not the requested size_a/size_b) -- entry_price - sl_price
        # isn't logged anywhere per-trade, so this backs it out from the
        # sizing formula itself: total_size = risk_amount / sl_distance
        # => sl_distance = risk_amount / total_size.
        total_filled_vol = sum(
            lr["entry_fill"]["vol"] for lr in leg_results
            if lr is not None and lr.get("entry_fill") is not None
        )
        implied_dist = (equity * config.RISK_PCT / total_filled_vol) if (equity and total_filled_vol) else None

        trade_realized = 0.0
        complete = True
        for lr in leg_results:
            if lr is None:
                continue
            if lr["status"] != "closed":
                complete = False
                unresolved.append((ts, direction, entry_price, lr["label"], lr["status"]))
            else:
                trade_realized += lr["realized"]
                if lr["realized"] < 0 and implied_dist:
                    entry_px = lr["entry_fill"]["price"]
                    close_px = lr["close_fill"]["price"]
                    actual_dist = abs(close_px - entry_px)
                    slippage_rows.append((ts, direction, lr["label"], entry_px, close_px,
                                           actual_dist, implied_dist, lr["realized"]))

        intended = (equity * config.RISK_PCT) if equity else None
        pct = (trade_realized / equity * 100) if equity else None

        realized_str = f"{trade_realized:+.2f}" if complete else f"{trade_realized:+.2f}*"
        pct_str = f"{pct:+.1f}%" if pct is not None else "n/a"
        equity_str = f"{equity:.2f}" if equity is not None else "n/a"
        intended_str = f"{intended:.2f}" if intended is not None else "n/a"

        print(f"{ts[:19]:<20} {direction:<5} {entry_price:>10.1f} {equity_str:>9} "
              f"{intended_str:>9} {realized_str:>10} {pct_str:>12}")

        if equity:
            totals_intended += intended
        totals_realized += trade_realized

    print("-" * 78)
    print(f"{'TOTAL (rows with * are partially unresolved -- see below)':<58} "
          f"{totals_realized:>+10.2f}")
    print()
    print(f"Sum of 2%-of-equity risk budgets across all trades shown: {totals_intended:.2f}")
    print(f"Sum of realized $ P&L across all trades shown:            {totals_realized:+.2f}")

    if slippage_rows:
        print()
        print("-" * 78)
        print("SLIPPAGE CHECK -- for each losing leg: the stop distance implied by the")
        print("2%-of-equity sizing math, vs the ACTUAL distance between the entry fill")
        print("and the closing fill. A ratio well above 1.0x means the exit happened")
        print("much further from entry than the intended stop -- i.e. slippage on a")
        print("stop-loss order that executes at market once triggered, not a sizing bug.")
        print("-" * 78)
        print(f"{'opened':<20} {'leg':<4} {'entry px':>10} {'close px':>10} "
              f"{'implied $':>10} {'actual $':>10} {'ratio':>7}")
        for ts, direction, label, entry_px, close_px, actual_dist, implied_dist, realized in slippage_rows:
            ratio = actual_dist / implied_dist if implied_dist else float("nan")
            print(f"{ts[:19]:<20} {label:<4} {entry_px:>10.1f} {close_px:>10.1f} "
                  f"{implied_dist:>10.1f} {actual_dist:>10.1f} {ratio:>6.1f}x")

    if unresolved:
        print()
        print("-" * 78)
        print("UNRESOLVED LEGS (not included in that leg's realized total above):")
        print("-" * 78)
        for ts, direction, entry_price, label, status in unresolved:
            print(f"  {ts[:19]}  {direction:<5} entry={entry_price}  leg={label}  -- {status}")


if __name__ == "__main__":
    main()
