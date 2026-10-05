import time

import pytest

from polybot.bot import Bot
from polybot.config import Config, PolymarketConfig, RiskConfig, StorageConfig
from polybot.execution.paper import PaperExecutor
from polybot.models import Book, Level, Market, MarketKind, Position, Signal
from polybot.portfolio import Portfolio
from polybot.storage import Storage


def market(end):
    return Market("m1", "0x1", "btc-above-100k", "Will the price of Bitcoin be above $100,000?", MarketKind.ABOVE,
                  "Y", "N", "Yes", "No", end, strike=100_000.0)


def test_portfolio_open_settle_and_reload(tmp_path):
    db = str(tmp_path / "t.sqlite3")
    st = Storage(db)
    pf = Portfolio(st, "paper", 1000)
    p = pf.add(Position(None, "m1", "s", "YES", "Y", 40, 0.5, 0.7, time.time(), time.time() + 60, 0.6, 0.08))
    assert pf.exposure == pytest.approx(20.7) and pf.balance == pytest.approx(979.3)
    # restart: open positions come back from SQLite
    pf2 = Portfolio(Storage(db), "paper", 1000)
    assert len(pf2.open) == 1 and pf2.balance == pytest.approx(979.3)
    pf2.settle(pf2.open[0], 1.0)
    assert pf2.realized_pnl == pytest.approx(40 - 20.7)
    assert pf2.balance == pytest.approx(1000 + 19.3) and pf2.exposure == 0
    trades = st.recent("trades")
    assert [t["action"] for t in trades] == ["SETTLE", "BUY"]


async def test_paper_executor_rewalks_current_book(tmp_path):
    pf = Portfolio(Storage(":memory:"), "paper", 1000)
    ex = PaperExecutor(pf, min_edge=0.03)
    m = market(time.time() + 3600)
    sig = Signal(time.time(), "m1", "s", "YES", "Y", 0.70, 0.50, 0.50, 0.0175, 0.18, 40, 100_000, 0.5, 3600, 100_000)
    # book moved: first level gone, second level still profitable, third not
    book = Book("Y", [], [Level(0.55, 20), Level(0.64, 1000), Level(0.69, 1000)], time.time())
    pos = await ex.buy(sig, m, book, max_usd=25)
    assert pos is not None
    assert pos.cost == pytest.approx(25)
    assert 0.55 < pos.avg_price < 0.64
    assert pos.edge_at_entry >= 0.03


def make_bot(tmp_path, **risk):
    cfg = Config(
        polymarket=PolymarketConfig(min_seconds_to_expiry=10),
        risk=RiskConfig(kill_switch_file=str(tmp_path / "KILL"), **risk),
        storage=StorageConfig(sqlite_path=str(tmp_path / "b.sqlite3")),
    )
    bot = Bot(cfg)
    now = time.time()
    m = market(now + 3600)
    bot.poly.markets = {m.market_id: m}
    bot.binance.bid, bot.binance.ask, bot.binance.last_msg_ts = 100_990.0, 101_010.0, now
    bot.poly.books = {
        "Y": Book("Y", [Level(0.50, 500)], [Level(0.52, 500)], now),
        "N": Book("N", [Level(0.46, 500)], [Level(0.48, 500)], now),
    }
    return bot, now


async def test_bot_step_trades_logs_and_respects_one_per_market(tmp_path):
    bot, now = make_bot(tmp_path)
    await bot.step(now)
    assert len(bot.portfolio.open) == 1
    sigs = bot.storage.recent("signals")
    assert sigs[0]["acted"] == 1 and sigs[0]["side"] == "YES"
    await bot.step(now + 0.5)
    assert len(bot.portfolio.open) == 1  # max_positions_per_market
    snap = bot.snapshot()
    assert snap["markets"][0]["fair_yes"] > 0.9 and snap["positions"]


async def test_bot_step_blocked_by_stale_book_and_kill_switch(tmp_path):
    bot, now = make_bot(tmp_path)
    for b in bot.poly.books.values():
        b.received_ts = now - 10
    await bot.step(now)
    assert not bot.portfolio.open
    assert "stale" in bot.storage.recent("signals")[0]["reason"]

    bot2, now2 = make_bot(tmp_path / "k")
    bot2.risk.engage_kill_switch("test")
    await bot2.step(now2)
    assert not bot2.portfolio.open
    assert bot2.halt_reason == "kill switch active"
    assert bot2.storage.recent("events")[0]["kind"] == "kill_switch"


async def test_bot_stale_binance_blocks(tmp_path):
    bot, now = make_bot(tmp_path)
    bot.binance.last_msg_ts = now - 5
    await bot.step(now)
    assert not bot.portfolio.open and "stale" in bot.halt_reason


async def test_daily_loss_halt(tmp_path):
    bot, now = make_bot(tmp_path, daily_loss_limit_usd=10)
    p = bot.portfolio.add(Position(None, "old", "old", "YES", "Z", 100, 0.5, 0, now - 100, now - 50, 0.6, 0.1))
    bot.portfolio.settle(p, 0.0, now)  # -50 realised today
    await bot.step(now)
    assert not bot.portfolio.open and bot.risk.daily_halted(now)
    assert any(e["kind"] == "daily_loss_halt" for e in bot.storage.recent("events"))


def test_live_mode_refused(tmp_path):
    cfg = Config(storage=StorageConfig(sqlite_path=str(tmp_path / "x.sqlite3")))
    cfg.execution.mode = "live"
    with pytest.raises(RuntimeError):
        Bot(cfg)
