from __future__ import annotations

from dataclasses import dataclass
from math import floor

from bots.choch_live.models import InstrumentMetadata


@dataclass(frozen=True)
class ShadowPositionSize:
    risk_cash: float
    intended_risk_cash: float
    desired_units_before_caps: float
    actual_risk_cash: float
    actual_risk_pct: float
    risk_per_unit: float
    units: float
    signed_units: float
    margin_required: float


@dataclass(frozen=True)
class AggregateRiskAdmission:
    nav: float
    max_account_risk_open: float
    aggregate_cap_cash: float
    existing_risk_cash: float
    candidate_intended_risk_cash: float
    candidate_actual_risk_cash: float
    resulting_aggregate_risk_cash: float
    admitted: bool
    rejection_reason: str | None = None


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

    intended_risk_cash = account_nav * risk_pct
    risk_per_unit = stop_distance_quote * quote_to_account_rate
    raw_units = intended_risk_cash / risk_per_unit
    capped_units = min(raw_units, float(max_units))
    units = _floor_to_precision(capped_units, metadata.trade_units_precision)
    if units < metadata.minimum_trade_size:
        units = 0.0

    actual_risk_cash = units * risk_per_unit
    actual_risk_pct = actual_risk_cash / account_nav
    margin_required = units * entry * metadata.margin_rate * quote_to_account_rate
    signed_units = units if direction == "bullish" else -units
    return ShadowPositionSize(
        risk_cash=float(intended_risk_cash),
        intended_risk_cash=float(intended_risk_cash),
        desired_units_before_caps=float(raw_units),
        actual_risk_cash=float(actual_risk_cash),
        actual_risk_pct=float(actual_risk_pct),
        risk_per_unit=float(risk_per_unit),
        units=float(units),
        signed_units=float(signed_units),
        margin_required=float(margin_required),
    )


@dataclass(frozen=True)
class MarginFeasibility:
    margin_required_pct_of_nav: float
    full_target_risk_feasible: bool
    max_margin_supported_units: float
    max_margin_supported_risk_cash: float
    max_margin_supported_risk_pct: float


@dataclass(frozen=True)
class MarginConstrainedSize:
    intended_risk_cash: float
    desired_units_before_caps: float
    full_size_units: float
    full_size_margin_required: float
    full_target_risk_feasible: bool
    margin_fallback_used: bool
    margin_supported_units: float
    final_units: float
    signed_units: float
    actual_risk_cash: float
    actual_risk_pct: float
    risk_per_unit: float
    margin_required: float
    margin_required_pct_of_nav: float
    max_margin_supported_units: float
    max_margin_supported_risk_cash: float
    max_margin_supported_risk_pct: float
    margin_floor_ok: bool


def assess_margin_feasibility(
    *,
    account_nav: float,
    margin_available: float,
    entry: float,
    metadata: InstrumentMetadata,
    quote_to_account_rate: float,
    risk_per_unit: float,
    margin_required: float,
) -> MarginFeasibility:
    if account_nav <= 0:
        raise ValueError("account_nav must be positive")
    if margin_available < 0:
        raise ValueError("margin_available cannot be negative")
    if entry <= 0:
        raise ValueError("entry must be positive")
    if quote_to_account_rate <= 0:
        raise ValueError("quote_to_account_rate must be positive")
    if risk_per_unit < 0:
        raise ValueError("risk_per_unit cannot be negative")

    margin_per_unit = entry * metadata.margin_rate * quote_to_account_rate
    if margin_per_unit <= 0:
        raise ValueError("margin_per_unit must be positive")

    max_units = _floor_to_precision(margin_available / margin_per_unit, metadata.trade_units_precision)
    if max_units < metadata.minimum_trade_size:
        max_units = 0.0
    supported_risk_cash = max_units * risk_per_unit
    return MarginFeasibility(
        margin_required_pct_of_nav=float(margin_required / account_nav),
        full_target_risk_feasible=bool(margin_required <= margin_available + 1e-9),
        max_margin_supported_units=float(max_units),
        max_margin_supported_risk_cash=float(supported_risk_cash),
        max_margin_supported_risk_pct=float(supported_risk_cash / account_nav),
    )


