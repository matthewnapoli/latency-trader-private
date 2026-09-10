"""Manual FOK execution. No retries, background strategies, or credential persistence."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .markets.auth import KalshiCredentials, PolymarketUSCredentials
from .markets.normalizer import kalshi_book_levels, polymarket_book_levels

KALSHI = "Kalshi"
POLYMARKET = "Polymarket US"
BASES = {KALSHI: "https://external-api.kalshi.com", POLYMARKET: "https://gateway.polymarket.us"}


class UnknownOrderStatus(RuntimeError):
    """A write may have reached the venue. Never automatically retry it."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(base, path, *, query=None, body=None, credentials=None):
    method = "POST" if body is not None else "GET"
    headers = {"Accept": "application/json", "User-Agent": "LatencyTrader/0.2"}
    if credentials:
        headers.update(credentials.headers(method, path))
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, allow_nan=False).encode()
    url = base + path + ("?" + urlencode(query) if query else "")
    try:
        with build_opener(NoRedirect()).open(
            Request(url, data=data, headers=headers, method=method), timeout=15
        ) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        # Do not echo response bodies, request headers, or credentials into the UI/logs.
        if method == "POST" and (exc.code >= 500 or exc.code == 408):
            raise UnknownOrderStatus(
                "Exchange error after submission. Check the exchange before retrying."
            ) from None
        raise RuntimeError(
            f"Exchange returned HTTP {exc.code}. Check credentials, permissions, and market availability."
        ) from None
    except (URLError, TimeoutError, OSError, ValueError):
        if method == "POST":
            raise UnknownOrderStatus(
                "Submission outcome is unknown. Check the exchange before retrying."
            ) from None
        raise RuntimeError(
            "Could not read exchange data. Check your connection and refresh."
        ) from None


def number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a finite numeric value.") from None


def list_events(venue, cursor=None):
    if venue == KALSHI:
        query = {"status": "open", "with_nested_markets": "true", "limit": 100}
        if cursor:
            query["cursor"] = cursor
        data = request_json(BASES[venue], "/trade-api/v2/events", query=query)
        return data.get("events", []), data.get("cursor")
    offset = int(cursor or 0)
    data = request_json(
        BASES[venue],
        "/v1/events",
        query={
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": offset,
        },
    )
    rows = data.get("events", [])
    return rows, str(offset + 100) if len(rows) == 100 else None


def market_id(venue, market):
    return market["ticker" if venue == KALSHI else "slug"]


def open_market(venue, market):
    if venue == KALSHI:
        return market.get("status") in {"open", "active"}
    return (
        bool(market.get("active"))
        and not market.get("closed")
        and str(market.get("ep3Status", "OPEN")) in {"OPEN", "MARKET_STATE_OPEN"}
    )


def get_market(venue, identifier):
    safe = quote(identifier, safe="")
    path = f"/trade-api/v2/markets/{safe}" if venue == KALSHI else f"/v1/market/slug/{safe}"
    return request_json(BASES[venue], path)["market"]


@dataclass(frozen=True)
class Quote:
    venue: str
    identifier: str
    outcome: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    received: float
    market: dict


def fetch_quote(venue, identifier, outcome):
    # Refresh metadata too, so a cached open flag cannot authorize a closed market.
    market = get_market(venue, identifier)
    if not open_market(venue, market):
        raise ValueError("This market is not open for trading.")
    safe = quote(identifier, safe="")
    path = (
        f"/trade-api/v2/markets/{safe}/orderbook" if venue == KALSHI else f"/v1/markets/{safe}/book"
    )
    started = time.monotonic()
    payload = request_json(BASES[venue], path)
    if time.monotonic() - started > 5:
        raise ValueError("Quote request was too slow. Refresh before trading.")
    if venue == POLYMARKET:
        state = payload.get("marketData", {}).get("state")
        if state not in {"OPEN", "MARKET_STATE_OPEN"}:
            raise ValueError("The order book is not open for trading.")
    bids, asks = (kalshi_book_levels if venue == KALSHI else polymarket_book_levels)(payload)
    bids = [level for level in bids if level.quantity > 0]
    asks = [level for level in asks if level.quantity > 0]
    bid, ask = (bids[0] if bids else None), (asks[0] if asks else None)
    bp, ap = (bid.price if bid else None), (ask.price if ask else None)
    bs, az = (bid.quantity if bid else Decimal(0)), (ask.quantity if ask else Decimal(0))
    if bp is not None and ap is not None and bp >= ap:
        raise ValueError("Book is crossed or locked. Refresh before trading.")
    if outcome == "NO":
        bp, ap, bs, az = (
            (1 - ap if ap is not None else None),
            (1 - bp if bp is not None else None),
            az,
            bs,
        )
    elif outcome != "YES":
        raise ValueError("Choose YES or NO.")
    return Quote(venue, identifier, outcome, bp, ap, bs, az, time.monotonic(), market)


