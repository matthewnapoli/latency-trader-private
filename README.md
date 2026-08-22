# Latency Trader: prediction-market venue layer

This repository contains the exchange-agnostic market-data, paper-execution, and latency-analysis vertical slice for the tennis CV application. It supports Kalshi and the retail **Polymarket US** API without using or assuming compatibility with Polymarket's international CLOB.

The live surface is read-only. `submitPaperOrder` / `submit_paper_order` only walks the displayed local L2 book; neither adapter contains a live order-submission call.

## Implemented

- Common `PredictionMarketVenue` contract in Swift and Python.
- Public REST market discovery and book bootstrapping.
- Authenticated read-only WebSocket subscriptions for books and trades.
- Kalshi snapshot + incremental-delta maintenance with per-subscription sequence-gap recovery.
- Polymarket US full-snapshot replacement, JSON heartbeat monitoring, REST rebootstrap, and reconnect. The retail US documentation does not expose a market-feed sequence field, so none is fabricated.
- Separate exchange and local receive timestamps. Local latency arithmetic uses monotonic nanoseconds.
- Raw-first JSONL capture plus normalized state records.
- IOC-style paper fills from displayed depth only.
- Fuzzy tennis market matching across player names, tournament, start time, event date, and wording. Low-confidence matches cannot reach the cross-venue coordinator without manual confirmation.
- Simultaneous local-book snapshots on a high-confidence CV signal.
- Per-venue reaction metrics and an iOS/Markdown cross-venue comparison.
- Offline raw-message replay through the same normalizers and gap logic used live.

## Layout

```text
ios/
  Markets/
    PredictionMarketVenue.swift
    KalshiVenue.swift
    PolymarketUSVenue.swift
    MarketNormalizer.swift
    MarketMatcher.swift
    CrossVenueLatencyAnalyzer.swift
    VenueTransport.swift
  UI/
    CrossVenueComparisonView.swift
python/latency_trader/
  markets/
  analysis.py
  replay.py
tests/
```

The verified wire contracts and source links are in [docs/MARKET_API_NOTES.md](docs/MARKET_API_NOTES.md).

## Python setup and checks

Python 3.10+ is required.

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest
```

The tests are also standard-library `unittest` compatible:

```bash
PYTHONPATH=python python -m unittest discover -s tests -v
```

Replay a raw/normalized JSONL capture:

```bash
latency-trader summarize data/markets.jsonl
latency-trader replay data/markets.jsonl
```

## Credentials

Keep keys outside the repository.

- Kalshi: API key ID plus downloaded RSA private-key PEM. WebSocket signing is RSA-PSS/SHA-256 over `timestamp_ms + METHOD + path_without_query`.
- Polymarket US: developer key ID plus base64 Ed25519 secret. WebSocket signing is Ed25519 over `timestamp_ms + METHOD + /v1/ws/markets`.

Public market discovery and REST order books do not require credentials. Both venues currently require credentials during the WebSocket handshake.

## Timestamp and recovery rules

Every normalized state carries:

- `exchange_timestamp`: venue time when the venue publishes one;
- local wall time: for correlating venue and device timelines;
- local monotonic time: for durations and latency calculations.

Missing venue timestamps remain `nil`/`None`; local receipt time is never relabeled as exchange time.

Kalshi books are marked unsynchronized on any subscription sequence gap. Deltas for that market are ignored until a new WebSocket snapshot arrives via `get_snapshot`. Paper fills fail while a book is stale.

Polymarket US market-data messages are treated as complete snapshots and replace the local book. Reconnect uses a fresh Ed25519 timestamp/signature, REST rebootstrap, then subscription restoration. Server JSON heartbeats and client ping/pong provide liveness detection. Because the official retail US feed does not document a sequence number, gap recovery is snapshot/staleness based.

## Metric definitions

- `signal_latency_ms = cv_detection_timestamp - observable_point_end_timestamp`
- `pre_event_mid` is the last supplied book at or before the observable point end. If an explicit pre-event book is unavailable, the signal-time snapshot is used and should be labeled as that fallback in the experiment record.
- `first_repricing_timestamp` is the first local monotonic receive time after CV detection whose mid moves by the configured threshold.
- `market_reaction_latency_ms = first_repricing_timestamp - observable_point_end_timestamp`
- `available_size_before_repricing` is displayed best-level size in the paper-order direction.
- `available_depth` is total displayed depth on the executable side of the normalized YES book.
- `simulated_pnl` is mark/settlement PnL on filled quantity, before venue fees.

Paper fills do not claim queue priority, hidden liquidity, fee accuracy, or real executability.

## Updated development sequence

1. Dataset/model research and licensing.
2. Legally usable data normalization and TrackNet baseline.
3. Ball + court tracking on prerecorded video.
4. Bounce/hit/point-end detection.
5. Winner classification and confidence calibration.
6. Core ML export and device optimization.
7. Native iPhone real-time inference.
8. **Kalshi read-only market data and recorder.**
9. **Polymarket US read-only market data and recorder.**
10. **Unified paper execution, matching, and cross-venue latency comparison.**
11. End-to-end device benchmark and optimization.

Each venue must pass auth-vector, normalization, replay, stale-book, and reconnect tests before being enabled in a live capture session.
