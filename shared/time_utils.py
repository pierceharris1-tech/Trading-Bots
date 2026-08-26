from __future__ import annotations

from datetime import datetime, timezone, timedelta


_GRANULARITY_SECONDS = {
    "M1": 1 * 60,
    "M5": 5 * 60,
    "M10": 10 * 60,
    "M15": 15 * 60,
    "M30": 30 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D": 24 * 60 * 60,
    "1D": 24 * 60 * 60,
}


def granularity_to_seconds(granularity: str) -> int:
    g = granularity.upper()
    if g not in _GRANULARITY_SECONDS:
        raise ValueError(f"Unsupported granularity: {granularity}")
    return _GRANULARITY_SECONDS[g]


def seconds_until_next_close(granularity: str, now: datetime | None = None) -> int:
    """
    Returns seconds until the next candle close boundary in UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    sec = granularity_to_seconds(granularity)

    # Find next boundary: round down to period then add one period
    epoch = int(now.timestamp())
    next_boundary = ((epoch // sec) + 1) * sec
    return max(1, next_boundary - epoch)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
