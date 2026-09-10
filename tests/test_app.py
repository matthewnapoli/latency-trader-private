import time
import tkinter as tk
import unittest
from unittest.mock import patch

from latency_trader.app import TradingApp


class AppTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk display unavailable")
        self.root.withdraw()
        self.app = TradingApp(self.root)

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()

    def test_selection_and_venue_change_clear_credentials_and_order(self):
        app = self.app
        app.events = [
            {
                "title": "Fixture",
                "markets": [{"ticker": "TEST", "title": "Test submarket", "status": "active"}],
            }
        ]
        app.filter_events()
        app.event_list.selection_set(0)
        app.select_event()
        app.market_list.selection_set(0)
        app.select_market()
        self.assertEqual(app.identifier.get(), "TEST")
        app.key_id.set("test-key")
        app.pem = b"fixture"
        app.venue.set("Polymarket US")
        app.change_venue()
        self.assertEqual(app.key_id.get(), "")
        self.assertEqual(app.pem, b"")
        self.assertEqual(app.identifier.get(), "")
        self.assertEqual(str(app.submit_button.cget("state")), "disabled")

    def test_ticket_edit_invalidates_review(self):
        self.app.preview = ("fixture",)
        self.app.quantity.set("2")
        self.assertIsNone(self.app.preview)

    @patch("latency_trader.app.submit_order")
    def test_double_click_submits_only_once(self, submit):
        submit.return_value = "FILLED | filled 1/1 | order fixture"
        self.app.preview = ("q", "BUY", "1", "credentials")
        self.app.submit()
        self.app.submit()
        deadline = time.monotonic() + 2
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(submit.call_count, 1)
        self.assertFalse(self.app.busy)


if __name__ == "__main__":
    unittest.main()
