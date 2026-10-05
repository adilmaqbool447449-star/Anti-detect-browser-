import json
from pathlib import Path

import httpx
import pytest

from polybot.config import PolymarketConfig
from polybot.feeds.polymarket import PolymarketFeed, classify, parse_book, parse_market
from polybot.models import MarketKind

FIX = Path(__file__).parent / "fixtures"
EVENT = json.loads((FIX / "gamma_event_updown_5m.json").read_text())
BOOK = json.loads((FIX / "clob_book_up.json").read_text())
ALL_KINDS = set(MarketKind)


@pytest.mark.parametrize("q,group,expected", [
    ("Bitcoin Up or Down - May 12, 7:10AM-7:15AM ET", "", (MarketKind.UP_DOWN, None, None)),
    ("Will the price of Bitcoin be above $116,000 on October 5?", "116,000", (MarketKind.ABOVE, 116000, None)),
    ("Bitcoin above ___ on October 6?", ">118k", (MarketKind.ABOVE, 118000, None)),
    ("Will the price of Bitcoin be less than $100,000 on October 5?", "", (MarketKind.BELOW, 100000, None)),
    ("Will the price of Bitcoin be between $112,000 and $114,000 on October 5?", "", (MarketKind.RANGE, 112000, 114000)),
    ("Bitcoin price on October 5?", "$112,000-$114,000", (MarketKind.RANGE, 112000, 114000)),
    ("Will Bitcoin reach $130,000 in October?", "", (MarketKind.TOUCH_UP, 130000, None)),
    ("What price will Bitcoin hit in October?", "↓ 100,000", (MarketKind.TOUCH_DOWN, 100000, None)),
    ("Will Bitcoin dip to $95k by Friday?", "", (MarketKind.TOUCH_DOWN, 95000, None)),
])
def test_classify_btc_markets(q, group, expected):
    assert classify(q, group) == expected


@pytest.mark.parametrize("q", [
    "Will MicroStrategy buy more Bitcoin this week?",
    "Bitcoin dominance above 60% on October 5?",
    "Will Ethereum be above $5,000 on October 5?",
    "Will a Bitcoin ETF see $1,000 inflows?",
    "Will the US create a Strategic Bitcoin Reserve in 2026?",
])
def test_classify_rejects_non_price_markets(q):
    assert classify(q) is None


def test_parse_real_updown_event_fixture():
    m = parse_market(EVENT["markets"][0], EVENT, PolymarketConfig())
    assert m.kind is MarketKind.UP_DOWN
    assert m.yes_label == "Up" and m.no_label == "Down"
    assert m.yes_token.startswith("4841891773")
    assert m.start_ts == 1778584200 and m.end_ts == 1778584500
    assert m.fee_rate == 0.07 and m.fee_exponent == 1
    assert "chain.link" in m.resolution_source


def test_parse_book_sorts_best_first():
    b = parse_book(BOOK, now=1.0)
    assert b.best_bid < b.best_ask
    assert all(b.bids[i].price >= b.bids[i + 1].price for i in range(len(b.bids) - 1))
    assert all(b.asks[i].price <= b.asks[i + 1].price for i in range(len(b.asks) - 1))
    assert b.token_id == BOOK["asset_id"]


def _strike_event(end_iso):
    def mk(i, k):
        return {"id": str(900 + i), "question": f"Will the price of Bitcoin be above ${k:,} on October 5?",
                "groupItemTitle": f"{k:,}", "conditionId": f"0x{i}", "slug": f"btc-above-{k}",
                "endDate": end_iso, "outcomes": '["Yes","No"]', "clobTokenIds": f'["y{i}","n{i}"]',
                "enableOrderBook": True, "acceptingOrders": True, "closed": False}
    return {"slug": "bitcoin-above-on-october-5", "title": "Bitcoin above ___ on October 5?", "endDate": end_iso,
            "markets": [mk(i, k) for i, k in enumerate(range(100_000, 130_000, 2_000))]}


async def test_scan_with_mock_api_caps_and_prioritises():
    now = 1778584250.0  # inside the fixture's 5m window
    end_iso = "2026-05-12T16:00:00Z"
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        p = dict(req.url.params)
        if req.url.path == "/events" and p.get("series_slug") == "btc-up-or-down-5m":
            return httpx.Response(200, json=[EVENT])
        if req.url.path == "/events" and p.get("tag_slug") == "bitcoin":
            return httpx.Response(200, json=[_strike_event(end_iso)])
        return httpx.Response(200, json=[])

    cfg = PolymarketConfig(gamma_url="http://gamma", max_markets=5)
    feed = PolymarketFeed(cfg, ALL_KINDS, spot=lambda: 115_000.0,
                          http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    mk = await feed.scan(now=now)
    assert len(mk) == 5
    vals = list(mk.values())
    assert vals[0].kind is MarketKind.UP_DOWN
    # strike markets ordered by closeness to spot
    assert {m.strike for m in vals[1:]} == {114000, 116000, 112000, 118000}


async def test_fetch_books_and_resolution():
    def handler(req: httpx.Request):
        if req.url.path == "/books":
            body = json.loads(req.content)
            return httpx.Response(200, json=[dict(BOOK, asset_id=b["token_id"]) for b in body])
        if req.url.path == "/markets/1":
            return httpx.Response(200, json={"closed": True, "outcomePrices": '["0", "1"]'})
        if req.url.path == "/markets/2":
            return httpx.Response(200, json={"closed": False, "outcomePrices": '["0.6", "0.4"]'})
        return httpx.Response(404)

    cfg = PolymarketConfig(gamma_url="http://g", clob_url="http://c")
    feed = PolymarketFeed(cfg, ALL_KINDS, spot=lambda: None,
                          http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await feed.fetch_books(["a", "b"])
    assert set(feed.books) == {"a", "b"}
    assert feed.book_age("a") < 1 and feed.book_age("zzz") == float("inf")
    assert await feed.fetch_resolution("1") == (0.0, 1.0)
    assert await feed.fetch_resolution("2") is None
