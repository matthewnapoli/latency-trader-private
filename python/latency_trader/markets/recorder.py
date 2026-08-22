from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from .models import OrderBookSnapshot, ReceiveTimestamp, Venue, to_jsonable


class JSONLMarketRecorder:
    """Append-only recorder. Raw messages are persisted before parsing."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def record_raw(self, venue: Venue, message: Any, received: ReceiveTimestamp) -> None:
        raw_payload = message if isinstance(message, str) else json.dumps(message, separators=(",", ":"))
        self._append(
            {
                "record_type": "raw_market_message",
                "venue": venue.value,
                "received": to_jsonable(received),
                "raw_payload": raw_payload,
            }
        )

    def record_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        self._append({"record_type": "normalized_market_state", "snapshot": to_jsonable(snapshot)})

    def _append(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
            handle.flush()
