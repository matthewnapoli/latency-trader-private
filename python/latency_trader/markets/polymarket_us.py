from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable
from typing import Any

from .auth import PolymarketUSCredentials
from .base import PredictionMarketVenue
from .book import LocalOrderBook
from .clock import receive_timestamp
from .http import get_json
from .models import (
    ContractID,
    NormalizedMarket,
    Outcome,
    ReceiveTimestamp,
    Trade,
    Venue,
)
from .normalizer import (
    decimal_value,
    normalize_polymarket_market,
    normalize_polymarket_state,
    polymarket_book_levels,
    timestamp_ns,
)


class PolymarketUSVenue(PredictionMarketVenue):
    """Retail US API adapter. This intentionally contains no international CLOB code."""

    venue = Venue.POLYMARKET_US
    GATEWAY_BASE = "https://gateway.polymarket.us"
    WS_URL = "wss://api.polymarket.us/v1/ws/markets"
    WS_PATH = "/v1/ws/markets"

    def __init__(self, credentials: PolymarketUSCredentials | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.credentials = credentials
        self._ws: Any = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stopping = False
        self._book_subscriptions: set[str] = set()
        self._trade_subscriptions: set[str] = set()

    async def authenticate(self) -> dict[str, str]:
        if self.credentials is None:
            raise PermissionError("Polymarket US API credentials are required for WebSockets")
        return self.credentials.headers("GET", self.WS_PATH)

    async def connect(self) -> None:
        if self._runner and not self._runner.done():
            return
        await self.authenticate()
        self._stopping = False
        self._runner = asyncio.create_task(self._run_forever(), name="polymarket-us-market-feed")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=15)
        except TimeoutError:
            await self.close()
            raise

    async def close(self) -> None:
        self._stopping = True
        self._connected.clear()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        self._runner = None

    async def discover_markets(self, query: str | None = None) -> list[NormalizedMarket]:
        markets: list[NormalizedMarket] = []
        offset = 0
        limit = 100
        while True:
            payload = await get_json(
                self.GATEWAY_BASE,
                "/v1/markets",
                query={"active": True, "closed": False, "limit": limit, "offset": offset},
            )
            page = payload.get("markets", [])
            markets.extend(normalize_polymarket_market(item) for item in page)
            if len(page) < limit:
                break
            offset += limit
        if query:
            needle = query.casefold()
            markets = [
                market
                for market in markets
                if needle in f"{market.title} {market.contract_wording} {market.tournament or ''}".casefold()
            ]
        return markets

    async def subscribe_order_book(self, contracts: Iterable[ContractID]) -> None:
        contracts = tuple(contracts)
        slugs = tuple(dict.fromkeys(contract.market_id for contract in contracts))
        for contract in contracts:
            self._validate_contract(contract)
        self._book_subscriptions.update(slugs)
        await asyncio.gather(
            *(self._bootstrap_book(ContractID(self.venue, slug, Outcome.YES)) for slug in slugs)
        )
        if slugs and self._ws is not None:
            await self._send_subscription("book", "SUBSCRIPTION_TYPE_MARKET_DATA", slugs)

    async def subscribe_trades(self, contracts: Iterable[ContractID]) -> None:
        contracts = tuple(contracts)
        slugs = tuple(dict.fromkeys(contract.market_id for contract in contracts))
        for contract in contracts:
            self._validate_contract(contract)
        self._trade_subscriptions.update(slugs)
        if slugs and self._ws is not None:
            await self._send_subscription("trades", "SUBSCRIPTION_TYPE_TRADE", slugs)

    async def _bootstrap_book(self, contract: ContractID) -> None:
        payload = await get_json(
            self.GATEWAY_BASE, f"/v1/markets/{contract.market_id}/book"
        )
        await self.ingest_message(payload, receive_timestamp())

    async def _run_forever(self) -> None:
        from websockets.asyncio.client import connect

        attempt = 0
        while not self._stopping:
            try:
                headers = await self.authenticate()
                async with connect(
                    self.WS_URL,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=30,
                    max_size=16 * 1024 * 1024,
                ) as websocket:
                    self._ws = websocket
                    self._connected.set()
                    attempt = 0
                    await self._bootstrap_all()
                    await self._restore_subscriptions()
                    while not self._stopping:
                        # The US API documents server heartbeats but no sequence field. A silent
                        # stream is recovered by reconnect + REST snapshots, never by guessed deltas.
                        raw = await asyncio.wait_for(websocket.recv(), timeout=90)
                        received = receive_timestamp()
                        if self.recorder:
                            self.recorder.record_raw(self.venue, raw, received)
                        message = json.loads(raw)
                        for item in message if isinstance(message, list) else [message]:
                            await self.ingest_message(item, received)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - every feed/transport failure enters reconnect
                self._connected.clear()
                self._ws = None
                if self._stopping:
                    break
                await asyncio.sleep(min(30.0, 2.0**attempt))
                attempt = min(attempt + 1, 5)
        self._connected.clear()

    async def _bootstrap_all(self) -> None:
        await asyncio.gather(
            *(
                self._bootstrap_book(ContractID(self.venue, slug, Outcome.YES))
                for slug in self._book_subscriptions
            )
        )

    async def _restore_subscriptions(self) -> None:
        if self._book_subscriptions:
            await self._send_subscription(
                "book", "SUBSCRIPTION_TYPE_MARKET_DATA", tuple(sorted(self._book_subscriptions))
            )
        if self._trade_subscriptions:
            await self._send_subscription(
                "trades", "SUBSCRIPTION_TYPE_TRADE", tuple(sorted(self._trade_subscriptions))
            )

    async def _send_subscription(
        self, prefix: str, subscription_type: str, slugs: tuple[str, ...]
    ) -> None:
        if len(slugs) > 100:
            for index in range(0, len(slugs), 100):
                await self._send_subscription(prefix, subscription_type, slugs[index : index + 100])
            return
        request_id = f"{prefix}-{abs(hash(slugs)) & 0xFFFFFFFF:x}"
        await self._send(
            {
                "subscribe": {
                    "requestId": request_id,
                    "subscriptionType": subscription_type,
                    "marketSlugs": list(slugs),
                    "responsesDebounced": False,
                }
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(message, separators=(",", ":")))

    async def ingest_message(
        self, message: dict[str, Any], received: ReceiveTimestamp | None = None
    ) -> None:
        received = received or receive_timestamp()
        if "heartbeat" in message:
            if self._ws is not None:
                await self._ws.ping()
            return
        market_data = message.get("marketData")
        if market_data:
            slug = str(market_data["marketSlug"])
            contract = ContractID(self.venue, slug, Outcome.YES)
            bids, asks = polymarket_book_levels(market_data)
            book = self._books.setdefault(slug, LocalOrderBook(contract))
            book.replace(
                bids=bids,
                asks=asks,
                state=normalize_polymarket_state(market_data.get("state")),
                exchange_timestamp_ns=timestamp_ns(market_data.get("transactTime")),
                received=received,
                sequence=None,  # no sequence field is documented by the retail US WebSocket
            )
            stats = market_data.get("stats") or {}
            if stats.get("lastTradePx") is not None:
                book.update_trade(
                    Trade(
                        contract=contract,
                        price=decimal_value(stats.get("lastTradePx")),
                        quantity=decimal_value(stats.get("lastTradeQty")),
                        exchange_timestamp_ns=timestamp_ns(stats.get("lastTradeSetTime")),
                        received=received,
                    )
                )
            self._record(book)
            return
        trade_data = message.get("trade")
        if trade_data:
            slug = str(trade_data["marketSlug"])
            trade = Trade(
                contract=ContractID(self.venue, slug, Outcome.YES),
                price=decimal_value(trade_data.get("price")),
                quantity=decimal_value(trade_data.get("quantity")),
                exchange_timestamp_ns=timestamp_ns(trade_data.get("tradeTime")),
                received=received,
                trade_id=trade_data.get("id"),
            )
            if slug in self._books:
                self._books[slug].update_trade(trade)
                self._record(self._books[slug])

    def _record(self, book: LocalOrderBook) -> None:
        if self.recorder:
            self.recorder.record_snapshot(book.snapshot())
