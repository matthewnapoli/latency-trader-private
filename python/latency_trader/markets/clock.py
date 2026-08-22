from __future__ import annotations

import time

from .models import ReceiveTimestamp


def receive_timestamp() -> ReceiveTimestamp:
    # Capture as close together as possible. Latency differences always use monotonic_ns.
    return ReceiveTimestamp(wall_time_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())

