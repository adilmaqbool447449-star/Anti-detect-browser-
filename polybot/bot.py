"""Orchestrator: wires feeds -> pricing/signals -> risk -> execution -> storage."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict

import httpx

from .config import Config
from .execution.paper import PaperExecutor
from .feeds.binance import BinanceFeed
from .feeds.polymarket import PolymarketFeed
from .models import Market, MarketKind, Signal
from .portfolio import Portfolio
from .risk import RiskManager
from .signals import MarketView, SignalEngine
from .storage import Storage

log = logging.getLogger(__name__)

# Touch markets need the full price path since the market's window opened,
# which we cannot reconstruct reliably from the event metadata; off by default.
DEFAULT_KINDS = {MarketKind.UP_DOWN, MarketKind.ABOVE, MarketKind.BELOW, MarketKind.RANGE}


class Bot:
    def __init__(self, cfg: Config, storage: Storage | None = None, enabled_kinds: set[MarketKind] | None = None):
        if cfg.execution.mode != "paper":
            raise RuntimeError("Only paper mode is implemented. Live trading has not been added.")
        self.cfg = cfg
        self.mode = cfg.execution.mode
        self.http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "polybot/0.1"})
        self.storage = storage or Storage(cfg.storage.sqlite_path)
        self.binance = BinanceFeed(cfg.binance, cfg.model, self.http)
        self.poly = PolymarketFeed(cfg.polymarket, enabled_kinds or DEFAULT_KINDS, lambda: self.binance.price, self.http)
        self.risk = RiskManager(cfg.risk)
        self.portfolio = Portfolio(self.storage, self.mode, cfg.risk.starting_balance_usd)
        self.engine = SignalEngine(cfg.signals, cfg.model, cfg.execution.paper_extra_slippage,
                                   cfg.polymarket.min_seconds_to_expiry)
        self.executor = PaperExecutor(self.portfolio, cfg.signals.min_edge, cfg.execution.paper_extra_slippage)
        self.views: dict[str, MarketView] = {}
        self.halt_reason: str | None = None
        self.started_ts = time.time()
        self._last_signal_log: dict[tuple[str, str], float] = {}
        self._strike_tasks: dict[str, asyncio.Task] = {}
        self._kill_logged = False
        self._stop = asyncio.Event()

    # -------------------------------------------------------------- loops --
    async def strategy_loop(self, every_s: float = 0.5) -> None:
        while not self._stop.is_set():
            try:
                await self.step()
            except Exception:  # noqa: BLE001 - never let the loop die silently
                log.exception("strategy step failed")
            await self._sleep(every_s)

    async def step(self, now: float | None = None) -> None:
        now = now or time.time()
        books = self.poly.books
        daily = self.portfolio.daily_pnl(books, now)
        if self.risk.update_daily_pnl(daily, now):
            self.storage.log_event("WARN", "daily_loss_halt", f"daily pnl {daily:.2f}")
        kill = self.risk.kill_switch_active()
        if kill and not self._kill_logged:
            self.storage.log_event("WARN", "kill_switch", "kill switch file present; trading stopped")
            await self.executor.cancel_all()
        self._kill_logged = kill

        spot, sigma = self.binance.price, self.binance.sigma
        views: dict[str, MarketView] = {}
        global_halt = self.risk.halt_reason({"binance": self.binance.age(now)}, now)
        self.halt_reason = global_halt
        for m in list(self.poly.markets.values()):
            self._ensure_strike(m, now)
            yes, no = books.get(m.yes_token), books.get(m.no_token)
            allowed = self.risk.allowed_usd(self.portfolio.exposure, self.portfolio.balance)
            view, sig = self.engine.evaluate(m, yes, no, spot, sigma, now, max(allowed, 1.0))
            views[m.market_id] = view
            if sig is not None:
                await self._handle_signal(sig, m, now, allowed)
        self.views = views

    async def _handle_signal(self, sig: Signal, m: Market, now: float, allowed: float) -> None:
        cost = sig.shares * (sig.vwap + sig.fee_per_share)
        ages = {
            "binance": self.binance.age(now),
            "book_yes": self.poly.book_age(m.yes_token, now),
            "book_no": self.poly.book_age(m.no_token, now),
        }
        decision = self.risk.check(cost, self.portfolio.exposure, self.portfolio.balance,
                                   self.portfolio.positions_in_market(m.market_id), ages, now)
        acted = False
        reason = decision.reason
        if decision.ok:
            book = self.poly.books.get(sig.token_id)
            pos = await self.executor.buy(sig, m, book, allowed) if book else None
            acted = pos is not None
            reason = "filled" if acted else "fill below min size"
        key = (m.market_id, sig.side)
        if acted or now - self._last_signal_log.get(key, 0) >= self.cfg.signals.signal_log_cooldown_s:
            self._last_signal_log[key] = now
            self.storage.log_signal(sig, acted, reason, self.mode)
            log.info("SIGNAL %s %s fair=%.3f ask=%.3f edge=%.3f -> %s", m.slug, sig.side, sig.fair, sig.ask, sig.edge, reason)

    def _ensure_strike(self, m: Market, now: float) -> None:
        """Up/Down strike = BTC price at window start; fetch it once the window opens."""
        if m.kind is not MarketKind.UP_DOWN or m.strike is not None or m.start_ts is None or m.start_ts > now:
            return
        t = self._strike_tasks.get(m.market_id)
        if t is not None and not t.done():
            return
        if now - m.start_ts > 60 * 60 * 6:
            return

        async def fetch():
            try:
                px = await self.binance.open_price_at(m.start_ts)
                if px:
                    m.strike = px
            except Exception as e:  # noqa: BLE001
                log.warning("open price fetch failed for %s: %s", m.slug, e)
                await asyncio.sleep(2)

        self._strike_tasks[m.market_id] = asyncio.create_task(fetch())
        if len(self._strike_tasks) > 500:
            for k in [k for k, v in self._strike_tasks.items() if v.done()]:
                del self._strike_tasks[k]

    async def settlement_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            for p in list(self.portfolio.open):
                if now < p.end_ts:
                    continue
                try:
                    res = await self.poly.fetch_resolution(p.market_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("resolution fetch failed for %s: %s", p.slug, e)
                    continue
                if res is None:
                    continue
                yes, no = res
                self.portfolio.settle(p, yes if p.side == "YES" else no, now)
                log.info("SETTLED %s %s pnl=%.2f", p.slug, p.side, p.pnl)
            await self._sleep(self.cfg.execution.settle_poll_interval_s)

    async def equity_loop(self, every_s: float = 60.0) -> None:
        while not self._stop.is_set():
            try:
                self.storage.log_equity(self.mode, self.portfolio.balance, self.portfolio.exposure,
                                        self.portfolio.realized_pnl, self.portfolio.unrealized_pnl(self.poly.books))
            except Exception:  # noqa: BLE001
                log.exception("equity snapshot failed")
            await self._sleep(every_s)

    # ---------------------------------------------------------- lifecycle --
    async def run(self) -> None:
        log.info("Starting polybot in %s mode", self.mode.upper())
        self.storage.log_event("INFO", "start", f"mode={self.mode}")
        tasks = [
            asyncio.create_task(self.binance.run(), name="binance_ws"),
            asyncio.create_task(self.binance.run_vol_refresher(), name="vol"),
            asyncio.create_task(self.poly.run_scanner(), name="scanner"),
            asyncio.create_task(self.poly.run_book_poller(), name="books"),
            asyncio.create_task(self.strategy_loop(), name="strategy"),
            asyncio.create_task(self.settlement_loop(), name="settlement"),
            asyncio.create_task(self.equity_loop(), name="equity"),
        ]
        if self.cfg.dashboard.enabled:
            from .dashboard.app import serve_dashboard
            tasks.append(asyncio.create_task(serve_dashboard(self), name="dashboard"))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception():
                    raise t.exception()
        finally:
            self.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.http.aclose()
            self.storage.log_event("INFO", "stop", "shutdown")

    async def stopped(self) -> None:
        await self._stop.wait()

    def stop(self) -> None:
        self._stop.set()
        self.binance.stop()
        self.poly.stop()

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            pass

    # ---------------------------------------------------------- dashboard --
    def snapshot(self) -> dict:
        now = time.time()
        books = self.poly.books
        open_pos = []
        for p in self.portfolio.open:
            d = asdict(p)
            d["mark_value"] = self.portfolio.mark(p, books)
            d["unrealized"] = d["mark_value"] - p.cost
            open_pos.append(d)
        return {
            "now": now,
            "mode": self.mode,
            "min_edge": self.cfg.signals.min_edge,
            "btc": {"price": self.binance.price, "bid": self.binance.bid, "ask": self.binance.ask,
                    "age_s": self.binance.age(now), "sigma": self.binance.sigma, "connected": self.binance.connected,
                    "reconnects": self.binance.reconnects},
            "polymarket": {"markets": len(self.poly.markets), "last_scan_age_s": now - self.poly.last_scan_ts if self.poly.last_scan_ts else None,
                           "scan_error": self.poly.last_scan_error},
            "risk": {"halt_reason": self.halt_reason, "kill_switch": self.risk.kill_switch_active(),
                     "daily_halted": self.risk.daily_halted(now), "limits": asdict(self.cfg.risk)},
            "pnl": {"balance": self.portfolio.balance, "exposure": self.portfolio.exposure,
                    "realized": self.portfolio.realized_pnl, "unrealized": self.portfolio.unrealized_pnl(books),
                    "daily": self.portfolio.daily_pnl(books, now), "starting_balance": self.portfolio.starting_balance},
            "markets": sorted((asdict(v) for v in self.views.values()), key=lambda v: v["end_ts"]),
            "positions": open_pos,
            "closed_positions": [r for r in self.storage.recent("positions", 30) if r["status"] == "settled" and r["mode"] == self.mode],
            "signals": self.storage.recent("signals", 30),
            "events": self.storage.recent("events", 15),
            "equity": self.storage.equity_curve(self.mode, 300),
        }
