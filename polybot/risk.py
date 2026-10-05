"""Risk manager. Every order passes through check() first; nothing bypasses it.

Controls:
  * max position size per trade (USD incl. fees)
  * max total exposure (sum of cost of open positions = max possible loss)
  * max positions per market
  * daily loss limit (realized today + unrealized, UTC day) -> latched halt
  * kill switch file -> immediate halt of all new trading
  * stale-data guard -> no trading if any required feed is older than N seconds
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import RiskConfig

log = logging.getLogger(__name__)


def utc_day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


@dataclass
class RiskDecision:
    ok: bool
    reason: str = "ok"


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.kill_path = Path(cfg.kill_switch_file)
        self._halted_day: float | None = None  # UTC day start when the daily-loss halt latched

    # --------------------------------------------------------- kill switch --
    def kill_switch_active(self) -> bool:
        return self.kill_path.exists()

    def engage_kill_switch(self, reason: str = "manual") -> None:
        self.kill_path.parent.mkdir(parents=True, exist_ok=True)
        self.kill_path.write_text(f"{datetime.now(timezone.utc).isoformat()} {reason}\n")
        log.warning("KILL SWITCH ENGAGED: %s", reason)

    # ---------------------------------------------------------- daily loss --
    def update_daily_pnl(self, daily_pnl: float, now: float | None = None) -> bool:
        """Latch a halt for the rest of the UTC day once the loss limit is hit.

        Returns True if this call newly triggered the halt.
        """
        day = utc_day_start(now or time.time())
        if self._halted_day is not None and self._halted_day != day:
            log.info("New UTC day: clearing daily-loss halt")
            self._halted_day = None
        if self._halted_day is None and daily_pnl <= -abs(self.cfg.daily_loss_limit_usd):
            self._halted_day = day
            log.warning("DAILY LOSS LIMIT HIT (%.2f): trading halted until next UTC day", daily_pnl)
            return True
        return False

    def daily_halted(self, now: float | None = None) -> bool:
        return self._halted_day is not None and self._halted_day == utc_day_start(now or time.time())

    # -------------------------------------------------------------- sizing --
    def allowed_usd(self, exposure: float, balance: float) -> float:
        return max(0.0, min(self.cfg.max_position_usd, self.cfg.max_total_exposure_usd - exposure, balance))

    def halt_reason(self, feed_ages: dict[str, float], now: float | None = None) -> str | None:
        if self.kill_switch_active():
            return "kill switch active"
        if self.daily_halted(now):
            return "daily loss limit hit"
        stale = {k: v for k, v in feed_ages.items() if v > self.cfg.max_feed_age_s}
        if stale:
            return "stale data: " + ", ".join(f"{k}={v:.1f}s" for k, v in stale.items())
        return None

    # --------------------------------------------------------------- check --
    def check(
        self,
        cost_usd: float,
        exposure: float,
        balance: float,
        positions_in_market: int,
        feed_ages: dict[str, float],
        now: float | None = None,
    ) -> RiskDecision:
        reason = self.halt_reason(feed_ages, now)
        if reason:
            return RiskDecision(False, reason)
        eps = 1e-6
        if cost_usd <= 0:
            return RiskDecision(False, "zero size")
        if cost_usd > self.cfg.max_position_usd + eps:
            return RiskDecision(False, f"size {cost_usd:.2f} > max_position_usd")
        if exposure + cost_usd > self.cfg.max_total_exposure_usd + eps:
            return RiskDecision(False, "max total exposure reached")
        if cost_usd > balance + eps:
            return RiskDecision(False, "insufficient balance")
        if positions_in_market >= self.cfg.max_positions_per_market:
            return RiskDecision(False, "already positioned in market")
        return RiskDecision(True)
