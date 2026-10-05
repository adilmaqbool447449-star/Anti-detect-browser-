import pytest

from polybot.config import ModelConfig, SignalConfig
from polybot.models import Book, Level, Market, MarketKind
from polybot.pricing import fair_yes_probability
from polybot.signals import SignalEngine

NOW = 1_000_000.0


def mk(kind=MarketKind.ABOVE, strike=100_000.0, end=NOW + 3600, start=None):
    return Market("1", "0x1", "s", "q", kind, "Y", "N", "Yes", "No", end, start_ts=start, strike=strike)


def book(token, bid, ask, size=1000):
    return Book(token, [Level(bid, size)], [Level(ask, size)], NOW)


def engine(min_edge=0.03, **kw):
    return SignalEngine(SignalConfig(min_edge=min_edge), ModelConfig(basis_uncertainty_bps=0), **kw)


def test_buys_yes_when_cheap():
    m = mk()
    fair = fair_yes_probability(m.kind, 101_000, 0.5, 3600, m.strike)
    view, sig = engine().evaluate(m, book("Y", 0.50, 0.52), book("N", 0.46, 0.48), 101_000, 0.5, NOW, 25)
    assert fair > 0.6
    assert sig is not None and sig.side == "YES" and sig.token_id == "Y"
    assert sig.edge == pytest.approx(fair - 0.52 - 0.07 * 0.52 * 0.48)
    assert sig.shares * sig.vwap + sig.shares * sig.fee_per_share == pytest.approx(25)
    assert view.edge_yes == pytest.approx(sig.edge)


def test_buys_no_when_yes_overpriced():
    view, sig = engine().evaluate(mk(), book("Y", 0.80, 0.82), book("N", 0.18, 0.20), 99_000, 0.5, NOW, 25)
    assert sig.side == "NO" and sig.token_id == "N"


def test_fees_and_spread_kill_small_edges():
    m = mk(strike=100_000.0)
    fair = fair_yes_probability(m.kind, 100_000, 0.5, 3600, m.strike)
    ask = round(fair - 0.035, 2)  # ~3c raw edge, but fee at ~0.46 is ~1.7c
    _, sig = engine(min_edge=0.03).evaluate(m, book("Y", ask - 0.01, ask), None, 100_000, 0.5, NOW, 25)
    assert sig is None


def test_only_takes_depth_that_is_still_profitable():
    m = mk()
    yes = Book("Y", [], [Level(0.50, 10), Level(0.96, 1000)], NOW)
    _, sig = engine().evaluate(m, yes, None, 101_000, 0.5, NOW, 100)
    assert sig.shares == pytest.approx(10) and sig.vwap == pytest.approx(0.50)


def test_min_order_size_and_price_bounds():
    m = mk()
    tiny = Book("Y", [], [Level(0.50, 2)], NOW)
    assert engine().evaluate(m, tiny, None, 101_000, 0.5, NOW, 25)[1] is None
    # extreme prices ignored even if the model disagrees
    _, sig = engine().evaluate(m, book("Y", 0.01, 0.02), None, 99_000, 0.5, NOW, 25)
    assert sig is None


def test_updown_waits_for_start_price_and_expiry_guard():
    m = mk(MarketKind.UP_DOWN, strike=None, end=NOW + 200, start=NOW - 100)
    view, sig = engine().evaluate(m, book("Y", 0.4, 0.5), book("N", 0.4, 0.5), 100_000, 0.5, NOW, 25)
    assert sig is None and view.note == "awaiting start price"
    m.strike = 100_300
    view, sig = engine(min_seconds_to_expiry=300).evaluate(m, book("Y", 0.4, 0.5), book("N", 0.4, 0.5), 100_000, 0.5, NOW, 25)
    assert sig is None and view.note == "too close to expiry" and view.fair_yes < 0.5
