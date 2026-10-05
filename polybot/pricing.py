"""Pure pricing functions. No I/O, fully unit tested.

Model: BTC follows a driftless (martingale) geometric Brownian motion over the
short horizons we care about. For a binary that pays $1, the fair price equals
the probability of the event. Basis between Binance and the market's actual
resolution source (e.g. Chainlink) is modelled as extra Gaussian noise on the
terminal log price.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from .models import Level, MarketKind

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _total_sd(sigma: float, tau_s: float, basis_bps: float) -> float:
    """Std-dev of terminal log price: diffusion + basis noise."""
    tau_y = max(tau_s, 0.0) / SECONDS_PER_YEAR
    basis = basis_bps / 1e4
    return math.sqrt(sigma * sigma * tau_y + basis * basis)


def prob_above(spot: float, strike: float, sigma: float, tau_s: float, basis_bps: float = 0.0) -> float:
    """P(S_T > K) under driftless GBM (risk-neutral with r=0)."""
    if strike <= 0:
        return 1.0
    sd = _total_sd(sigma, tau_s, basis_bps)
    if sd <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) - 0.5 * sd * sd) / sd
    return norm_cdf(d2)


def prob_range(spot: float, low: float, high: float, sigma: float, tau_s: float, basis_bps: float = 0.0) -> float:
    return max(0.0, prob_above(spot, low, sigma, tau_s, basis_bps) - prob_above(spot, high, sigma, tau_s, basis_bps))


def prob_touch(spot: float, barrier: float, sigma: float, tau_s: float, basis_bps: float = 0.0) -> float:
    """P(path touches barrier before T) for GBM with drift mu = -sigma^2/2 in log space.

    Works for barriers above or below spot (reflection principle). Basis noise
    is folded in by widening the effective variance, which is a conservative
    approximation for barrier markets.
    """
    if barrier <= 0:
        return 0.0
    b = math.log(barrier / spot)
    if b == 0:
        return 1.0
    tau_y = max(tau_s, 0.0) / SECONDS_PER_YEAR
    var = sigma * sigma * tau_y + (basis_bps / 1e4) ** 2
    if var <= 0:
        return 0.0
    sd = math.sqrt(var)
    s2 = var / tau_y if tau_y > 0 else sigma * sigma  # effective variance rate
    mu = -0.5 * s2
    if b > 0:  # upper barrier: P(max X_t >= b)
        p = norm_cdf((-b + mu * tau_y) / sd) + math.exp(2 * mu * b / s2) * norm_cdf((-b - mu * tau_y) / sd)
    else:  # lower barrier: P(min X_t <= b)
        p = norm_cdf((b - mu * tau_y) / sd) + math.exp(2 * mu * b / s2) * norm_cdf((b + mu * tau_y) / sd)
    return min(1.0, max(0.0, p))


def fair_yes_probability(
    kind: MarketKind,
    spot: float,
    sigma: float,
    tau_s: float,
    strike: float | None = None,
    strike_high: float | None = None,
    basis_bps: float = 0.0,
) -> float | None:
    """Fair probability that the YES outcome wins. None if not priceable."""
    if spot <= 0 or tau_s < 0:
        return None
    if kind is MarketKind.UP_DOWN:
        if strike is None:
            # Window not started yet: S_start unknown, outcome ~ coin flip.
            # Ties resolve Up; ignore the measure-zero tie.
            return 0.5
        # Up wins if S_end >= S_start.
        return prob_above(spot, strike, sigma, tau_s, basis_bps)
    if strike is None:
        return None
    if kind is MarketKind.ABOVE:
        return prob_above(spot, strike, sigma, tau_s, basis_bps)
    if kind is MarketKind.BELOW:
        return 1.0 - prob_above(spot, strike, sigma, tau_s, basis_bps)
    if kind is MarketKind.RANGE:
        if strike_high is None:
            return None
        return prob_range(spot, strike, strike_high, sigma, tau_s, basis_bps)
    if kind is MarketKind.TOUCH_UP:
        return 1.0 if spot >= strike else prob_touch(spot, strike, sigma, tau_s, basis_bps)
    if kind is MarketKind.TOUCH_DOWN:
        return 1.0 if spot <= strike else prob_touch(spot, strike, sigma, tau_s, basis_bps)
    return None


# ---------------------------------------------------------------- fees ----

def taker_fee_per_share(price: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket taker fee in USDC per share: rate * (p * (1 - p)) ** exponent.

    With the crypto schedule (rate 0.07, exponent 1) this is 1.75c/share at
    p = 0.50 and shrinks toward the extremes. Makers pay nothing.
    """
    p = min(max(price, 0.0), 1.0)
    return rate * (p * (1.0 - p)) ** exponent


def simulate_taker_buy(
    asks: Sequence[Level],
    max_usd: float,
    fee_rate: float,
    fee_exponent: float = 1.0,
    extra_slippage: float = 0.0,
    limit_price: float | None = None,
) -> tuple[float, float, float]:
    """Walk the ask side buying up to max_usd (price + fee) of shares.

    Returns (shares, vwap_price, total_fee). Stops at limit_price if given.
    """
    shares = cost = fees = 0.0
    budget = max_usd
    for lvl in asks:
        px = lvl.price + extra_slippage
        if limit_price is not None and lvl.price > limit_price:
            break
        fee = taker_fee_per_share(lvl.price, fee_rate, fee_exponent)
        per_share = px + fee
        if per_share <= 0:
            continue
        take = min(lvl.size, budget / per_share)
        if take <= 0:
            break
        shares += take
        cost += take * px
        fees += take * fee
        budget -= take * per_share
        if budget <= 1e-9:
            break
    if shares <= 0:
        return 0.0, 0.0, 0.0
    return shares, cost / shares, fees


# ---------------------------------------------------------- volatility ----

def realized_vol(prices: Sequence[float], interval_s: float) -> float | None:
    """Annualised close-to-close realised vol (zero-mean estimator)."""
    if len(prices) < 3 or interval_s <= 0:
        return None
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    var = sum(r * r for r in rets) / len(rets)
    return math.sqrt(var * SECONDS_PER_YEAR / interval_s)


def blend_vol(short: float | None, long: float | None, w_short: float, floor: float, cap: float) -> float:
    if short is None and long is None:
        return floor
    if short is None:
        v = long
    elif long is None:
        v = short
    else:
        v = w_short * short + (1 - w_short) * long
    return min(max(v, floor), cap)
