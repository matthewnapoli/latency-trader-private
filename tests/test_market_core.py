from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from latency_trader.analysis import analyze_venue_signal, render_cross_venue_comparison
from latency_trader.markets.auth import KalshiCredentials, PolymarketUSCredentials
from latency_trader.markets.book import LocalOrderBook, SequenceGap
from latency_trader.markets.kalshi import KalshiVenue
from latency_trader.markets.matcher import MarketMatcher
from latency_trader.markets.models import (
    BookLevel,
    ContractID,
    MarketState,
    NormalizedMarket,
    OrderSide,
    PaperOrder,
    PointSignal,
    ReceiveTimestamp,
    Venue,
)
from latency_trader.markets.normalizer import kalshi_book_levels
from latency_trader.markets.paper import simulate_ioc
from latency_trader.markets.polymarket_us import PolymarketUSVenue
from latency_trader.markets.recorder import JSONLMarketRecorder
from latency_trader.replay import replay_raw, summarize_jsonl


class DummyWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        self.sent.append(value)


class AuthenticationTests(unittest.TestCase):
    def test_kalshi_rsa_pss_signature(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        credentials = KalshiCredentials("key", pem)
        headers = credentials.headers(
            "GET", "/trade-api/v2/markets?status=open", timestamp_ms=1_700_000_000_000
        )
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        key.public_key().verify(
            signature,
            b"1700000000000GET/trade-api/v2/markets",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
            hashes.SHA256(),
        )

    def test_polymarket_us_ed25519_signature(self) -> None:
        key = ed25519.Ed25519PrivateKey.generate()
        secret = base64.b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
        credentials = PolymarketUSCredentials("key", secret)
        headers = credentials.headers("GET", "/v1/ws/markets", timestamp_ms=1_700_000_000_000)
        key.public_key().verify(
            base64.b64decode(headers["X-PM-Signature"]),
            b"1700000000000GET/v1/ws/markets",
        )


class BookTests(unittest.TestCase):
    def test_kalshi_no_bids_become_yes_asks(self) -> None:
        bids, asks = kalshi_book_levels(
            {
                "yes_dollars_fp": [["0.40", "10.00"]],
                "no_dollars_fp": [["0.45", "8.00"], ["0.35", "5.00"]],
            }
        )
        self.assertEqual(bids[0], BookLevel(Decimal("0.40"), Decimal("10.00")))
        self.assertEqual(asks[0], BookLevel(Decimal("0.55"), Decimal("8.00")))

    def test_local_book_rejects_sequence_gap(self) -> None:
        contract = ContractID(Venue.KALSHI, "TENNIS")
        book = LocalOrderBook(contract)
        received = ReceiveTimestamp(1, 1)
        book.replace(
            bids=[BookLevel(Decimal("0.4"), Decimal(10))],
            asks=[BookLevel(Decimal("0.6"), Decimal(10))],
            state=MarketState.OPEN,
            exchange_timestamp_ns=None,
            received=received,
            sequence=4,
        )
        with self.assertRaises(SequenceGap):
            book.apply_delta(
                side="bid",
                price=Decimal("0.4"),
                quantity_delta=Decimal(1),
                sequence=6,
                exchange_timestamp_ns=None,
                received=received,
            )
        self.assertFalse(book.snapshot().synchronized)

    def test_paper_ioc_walks_displayed_depth(self) -> None:
        contract = ContractID(Venue.POLYMARKET_US, "match")
        book = LocalOrderBook(contract)
        book.replace(
            bids=[BookLevel(Decimal("0.49"), Decimal(8))],
            asks=[
                BookLevel(Decimal("0.51"), Decimal(3)),
                BookLevel(Decimal("0.52"), Decimal(4)),
            ],
            state=MarketState.OPEN,
            exchange_timestamp_ns=None,
            received=ReceiveTimestamp(1, 1),
        )
        fill = simulate_ioc(
            PaperOrder(contract, OrderSide.BUY, Decimal(5), Decimal("0.52")), book.snapshot()
        )
        self.assertEqual(fill.filled_quantity, Decimal(5))
        self.assertEqual(fill.average_price, Decimal("0.514"))


class FeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_kalshi_gap_marks_stale_until_fresh_snapshot(self) -> None:
        venue = KalshiVenue()
        venue._ws = DummyWebSocket()
        received = ReceiveTimestamp(10, 20)
        await venue.ingest_message(
            {
                "type": "orderbook_snapshot",
                "sid": 2,
                "seq": 10,
                "msg": {
                    "market_ticker": "KXTENNIS",
                    "yes_dollars_fp": [["0.40", "10"]],
                    "no_dollars_fp": [["0.40", "10"]],
                },
            },
            received,
        )
        await venue.ingest_message(
            {
                "type": "orderbook_delta",
                "sid": 2,
                "seq": 12,
                "msg": {
                    "market_ticker": "KXTENNIS",
                    "price_dollars": "0.40",
                    "delta_fp": "2",
                    "side": "yes",
                    "ts_ms": 100,
                },
            },
            received,
        )
        self.assertFalse(venue._books["KXTENNIS"].snapshot().synchronized)
        self.assertIn('"action":"get_snapshot"', venue._ws.sent[-1])

        await venue.ingest_message(
            {
                "type": "orderbook_snapshot",
                "sid": 2,
                "seq": 13,
                "msg": {
                    "market_ticker": "KXTENNIS",
                    "yes_dollars_fp": [["0.41", "7"]],
                    "no_dollars_fp": [["0.40", "10"]],
                },
            },
            received,
        )
        self.assertTrue(venue._books["KXTENNIS"].snapshot().synchronized)

    async def test_polymarket_us_full_snapshot_and_trade(self) -> None:
        venue = PolymarketUSVenue()
        received = ReceiveTimestamp(200, 300)
        await venue.ingest_message(
            {
                "marketData": {
                    "marketSlug": "sinner-alcaraz",
                    "bids": [{"px": {"value": "0.54", "currency": "USD"}, "qty": "12.5"}],
                    "offers": [{"px": {"value": "0.56", "currency": "USD"}, "qty": "9"}],
                    "state": "MARKET_STATE_OPEN",
                    "stats": {"lastTradePx": {"value": "0.55"}, "lastTradeQty": "2"},
                    "transactTime": "2026-08-22T10:00:00Z",
                }
            },
            received,
        )
        snapshot = venue._books["sinner-alcaraz"].snapshot()
        self.assertEqual(snapshot.best_bid.price, Decimal("0.54"))
        self.assertEqual(snapshot.best_ask.quantity, Decimal(9))
        self.assertEqual(
            snapshot.exchange_timestamp_ns,
            int(datetime(2026, 8, 22, 10, tzinfo=timezone.utc).timestamp() * 1_000_000_000),
        )
        self.assertIsNone(snapshot.sequence)

    async def test_raw_jsonl_replays_through_live_normalizers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "markets.jsonl"
            recorder = JSONLMarketRecorder(path)
            received = ReceiveTimestamp(100, 200)
            recorder.record_raw(
                Venue.KALSHI,
                {
                    "type": "orderbook_snapshot",
                    "sid": 1,
                    "seq": 1,
                    "msg": {
                        "market_ticker": "KXTENNIS",
                        "yes_dollars_fp": [["0.40", "10"]],
                        "no_dollars_fp": [["0.40", "10"]],
                    },
                },
                received,
            )
            recorder.record_raw(
                Venue.POLYMARKET_US,
                {
                    "marketData": {
                        "marketSlug": "tennis-match",
                        "bids": [{"px": {"value": "0.44"}, "qty": "5"}],
                        "offers": [{"px": {"value": "0.56"}, "qty": "7"}],
                        "state": "MARKET_STATE_OPEN",
                    }
                },
                received,
            )
            books = await replay_raw(path)
            self.assertTrue(books[Venue.KALSHI]["KXTENNIS"].synchronized)
            self.assertEqual(
                books[Venue.POLYMARKET_US]["tennis-match"].best_ask.price,
                Decimal("0.56"),
            )
            self.assertEqual(summarize_jsonl(path), {"raw_market_message": 2})


class MatcherAndAnalysisTests(unittest.TestCase):
    def market(
        self, venue: Venue, market_id: str, players: tuple[str, ...], wording: str
    ) -> NormalizedMarket:
        start = datetime(2026, 8, 22, 18, tzinfo=timezone.utc)
        return NormalizedMarket(
            contract=ContractID(venue, market_id),
            title=wording,
            contract_wording=wording,
            state=MarketState.OPEN,
            player_names=players,
            tournament="US Open",
            scheduled_start=start,
            event_date=start.date(),
        )

    def test_low_confidence_match_requires_confirmation(self) -> None:
        matcher = MarketMatcher()
        decision = matcher.compare(
            self.market(Venue.KALSHI, "a", ("Jannik Sinner", "Carlos Alcaraz"), "Sinner wins"),
            self.market(Venue.POLYMARKET_US, "b", ("Coco Gauff", "Aryna Sabalenka"), "Gauff wins"),
        )
        self.assertTrue(decision.requires_manual_confirmation)
        self.assertFalse(decision.usable)
        self.assertTrue(decision.confirm().usable)

    def test_analysis_and_cross_venue_table(self) -> None:
        def snapshot(venue: Venue, market: str, bid: str, ask: str, mono: int):
            contract = ContractID(venue, market)
            book = LocalOrderBook(contract)
            book.replace(
                bids=[BookLevel(Decimal(bid), Decimal(10))],
                asks=[BookLevel(Decimal(ask), Decimal(6))],
                state=MarketState.OPEN,
                exchange_timestamp_ns=None,
                received=ReceiveTimestamp(mono, mono),
            )
            return book.snapshot()

        signal = PointSignal("p1", "A", Decimal("0.99"), 1_000_000_000, 1_020_000_000)
        kalshi_book = snapshot(Venue.KALSHI, "k", "0.48", "0.52", 1_020_000_000)
        poly_book = snapshot(Venue.POLYMARKET_US, "p", "0.47", "0.53", 1_020_000_000)
        kalshi_fill = simulate_ioc(
            PaperOrder(kalshi_book.contract, OrderSide.BUY, Decimal(2)), kalshi_book
        )
        poly_fill = simulate_ioc(
            PaperOrder(poly_book.contract, OrderSide.BUY, Decimal(2)), poly_book
        )
        kalshi_result = analyze_venue_signal(
            signal=signal,
            signal_book=kalshi_book,
            fill=kalshi_fill,
            subsequent_books=[snapshot(Venue.KALSHI, "k", "0.58", "0.62", 1_080_000_000)],
            settlement_or_mark=Decimal(1),
        )
        poly_result = analyze_venue_signal(
            signal=signal,
            signal_book=poly_book,
            fill=poly_fill,
            subsequent_books=[snapshot(Venue.POLYMARKET_US, "p", "0.57", "0.63", 1_090_000_000)],
            settlement_or_mark=Decimal(1),
        )
        self.assertEqual(kalshi_result.signal_latency_ms, Decimal(20))
        self.assertEqual(kalshi_result.market_reaction_latency_ms, Decimal(80))
        table = render_cross_venue_comparison(kalshi_result, poly_result)
        self.assertIn("| CV signal latency | 20 ms | 20 ms |", table)
        self.assertIn("| Simulated PnL |", table)


if __name__ == "__main__":
    unittest.main()
