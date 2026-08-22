from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from decimal import Decimal

from .markets.base import PredictionMarketVenue
from .markets.matcher import MarketMatch
from .markets.models import (
    ContractID,
    OrderBookSnapshot,
    OrderSide,
    PaperFill,
    PaperOrder,
    PointSignal,
    Venue,
    VenueLatencyResult,
)
from .markets.paper import mark_to_market_pnl


class CrossVenueSnapshotCoordinator:
    """Freezes each local book in the same event-loop turn when a CV signal arrives."""

    def __init__(self, *, minimum_confidence: Decimal = Decimal("0.95")) -> None:
        self.minimum_confidence = minimum_confidence

    async def snapshot(
        self,
        signal: PointSignal,
        match: MarketMatch,
        venues: Mapping[Venue, PredictionMarketVenue],
    ) -> dict[Venue, OrderBookSnapshot]:
        if signal.confidence < self.minimum_confidence:
            raise PermissionError("CV signal is below the configured confidence threshold")
        if not match.usable:
            raise PermissionError("low-confidence market match requires manual confirmation")
        match_venues = {match.left.contract.venue, match.right.contract.venue}
        if match_venues != {Venue.KALSHI, Venue.POLYMARKET_US}:
            raise ValueError("cross-venue match must contain one contract from each venue")

        async def take(venue: Venue, contract: ContractID):
            await asyncio.sleep(0)
            return venue, venues[venue].record_market_state(contract)

        pairs = await asyncio.gather(
            take(match.left.contract.venue, match.left.contract),
            take(match.right.contract.venue, match.right.contract),
        )
        return dict(pairs)


def analyze_venue_signal(
    *,
    signal: PointSignal,
    signal_book: OrderBookSnapshot,
    fill: PaperFill,
    subsequent_books: Iterable[OrderBookSnapshot],
    pre_event_book: OrderBookSnapshot | None = None,
    settlement_or_mark: Decimal | None = None,
    repricing_threshold: Decimal = Decimal("0.001"),
) -> VenueLatencyResult:
    """Reaction latency is physical point end -> first locally received repricing."""

    baseline = pre_event_book or signal_book
    baseline_mid = baseline.mid
    first_repricing_ns: int | None = None
    if baseline_mid is not None:
        for book in sorted(subsequent_books, key=lambda item: item.received.monotonic_ns):
            if book.received.monotonic_ns < signal.cv_detection_monotonic_ns or book.mid is None:
                continue
            if abs(book.mid - baseline_mid) >= repricing_threshold:
                first_repricing_ns = book.received.monotonic_ns
                break

    if first_repricing_ns is None:
        reaction_ms = None
    else:
        reaction_ms = Decimal(
            first_repricing_ns - signal.observable_point_end_monotonic_ns
        ) / Decimal(1000000)

    if fill.order.side == OrderSide.BUY:
        best = signal_book.best_ask
        executable_side = signal_book.asks
    else:
        best = signal_book.best_bid
        executable_side = signal_book.bids

    return VenueLatencyResult(
        venue=signal_book.contract.venue,
        contract=signal_book.contract,
        observable_point_end_timestamp_ns=signal.observable_point_end_monotonic_ns,
        cv_detection_timestamp_ns=signal.cv_detection_monotonic_ns,
        signal_latency_ms=signal.signal_latency_ms,
        pre_event_mid=baseline_mid,
        simulated_fill_price=fill.average_price,
        first_repricing_timestamp_ns=first_repricing_ns,
        market_reaction_latency_ms=reaction_ms,
        available_size_before_repricing=(best.quantity if best else Decimal(0)),
        simulated_pnl=(
            mark_to_market_pnl(fill, settlement_or_mark)
            if settlement_or_mark is not None
            else None
        ),
        best_available_price=(best.price if best else None),
        available_depth=sum((level.quantity for level in executable_side), Decimal(0)),
    )


def select_pre_event_book(
    history: Iterable[OrderBookSnapshot], signal: PointSignal
) -> OrderBookSnapshot | None:
    eligible = [
        book
        for book in history
        if book.received.monotonic_ns <= signal.observable_point_end_monotonic_ns
    ]
    return max(eligible, key=lambda book: book.received.monotonic_ns, default=None)


def render_cross_venue_comparison(
    kalshi: VenueLatencyResult, polymarket_us: VenueLatencyResult
) -> str:
    if kalshi.venue != Venue.KALSHI or polymarket_us.venue != Venue.POLYMARKET_US:
        raise ValueError("comparison columns must be Kalshi then Polymarket US")

    def show(value: Decimal | None, suffix: str = "") -> str:
        if value is None:
            return "—"
        rendered = format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")
        return f"{rendered}{suffix}"

    rows = [
        ("CV signal latency", show(kalshi.signal_latency_ms, " ms"), show(polymarket_us.signal_latency_ms, " ms")),
        ("Market reaction latency", show(kalshi.market_reaction_latency_ms, " ms"), show(polymarket_us.market_reaction_latency_ms, " ms")),
        ("Best available price", show(kalshi.best_available_price), show(polymarket_us.best_available_price)),
        ("Available depth", show(kalshi.available_depth), show(polymarket_us.available_depth)),
        ("Simulated fill", show(kalshi.simulated_fill_price), show(polymarket_us.simulated_fill_price)),
        ("Simulated PnL", show(kalshi.simulated_pnl), show(polymarket_us.simulated_pnl)),
    ]
    output = [
        "| Metric | Kalshi | Polymarket US |",
        "|---|---:|---:|",
    ]
    output.extend(f"| {metric} | {left} | {right} |" for metric, left, right in rows)
    return "\n".join(output)


def paper_orders_for_match(
    match: MarketMatch, *, side: OrderSide, quantity: Decimal, signal_id: str
) -> tuple[PaperOrder, PaperOrder]:
    if not match.usable:
        raise PermissionError("low-confidence market match requires manual confirmation")
    return (
        PaperOrder(match.left.contract, side, quantity, signal_id=signal_id),
        PaperOrder(match.right.contract, side, quantity, signal_id=signal_id),
    )
