from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from .book import LocalOrderBook
from .models import (
    ContractID,
    NormalizedMarket,
    OrderBookSnapshot,
    PaperFill,
    PaperOrder,
    Venue,
)
from .paper import simulate_ioc
from .recorder import JSONLMarketRecorder


class PredictionMarketVenue(ABC):
    """Common read-only venue surface. submit_paper_order never calls a live order endpoint."""

    venue: Venue

    def __init__(self, *, recorder: JSONLMarketRecorder | None = None) -> None:
        self.recorder = recorder
        self._books: dict[str, LocalOrderBook] = {}

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def authenticate(self) -> dict[str, str]: ...

    @abstractmethod
    async def discover_markets(self, query: str | None = None) -> list[NormalizedMarket]: ...

    @abstractmethod
    async def subscribe_order_book(self, contracts: Iterable[ContractID]) -> None: ...

    @abstractmethod
    async def subscribe_trades(self, contracts: Iterable[ContractID]) -> None: ...

    def get_best_bid_ask(self, contract: ContractID):
        book = self.get_order_book(contract)
        return book.best_bid, book.best_ask

    def get_order_book(self, contract: ContractID) -> OrderBookSnapshot:
        self._validate_contract(contract)
        try:
            return self._books[contract.market_id].snapshot()
        except KeyError as error:
            raise KeyError(f"no local book for {contract.market_id}; subscribe or bootstrap first") from error

    def submit_paper_order(self, order: PaperOrder) -> PaperFill:
        self._validate_contract(order.contract)
        return simulate_ioc(order, self.get_order_book(order.contract))

    def record_market_state(self, contract: ContractID) -> OrderBookSnapshot:
        snapshot = self.get_order_book(contract)
        if self.recorder:
            self.recorder.record_snapshot(snapshot)
        return snapshot

    @abstractmethod
    async def close(self) -> None: ...

    def _validate_contract(self, contract: ContractID) -> None:
        if contract.venue != self.venue:
            raise ValueError(f"{self.venue.value} adapter cannot handle {contract.venue.value} contract")

