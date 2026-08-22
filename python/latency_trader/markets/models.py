from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET_US = "polymarket_us"


class Outcome(str, Enum):
    YES = "yes"
    NO = "no"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class MarketState(str, Enum):
    PREOPEN = "preopen"
    OPEN = "open"
    SUSPENDED = "suspended"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReceiveTimestamp:
    """Wall time is comparable to exchange time; monotonic time is used for latency."""

    wall_time_ns: int
    monotonic_ns: int


@dataclass(frozen=True, slots=True)
class ContractID:
    venue: Venue
    market_id: str
    outcome: Outcome = Outcome.YES


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.price <= Decimal(1):
            raise ValueError(f"normalized price must be in [0, 1], got {self.price}")
        if self.quantity < 0:
            raise ValueError("quantity cannot be negative")


@dataclass(frozen=True, slots=True)
class Trade:
    contract: ContractID
    price: Decimal
    quantity: Decimal
    exchange_timestamp_ns: int | None
    received: ReceiveTimestamp
    trade_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedMarket:
    contract: ContractID
    title: str
    contract_wording: str
    state: MarketState
    player_names: tuple[str, ...] = ()
    tournament: str | None = None
    scheduled_start: datetime | None = None
    event_date: date | None = None
    source_payload: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    contract: ContractID
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    state: MarketState
    exchange_timestamp_ns: int | None
    received: ReceiveTimestamp
    last_trade: Trade | None = None
    sequence: int | None = None
    synchronized: bool = True

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / Decimal(2)

    @property
    def total_depth(self) -> Decimal:
        return sum((level.quantity for level in self.bids + self.asks), Decimal(0))


@dataclass(frozen=True, slots=True)
class PaperOrder:
    contract: ContractID
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal | None = None
    signal_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("paper-order quantity must be positive")
        if self.limit_price is not None and not Decimal(0) <= self.limit_price <= Decimal(1):
            raise ValueError("limit price must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class PaperFill:
    order: PaperOrder
    submitted: ReceiveTimestamp
    filled_quantity: Decimal
    average_price: Decimal | None
    notional: Decimal
    unfilled_quantity: Decimal
    levels_consumed: tuple[BookLevel, ...]


@dataclass(frozen=True, slots=True)
class PointSignal:
    signal_id: str
    winner: str
    confidence: Decimal
    observable_point_end_monotonic_ns: int
    cv_detection_monotonic_ns: int

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError("signal confidence must be in [0, 1]")
        if self.cv_detection_monotonic_ns < self.observable_point_end_monotonic_ns:
            raise ValueError("CV detection cannot precede the observable point end")

    @property
    def signal_latency_ms(self) -> Decimal:
        delta = self.cv_detection_monotonic_ns - self.observable_point_end_monotonic_ns
        return Decimal(delta) / Decimal(1000000)


@dataclass(frozen=True, slots=True)
class VenueLatencyResult:
    venue: Venue
    contract: ContractID
    observable_point_end_timestamp_ns: int
    cv_detection_timestamp_ns: int
    signal_latency_ms: Decimal
    pre_event_mid: Decimal | None
    simulated_fill_price: Decimal | None
    first_repricing_timestamp_ns: int | None
    market_reaction_latency_ms: Decimal | None
    available_size_before_repricing: Decimal
    simulated_pnl: Decimal | None
    best_available_price: Decimal | None
    available_depth: Decimal


def utc_now_ns() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1_000_000_000)


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value
