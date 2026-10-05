"""Polymarket market scanner (Gamma API) and order-book poller (CLOB API).

Endpoints used (all public, no auth):
  GET  {gamma}/events?series_slug=...&closed=false&order=endDate&ascending=true
  GET  {gamma}/events?tag_slug=bitcoin&closed=false&active=true&end_date_min=..&end_date_max=..
  GET  {gamma}/markets/{id}                  -> resolution (closed, outcomePrices)
  POST {clob}/books   body=[{"token_id": ...}] -> order books
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from ..config import PolymarketConfig
from ..models import Book, Level, Market, MarketKind

log = logging.getLogger(__name__)

_NUM = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?"
_RANGE_RE = re.compile(rf"between\s+{_NUM}\s+(?:and|-|to)\s+{_NUM}|{_NUM}\s*[-–]\s*{_NUM}")
_EXCLUDE_RE = re.compile(
    r"dominance|market cap|etf|reserve|microstrategy|strategy |saylor|treasury|ath\b|all[- ]time|"
    r"ethereum|\beth\b|solana|\bsol\b|\bxrp\b|doge|halving|satoshi|el salvador|flipp",
    re.I,
)
_BTC_RE = re.compile(r"\bbitcoin\b|\bbtc\b", re.I)
_ABOVE_RE = re.compile(r">|≥|\babove\b|greater than|higher than|\bover\b|at least|or more|or higher", re.I)
_BELOW_RE = re.compile(r"<|≤|\bbelow\b|less than|lower than|\bunder\b|or less|or lower", re.I)
_TOUCH_UP_RE = re.compile(r"↑|\breach\b|\bhit\b|rise to|climb to|surpass|\bpump\b", re.I)
_TOUCH_DOWN_RE = re.compile(r"↓|\bdip\b|fall to|drop to|crash to|\bdump\b", re.I)


def _to_float(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _jsonish(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


def _strikes(text: str) -> list[float]:
    return [v for n, k in re.findall(_NUM, text) if (v := _to_float(n, k)) >= 1000]


def classify(question: str, group_title: str = "") -> tuple[MarketKind, float | None, float | None] | None:
    """Map a market question to (kind, strike, strike_high). None if not a BTC price market."""
    q = f"{question} {group_title}".strip()
    if not _BTC_RE.search(q) or _EXCLUDE_RE.search(q):
        return None
    if re.search(r"up or down", q, re.I):
        return MarketKind.UP_DOWN, None, None
    m = _RANGE_RE.search(q)
    if m:
        g = m.groups()
        nums = [(g[0], g[1]), (g[2], g[3])] if g[0] else [(g[4], g[5]), (g[6], g[7])]
        lo, hi = sorted(_to_float(n, k) for n, k in nums)
        if lo >= 1000 and lo < hi < 2 * lo:
            return MarketKind.RANGE, lo, hi
    strikes = _strikes(q)
    if not strikes:
        return None
    k = strikes[0]
    if _TOUCH_DOWN_RE.search(q):
        return MarketKind.TOUCH_DOWN, k, None
    if _TOUCH_UP_RE.search(q):
        return MarketKind.TOUCH_UP, k, None
    if _BELOW_RE.search(q):
        return MarketKind.BELOW, k, None
    if _ABOVE_RE.search(q):
        return MarketKind.ABOVE, k, None
    return None


def parse_market(m: dict, event: dict, cfg: PolymarketConfig) -> Market | None:
    if m.get("closed") or not m.get("enableOrderBook", True) or m.get("acceptingOrders") is False:
        return None
    tokens = _jsonish(m.get("clobTokenIds")) or []
    outcomes = _jsonish(m.get("outcomes")) or []
    if len(tokens) != 2 or len(outcomes) != 2:
        return None
    labels = [str(o).lower() for o in outcomes]
    if labels not in (["yes", "no"], ["up", "down"]):
        return None
    cls = classify(m.get("question") or event.get("title", ""), m.get("groupItemTitle") or "")
    if cls is None:
        return None
    kind, strike, strike_high = cls
    end_ts = _parse_ts(m.get("endDate") or event.get("endDate"))
    if end_ts is None:
        return None
    start_ts = _parse_ts(m.get("eventStartTime") or event.get("startTime")) if kind is MarketKind.UP_DOWN else None
    if kind is MarketKind.UP_DOWN and start_ts is None:
        return None
    fee_rate, fee_exp = cfg.default_fee_rate, cfg.default_fee_exponent
    sched = m.get("feeSchedule")
    if m.get("feesEnabled") is False:
        fee_rate = 0.0
    elif isinstance(sched, dict) and "rate" in sched:
        fee_rate = float(sched["rate"])
        fee_exp = float(sched.get("exponent", 1))
    return Market(
        market_id=str(m.get("id")),
        condition_id=m.get("conditionId", ""),
        slug=m.get("slug") or event.get("slug", ""),
        question=m.get("question") or event.get("title", ""),
        kind=kind,
        yes_token=str(tokens[0]),
        no_token=str(tokens[1]),
        yes_label=str(outcomes[0]),
        no_label=str(outcomes[1]),
        end_ts=end_ts,
        start_ts=start_ts,
        strike=strike,
        strike_high=strike_high,
        fee_rate=fee_rate,
        fee_exponent=fee_exp,
        tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
        min_order_size=float(m.get("orderMinSize") or 5),
        resolution_source=m.get("resolutionSource") or event.get("resolutionSource") or "",
    )


def parse_book(raw: dict, now: float) -> Book:
    bids = sorted((Level(float(x["price"]), float(x["size"])) for x in raw.get("bids", [])), key=lambda l: -l.price)
    asks = sorted((Level(float(x["price"]), float(x["size"])) for x in raw.get("asks", [])), key=lambda l: l.price)
    return Book(token_id=str(raw.get("asset_id")), bids=bids, asks=asks, received_ts=now)


class PolymarketFeed:
    def __init__(
        self,
        cfg: PolymarketConfig,
        enabled_kinds: set[MarketKind],
        spot: Callable[[], float | None],
        http: httpx.AsyncClient | None = None,
    ):
        self.cfg = cfg
        self.enabled_kinds = enabled_kinds
        self.spot = spot
        self.http = http or httpx.AsyncClient(timeout=10)
        self.markets: dict[str, Market] = {}
        self.books: dict[str, Book] = {}
        self.last_scan_ts = 0.0
        self.last_scan_error: str | None = None
        self._stop = asyncio.Event()

    # ----------------------------------------------------------- scanning --
    async def _get(self, path: str, params: dict) -> list | dict:
        r = await self.http.get(f"{self.cfg.gamma_url}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _iso(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def scan(self, now: float | None = None) -> dict[str, Market]:
        now = now or time.time()
        horizon = now + self.cfg.max_horizon_hours * 3600
        min_end = now + self.cfg.min_seconds_to_expiry
        updown: list[Market] = []
        strike_mkts: list[Market] = []

        for slug in self.cfg.series_slugs:
            events = await self._get("/events", {
                "series_slug": slug, "closed": "false", "limit": 50,
                "order": "endDate", "ascending": "true", "end_date_min": self._iso(min_end),
            })
            found: list[Market] = []
            for ev in events if isinstance(events, list) else []:
                for m in ev.get("markets", []):
                    pm = parse_market(m, ev, self.cfg)
                    if pm and min_end <= pm.end_ts <= horizon:
                        found.append(pm)
            found.sort(key=lambda x: x.end_ts)
            updown.extend(found[: self.cfg.max_events_per_series])

        for tag in self.cfg.tag_slugs:
            events = await self._get("/events", {
                "tag_slug": tag, "closed": "false", "active": "true", "limit": 100,
                "order": "endDate", "ascending": "true",
                "end_date_min": self._iso(min_end), "end_date_max": self._iso(horizon),
            })
            for ev in events if isinstance(events, list) else []:
                for m in ev.get("markets", []):
                    pm = parse_market(m, ev, self.cfg)
                    if pm and min_end <= pm.end_ts <= horizon:
                        (updown if pm.kind is MarketKind.UP_DOWN else strike_mkts).append(pm)

        spot = self.spot()

        def strike_rank(mk: Market) -> float:
            if spot and mk.strike:
                k = mk.strike if mk.strike_high is None else (mk.strike + mk.strike_high) / 2
                return abs(math.log(k / spot))
            return mk.end_ts

        selected: dict[str, Market] = {}
        for mk in sorted(updown, key=lambda x: x.end_ts) + sorted(strike_mkts, key=strike_rank):
            if mk.kind not in self.enabled_kinds or mk.market_id in selected:
                continue
            selected[mk.market_id] = mk
            if len(selected) >= self.cfg.max_markets:
                break
        return selected

    async def run_scanner(self) -> None:
        while not self._stop.is_set():
            try:
                self.markets = await self.scan()
                self.last_scan_ts = time.time()
                self.last_scan_error = None
                live_tokens = {t for m in self.markets.values() for t in (m.yes_token, m.no_token)}
                for t in list(self.books):
                    if t not in live_tokens:
                        del self.books[t]
                log.info("Scanned %d BTC markets", len(self.markets))
            except Exception as e:  # noqa: BLE001
                self.last_scan_error = str(e)[:200]
                log.warning("Polymarket scan failed: %s", self.last_scan_error)
            await self._sleep(self.cfg.scan_interval_s)

    # -------------------------------------------------------------- books --
    async def fetch_books(self, tokens: list[str]) -> None:
        for i in range(0, len(tokens), 50):
            chunk = tokens[i : i + 50]
            r = await self.http.post(f"{self.cfg.clob_url}/books", json=[{"token_id": t} for t in chunk])
            r.raise_for_status()
            now = time.time()
            for raw in r.json():
                b = parse_book(raw, now)
                self.books[b.token_id] = b

    async def run_book_poller(self) -> None:
        delay = self.cfg.book_poll_interval_s
        while not self._stop.is_set():
            tokens = [t for m in list(self.markets.values()) for t in (m.yes_token, m.no_token)]
            try:
                if tokens:
                    await self.fetch_books(tokens)
                delay = self.cfg.book_poll_interval_s
            except Exception as e:  # noqa: BLE001
                log.warning("Book poll failed: %s", str(e)[:200])
                delay = min(delay * 2, 30.0)  # back off on errors / rate limits
            await self._sleep(delay)

    def book_age(self, token: str, now: float | None = None) -> float:
        b = self.books.get(token)
        return float("inf") if b is None else (now or time.time()) - b.received_ts

    # --------------------------------------------------------- resolution --
    async def fetch_resolution(self, market_id: str) -> tuple[float, float] | None:
        """(yes_payout, no_payout) once Polymarket has resolved the market, else None."""
        m = await self._get(f"/markets/{market_id}", {})
        if not isinstance(m, dict) or not m.get("closed"):
            return None
        prices = _jsonish(m.get("outcomePrices")) or []
        if len(prices) != 2:
            return None
        yes, no = float(prices[0]), float(prices[1])
        if {round(yes, 6), round(no, 6)} not in ({0.0, 1.0}, {0.5}):
            return None  # closed but not finalised yet
        return yes, no

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()
