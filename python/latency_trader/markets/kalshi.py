from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from .auth import KalshiCredentials
from .base import PredictionMarketVenue
from .book import LocalOrderBook
from .clock import receive_timestamp
from .http import get_json
from .models import (
    ContractID,
    MarketState,
    NormalizedMarket,
    Outcome,
    ReceiveTimestamp,
    Trade,
    Venue,
)
from .normalizer import (
    decimal_value,
    kalshi_book_levels,
    normalize_kalshi_market,
)


class KalshiVenue(PredictionMarketVenue):
    venue = Venue.KALSHI
    REST_BASE = "https://external-api.kalshi.com"
    WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    WS_PATH = "/trade-api/ws/v2"

    def __init__(self, credentials: KalshiCredentials | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.credentials = credentials
        self._ws: Any = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stopping = False
        self._book_subscriptions: set[str] = set()
        self._trade_subscriptions: set[str] = set()
        self._last_sequence_by_sid: dict[int, int] = {}
        self._stale_streams: set[tuple[int, str]] = set()
        self._pending_commands: dict[int, tuple[str, tuple[str, ...]]] = {}
        self._next_command_id = 1

    async def authenticate(self) -> dict[str, str]:
        if self.credentials is None:
            raise PermissionError("Kalshi WebSocket credentials are required")
        return self.credentials.headers("GET", self.WS_PATH)

    async def connect(self) -> None:
        if self._runner and not self._runner.done():
            return
        await self.authenticate()
        self._stopping = False
        self._runner = asyncio.create_task(self._run_forever(), name="kalshi-market-feed")
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
        cursor: str | None = None
        while True:
            payload = await get_json(
                self.REST_BASE,
                "/trade-api/v2/markets",
                query={"status": "open", "limit": 1000, "cursor": cursor},
            )
            markets.extend(normalize_kalshi_market(item) for item in payload.get("markets", []))
            cursor = payload.get("cursor") or None
            if not cursor:
                break
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
        for contract in contracts:
            self._validate_contract(contract)
        tickers = tuple(dict.fromkeys(contract.market_id for contract in contracts))
        for ticker in tickers:
            contract = ContractID(self.venue, ticker, Outcome.YES)
            self._book_subscriptions.add(ticker)
            await self._bootstrap_book(contract)
        if tickers and self._ws is not None:
            await self._subscribe("orderbook_delta", tickers)

    async def subscribe_trades(self, contracts: Iterable[ContractID]) -> None:
        contracts = tuple(contracts)
        tickers = tuple(dict.fromkeys(contract.market_id for contract in contracts))
        for contract in contracts:
            self._validate_contract(contract)
        self._trade_subscriptions.update(tickers)
        if tickers and self._ws is not None:
            await self._subscribe("trade", tickers)

    async def _bootstrap_book(self, contract: ContractID) -> None:
        payload = await get_json(
            self.REST_BASE,
            f"/trade-api/v2/markets/{contract.market_id}/orderbook",
            query={"depth": 0},
        )
        bids, asks = kalshi_book_levels(payload)
        received = receive_timestamp()
        book = self._books.setdefault(contract.market_id, LocalOrderBook(contract))
        book.replace(
            bids=bids,
            asks=asks,
            state=MarketState.OPEN,
            exchange_timestamp_ns=None,
            received=received,
        )
        self._record(book)

    async def _run_forever(self) -> None:
        from websockets.asyncio.client import connect

        attempt = 0
        while not self._stopping:
            try:
                headers = await self.authenticate()  # fresh timestamp/signature on every attempt
                async with connect(
                    self.WS_URL,
                    additional_headers=headers,
                    ping_interval=10,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                ) as websocket:
                    self._ws = websocket
                    self._connected.set()
                    attempt = 0
                    self._last_sequence_by_sid.clear()
                    self._stale_streams.clear()
                    await self._restore_subscriptions()
                    async for raw in websocket:
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

    async def _restore_subscriptions(self) -> None:
        if self._book_subscriptions:
            await self._subscribe("orderbook_delta", tuple(sorted(self._book_subscriptions)))
        if self._trade_subscriptions:
            await self._subscribe("trade", tuple(sorted(self._trade_subscriptions)))

    async def _subscribe(self, channel: str, markets: tuple[str, ...]) -> None:
        command_id = self._next_command_id
        self._next_command_id += 1
        self._pending_commands[command_id] = (channel, markets)
        await self._send(
            {
                "id": command_id,
                "cmd": "subscribe",
                "params": {"channels": [channel], "market_tickers": list(markets)},
            }
        )

    async def _request_snapshot(self, sid: int, market: str) -> None:
        command_id = self._next_command_id
        self._next_command_id += 1
        await self._send(
            {
                "id": command_id,
                "cmd": "update_subscription",
                "params": {"sids": [sid], "market_tickers": [market], "action": "get_snapshot"},
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(message, separators=(",", ":")))

    async def ingest_message(
        self, message: dict[str, Any], received: ReceiveTimestamp | None = None
    ) -> None:
        """Public for deterministic fixture replay; live messages are recorded before this call."""

        received = received or receive_timestamp()
        message_type = message.get("type")
        body = message.get("msg") or {}
        sid = int(message.get("sid", 0))

        if message_type == "subscribed":
            command = self._pending_commands.pop(int(message.get("id", 0)), None)
            if command and command[0] == "orderbook_delta":
                for market in command[1]:
                    self._stale_streams.discard((sid, market))
            return

        if message_type == "orderbook_snapshot":
            ticker = str(body["market_ticker"])
            contract = ContractID(self.venue, ticker, Outcome.YES)
            bids, asks = kalshi_book_levels(body)
            sequence = int(message["seq"])
            self._last_sequence_by_sid[sid] = sequence
            book = self._books.setdefault(ticker, LocalOrderBook(contract))
            book.replace(
                bids=bids,
                asks=asks,
                state=MarketState.OPEN,
                exchange_timestamp_ns=None,
                received=received,
                sequence=sequence,
            )
            self._stale_streams.discard((sid, ticker))
            self._record(book)
            return

        if message_type == "orderbook_delta":
            ticker = str(body["market_ticker"])
            sequence = int(message["seq"])
            previous = self._last_sequence_by_sid.get(sid)
            self._last_sequence_by_sid[sid] = sequence
            book = self._books.setdefault(
                ticker, LocalOrderBook(ContractID(self.venue, ticker, Outcome.YES))
            )
            if previous is None or sequence != previous + 1:
                book.mark_stale()
                stream = (sid, ticker)
                if stream not in self._stale_streams:
                    self._stale_streams.add(stream)
                    await self._request_snapshot(sid, ticker)
                return
            if (sid, ticker) in self._stale_streams:
                return
            raw_price = decimal_value(body.get("price_dollars"))
            raw_side = str(body.get("side", "yes")).lower()
            if raw_side not in {"yes", "no"}:
                raise ValueError(f"invalid Kalshi order-book side: {raw_side}")
            normalized_side = "bid" if raw_side == "yes" else "ask"
            price = raw_price if raw_side == "yes" else Decimal(1) - raw_price
            book.apply_delta(
                side=normalized_side,
                price=price,
                quantity_delta=decimal_value(body.get("delta_fp")),
                sequence=sequence,
                exchange_timestamp_ns=(int(body["ts_ms"]) * 1_000_000 if body.get("ts_ms") else None),
                received=received,
                enforce_sequence=False,
            )
            self._record(book)
            return

        if message_type == "trade":
            ticker = str(body["market_ticker"])
            trade = Trade(
                contract=ContractID(self.venue, ticker, Outcome.YES),
                price=decimal_value(body.get("yes_price_dollars")),
                quantity=decimal_value(body.get("count_fp")),
                exchange_timestamp_ns=(int(body["ts_ms"]) * 1_000_000 if body.get("ts_ms") else None),
                received=received,
                trade_id=body.get("trade_id"),
            )
            if ticker in self._books:
                self._books[ticker].update_trade(trade)
                self._record(self._books[ticker])

    def _record(self, book: LocalOrderBook) -> None:
        if self.recorder:
            self.recorder.record_snapshot(book.snapshot())
