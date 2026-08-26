from __future__ import annotations

from dataclasses import dataclass
from math import floor

from bots.choch_live.models import InstrumentMetadata


@dataclass(frozen=True)
class ShadowPositionSize:
    risk_cash: float
    risk_per_unit: float
    units: float
    signed_units: float
    margin_required: float


def quote_currency(instrument: str) -> str:
    parts = instrument.split("_")
    if len(parts) < 2:
        raise ValueError(f"Cannot infer quote currency from instrument {instrument!r}")
    return parts[-1]


def _floor_to_precision(value: float, precision: int) -> float:
    scale = 10**precision
    return floor((value * scale) + 1e-9) / scale


def size_shadow_position(
    *,
    account_nav: float,
    risk_pct: float,
    entry: float,
    stop: float,
    direction: str,
    metadata: InstrumentMetadata,
    quote_to_account_rate: float,
    max_units: float,
) -> ShadowPositionSize:
    if account_nav <= 0:
        raise ValueError("account_nav must be positive")
    if not (0 < risk_pct < 1):
        raise ValueError("risk_pct must be between 0 and 1")
    if quote_to_account_rate <= 0:
        raise ValueError("quote_to_account_rate must be positive")

    stop_distance_quote = abs(entry - stop)
    if stop_distance_quote <= 0:
        raise ValueError("entry and stop must differ")

    risk_cash = account_nav * risk_pct
    risk_per_unit = stop_distance_quote * quote_to_account_rate
    raw_units = risk_cash / risk_per_unit
    capped_units = min(raw_units, float(max_units))
    units = _floor_to_precision(capped_units, metadata.trade_units_precision)
    if units < metadata.minimum_trade_size:
        units = 0.0

    margin_required = units * entry * metadata.margin_rate * quote_to_account_rate
    signed_units = units if direction == "bullish" else -units
    return ShadowPositionSize(
        risk_cash=float(risk_cash),
        risk_per_unit=float(risk_per_unit),
        units=float(units),
        signed_units=float(signed_units),
        margin_required=float(margin_required),
    )
