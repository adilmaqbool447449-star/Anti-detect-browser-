"""Paper execution: simulated taker fills against the live order book.

A fill re-walks the *current* book at execution time (not the book the
signal saw), charges Polymarket's taker fee per level, and only consumes
levels that still clear the minimum edge. No orders are ever sent.
"""
from __future__ import annotations

import logging
import time

from ..models import Book, Market, Position, Signal
from ..portfolio import Portfolio
from ..pricing import simulate_taker_buy, taker_fee_per_share

log = logging.getLogger(__name__)


class PaperExecutor:
    mode = "paper"

    def __init__(self, portfolio: Portfolio, min_edge: float, extra_slippage: float = 0.0):
        self.portfolio = portfolio
        self.min_edge = min_edge
        self.extra_slippage = extra_slippage

    async def buy(self, sig: Signal, market: Market, book: Book, max_usd: float) -> Position | None:
        # Only take levels that remain profitable after fee + slippage.
        levels = []
        for lvl in book.asks:
            fee = taker_fee_per_share(lvl.price, market.fee_rate, market.fee_exponent)
            if sig.fair - (lvl.price + self.extra_slippage) - fee < self.min_edge:
                break
            levels.append(lvl)
        shares, vwap, fees = simulate_taker_buy(
            levels, max_usd, market.fee_rate, market.fee_exponent, self.extra_slippage
        )
        if shares < market.min_order_size:
            return None
        pos = Position(
            position_id=None, market_id=market.market_id, slug=market.slug, side=sig.side,
            token_id=sig.token_id, shares=shares, avg_price=vwap, fees=fees, opened_ts=time.time(),
            end_ts=market.end_ts, fair_at_entry=sig.fair, edge_at_entry=sig.fair - vwap - fees / shares,
        )
        self.portfolio.add(pos)
        log.info("PAPER BUY %s %s %.2f sh @ %.4f fee %.4f edge %.4f",
                 market.slug, sig.side, shares, vwap, fees, pos.edge_at_entry)
        return pos

    async def cancel_all(self) -> None:
        """Paper mode has no resting orders; present for interface parity."""
        return None
