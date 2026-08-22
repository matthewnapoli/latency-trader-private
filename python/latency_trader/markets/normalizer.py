from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import (
    BookLevel,
    ContractID,
    MarketState,
    NormalizedMarket,
    Outcome,
    Venue,
)


def decimal_value(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def timestamp_ns(value: Any) -> int | None:
    parsed = parse_datetime(value)
    return int(parsed.timestamp() * 1_000_000_000) if parsed else None


def extract_player_names(text: str) -> tuple[str, ...]:
    # Discovery metadata is not uniform across venues. This deliberately extracts only
    # explicit match separators; uncertain results stay empty and therefore match poorly.
    cleaned = re.sub(r"\s+", " ", text).strip()
    for separator in (" vs. ", " vs ", " v. ", " v ", " - "):
        if separator in cleaned.lower():
            lower = cleaned.lower()
            index = lower.index(separator)
            left = cleaned[:index].strip(" ?:-")
            right = cleaned[index + len(separator) :].strip(" ?:-")
            right = re.split(r"\?| to win| winner", right, flags=re.IGNORECASE)[0].strip()
            if left and right:
                return (left, right)
    return ()


def normalize_kalshi_state(value: Any) -> MarketState:
    return {
        "initialized": MarketState.PREOPEN,
        "unopened": MarketState.PREOPEN,
        "open": MarketState.OPEN,
        "paused": MarketState.SUSPENDED,
        "closed": MarketState.CLOSED,
        "settled": MarketState.SETTLED,
        "finalized": MarketState.SETTLED,
    }.get(str(value).lower(), MarketState.UNKNOWN)


def normalize_polymarket_state(value: Any) -> MarketState:
    raw = str(value).upper()
    if raw in {"MARKET_STATE_OPEN", "OPEN"}:
        return MarketState.OPEN
    if raw in {"MARKET_STATE_PREOPEN", "PREOPEN"}:
        return MarketState.PREOPEN
    if raw in {"MARKET_STATE_SUSPENDED", "MARKET_STATE_HALTED", "SUSPENDED", "HALTED"}:
        return MarketState.SUSPENDED
    if raw in {"MARKET_STATE_EXPIRED", "MARKET_STATE_TERMINATED", "CLOSED"}:
        return MarketState.CLOSED
    if raw in {"SETTLED", "RESOLVED"}:
        return MarketState.SETTLED
    return MarketState.UNKNOWN


def kalshi_book_levels(payload: dict[str, Any]) -> tuple[list[BookLevel], list[BookLevel]]:
    book = payload.get("orderbook_fp", payload)
    yes = book.get("yes_dollars", book.get("yes_dollars_fp", [])) or []
    no = book.get("no_dollars", book.get("no_dollars_fp", [])) or []
    bids = [BookLevel(decimal_value(level[0]), decimal_value(level[1])) for level in yes]
    # Kalshi returns NO bids, not YES asks. A NO bid at p is a YES ask at 1-p.
    asks = [
        BookLevel(Decimal(1) - decimal_value(level[0]), decimal_value(level[1]))
        for level in no
    ]
    return sorted(bids, key=lambda x: x.price, reverse=True), sorted(asks, key=lambda x: x.price)


def polymarket_book_levels(payload: dict[str, Any]) -> tuple[list[BookLevel], list[BookLevel]]:
    market_data = payload.get("marketData", payload)
    bids = [
        BookLevel(decimal_value(item.get("px")), decimal_value(item.get("qty")))
        for item in market_data.get("bids", [])
    ]
    asks = [
        BookLevel(decimal_value(item.get("px")), decimal_value(item.get("qty")))
        for item in market_data.get("offers", [])
    ]
    return sorted(bids, key=lambda x: x.price, reverse=True), sorted(asks, key=lambda x: x.price)


def normalize_kalshi_market(payload: dict[str, Any]) -> NormalizedMarket:
    title = str(payload.get("title") or payload.get("subtitle") or payload.get("ticker") or "")
    start = parse_datetime(payload.get("occurrence_datetime") or payload.get("open_time"))
    return NormalizedMarket(
        contract=ContractID(Venue.KALSHI, str(payload["ticker"]), Outcome.YES),
        title=title,
        contract_wording=str(payload.get("rules_primary") or title),
        state=normalize_kalshi_state(payload.get("status")),
        player_names=extract_player_names(title),
        tournament=str(payload.get("series_ticker") or payload.get("event_ticker") or "") or None,
        scheduled_start=start,
        event_date=start.date() if start else None,
        source_payload=payload,
    )


def normalize_polymarket_market(payload: dict[str, Any]) -> NormalizedMarket:
    title = str(payload.get("question") or payload.get("title") or payload.get("slug") or "")
    start = parse_datetime(payload.get("gameStartTime") or payload.get("startDate"))
    if payload.get("closed"):
        state = MarketState.CLOSED
    elif payload.get("active"):
        state = MarketState.OPEN
    else:
        state = normalize_polymarket_state(payload.get("ep3Status"))
    tags = payload.get("tags") or []
    tournament = next((str(tag.get("label")) for tag in tags if tag.get("label")), None)
    return NormalizedMarket(
        contract=ContractID(Venue.POLYMARKET_US, str(payload["slug"]), Outcome.YES),
        title=title,
        contract_wording=str(payload.get("description") or title),
        state=state,
        player_names=extract_player_names(title),
        tournament=tournament,
        scheduled_start=start,
        event_date=start.date() if start else None,
        source_payload=payload,
    )


def date_or_none(value: datetime | None) -> date | None:
    return value.date() if value else None

