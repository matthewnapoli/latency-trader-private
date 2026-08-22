from __future__ import annotations

from decimal import Decimal

from .clock import receive_timestamp
from .models import BookLevel, OrderBookSnapshot, OrderSide, PaperFill, PaperOrder


def simulate_ioc(order: PaperOrder, book: OrderBookSnapshot) -> PaperFill:
    """Walks displayed L2 only. It models no queue priority, hidden liquidity, fees, or slippage."""

    if order.contract != book.contract:
        raise ValueError("order and book contracts differ")
    if not book.synchronized:
        raise ValueError("paper execution is forbidden on a stale book")

    source = book.asks if order.side == OrderSide.BUY else book.bids
    remaining = order.quantity
    consumed: list[BookLevel] = []
    notional = Decimal(0)

    for level in source:
        if order.limit_price is not None:
            outside_limit = (
                order.side == OrderSide.BUY and level.price > order.limit_price
            ) or (order.side == OrderSide.SELL and level.price < order.limit_price)
            if outside_limit:
                break
        quantity = min(remaining, level.quantity)
        if quantity <= 0:
            continue
        consumed.append(BookLevel(level.price, quantity))
        notional += level.price * quantity
        remaining -= quantity
        if remaining == 0:
            break

    filled = order.quantity - remaining
    return PaperFill(
        order=order,
        submitted=receive_timestamp(),
        filled_quantity=filled,
        average_price=(notional / filled if filled else None),
        notional=notional,
        unfilled_quantity=remaining,
        levels_consumed=tuple(consumed),
    )


def mark_to_market_pnl(fill: PaperFill, mark: Decimal) -> Decimal | None:
    if fill.average_price is None:
        return None
    direction = Decimal(1) if fill.order.side == OrderSide.BUY else Decimal(-1)
    return direction * fill.filled_quantity * (mark - fill.average_price)

