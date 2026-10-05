"""Binance BTC/USDT live feed.

Public market data only, no API key. Uses the combined stream endpoint
(`/stream?streams=btcusdt@bookTicker/btcusdt@aggTrade`). The price we use is
the bookTicker mid; aggTrade gives last trade. bookTicker carries no event
time, so freshness is measured by local receive time.

The websockets library answers the server's ping frames automatically.
Reconnects with exponential backoff and proactively before Binance's 24h cut.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque

import httpx
import websockets

from ..config import BinanceConfig, ModelConfig
from ..pricing import blend_vol, realized_vol

log = logging.getLogger(__name__)


class BinanceFeed:
    def __init__(self, cfg: BinanceConfig, model: ModelConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.model = model
        self.http = http or httpx.AsyncClient(timeout=10)
        self.bid: float | None = None
        self.ask: float | None = None
        self.last_trade: float | None = None
        self.last_msg_ts: float = 0.0
        self.connected = False
        self.reconnects = 0
        # 1-second samples of mid for short-horizon vol: (ts, price)
        self._samples: deque[tuple[float, float]] = deque(maxlen=max(60, model.short_vol_window_s + 5))
        self._kline_vol: float | None = None
        self._open_cache: dict[int, float] = {}
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ state --
    @property
    def price(self) -> float | None:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last_trade

    def age(self, now: float | None = None) -> float:
        if not self.last_msg_ts:
            return float("inf")
        return (now or time.time()) - self.last_msg_ts

    @property
    def sigma(self) -> float:
        prices = [p for _, p in self._samples]
        short = realized_vol(prices, 1.0) if len(prices) >= 120 else None
        return blend_vol(short, self._kline_vol, self.model.short_vol_weight, self.model.vol_floor, self.model.vol_cap)

    def handle_message(self, raw: str | bytes, now: float | None = None) -> None:
        now = now or time.time()
        msg = json.loads(raw)
        data = msg.get("data", msg)  # combined stream wraps payload in {"stream","data"}
        if "b" in data and "a" in data and "u" in data:  # bookTicker
            self.bid, self.ask = float(data["b"]), float(data["a"])
        elif data.get("e") in ("aggTrade", "trade"):
            self.last_trade = float(data["p"])
        else:
            return
        self.last_msg_ts = now
        self._sample(now)

    def _sample(self, now: float) -> None:
        px = self.price
        if px is None:
            return
        sec = int(now)
        if not self._samples or int(self._samples[-1][0]) < sec:
            self._samples.append((now, px))

    # --------------------------------------------------------- websocket --
    def stream_url(self) -> str:
        s = self.cfg.symbol.lower()
        return f"{self.cfg.ws_url}?streams={s}@bookTicker/{s}@aggTrade"

    async def run(self) -> None:
        delay = self.cfg.reconnect_min_s
        while not self._stop.is_set():
            started = time.time()
            try:
                async with websockets.connect(self.stream_url(), ping_interval=None, max_queue=1024) as ws:
                    self.connected = True
                    log.info("Binance WS connected")
                    while not self._stop.is_set():
                        if time.time() - started > self.cfg.max_connection_age_s:
                            log.info("Binance WS rotating connection before 24h limit")
                            break
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        self.handle_message(raw)
                        delay = self.cfg.reconnect_min_s  # healthy -> reset backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure means reconnect
                log.warning("Binance WS error: %s", e.__class__.__name__ + ": " + str(e)[:200])
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            sleep = min(delay, self.cfg.reconnect_max_s) * (0.8 + 0.4 * random.random())
            log.info("Binance WS reconnecting in %.1fs", sleep)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self.cfg.reconnect_max_s)

    def stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- REST --
    async def _klines(self, **params) -> list:
        r = await self.http.get(f"{self.cfg.rest_url}/api/v3/klines", params={"symbol": self.cfg.symbol, **params})
        r.raise_for_status()
        return r.json()

    async def refresh_kline_vol(self) -> None:
        rows = await self._klines(interval="1m", limit=self.model.kline_vol_window_min + 1)
        closes = [float(r[4]) for r in rows]
        self._kline_vol = realized_vol(closes, 60.0)
        log.debug("kline vol %.3f", self._kline_vol or 0)

    async def run_vol_refresher(self, every_s: float = 60.0) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh_kline_vol()
            except Exception as e:  # noqa: BLE001
                log.warning("kline vol refresh failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=every_s)
            except asyncio.TimeoutError:
                pass

    async def open_price_at(self, ts: float) -> float | None:
        """Open of the 1m kline starting at ts (used as Up/Down strike).

        Note: 5m/15m Up/Down markets resolve on Chainlink BTC/USD, so this is
        an approximation; the gap is covered by basis_uncertainty_bps.
        """
        minute = int(ts // 60 * 60)
        if minute in self._open_cache:
            return self._open_cache[minute]
        if ts > time.time():
            return None
        rows = await self._klines(interval="1m", startTime=minute * 1000, limit=1)
        if not rows or int(rows[0][0]) // 1000 != minute:
            return None
        px = float(rows[0][1])
        self._open_cache[minute] = px
        if len(self._open_cache) > 2000:
            self._open_cache.pop(next(iter(self._open_cache)))
        return px