def build_order(q, action, quantity, *, now=None):
    if action not in {"BUY", "SELL"}:
        raise ValueError("Choose buy or sell.")
    if (time.monotonic() if now is None else now) - q.received > 5:
        raise ValueError("Quote expired. Refresh and review a new quote.")
    qty = number(quantity)
    if qty <= 0 or qty != qty.to_integral_value() or qty > 1000000:
        raise ValueError("Enter a whole number of contracts from 1 to 1,000,000.")
    price = q.ask if action == "BUY" else q.bid
    if price is None or not 0 < price < 1:
        raise ValueError("No executable price is available on this side.")
    yes_price = price if q.outcome == "YES" else 1 - price
    if q.venue == KALSHI:
        return "/trade-api/v2/portfolio/events/orders", {
            "ticker": q.identifier,
            "client_order_id": str(uuid.uuid4()),
            "side": "bid" if (action == "BUY") == (q.outcome == "YES") else "ask",
            "price": format(yes_price, "f"),
            "count": format(qty, ".2f"),
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": True,
        }
    tick = number(q.market.get("orderPriceMinTickSize", "0.01"))
    minimum = number(q.market.get("minimumTradeQty", "1"))
    if tick <= 0 or minimum <= 0 or yes_price % tick or qty < minimum or qty % minimum:
        raise ValueError("Price or quantity does not match this market's trading increments.")
    direction = "LONG" if q.outcome == "YES" else "SHORT"
    return "/v1/orders", {
        "marketSlug": q.identifier,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": format(yes_price, "f"), "currency": "USD"},
        "quantity": int(qty),
        "tif": "TIME_IN_FORCE_FILL_OR_KILL",
        "intent": f"ORDER_INTENT_{action}_{direction}",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL",
        "synchronousExecution": True,
        "maxBlockTime": "5",
    }


def submit_order(q, action, quantity, credentials):
    expected = KalshiCredentials if q.venue == KALSHI else PolymarketUSCredentials
    if not isinstance(credentials, expected) or not credentials.key_id.strip():
        raise ValueError("Enter credentials for the selected exchange.")
    path, body = build_order(q, action, quantity)
    base = BASES[KALSHI] if q.venue == KALSHI else "https://api.polymarket.us"
    response = request_json(base, path, body=body, credentials=credentials)
    try:
        return summarize_result(q.venue, response, number(quantity))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise UnknownOrderStatus(
            "Unreadable order result. Check the exchange before retrying."
        ) from None


def summarize_result(venue, response, quantity):
    if venue == KALSHI:
        identifier = response.get("order_id", "unknown")
        filled = number(response.get("fill_count", "0"))
        remaining = number(response.get("remaining_count", "0"))
        if filled == quantity:
            status = "FILLED"
        elif (
            filled == 0
            and remaining == 0
            and identifier != "unknown"
            and "fill_count" in response
            and "remaining_count" in response
        ):
            status = "NOT FILLED"
        else:
            status = "CHECK EXCHANGE"
    else:
        identifier = response.get("id", "unknown")
        executions = response.get("executions") or []
        latest = executions[-1].get("order", {}) if executions else {}
        status = latest.get("state", "UNKNOWN").removeprefix("ORDER_STATE_")
        filled = number(latest.get("cumQuantity", "0"))
        if (status == "FILLED" and filled != quantity) or (filled > 0 and filled != quantity):
            status = "CHECK EXCHANGE"
    return f"{status} | filled {filled}/{quantity} | order {identifier}"
