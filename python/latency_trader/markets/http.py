from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class HTTPStatusError(RuntimeError):
    pass


async def get_json(
    base_url: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    def perform() -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}{path}"
        if query:
            values: list[tuple[str, str]] = []
            for key, value in query.items():
                if value is None:
                    continue
                if isinstance(value, (list, tuple)):
                    values.extend((key, str(item)) for item in value)
                else:
                    values.append((key, str(value).lower() if isinstance(value, bool) else str(value)))
            if values:
                url += "?" + urlencode(values)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "LatencyTrader/0.1 (+read-only market data)",
            **(headers or {}),
        }
        request = Request(url, headers=request_headers, method="GET")
        with urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise HTTPStatusError(f"GET {url} returned {response.status}")
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(perform)