def constrain_shadow_position_to_margin(
    *,
    account_nav: float,
    margin_available: float,
    entry: float,
    direction: str,
    metadata: InstrumentMetadata,
    quote_to_account_rate: float,
    max_units: float,
    min_margin_supported_risk_pct: float,
    full_size: ShadowPositionSize,
) -> MarginConstrainedSize:
    if not (0 < min_margin_supported_risk_pct <= 1):
        raise ValueError("min_margin_supported_risk_pct must be between 0 and 1")
    feasibility = assess_margin_feasibility(
        account_nav=account_nav,
        margin_available=margin_available,
        entry=entry,
        metadata=metadata,
        quote_to_account_rate=quote_to_account_rate,
        risk_per_unit=full_size.risk_per_unit,
        margin_required=full_size.margin_required,
    )
    margin_supported_units = min(feasibility.max_margin_supported_units, float(max_units), full_size.units)
    margin_supported_units = _floor_to_precision(margin_supported_units, metadata.trade_units_precision)
    if margin_supported_units < metadata.minimum_trade_size:
        margin_supported_units = 0.0

    fallback_used = not feasibility.full_target_risk_feasible
    final_units = margin_supported_units if fallback_used else full_size.units
    actual_risk_cash = final_units * full_size.risk_per_unit
    actual_risk_pct = actual_risk_cash / account_nav
    margin_required = final_units * entry * metadata.margin_rate * quote_to_account_rate
    margin_floor_ok = (not fallback_used) or actual_risk_pct >= min_margin_supported_risk_pct - 1e-12
    signed_units = final_units if direction == "bullish" else -final_units

    return MarginConstrainedSize(
        intended_risk_cash=full_size.intended_risk_cash,
        desired_units_before_caps=full_size.desired_units_before_caps,
        full_size_units=full_size.units,
        full_size_margin_required=full_size.margin_required,
        full_target_risk_feasible=feasibility.full_target_risk_feasible,
        margin_fallback_used=fallback_used,
        margin_supported_units=float(margin_supported_units),
        final_units=float(final_units),
        signed_units=float(signed_units),
        actual_risk_cash=float(actual_risk_cash),
        actual_risk_pct=float(actual_risk_pct),
        risk_per_unit=full_size.risk_per_unit,
        margin_required=float(margin_required),
        margin_required_pct_of_nav=float(margin_required / account_nav),
        max_margin_supported_units=float(margin_supported_units),
        max_margin_supported_risk_cash=float(margin_supported_units * full_size.risk_per_unit),
        max_margin_supported_risk_pct=float((margin_supported_units * full_size.risk_per_unit) / account_nav),
        margin_floor_ok=bool(margin_floor_ok),
    )


def assess_aggregate_risk_admission(
    *,
    nav: float,
    max_account_risk_open: float,
    existing_risk_cash: float,
    candidate_intended_risk_cash: float,
    candidate_actual_risk_cash: float,
) -> AggregateRiskAdmission:
    if nav <= 0:
        raise ValueError("nav must be positive")
    if not (0 < max_account_risk_open < 1):
        raise ValueError("max_account_risk_open must be between 0 and 1")
    if existing_risk_cash < 0:
        raise ValueError("existing_risk_cash cannot be negative")
    if candidate_actual_risk_cash < 0:
        raise ValueError("candidate_actual_risk_cash cannot be negative")

    aggregate_cap_cash = nav * max_account_risk_open
    resulting = existing_risk_cash + candidate_actual_risk_cash
    admitted = resulting <= aggregate_cap_cash + 1e-9
    return AggregateRiskAdmission(
        nav=float(nav),
        max_account_risk_open=float(max_account_risk_open),
        aggregate_cap_cash=float(aggregate_cap_cash),
        existing_risk_cash=float(existing_risk_cash),
        candidate_intended_risk_cash=float(candidate_intended_risk_cash),
        candidate_actual_risk_cash=float(candidate_actual_risk_cash),
        resulting_aggregate_risk_cash=float(resulting),
        admitted=admitted,
        rejection_reason=None if admitted else "aggregate_risk_cap_exceeded",
    )


def current_stop_risk_cash(
    *,
    remaining_units: float,
    current_price: float,
    stop_price: float,
    direction: str,
    quote_to_account_rate: float,
) -> float:
    if remaining_units < 0:
        raise ValueError("remaining_units cannot be negative")
    if quote_to_account_rate <= 0:
        raise ValueError("quote_to_account_rate must be positive")
    if remaining_units == 0:
        return 0.0

    if direction == "bullish":
        distance = current_price - stop_price
    elif direction == "bearish":
        distance = stop_price - current_price
    else:
        raise ValueError("direction must be bullish or bearish")

    if distance < 0:
        raise ValueError("current price is beyond protective stop")
    return float(remaining_units * distance * quote_to_account_rate)
