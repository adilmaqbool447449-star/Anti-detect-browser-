"""SQLite persistence for signals, trades, positions, equity and events.

Writes are tiny and infrequent, so plain sqlite3 (WAL mode) on the event loop
thread is fine and keeps ordering simple.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .models import Position, Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL, market_id TEXT, slug TEXT, side TEXT, token_id TEXT,
  fair REAL, ask REAL, vwap REAL, fee_per_share REAL, edge REAL, shares REAL,
  spot REAL, sigma REAL, tau_s REAL, strike REAL, acted INTEGER, reason TEXT, mode TEXT
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, mode TEXT, market_id TEXT, slug TEXT, side TEXT, token_id TEXT,
  shares REAL, avg_price REAL, fees REAL, opened_ts REAL, end_ts REAL,
  fair_at_entry REAL, edge_at_entry REAL, status TEXT, payout REAL, pnl REAL, closed_ts REAL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, ts REAL, mode TEXT, action TEXT, position_id INTEGER, market_id TEXT,
  slug TEXT, side TEXT, token_id TEXT, shares REAL, price REAL, fee REAL, cash_flow REAL
);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL, mode TEXT, balance REAL, exposure REAL, realized_pnl REAL, unrealized_pnl REAL, equity REAL
);
CREATE TABLE IF NOT EXISTS events (ts REAL, level TEXT, kind TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status);
"""

_POS_COLS = (
    "mode, market_id, slug, side, token_id, shares, avg_price, fees, opened_ts, end_ts, "
    "fair_at_entry, edge_at_entry, status, payout, pnl, closed_ts"
)


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ writes --
    def log_signal(self, s: Signal, acted: bool, reason: str, mode: str) -> None:
        d = asdict(s)
        cols = list(d) + ["acted", "reason", "mode"]
        self.db.execute(
            f"INSERT INTO signals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [*d.values(), int(acted), reason, mode],
        )
        self.db.commit()

    def open_position(self, p: Position, mode: str) -> int:
        cur = self.db.execute(
            f"INSERT INTO positions ({_POS_COLS}) VALUES ({', '.join('?' * 16)})",
            (mode, p.market_id, p.slug, p.side, p.token_id, p.shares, p.avg_price, p.fees, p.opened_ts,
             p.end_ts, p.fair_at_entry, p.edge_at_entry, p.status, p.payout, p.pnl, p.closed_ts),
        )
        pid = cur.lastrowid
        self.db.execute(
            "INSERT INTO trades (ts, mode, action, position_id, market_id, slug, side, token_id, shares, price, fee, cash_flow)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.opened_ts, mode, "BUY", pid, p.market_id, p.slug, p.side, p.token_id, p.shares, p.avg_price, p.fees, -p.cost),
        )
        self.db.commit()
        return pid

    def settle_position(self, p: Position, mode: str) -> None:
        self.db.execute(
            "UPDATE positions SET status=?, payout=?, pnl=?, closed_ts=? WHERE id=?",
            (p.status, p.payout, p.pnl, p.closed_ts, p.position_id),
        )
        self.db.execute(
            "INSERT INTO trades (ts, mode, action, position_id, market_id, slug, side, token_id, shares, price, fee, cash_flow)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.closed_ts, mode, "SETTLE", p.position_id, p.market_id, p.slug, p.side, p.token_id, p.shares,
             (p.payout or 0) / p.shares if p.shares else 0, 0.0, p.payout),
        )
        self.db.commit()

    def log_equity(self, mode: str, balance: float, exposure: float, realized: float, unrealized: float) -> None:
        self.db.execute(
            "INSERT INTO equity VALUES (?,?,?,?,?,?,?)",
            (time.time(), mode, balance, exposure, realized, unrealized, balance + exposure + unrealized),
        )
        self.db.commit()

    def log_event(self, level: str, kind: str, message: str) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (time.time(), level, kind, message))
        self.db.commit()

    # ------------------------------------------------------------- reads --
    def load_positions(self, mode: str, status: str | None = None) -> list[Position]:
        q, args = "SELECT * FROM positions WHERE mode=?", [mode]
        if status:
            q += " AND status=?"
            args.append(status)
        out = []
        for r in self.db.execute(q + " ORDER BY id", args):
            out.append(Position(
                position_id=r["id"], market_id=r["market_id"], slug=r["slug"], side=r["side"],
                token_id=r["token_id"], shares=r["shares"], avg_price=r["avg_price"], fees=r["fees"],
                opened_ts=r["opened_ts"], end_ts=r["end_ts"], fair_at_entry=r["fair_at_entry"],
                edge_at_entry=r["edge_at_entry"], status=r["status"], payout=r["payout"], pnl=r["pnl"],
                closed_ts=r["closed_ts"],
            ))
        return out

    def realized_pnl(self, mode: str, since_ts: float = 0.0) -> float:
        r = self.db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE mode=? AND status='settled' AND closed_ts>=?",
            (mode, since_ts),
        ).fetchone()
        return float(r[0])

    def recent(self, table: str, limit: int = 50) -> list[dict]:
        assert table in {"signals", "events", "positions", "trades"}
        return [dict(r) for r in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))]

    def equity_curve(self, mode: str, limit: int = 500) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, equity FROM equity WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
