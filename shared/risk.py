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
    quote_to_account_rate: float = 1.0,
) -> tuple[int, float, float]:
    """
    Returns (units, risk_cash, margin_required).

    quote_to_account_rate: conversion rate from quote currency to account
    currency (GBP). For USD_JPY, quote is JPY so pass GBP_JPY rate.
    For EUR_USD, quote is USD so pass GBP_USD rate.
    For GBP_USD, quote is USD so pass GBP_USD rate.
    For XAU_USD (CFD), quote is USD so pass GBP_USD rate.
    Default 1.0 = no conversion (e.g. EUR_GBP where quote is GBP).
    """
    if equity <= 0:
        return 0, 0.0, 0.0

    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return 0, 0.0, 0.0

    risk_cash = equity * float(risk_pct)

    # Convert risk amount to quote currency
    risk_in_quote = risk_cash * quote_to_account_rate

    raw_units = risk_in_quote / stop_dist

    units = int(raw_units)
    if units < min_units:
        units = 0
    units = max(-max_units, min(max_units, units))

    notional = abs(units) * entry
    margin_required = notional / float(leverage) if leverage > 0 else notional

    return units, float(risk_cash), float(margin_required)

def clamp_units(units: int, max_abs: int = 50_000) -> int:
    if units == 0:
        return 0
    if units > 0:
        return min(units, max_abs)
    return max(units, -max_abs)
