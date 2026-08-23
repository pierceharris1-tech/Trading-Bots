#!/usr/bin/env python3
"""
Human-readable digest of kraken-ob-bot-signals.log -- how many signals
the bot evaluated, how many it opened vs skipped, and (for skips) why.

Usage:
    python3 signal_summary.py                     # everything in the log
    python3 signal_summary.py --days 7             # last 7 days
    python3 signal_summary.py --since 2026-08-20    # since a date (UTC)
    python3 signal_summary.py --path /path/to/kraken-ob-bot-signals.log

Read-only -- never modifies the log, and safe to run while the bot is
actively writing to it (each line is appended atomically by
signal_log.py, so a summary run mid-write just sees whatever complete
lines exist at that moment; a torn last line is skipped and counted,
not crashed on).
"""
import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    import config
    DEFAULT_PATH = config.SIGNAL_LOG_PATH
except Exception:
    # fall back to the documented convention if run somewhere config.py
    # (which needs env vars set) isn't importable -- e.g. copied out for
    # ad-hoc analysis of a downloaded log file
    DEFAULT_PATH = "/home/ubuntu/logs/bots/kraken-ob-bot-signals.log"


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--path", default=DEFAULT_PATH,
                    help=f"signal log path (default: {DEFAULT_PATH})")
    p.add_argument("--days", type=float, default=None,
                    help="only include events from the last N days")
    p.add_argument("--since", default=None,
                    help="only include events at/after this UTC date (YYYY-MM-DD)")
    return p.parse_args()


def load_events(path, cutoff=None):
    events, bad_lines = [], 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1  # e.g. a torn last line from a crash mid-write
                continue
            if cutoff is not None:
                try:
                    ts = datetime.fromisoformat(rec["ts"])
                except (KeyError, ValueError):
                    continue
                if ts < cutoff:
                    continue
            events.append(rec)
    return events, bad_lines


def main():
    args = parse_args()

    cutoff = None
    if args.days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    elif args.since is not None:
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    try:
        events, bad_lines = load_events(args.path, cutoff)
    except FileNotFoundError:
        print(f"No signal log found at {args.path} -- nothing to summarize yet.")
        return

    if not events:
        window = f" since {cutoff.date()}" if cutoff else ""
        print(f"No signal events found in {args.path}{window}.")
        return

    opened = [e for e in events if e.get("decision") == "opened"]
    skipped = [e for e in events if e.get("decision") == "skipped"]
    reason_counts = Counter(e.get("reason") or "unknown" for e in skipped)
    all_direction_counts = Counter(e.get("direction") or "unknown" for e in events)
    opened_direction_counts = Counter(e.get("direction") or "unknown" for e in opened)

    ts_values = sorted(e["ts"] for e in events if e.get("ts"))
    window_start = ts_values[0] if ts_values else "?"
    window_end = ts_values[-1] if ts_values else "?"

    print("=" * 60)
    print("Kraken OB Bot -- Signal Digest")
    print("=" * 60)
    print(f"Log file:     {args.path}")
    print(f"Window:       {window_start}  ->  {window_end}")
    line = f"Total events: {len(events)}"
    if bad_lines:
        line += f"  ({bad_lines} unparseable line(s) skipped)"
    print(line)
    print()
    print(f"Opened:  {len(opened)}  "
          f"(bull={opened_direction_counts.get('bull', 0)}, "
          f"bear={opened_direction_counts.get('bear', 0)})")
    print(f"Skipped: {len(skipped)}")
    if reason_counts:
        print()
        print("Skip reasons:")
        for reason, count in reason_counts.most_common():
            print(f"  {reason:<22} {count}")
    print()
    print(f"All evaluated signals by direction: "
          f"bull={all_direction_counts.get('bull', 0)}, "
          f"bear={all_direction_counts.get('bear', 0)}")

    if opened:
        print()
        print("Most recent opened trades:")
        for e in opened[-5:]:
            print(f"  {e['ts']}  {e.get('direction', '?'):<4}  "
                  f"entry={e.get('entry_price')}  "
                  f"size_a={e.get('size_a')}  size_b={e.get('size_b')}")

    if skipped:
        print()
        print("Most recent skips:")
        for e in skipped[-5:]:
            detail = e.get("detail") or ""
            print(f"  {e['ts']}  {e.get('direction', '?'):<4}  "
                  f"reason={e.get('reason')}  {detail}")


if __name__ == "__main__":
    main()
