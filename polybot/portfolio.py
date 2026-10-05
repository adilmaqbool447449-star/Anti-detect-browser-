"""Position and cash bookkeeping, rebuilt from SQLite on restart."""
from __future__ import annotations

import time

from .models import Book, Position
from .pricing import taker_fee_per_share
from .risk import utc_day_start
from .storage import Storage


class Portfolio:
    def __init__(self, storage: Storage, mode: str, starting_balance: float):
        self.storage = storage
        self.mode = mode
        self.starting_balance = starting_balance
        self.open: list[Position] = storage.load_positions(mode, "open")

    @property
    def realized_pnl(self) -> float:
        return self.storage.realized_pnl(self.mode)

    @property
    def exposure(self) -> float:
        return sum(p.cost for p in self.open)

    @property
    def balance(self) -> float:
        """Cash: start + realized P&L - cash locked in open positions."""
        return self.starting_balance + self.realized_pnl - self.exposure

    def positions_in_market(self, market_id: str) -> int:
        return sum(1 for p in self.open if p.market_id == market_id)

    def mark(self, p: Position, books: dict[str, Book], fee_rate: float = 0.07) -> float:
        """Liquidation value at best bid net of taker fee (entry price if no book)."""
        b = books.get(p.token_id)
        if b is None or b.best_bid is None:
            return p.shares * p.avg_price
        bid = b.best_bid
        return p.shares * (bid - taker_fee_per_share(bid, fee_rate))

    def unrealized_pnl(self, books: dict[str, Book]) -> float:
        return sum(self.mark(p, books) - p.cost for p in self.open)

    def daily_pnl(self, books: dict[str, Book], now: float | None = None) -> float:
        start = utc_day_start(now or time.time())
        return self.storage.realized_pnl(self.mode, since_ts=start) + self.unrealized_pnl(books)

    def add(self, p: Position) -> Position:
        p.position_id = self.storage.open_position(p, self.mode)
        self.open.append(p)
        return p

    def settle(self, p: Position, payout_per_share: float, now: float | None = None) -> Position:
        p.status = "settled"
        p.payout = p.shares * payout_per_share
        p.pnl = p.payout - p.cost
        p.closed_ts = now or time.time()
        self.storage.settle_position(p, self.mode)
        self.open = [q for q in self.open if q.position_id != p.position_id]
        return p
