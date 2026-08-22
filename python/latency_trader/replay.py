from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .markets.kalshi import KalshiVenue
from .markets.models import ReceiveTimestamp, Venue
from .markets.polymarket_us import PolymarketUSVenue


async def replay_raw(path: str | Path) -> dict[Venue, dict[str, Any]]:
    """Replay raw JSONL through the same normalizers/gap checks used by live adapters."""

    venues = {
        Venue.KALSHI: KalshiVenue(),
        Venue.POLYMARKET_US: PolymarketUSVenue(),
    }
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("record_type") != "raw_market_message":
            continue
        venue = Venue(record["venue"])
        received_data = record["received"]
        received = ReceiveTimestamp(
            wall_time_ns=int(received_data["wall_time_ns"]),
            monotonic_ns=int(received_data["monotonic_ns"]),
        )
        adapter = venues[venue]
        messages = record.get("message")
        if messages is None:
            messages = json.loads(record["raw_payload"])
        for message in messages if isinstance(messages, list) else [messages]:
            await adapter.ingest_message(message, received)  # type: ignore[attr-defined]

    return {
        venue: {market_id: book.snapshot() for market_id, book in adapter._books.items()}
        for venue, adapter in venues.items()
    }


def summarize_jsonl(path: str | Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        kind = json.loads(line).get("record_type", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts
