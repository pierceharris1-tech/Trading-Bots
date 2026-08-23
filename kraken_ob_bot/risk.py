"""
Position sizing — 2% risk per trade, fixed 5x leverage, 90/10 split
between the base position (A) and the runner (B).

Leverage changes capital efficiency (how much margin a trade ties up),
never the dollar amount risked -- that's fixed by risk_pct regardless
of leverage.
"""

RISK_PCT = 0.02
LEVERAGE = 5
POS_A_PCT = 0.90
POS_B_PCT = 0.10
KRAKEN_MIN_BTC_ORDER = 0.00005  # check against AssetPairs "ordermin" at startup; this is a fallback
MARGIN_FLOOR_PCT = 200  # won't open a new trade if margin level would drop below this


class SizingError(Exception):
    """reason is a stable code for signal_log.py to record -- keep it in
    sync with signal_log.REASON_CODES if a new one is added here."""

    def __init__(self, message, reason="sizing_error"):
        super().__init__(message)
        self.reason = reason


def size_trade(balance, entry_price, sl_price, free_margin, ordermin=KRAKEN_MIN_BTC_ORDER,
               risk_pct=RISK_PCT, leverage=LEVERAGE):
    """
    Returns a dict with the full position plan, split into legs A (90%) and
    B (10%), or raises SizingError if the trade can't be taken as sized.
    """
    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0:
        raise SizingError("SL distance is zero or negative -- degenerate setup")

    risk_amount = balance * risk_pct
    total_size = risk_amount / sl_distance  # in BTC

    required_margin = (total_size * entry_price) / leverage
    if required_margin > free_margin:
        raise SizingError(
            f"required_margin {required_margin:.2f} exceeds free_margin {free_margin:.2f}",
            reason="insufficient_capital",
        )

    size_a = total_size * POS_A_PCT
    size_b = total_size * POS_B_PCT

    if size_b < ordermin:
        # runner leg too small to place on its own -- fold it into A rather
        # than silently dropping the runner or rejecting the whole trade
        size_a = total_size
        size_b = 0.0

    if size_a < ordermin:
        raise SizingError(
            f"position size {size_a:.8f} BTC below Kraken minimum {ordermin}"
        )

    return {
        "total_size": total_size,
        "size_a": size_a,
        "size_b": size_b,
        "risk_amount": risk_amount,
        "sl_distance": sl_distance,
        "required_margin": required_margin,
        "leverage": leverage,
    }


def check_margin_floor(current_margin_level_pct, floor_pct=MARGIN_FLOOR_PCT):
    """Call before opening any new trade -- refuse if margin level would
    already be at/under the floor (this is a pre-check on CURRENT margin
    level; position_manager re-checks post-trade margin level too)."""
    return current_margin_level_pct > floor_pct
