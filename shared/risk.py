def fixed_units_for_practice() -> int:
    """
    Ultra-safe default for first practice bot.
    +units = long, -units = short.
    """
    return 1000  # tiny size; adjust later

def units_from_risk(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    leverage: float,
    min_units: int = 1,
    max_units: int = 1_000_000,
) -> tuple[int, float, float]:
    """
    Returns (units, risk_cash, margin_required).

    For XAU_USD on OANDA, units are ounces (CFD units).
    Approx loss at SL = abs(entry-stop) * abs(units)
    so units = risk_cash / stop_distance.
    """
    if equity <= 0:
        return 0, 0.0, 0.0

    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return 0, 0.0, 0.0

    risk_cash = equity * float(risk_pct)
    raw_units = risk_cash / stop_dist

    # Convert to int units, clamp
    units = int(raw_units)
    if units < min_units:
        units = 0
    units = max(-max_units, min(max_units, units))

    # Margin required (approx): notional/leverage
    notional = abs(units) * entry
    margin_required = notional / float(leverage) if leverage > 0 else notional

    return units, float(risk_cash), float(margin_required)


def clamp_units(units: int, max_abs: int = 50_000) -> int:
    if units == 0:
        return 0
    if units > 0:
        return min(units, max_abs)
    return max(units, -max_abs)
