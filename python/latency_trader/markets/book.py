from __future__ import annotations

from decimal import Decimal

from .models import (
    BookLevel,
    ContractID,
    MarketState,
    OrderBookSnapshot,
    ReceiveTimestamp,
    Trade,
)


class SequenceGap(RuntimeError):
    def __init__(self, expected: int, received: int) -> None:
        super().__init__(f"order-book sequence gap: expected {expected}, received {received}")
        self.expected = expected
        self.received = received


class LocalOrderBook:
    """Mutable L2 book. Public snapshots are immutable copies."""

    def __init__(self, contract: ContractID) -> None:
        self.contract = contract
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.state = MarketState.UNKNOWN
        self.exchange_timestamp_ns: int | None = None
        self.received = ReceiveTimestamp(0, 0)
        self.last_trade: Trade | None = None
        self.sequence: int | None = None
        self.synchronized = False

    def replace(
        self,
        *,
        bids: list[BookLevel] | tuple[BookLevel, ...],
        asks: list[BookLevel] | tuple[BookLevel, ...],
        state: MarketState,
        exchange_timestamp_ns: int | None,
        received: ReceiveTimestamp,
        sequence: int | None = None,
    ) -> None:
        self._bids = {level.price: level.quantity for level in bids if level.quantity > 0}
        self._asks = {level.price: level.quantity for level in asks if level.quantity > 0}
        self.state = state
        self.exchange_timestamp_ns = exchange_timestamp_ns
        self.received = received
        self.sequence = sequence
        self.synchronized = True
        self._validate_uncrossed()

    def apply_delta(
        self,
        *,
        side: str,
        price: Decimal,
        quantity_delta: Decimal,
        sequence: int,
        exchange_timestamp_ns: int | None,
        received: ReceiveTimestamp,
        enforce_sequence: bool = True,
    ) -> None:
        if not self.synchronized or (enforce_sequence and self.sequence is None):
            raise SequenceGap(sequence, sequence)
        if enforce_sequence:
            assert self.sequence is not None
            expected = self.sequence + 1
            if sequence != expected:
                self.synchronized = False
                raise SequenceGap(expected, sequence)
        levels = self._bids if side == "bid" else self._asks
        new_quantity = levels.get(price, Decimal(0)) + quantity_delta
        if new_quantity < 0:
            self.synchronized = False
            raise ValueError(f"delta makes {side} level negative at {price}")
        if new_quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = new_quantity
        self.sequence = sequence
        self.exchange_timestamp_ns = exchange_timestamp_ns
        self.received = received
        self._validate_uncrossed()

    def update_trade(self, trade: Trade) -> None:
        self.last_trade = trade

    def mark_stale(self) -> None:
        self.synchronized = False

    def snapshot(self) -> OrderBookSnapshot:
        bids = tuple(BookLevel(price, qty) for price, qty in sorted(self._bids.items(), reverse=True))
        asks = tuple(BookLevel(price, qty) for price, qty in sorted(self._asks.items()))
        return OrderBookSnapshot(
            contract=self.contract,
            bids=bids,
            asks=asks,
            state=self.state,
            exchange_timestamp_ns=self.exchange_timestamp_ns,
            received=self.received,
            last_trade=self.last_trade,
            sequence=self.sequence,
            synchronized=self.synchronized,
        )

    def _validate_uncrossed(self) -> None:
        if self._bids and self._asks and max(self._bids) > min(self._asks):
            self.synchronized = False
            raise ValueError("normalized order book is crossed")
