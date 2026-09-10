"""Local desktop order ticket. All Tk access stays on the UI thread."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from cryptography.exceptions import UnsupportedAlgorithm

from .markets.auth import KalshiCredentials, PolymarketUSCredentials
from .trading import (
    KALSHI,
    POLYMARKET,
    UnknownOrderStatus,
    build_order,
    fetch_quote,
    list_events,
    market_id,
    number,
    open_market,
    submit_order,
)


class TradingApp:
    def __init__(self, root):
        self.root = root
        root.title("Latency Trader | Manual AON Orders")
        root.geometry("1050x850")
        root.minsize(850, 650)
        self.busy = False
        self.locked = False
        self.jobs = queue.Queue()
        self.events = []
        self.filtered = []
        self.markets = []
        self.cursor = None
        self.preview = None
        self.pem = b""
        self.venue = tk.StringVar(value=KALSHI)
        self.key_id = tk.StringVar()
        self.secret = tk.StringVar()
        self.key_file = tk.StringVar(value="No RSA key selected")
        self.search = tk.StringVar()
        self.identifier = tk.StringVar()
        self.outcome = tk.StringVar(value="YES")
        self.quantity = tk.StringVar(value="1")
        self.status = tk.StringVar(
            value="Choose an exchange and load markets. Credentials are only needed to trade."
        )
        self.quote_text = tk.StringVar(value="Select a submarket to view its order ticket.")
        self.review_text = tk.StringVar(
            value="Buy uses the best ask. Sell uses the best bid. No partial fills requested."
        )
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa", foreground="#192b43", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("TButton", padding=8, font=("Segoe UI", 10))
        style.configure("TLabelframe", background="#f4f6fa")
        style.configure("TLabelframe.Label", background="#f4f6fa", font=("Segoe UI", 11, "bold"))
        canvas = tk.Canvas(root, highlightthickness=0, background="#f4f6fa")
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        page = ttk.Frame(canvas, padding=22)
        window = canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        ttk.Label(page, text="Latency Trader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Manual orders. Current quotes. All or none.").pack(
            anchor="w", pady=(0, 14)
        )

        connection = ttk.LabelFrame(page, text="1  Exchange & credentials", padding=12)
        connection.pack(fill="x")
        self.venue_box = ttk.Combobox(
            connection,
            textvariable=self.venue,
            values=[KALSHI, POLYMARKET],
            state="readonly",
            width=18,
        )
        self.venue_box.grid(row=0, column=0, padx=(0, 12))
        self.venue_box.bind("<<ComboboxSelected>>", self.change_venue)
        ttk.Label(connection, text="API key ID").grid(row=0, column=1)
        ttk.Entry(connection, textvariable=self.key_id, width=42).grid(
            row=0, column=2, padx=8, sticky="ew"
        )
        ttk.Button(connection, text="Clear credentials", command=self.clear_credentials).grid(
            row=0, column=3
        )
        connection.columnconfigure(2, weight=1)
        self.pem_button = ttk.Button(
            connection, text="Choose RSA key (.pem)", command=self.choose_key
        )
        self.pem_button.grid(row=1, column=0, pady=(8, 0))
        self.pem_label = ttk.Label(connection, textvariable=self.key_file)
        self.pem_label.grid(row=1, column=1, columnspan=3, sticky="w", padx=8)
        self.secret_label = ttk.Label(connection, text="API secret (base64)")
        self.secret_entry = ttk.Entry(connection, textvariable=self.secret, show="*", width=55)

        discovery = ttk.LabelFrame(page, text="2  Select a market & submarket", padding=12)
        discovery.pack(fill="both", expand=True, pady=12)
        toolbar = ttk.Frame(discovery)
        toolbar.pack(fill="x")
        self.load_button = ttk.Button(
            toolbar, text="Load markets", command=lambda: self.load_events(False)
        )
        self.load_button.pack(side="left")
        self.more_button = ttk.Button(
            toolbar, text="Load more", command=lambda: self.load_events(True), state="disabled"
        )
        self.more_button.pack(side="left", padx=8)
        ttk.Label(toolbar, text="Filter loaded markets").pack(side="left", padx=8)
        ttk.Entry(toolbar, textvariable=self.search).pack(side="left", fill="x", expand=True)
        self.search.trace_add("write", lambda *_: self.filter_events())
        lists = ttk.Frame(discovery)
        lists.pack(fill="both", expand=True, pady=8)
        self.event_list = tk.Listbox(lists, exportselection=False, height=7, font=("Segoe UI", 10))
        self.market_list = tk.Listbox(lists, exportselection=False, height=7, font=("Segoe UI", 10))
        for box in (self.event_list, self.market_list):
            box.pack(side="left", fill="both", expand=True, padx=3)
        self.event_list.bind("<<ListboxSelect>>", self.select_event)
        self.market_list.bind("<<ListboxSelect>>", self.select_market)
        ttk.Label(discovery, text="Selected submarket ticker / slug (or paste one directly)").pack(
            anchor="w"
        )
        ttk.Entry(discovery, textvariable=self.identifier).pack(fill="x", pady=4)
        self.identifier.trace_add("write", lambda *_: self.invalidate())

        ticket = ttk.LabelFrame(page, text="3  Order ticket", padding=12)
        ticket.pack(fill="x")
        row = ttk.Frame(ticket)
        row.pack(fill="x")
        ttk.Label(row, text="Outcome").pack(side="left")
        ttk.Combobox(
            row, textvariable=self.outcome, values=["YES", "NO"], state="readonly", width=7
        ).pack(side="left", padx=8)
        ttk.Label(row, text="Contracts").pack(side="left", padx=(12, 0))
        ttk.Entry(row, textvariable=self.quantity, width=10).pack(side="left", padx=8)
        self.refresh_button = ttk.Button(row, text="Refresh quote", command=self.refresh_quote)
        self.refresh_button.pack(side="right")
        for value in (self.outcome, self.quantity, self.key_id, self.secret):
            value.trace_add("write", lambda *_: self.invalidate())
        ttk.Label(
            ticket, textvariable=self.quote_text, wraplength=940, font=("Segoe UI", 12, "bold")
        ).pack(anchor="w", pady=10)
        actions = ttk.Frame(ticket)
        actions.pack(fill="x")
        self.buy_button = ttk.Button(
            actions, text="Review BUY AON at best ask", command=lambda: self.prepare("BUY")
        )
        self.buy_button.pack(side="left", padx=(0, 8))
        self.sell_button = ttk.Button(
            actions, text="Review SELL AON at best bid", command=lambda: self.prepare("SELL")
        )
        self.sell_button.pack(side="left")
        ttk.Label(ticket, textvariable=self.review_text, wraplength=940).pack(anchor="w", pady=8)
        self.submit_button = ttk.Button(
            ticket, text="Submit live FOK order", state="disabled", command=self.submit
        )
        self.submit_button.pack(anchor="w")
        ttk.Label(
            ticket,
            text="FOK fills the entire quantity immediately or cancels. Prices can move before the order arrives.",
            wraplength=940,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(page, textvariable=self.status, wraplength=940).pack(anchor="w", pady=10)
        self.history = tk.Text(page, height=4, state="disabled", font=("Consolas", 10), wrap="word")
        self.history.pack(fill="x")
        self.root.after(100, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def invalidate(self):
        self.preview = None
        if hasattr(self, "submit_button"):
            self.submit_button.configure(state="disabled")
            self.review_text.set("Review a new order after changing the ticket.")

    def clear_credentials(self):
        self.pem = b""
        self.key_id.set("")
        self.secret.set("")
        self.key_file.set("No RSA key selected")
        self.invalidate()

    def choose_key(self):
        path = filedialog.askopenfilename(
            filetypes=[("RSA key", "*.pem *.key"), ("All files", "*.*")]
        )
        if path:
            try:
                self.pem = Path(path).read_bytes()
                KalshiCredentials("validation", self.pem).headers("GET", "/")
                self.key_file.set(Path(path).name)
                self.invalidate()
            except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
                self.pem = b""
                self.key_file.set("Invalid RSA key")
                self.status.set("Could not load an unencrypted RSA PEM key.")

    def change_venue(self, _=None):
        self.clear_credentials()
        self.events = []
        self.cursor = None
        self.filter_events()
        self.identifier.set("")
        self.more_button.configure(state="disabled")
        self.quote_text.set("Select a submarket to view its order ticket.")
        if self.venue.get() == KALSHI:
            self.secret_label.grid_remove()
            self.secret_entry.grid_remove()
            self.pem_button.grid()
            self.pem_label.grid()
        else:
            self.pem_button.grid_remove()
            self.pem_label.grid_remove()
            self.secret_label.grid(row=1, column=0, pady=8)
            self.secret_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=8)

    def run(self, work, done):
        if self.busy:
            return
        self.busy = True
        self.venue_box.configure(state="disabled")
        for button in (
            self.load_button,
            self.more_button,
            self.refresh_button,
            self.buy_button,
            self.sell_button,
            self.submit_button,
        ):
            button.configure(state="disabled")

        def worker():
            try:
                self.jobs.put((done, work(), None))
            except Exception as error:  # noqa: BLE001 - deliver worker failures to the UI thread
                self.jobs.put((done, None, error))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            done, value, error = self.jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.venue_box.configure(state="readonly")
            for button in (
                self.load_button,
                self.refresh_button,
                self.buy_button,
                self.sell_button,
            ):
                button.configure(state="normal")
            if error:
                self.invalidate()
                if isinstance(error, UnknownOrderStatus):
                    self.locked = True
                    self.log(str(error))
                self.status.set(
                    str(error)
                    if isinstance(error, (ValueError, RuntimeError))
                    else "Request failed. Check the connection and credentials."
                )
            else:
                try:
                    done(value)
                except (ValueError, RuntimeError) as error:
                    self.invalidate()
                    self.status.set(str(error))
            self.more_button.configure(state="normal" if self.cursor else "disabled")
        if self.preview and time.monotonic() - self.preview[0].received > 5:
            self.invalidate()
            self.review_text.set("Quote expired. Review again for the latest price.")
        self.root.after(100, self.poll)

    def load_events(self, more):
        venue = self.venue.get()
        cursor = self.cursor if more else None
        self.status.set("Loading markets...")

        def done(result):
            rows, self.cursor = result
            self.events = self.events + rows if more else rows
            self.filter_events()
            self.status.set(f"Loaded {len(self.events)} markets. Filter the list or load more.")

        self.run(lambda: list_events(venue, cursor), done)

    def filter_events(self):
        self.invalidate()
        needle = self.search.get().casefold()
        self.filtered = [event for event in self.events if needle in str(event).casefold()]
        self.event_list.delete(0, "end")
        self.market_list.delete(0, "end")
        self.markets = []
        for event in self.filtered:
            self.event_list.insert("end", event.get("title", "Untitled market"))

    def select_event(self, _=None):
        if not self.event_list.curselection():
            return
        self.invalidate()
        self.identifier.set("")
        event = self.filtered[self.event_list.curselection()[0]]
        self.markets = [m for m in event.get("markets", []) if open_market(self.venue.get(), m)]
        self.market_list.delete(0, "end")
        for market in self.markets:
            label = market.get("yes_sub_title") or market.get("title") or market.get("question")
            self.market_list.insert("end", label or market_id(self.venue.get(), market))
        self.status.set(f"{len(self.markets)} open submarkets. Select one on the right.")

    def select_market(self, _=None):
        if self.market_list.curselection():
            market = self.markets[self.market_list.curselection()[0]]
            self.identifier.set(market_id(self.venue.get(), market))
            self.quote_text.set(
                market.get("description") or market.get("rules_primary") or market.get("title", "")
            )

    def ticket(self):
        identifier = self.identifier.get().strip()
        if not identifier:
            raise ValueError("Select a submarket or paste its ticker / slug.")
        return self.venue.get(), identifier, self.outcome.get()

    def show_quote(self, q):
        self.quote_text.set(
            f"{q.outcome} | Best bid: ${q.bid} ({q.bid_size} contracts) | Best ask: ${q.ask} ({q.ask_size} contracts)"
        )

    def refresh_quote(self):
        self.invalidate()
        try:
            ticket = self.ticket()
        except ValueError as error:
            self.status.set(str(error))
            return

        def done(q):
            if ticket == self.ticket():
                self.show_quote(q)
                self.status.set("Quote refreshed. Review a buy or sell order to submit.")

        self.run(lambda: fetch_quote(*ticket), done)

    def credentials(self):
        key_id = self.key_id.get().strip()
        if not key_id:
            raise ValueError("Enter your API key ID.")
        credentials = (
            KalshiCredentials(key_id, self.pem)
            if self.venue.get() == KALSHI
            else PolymarketUSCredentials(key_id, self.secret.get().strip())
        )
        try:
            credentials.headers("GET", "/")
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError("Enter a valid signing key for this exchange.") from None
        return credentials

    def prepare(self, action):
        self.invalidate()
        if self.locked:
            self.status.set(
                "An earlier submission has unknown status. Check the exchange, then restart the app."
            )
            return
        try:
            ticket = self.ticket()
            quantity = self.quantity.get()
            credentials = self.credentials()
        except ValueError as error:
            self.status.set(str(error))
            return
        self.status.set("Fetching the latest executable quote...")

        def done(q):
            if (
                ticket != self.ticket()
                or quantity != self.quantity.get()
                or credentials != self.credentials()
            ):
                return
            try:
                build_order(q, action, quantity)
            except ValueError as error:
                self.status.set(str(error))
                return
            self.preview = (q, action, quantity, credentials)
            self.show_quote(q)
            price = q.ask if action == "BUY" else q.bid
            self.review_text.set(
                f"LIVE: {action} {quantity} {q.outcome} | {q.identifier} | limit ${price} | notional ${number(quantity) * price} before fees. Valid for 5 seconds."
            )
            self.submit_button.configure(state="normal")
            self.status.set("Review the ticket, then submit. The limit price will not be widened.")

        self.run(lambda: fetch_quote(*ticket), done)

    def submit(self):
        if not self.preview or self.busy or self.locked:
            return
        order = self.preview
        self.invalidate()
        self.status.set("Submitting one live order...")

        def done(result):
            self.log(result)
            self.status.set(result)
            if result.split(" |", 1)[0] not in {
                "FILLED",
                "NOT FILLED",
                "CANCELED",
                "REJECTED",
                "EXPIRED",
            }:
                self.locked = True

        self.run(lambda: submit_order(*order), done)

    def log(self, value):
        self.history.configure(state="normal")
        self.history.insert("end", time.strftime("%H:%M:%S ") + value + "\n")
        self.history.see("end")
        self.history.configure(state="disabled")

    def close(self):
        if self.busy and not messagebox.askyesno(
            "Request in progress",
            "A request is still in progress. If you submitted an order, check its status on the exchange. Close anyway?",
        ):
            return
        self.clear_credentials()
        self.root.destroy()


def main():
    root = tk.Tk()
    TradingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
