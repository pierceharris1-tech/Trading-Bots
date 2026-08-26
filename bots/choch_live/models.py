from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StrategyLeg(str, Enum):
    S1 = "S1"
    S2 = "S2"


class SetupState(str, Enum):
    IDLE = "idle"
    SETUP_DETECTED = "setup_detected"
    PENDING_RETRACEMENT = "pending_retracement"
    FILLED = "filled"
    POSITION_OPEN = "position_open"
    TP = "tp"
    SL = "sl"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"
    ARMED = "armed"
    OBSERVING = "observing"
    DECISION_REACHED = "decision_reached"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    ENTERED = "entered"
    OPEN = "open"
    TIMEOUT = "timeout"
    COMPLETE = "complete"


class OrderState(str, Enum):
    LOCAL_PENDING = "local_pending"
    BROKER_PENDING = "broker_pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    MISSING = "missing"
    UNKNOWN = "unknown"


class TradeState(str, Enum):
    LOCAL_OPEN = "local_open"
    BROKER_OPEN = "broker_open"
    CLOSED = "closed"
    MISSING = "missing"
    UNKNOWN = "unknown"


class EventType(str, Enum):
    STARTUP_VALIDATION_PASSED = "STARTUP_VALIDATION_PASSED"
    STARTUP_VALIDATION_FAILED = "STARTUP_VALIDATION_FAILED"
    RECONCILIATION_COMPLETE = "RECONCILIATION_COMPLETE"
    LOCAL_PENDING_BROKER_MISSING = "LOCAL_PENDING_BROKER_MISSING"
    BROKER_PENDING_LOCAL_MISSING = "BROKER_PENDING_LOCAL_MISSING"
    LOCAL_OPEN_BROKER_MISSING = "LOCAL_OPEN_BROKER_MISSING"
    BROKER_OPEN_LOCAL_MISSING = "BROKER_OPEN_LOCAL_MISSING"
    CANDLE_PROCESSED = "CANDLE_PROCESSED"
    ENGINE_HEARTBEAT = "ENGINE_HEARTBEAT"


@dataclass(frozen=True)
class AccountMetadata:
    account_id: str
    currency: str
    last_transaction_id: str | None


@dataclass(frozen=True)
class InstrumentConfig:
    key: str
    name: str


@dataclass(frozen=True)
class InstrumentMetadata:
    key: str
    name: str
    margin_rate: float
    minimum_trade_size: float
    trade_units_precision: int
    pip_location: int
    display_precision: int
    instrument_type: str

    @property
    def effective_leverage(self) -> float:
        return 1.0 / self.margin_rate
