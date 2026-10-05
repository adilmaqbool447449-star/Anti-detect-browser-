import importlib.util
import time
from pathlib import Path

import pytest

from polybot.models import Position
from polybot.portfolio import Portfolio
from polybot.storage import Storage

spec = importlib.util.spec_from_file_location("evaluate", Path(__file__).parents[1] / "scripts" / "evaluate.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_max_drawdown():
    assert ev.max_drawdown([5, -3, -4, 2, 10, -1]) == 7
    assert ev.max_drawdown([]) == 0


def test_summarize(tmp_path):
    db = str(tmp_path / "e.sqlite3")
    pf = Portfolio(Storage(db), "paper", 1000)
    now = time.time()
    # win: 10 sh @0.40 + 0.168 fee -> payout 10
    w = pf.add(Position(None, "1", "btc-updown-5m-1", "YES", "a", 10, 0.40, 0.168, now, now, 0.50, 0.08))
    l = pf.add(Position(None, "2", "bitcoin-above-x", "NO", "b", 10, 0.60, 0.168, now, now, 0.70, 0.08))
    pf.settle(w, 1.0, now)
    pf.settle(l, 0.0, now + 1)
    s = ev.summarize(db)
    assert s["settled"] == 2 and s["wins"] == 1 and s["win_rate"] == 0.5
    assert s["total_pnl"] == pytest.approx((10 - 4.168) + (0 - 6.168))
    assert s["max_drawdown"] == pytest.approx(6.168)
    assert s["fees_paid"] == pytest.approx(0.336)
    assert s["avg_realized_edge_after_fees"] == pytest.approx(((1 - 0.4 - 0.0168) + (0 - 0.6 - 0.0168)) / 2)
    assert set(s["by_bucket"]) == {"updown-5m", "strike"}
