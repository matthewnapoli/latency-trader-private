import base64
import time
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import patch
from urllib.error import URLError

from latency_trader.markets.auth import PolymarketUSCredentials
from latency_trader.trading import (
    KALSHI,
    POLYMARKET,
    Quote,
    UnknownOrderStatus,
    build_order,
    fetch_quote,
    list_events,
    request_json,
    submit_order,
    summarize_result,
)


class TradingTests(unittest.TestCase):
    def quote(self, venue=KALSHI, outcome="YES"):
        return Quote(
            venue,
            "fixture",
            outcome,
            D("0.40"),
            D("0.43"),
            D(100),
            D(100),
            time.monotonic(),
            {
                "minimumTradeQty": 1,
                "orderPriceMinTickSize": "0.01",
            },
        )

    def test_all_eight_order_directions_and_fok(self):
        for venue in (KALSHI, POLYMARKET):
            for outcome in ("YES", "NO"):
                for action in ("BUY", "SELL"):
                    with self.subTest(venue=venue, outcome=outcome, action=action):
                        q = self.quote(venue, outcome)
                        path, order = build_order(q, action, "12")
                        outcome_price = D("0.43") if action == "BUY" else D("0.40")
                        yes_price = outcome_price if outcome == "YES" else 1 - outcome_price
                        if venue == KALSHI:
                            self.assertEqual(path, "/trade-api/v2/portfolio/events/orders")
                            self.assertEqual(D(order["price"]), yes_price)
                            expected_side = (
                                "bid" if (action == "BUY") == (outcome == "YES") else "ask"
                            )
                            self.assertEqual(order["side"], expected_side)
                            self.assertEqual(order["time_in_force"], "fill_or_kill")
                        else:
                            self.assertEqual(path, "/v1/orders")
                            self.assertEqual(D(order["price"]["value"]), yes_price)
                            direction = "LONG" if outcome == "YES" else "SHORT"
                            self.assertEqual(order["intent"], f"ORDER_INTENT_{action}_{direction}")
                            self.assertEqual(order["tif"], "TIME_IN_FORCE_FILL_OR_KILL")

    def test_invalid_quantities_stale_quotes_and_empty_sides(self):
        for qty in ("0", "-1", "nan", "Infinity", "1.1", "1000001", "abc"):
            with self.subTest(qty=qty), self.assertRaises(ValueError):
                build_order(self.quote(), "BUY", qty)
        with self.assertRaises(ValueError):
            build_order(replace(self.quote(), received=time.monotonic() - 6), "BUY", 1)
        with self.assertRaises(ValueError):
            build_order(replace(self.quote(), ask=None), "BUY", 1)

    def test_polymarket_increment_validation(self):
        with self.assertRaises(ValueError):
            build_order(replace(self.quote(POLYMARKET), ask=D("0.431")), "BUY", 1)

    @patch("latency_trader.trading.request_json")
    def test_no_book_inversion_and_zero_size_filter(self, request):
        request.side_effect = [
            {"market": {"status": "active"}},
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "10"], ["0.41", "0"]],
                    "no_dollars": [["0.57", "20"]],
                },
            },
        ]
        q = fetch_quote(KALSHI, "test", "NO")
        self.assertEqual(
            (q.bid, q.ask, q.bid_size, q.ask_size), (D("0.57"), D("0.60"), D(20), D(10))
        )

    @patch("latency_trader.trading.request_json")
    def test_closed_market_and_halted_book_rejected(self, request):
        request.return_value = {"market": {"status": "closed"}}
        with self.assertRaises(ValueError):
            fetch_quote(KALSHI, "test", "YES")
        request.side_effect = [
            {"market": {"active": True, "closed": False}},
            {"marketData": {"state": "MARKET_STATE_HALTED"}},
        ]
        with self.assertRaises(ValueError):
            fetch_quote(POLYMARKET, "test", "YES")

    @patch("latency_trader.trading.build_opener")
    def test_post_timeout_is_unknown_and_never_retried(self, opener):
        opener.return_value.open.side_effect = URLError("timeout")
        with self.assertRaises(UnknownOrderStatus):
            request_json("https://api.polymarket.us", "/v1/orders", body={"test": True})
        self.assertEqual(opener.return_value.open.call_count, 1)

    @patch("latency_trader.trading.request_json")
    def test_submit_uses_authenticated_host_and_preserves_limit(self, request):
        q = self.quote(POLYMARKET, "NO")
        credentials = PolymarketUSCredentials("test", base64.b64encode(bytes(32)).decode())
        request.return_value = {
            "id": "abc",
            "executions": [{"order": {"state": "ORDER_STATE_FILLED", "cumQuantity": 5}}],
        }
        result = submit_order(q, "BUY", 5, credentials)
        self.assertTrue(result.startswith("FILLED"))
        args, kwargs = request.call_args
        self.assertEqual(args, ("https://api.polymarket.us", "/v1/orders"))
        self.assertIs(kwargs["credentials"], credentials)
        self.assertEqual(kwargs["body"]["price"]["value"], "0.57")
        self.assertEqual(request.call_count, 1)

    def test_acknowledgment_is_not_a_fill(self):
        self.assertTrue(summarize_result(POLYMARKET, {"id": "abc"}, D(5)).startswith("UNKNOWN"))
        self.assertTrue(
            summarize_result(
                KALSHI, {"order_id": "abc", "fill_count": "2", "remaining_count": "0"}, D(5)
            ).startswith("CHECK EXCHANGE")
        )

    @patch("latency_trader.trading.request_json")
    def test_event_pagination(self, request):
        request.return_value = {"events": [], "cursor": "next"}
        self.assertEqual(list_events(KALSHI, "previous"), ([], "next"))
        self.assertEqual(request.call_args.kwargs["query"]["cursor"], "previous")
        request.return_value = {"events": [{}] * 100}
        self.assertEqual(list_events(POLYMARKET, "100")[1], "200")


if __name__ == "__main__":
    unittest.main()
