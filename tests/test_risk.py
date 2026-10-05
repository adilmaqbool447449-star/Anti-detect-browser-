from datetime import datetime, timezone

import pytest

from polybot.config import RiskConfig
from polybot.risk import RiskManager, utc_day_start

FRESH = {"binance": 0.5, "book": 1.0}
NOON = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def rm(tmp_path):
    return RiskManager(RiskConfig(max_position_usd=25, max_total_exposure_usd=100, daily_loss_limit_usd=50,
                                  max_feed_age_s=3, max_positions_per_market=1,
                                  kill_switch_file=str(tmp_path / "KILL")))


def test_allows_normal_trade(rm):
    assert rm.check(20, 0, 1000, 0, FRESH, NOON).ok


@pytest.mark.parametrize("cost,exposure,balance,in_mkt,reason", [
    (30, 0, 1000, 0, "max_position_usd"),
    (20, 90, 1000, 0, "max total exposure"),
    (20, 0, 10, 0, "insufficient balance"),
    (20, 0, 1000, 1, "already positioned"),
    (0, 0, 1000, 0, "zero size"),
])
def test_rejections(rm, cost, exposure, balance, in_mkt, reason):
    d = rm.check(cost, exposure, balance, in_mkt, FRESH, NOON)
    assert not d.ok and reason in d.reason


def test_exposure_boundary_is_inclusive(rm):
    assert rm.check(25, 75, 1000, 0, FRESH, NOON).ok


def test_stale_feed_blocks_trading(rm):
    d = rm.check(10, 0, 1000, 0, {"binance": 3.5, "book": 0.2}, NOON)
    assert not d.ok and "stale" in d.reason and "binance" in d.reason
    assert not rm.check(10, 0, 1000, 0, {"binance": 0.1, "book": float("inf")}, NOON).ok
    assert rm.check(10, 0, 1000, 0, {"binance": 3.0, "book": 3.0}, NOON).ok


def test_kill_switch_file(rm):
    assert not rm.kill_switch_active()
    rm.engage_kill_switch("test")
    assert rm.kill_switch_active()
    d = rm.check(10, 0, 1000, 0, FRESH, NOON)
    assert not d.ok and d.reason == "kill switch active"
    rm.kill_path.unlink()
    assert rm.check(10, 0, 1000, 0, FRESH, NOON).ok


def test_daily_loss_limit_latches_until_next_utc_day(rm):
    assert rm.update_daily_pnl(-49.99, NOON) is False
    assert rm.check(10, 0, 1000, 0, FRESH, NOON).ok
    assert rm.update_daily_pnl(-50.0, NOON) is True
    assert not rm.check(10, 0, 1000, 0, FRESH, NOON).ok
    # recovery the same day does NOT un-halt
    rm.update_daily_pnl(+20, NOON + 3600)
    assert rm.check(10, 0, 1000, 0, FRESH, NOON + 3600).reason == "daily loss limit hit"
    # next UTC day clears it
    tomorrow = NOON + 86400
    rm.update_daily_pnl(0, tomorrow)
    assert rm.check(10, 0, 1000, 0, FRESH, tomorrow).ok


def test_allowed_usd(rm):
    assert rm.allowed_usd(0, 1000) == 25
    assert rm.allowed_usd(90, 1000) == 10
    assert rm.allowed_usd(100, 1000) == 0
    assert rm.allowed_usd(0, 5) == 5


def test_utc_day_start():
    assert utc_day_start(NOON) == NOON - 12 * 3600
