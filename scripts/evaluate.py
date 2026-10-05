#!/usr/bin/env python3
"""Summarise paper-trading results from the SQLite log.

  python scripts/evaluate.py [--db data/polybot.sqlite3] [--mode paper] [--days 7]

Reports signal counts, win rate, predicted vs realised edge after fees,
total P&L, max drawdown, and calibration (does the model beat the market?).
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time


def bucket(slug: str) -> str:
    for tag in ("updown-5m", "updown-15m", "updown-1h", "updown-4h"):
        if tag in slug:
            return tag
    return "updown-other" if "updown" in slug or "up-or-down" in slug else "strike"


def max_drawdown(pnls: list[float]) -> float:
    peak = cum = mdd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def summarize(db_path: str, mode: str = "paper", days: float | None = None) -> dict:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    since = time.time() - days * 86400 if days else 0
    sig = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(acted),0) acted, AVG(edge) avg_edge FROM signals WHERE mode=? AND ts>=?",
        (mode, since)).fetchone()
    pos = [dict(r) for r in db.execute(
        "SELECT * FROM positions WHERE mode=? AND status='settled' AND closed_ts>=? ORDER BY closed_ts",
        (mode, since))]
    open_n = db.execute("SELECT COUNT(*) FROM positions WHERE mode=? AND status='open'", (mode,)).fetchone()[0]

    out: dict = {"signals": sig["n"], "signals_acted": sig["acted"],
                 "avg_signal_edge": sig["avg_edge"], "open_positions": open_n, "settled": len(pos)}
    if not pos:
        return out
    wins = [p for p in pos if p["pnl"] > 0]
    pnls = [p["pnl"] for p in pos]
    cost = sum(p["shares"] * p["avg_price"] + p["fees"] for p in pos)
    outcome = [1.0 if p["payout"] and p["payout"] > 0 else 0.0 for p in pos]
    # realised edge per share = payout/share - price - fee/share
    realized_edge = [o - p["avg_price"] - p["fees"] / p["shares"] for o, p in zip(outcome, pos)]
    brier_model = sum((p["fair_at_entry"] - o) ** 2 for p, o in zip(pos, outcome)) / len(pos)
    brier_mkt = sum((p["avg_price"] - o) ** 2 for p, o in zip(pos, outcome)) / len(pos)
    out.update({
        "wins": len(wins),
        "win_rate": len(wins) / len(pos),
        "avg_predicted_edge_after_fees": sum(p["edge_at_entry"] for p in pos) / len(pos),
        "avg_realized_edge_after_fees": sum(realized_edge) / len(pos),
        "fees_paid": sum(p["fees"] for p in pos),
        "total_pnl": sum(pnls),
        "return_on_capital_deployed": sum(pnls) / cost if cost else 0.0,
        "max_drawdown": max_drawdown(pnls),
        "brier_model": brier_model,
        "brier_market": brier_mkt,
        "edge_t_stat": _tstat(realized_edge),
    })
    by: dict[str, list] = {}
    for p in pos:
        by.setdefault(bucket(p["slug"]), []).append(p["pnl"])
    out["by_bucket"] = {k: {"n": len(v), "pnl": sum(v), "win_rate": sum(1 for x in v if x > 0) / len(v)} for k, v in by.items()}
    return out


def _tstat(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return m / math.sqrt(var / len(xs)) if var > 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/polybot.sqlite3")
    ap.add_argument("--mode", default="paper")
    ap.add_argument("--days", type=float)
    a = ap.parse_args()
    s = summarize(a.db, a.mode, a.days)
    f = lambda v, pct=False: "n/a" if v is None else (f"{v:.1%}" if pct else (f"{v:,.4f}" if isinstance(v, float) else str(v)))
    print(f"Signals logged:            {s['signals']}  (acted on: {s['signals_acted']})")
    print(f"Avg signal edge:           {f(s['avg_signal_edge'])}")
    print(f"Open positions:            {s['open_positions']}")
    print(f"Settled positions:         {s['settled']}")
    if not s["settled"]:
        print("\nNo settled positions yet - let the bot run longer.")
        return 0
    print(f"Win rate:                  {f(s['win_rate'], True)}  ({s['wins']}/{s['settled']})")
    print(f"Avg PREDICTED edge (net):  {f(s['avg_predicted_edge_after_fees'])} $/share")
    print(f"Avg REALISED edge (net):   {f(s['avg_realized_edge_after_fees'])} $/share   t-stat {f(s['edge_t_stat'])}")
    print(f"Fees paid:                 ${s['fees_paid']:,.2f}")
    print(f"Total P&L:                 ${s['total_pnl']:,.2f}  ({f(s['return_on_capital_deployed'], True)} of capital deployed)")
    print(f"Max drawdown:              ${s['max_drawdown']:,.2f}")
    print(f"Brier score model/market:  {s['brier_model']:.4f} / {s['brier_market']:.4f}  (lower is better)")
    print("\nBy market type:")
    for k, v in sorted(s["by_bucket"].items()):
        print(f"  {k:<14} n={v['n']:<5} pnl=${v['pnl']:>9,.2f}  win={v['win_rate']:.1%}")
    if s["settled"] < 100:
        print("\nNote: fewer than 100 settled trades; results are not statistically meaningful yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
