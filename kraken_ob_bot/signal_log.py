"""
Structured JSON-lines logging of every signal the bot evaluates -- opened
or skipped, with a reason code and the numbers behind the decision when
skipped. Kept separate from the existing text logs
(kraken-ob-bot-trades.log / -errors.log) so it's easy to grep/parse on
its own later, e.g. "how many signals did we skip this week and why" --
see signal_summary.py for a ready-made digest of this file.

One JSON object per line. Each write is a single open-append-flush-fsync,
no buffering across calls, so a crash mid-cycle can corrupt at most the
last line, never an earlier one -- every prior line stays independently
parseable.

This module must never be the reason a trade doesn't happen: every
public function catches and swallows its own errors (bad path,
permissions, disk full, non-serializable field) and falls back to a
plain error_log line instead of raising, so a logging problem degrades
to "we lost one log line" rather than "the bot crashed / skipped a real
trade".
"""
import json
import logging
import os
from datetime import datetime, timezone

import config

# Keep this in sync with the reason strings actually raised in risk.py /
# bot.py -- not enforced (an unrecognised reason is still logged, just
# flagged in `detail`) so a new reason added in one place without the
# other is visible in the log rather than silently dropped.
REASON_CODES = (
    "insufficient_capital",  # required_margin > free_margin
    "sizing_error",           # any other SizingError (degenerate SL distance, below ordermin)
    "margin_floor",           # margin level at/under MARGIN_FLOOR_PCT
    "zero_equity",            # equity <= 0 or unavailable from TradeBalance
    "balance_fetch_failed",   # the TradeBalance API call itself failed
    "rate_limited",           # Kraken rate limit hit while placing the order
    "kraken_api_error",       # any other KrakenAPIError while opening
    "expired_unfilled",       # was pending (skipped, retried each cycle) but its
                               # OB's mitigation window closed before capital freed up
)

_fallback_log = logging.getLogger("ob_bot.signal_log")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _write(record, path=None):
    path = path or config.SIGNAL_LOG_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(record, default=str)
        with open(path, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        # logging must never take down the trading loop -- fall back to
        # the ordinary error logger and move on
        _fallback_log.error(f"signal_log write failed ({e}); record was: {record}")


def log_opened(sig, sizes, equity=None, free_margin=None, margin_level=None, path=None):
    _write({
        "ts": _now_iso(),
        "direction": sig.get("direction"),
        "ob_time": sig.get("ob_time"),
        "bos_time": sig.get("bos_time"),
        "entry_price": sig.get("entry_price"),
        "decision": "opened",
        "reason": None,
        "detail": None,
        "equity": equity,
        "free_margin": free_margin,
        "margin_level": margin_level,
        "required_margin": (sizes or {}).get("required_margin"),
        "size_a": (sizes or {}).get("size_a"),
        "size_b": (sizes or {}).get("size_b"),
    }, path)


def log_skipped(sig, reason, detail="", equity=None, free_margin=None, margin_level=None,
                 required_margin=None, path=None):
    if reason not in REASON_CODES:
        # never let a typo'd/unknown reason code silently vanish into an
        # "unknown" bucket with no trace of what it actually was
        detail = f"[unrecognised reason code {reason!r}] {detail}"
    _write({
        "ts": _now_iso(),
        "direction": sig.get("direction"),
        "ob_time": sig.get("ob_time"),
        "bos_time": sig.get("bos_time"),
        "entry_price": sig.get("entry_price"),
        "decision": "skipped",
        "reason": reason,
        "detail": str(detail) if detail else "",
        "equity": equity,
        "free_margin": free_margin,
        "margin_level": margin_level,
        "required_margin": required_margin,
        "size_a": None,
        "size_b": None,
    }, path)
