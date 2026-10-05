"""Plain data types shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MarketKind(str, Enum):
    UP_DOWN = "up_down"  # YES ("Up") if S_end >= S_start
    ABOVE = "above"  # YES if S_T > K
    BELOW = "below"  # YES if S_T < K
    RANGE = "range"  # YES if K_low <= S_T < K_high
    TOUCH_UP = "touch_up"  # YES if max(S) >= K before expiry
    TOUCH_DOWN = "touch_down"  # YES if min(S) <= K before expiry


@dataclass
class Market:
    market_id: str
    condition_id: str
    slug: str
    question: str
    kind: MarketKind
    yes_token: str
    no_token: str
    yes_label: str
    no_label: str
    end_ts: float  # unix seconds
    start_ts: float | None = None  # window start for UP_DOWN
    strike: float | None = None
    strike_high: float | None = None  # RANGE upper bound
    fee_rate: float = 0.07
    fee_exponent: float = 1.0
    tick_size: float = 0.01
    min_order_size: float = 5.0
    resolution_source: str = ""


@dataclass
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    bids: list[Level]  # best (highest) first
    asks: list[Level]  # best (lowest) first
    received_ts: float  # local receive time

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return None


@dataclass
class Signal:
    ts: float
    market_id: str
    slug: str
    side: str  # "YES" | "NO"
    token_id: str
    fair: float  # fair probability of the side being bought
    ask: float  # best ask for that side
    vwap: float  # expected average fill price for intended size
    fee_per_share: float
    edge: float  # fair - vwap - fee_per_share
    shares: float
    spot: float
    sigma: float
    tau_s: float
    strike: float | None


@dataclass
class Position:
    position_id: int | None
    market_id: str
    slug: str
    side: str
    token_id: str
    shares: float
    avg_price: float
    fees: float
    opened_ts: float
    end_ts: float
    fair_at_entry: float
    edge_at_entry: float
    status: str = "open"  # open | settled
    payout: float | None = None
    pnl: float | None = None
    closed_ts: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return self.shares * self.avg_price + self.fees
