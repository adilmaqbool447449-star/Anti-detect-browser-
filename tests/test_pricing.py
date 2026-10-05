import math
import random

import pytest

from polybot.models import Level, MarketKind
from polybot.pricing import (
    SECONDS_PER_YEAR, blend_vol, fair_yes_probability, norm_cdf, prob_above, prob_range,
    prob_touch, realized_vol, simulate_taker_buy, taker_fee_per_share,
)

SIG = 0.5
H1 = 3600.0


def test_norm_cdf_basics():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_prob_above_atm_is_just_below_half():
    p = prob_above(100_000, 100_000, SIG, H1)
    assert 0.49 < p < 0.5  # -sigma^2 tau / 2 convexity term


def test_prob_above_monotonic_in_spot_and_bounded():
    ps = [prob_above(s, 100_000, SIG, H1) for s in range(98_000, 102_001, 500)]
    assert all(a < b for a, b in zip(ps, ps[1:]))
    assert 0 < ps[0] < ps[-1] < 1


def test_prob_above_collapses_to_step_at_expiry():
    assert prob_above(100_001, 100_000, SIG, 0) == 1.0
    assert prob_above(99_999, 100_000, SIG, 0) == 0.0


def test_prob_above_matches_closed_form():
    s, k, tau = 101_000, 100_000, 6 * H1
    sd = SIG * math.sqrt(tau / SECONDS_PER_YEAR)
    expected = norm_cdf((math.log(s / k) - sd * sd / 2) / sd)
    assert prob_above(s, k, SIG, tau) == pytest.approx(expected, rel=1e-12)


def test_basis_noise_pulls_toward_half():
    near = prob_above(100_050, 100_000, SIG, 30)
    with_basis = prob_above(100_050, 100_000, SIG, 30, basis_bps=10)
    assert 0.5 < with_basis < near


def test_above_below_complement_and_range():
    a = fair_yes_probability(MarketKind.ABOVE, 100_000, SIG, H1, 101_000)
    b = fair_yes_probability(MarketKind.BELOW, 100_000, SIG, H1, 101_000)
    assert a + b == pytest.approx(1.0)
    r = prob_range(100_000, 99_000, 101_000, SIG, H1)
    assert r == pytest.approx(prob_above(100_000, 99_000, SIG, H1) - prob_above(100_000, 101_000, SIG, H1))
    assert fair_yes_probability(MarketKind.RANGE, 100_000, SIG, H1, 99_000, 101_000) == pytest.approx(r)


def test_updown_before_window_is_coin_flip_and_after_uses_open():
    assert fair_yes_probability(MarketKind.UP_DOWN, 100_000, SIG, 300, None) == 0.5
    up = fair_yes_probability(MarketKind.UP_DOWN, 100_200, SIG, 120, 100_000)
    assert up > 0.8


def test_unpriceable_inputs():
    assert fair_yes_probability(MarketKind.ABOVE, 100_000, SIG, H1, None) is None
    assert fair_yes_probability(MarketKind.RANGE, 100_000, SIG, H1, 99_000, None) is None
    assert fair_yes_probability(MarketKind.ABOVE, 0, SIG, H1, 1) is None


def test_touch_small_drift_reflection_principle():
    # For tiny drift, P(touch) ~= 2 * P(S_T > H)
    s, h, tau = 100_000, 100_500, 600
    assert prob_touch(s, h, SIG, tau) == pytest.approx(2 * prob_above(s, h, SIG, tau), rel=0.02)
    assert fair_yes_probability(MarketKind.TOUCH_UP, 100_600, SIG, tau, 100_500) == 1.0
    assert fair_yes_probability(MarketKind.TOUCH_DOWN, 100_000, SIG, tau, 99_500) == pytest.approx(
        prob_touch(100_000, 99_500, SIG, tau))


def test_touch_matches_monte_carlo():
    rng = random.Random(7)
    s0, h, sigma, tau = 100.0, 103.0, 0.8, 7 * 24 * 3600.0
    steps, paths = 400, 3000
    dt = tau / SECONDS_PER_YEAR / steps
    sd = sigma * math.sqrt(dt)
    hits = 0
    for _ in range(paths):
        x, b = 0.0, math.log(h / s0)
        for _ in range(steps):
            x += -0.5 * sigma * sigma * dt + sd * rng.gauss(0, 1)
            if x >= b:
                hits += 1
                break
    mc = hits / paths
    # discrete monitoring slightly under-counts touches
    assert prob_touch(s0, h, sigma, tau) == pytest.approx(mc, abs=0.04)


def test_taker_fee_schedule():
    assert taker_fee_per_share(0.5, 0.07) == pytest.approx(0.0175)
    assert taker_fee_per_share(0.25, 0.07) == pytest.approx(0.07 * 0.1875)
    assert taker_fee_per_share(0.0, 0.07) == 0 and taker_fee_per_share(1.0, 0.07) == 0
    assert taker_fee_per_share(0.3, 0.07) == pytest.approx(taker_fee_per_share(0.7, 0.07))
    assert taker_fee_per_share(0.5, 0.0) == 0


def test_simulate_taker_buy_walks_book_within_budget():
    asks = [Level(0.40, 10), Level(0.42, 100)]
    shares, vwap, fees = simulate_taker_buy(asks, 20.0, 0.07)
    spent = shares * vwap + fees
    assert spent == pytest.approx(20.0)
    assert shares > 10 and 0.40 < vwap < 0.42
    assert fees == pytest.approx(10 * taker_fee_per_share(0.40, 0.07) + (shares - 10) * taker_fee_per_share(0.42, 0.07))
    # limit price stops at first level
    s2, v2, _ = simulate_taker_buy(asks, 20.0, 0.07, limit_price=0.41)
    assert s2 == 10 and v2 == 0.40
    assert simulate_taker_buy([], 20, 0.07) == (0.0, 0.0, 0.0)


def test_realized_vol_known_value():
    r = 0.001
    prices = [100.0]
    for i in range(200):
        prices.append(prices[-1] * math.exp(r if i % 2 else -r))
    assert realized_vol(prices, 60) == pytest.approx(r * math.sqrt(SECONDS_PER_YEAR / 60), rel=1e-9)
    assert realized_vol([1, 2], 60) is None


def test_blend_vol_floor_cap():
    assert blend_vol(None, None, 0.5, 0.2, 2) == 0.2
    assert blend_vol(0.4, 0.6, 0.5, 0.2, 2) == pytest.approx(0.5)
    assert blend_vol(5.0, None, 0.5, 0.2, 2) == 2
