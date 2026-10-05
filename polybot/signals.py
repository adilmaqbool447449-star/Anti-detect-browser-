"""Signal engine: compares model fair value with executable Polymarket prices.

Edge is measured against what we would actually pay as a taker: the ask
(so the spread is included), walked through book depth for the intended size,
plus Polymarket's taker fee at each fill price.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import ModelConfig, SignalConfig
from .models import Book, Level, Market, Signal
from .pricing import fair_yes_probability, simulate_taker_buy, taker_fee_per_share


@dataclass
class MarketView:
    market_id: str
    slug: str
    question: str
    kind: str
    end_ts: float
    tau_s: float
    strike: float | None
    strike_high: float | None
    fair_yes: float | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    edge_yes: float | None  # top-of-book edge after fee
    edge_no: float | None
    note: str = ""


def _tob_edge(fair: float | None, ask: float | None, m: Market) -> float | None:
    if fair is None or ask is None:
        return None
    return fair - ask - taker_fee_per_share(ask, m.fee_rate, m.fee_exponent)


class SignalEngine:
    def __init__(
        self,
        sig: SignalConfig,
        model: ModelConfig,
        extra_slippage: float = 0.0,
        min_seconds_to_expiry: float = 0.0,
    ):
        self.sig = sig
        self.model = model
        self.extra_slippage = extra_slippage
        self.min_seconds_to_expiry = min_seconds_to_expiry

    def evaluate(
        self,
        m: Market,
        yes_book: Book | None,
        no_book: Book | None,
        spot: float | None,
        sigma: float,
        now: float,
        max_usd: float,
    ) -> tuple[MarketView, Signal | None]:
        tau = m.end_ts - now
        view = MarketView(
            market_id=m.market_id, slug=m.slug, question=m.question, kind=m.kind.value,
            end_ts=m.end_ts, tau_s=tau, strike=m.strike, strike_high=m.strike_high, fair_yes=None,
            yes_bid=yes_book.best_bid if yes_book else None, yes_ask=yes_book.best_ask if yes_book else None,
            no_bid=no_book.best_bid if no_book else None, no_ask=no_book.best_ask if no_book else None,
            edge_yes=None, edge_no=None,
        )
        if spot is None:
            view.note = "no spot"
            return view, None
        if tau <= 0:
            view.note = "expired"
            return view, None
        started = m.start_ts is not None and m.start_ts <= now
        if m.kind.value == "up_down" and started and m.strike is None:
            view.note = "awaiting start price"
            return view, None
        fair_yes = fair_yes_probability(
            m.kind, spot, sigma, tau, m.strike if (m.kind.value != "up_down" or started) else None,
            m.strike_high, self.model.basis_uncertainty_bps,
        )
        if fair_yes is None:
            view.note = "unpriceable"
            return view, None
        view.fair_yes = fair_yes
        view.edge_yes = _tob_edge(fair_yes, view.yes_ask, m)
        view.edge_no = _tob_edge(1 - fair_yes, view.no_ask, m)

        if tau < self.min_seconds_to_expiry:
            view.note = "too close to expiry"
            return view, None

        best: Signal | None = None
        for side, book, fair in (("YES", yes_book, fair_yes), ("NO", no_book, 1 - fair_yes)):
            if book is None or not book.asks:
                continue
            ask = book.asks[0].price
            if not (self.sig.min_price <= ask <= self.sig.max_price):
                continue
            levels = self._profitable_levels(book.asks, fair, m)
            if not levels:
                continue
            shares, vwap, fees = simulate_taker_buy(
                levels, max_usd, m.fee_rate, m.fee_exponent, self.extra_slippage
            )
            if shares < m.min_order_size:
                continue
            fee_ps = fees / shares
            edge = fair - vwap - fee_ps
            if edge < self.sig.min_edge:
                continue
            s = Signal(
                ts=now, market_id=m.market_id, slug=m.slug, side=side,
                token_id=m.yes_token if side == "YES" else m.no_token,
                fair=fair, ask=ask, vwap=vwap, fee_per_share=fee_ps, edge=edge, shares=shares,
                spot=spot, sigma=sigma, tau_s=tau, strike=m.strike,
            )
            if best is None or s.edge > best.edge:
                best = s
        return view, best

    def _profitable_levels(self, asks: list[Level], fair: float, m: Market) -> list[Level]:
        out = []
        for lvl in asks:
            fee = taker_fee_per_share(lvl.price, m.fee_rate, m.fee_exponent)
            if fair - (lvl.price + self.extra_slippage) - fee < self.sig.min_edge:
                break
            out.append(lvl)
        return out
