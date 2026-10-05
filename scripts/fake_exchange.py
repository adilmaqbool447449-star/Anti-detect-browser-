#!/usr/bin/env python3
"""Local stand-in for Binance + Polymarket public APIs, for offline end-to-end runs.

  python scripts/fake_exchange.py --ws-port 9001 --http-port 9002 [--drop-every 20]

Serves:
  ws://127.0.0.1:WS/stream          Binance combined stream (bookTicker + aggTrade), random walk
  GET  /api/v3/klines               Binance klines
  GET  /events                      Gamma events (5m up/down series + "bitcoin" tag strike markets)
  GET  /markets/{id}                Gamma market (resolution once expired)
  POST /books                       CLOB order books, priced off a noisy (sometimes wrong) fair value

Then point config at it (see scripts/e2e_config.yaml).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from datetime import datetime, timezone

import uvicorn
import websockets
from fastapi import FastAPI, Request

from polybot.pricing import prob_above

rng = random.Random(42)
SIGMA = 0.6  # annualised vol of the fake walk
STATE = {"price": 100_000.0, "hist": {}}  # hist: minute -> open price
MARKETS: dict[str, dict] = {}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tick() -> None:
    dt = 0.2 / (365 * 24 * 3600)
    STATE["price"] *= math.exp(SIGMA * math.sqrt(dt) * rng.gauss(0, 1))
    minute = int(time.time() // 60 * 60)
    STATE["hist"].setdefault(minute, STATE["price"])


def ensure_markets(now: float) -> None:
    # 2-minute "up/down" windows so a short run sees full lifecycles.
    base = int(now // 120 * 120)
    for start in (base - 120, base, base + 120):
        mid = f"ud{start}"
        if mid not in MARKETS:
            MARKETS[mid] = {"id": mid, "kind": "updown", "start": start, "end": start + 120, "strike": None,
                            "question": "Bitcoin Up or Down - test window", "slug": f"btc-updown-5m-{start}"}
    # strike markets expiring every 3 minutes, strikes around spot
    end = int(now // 180 * 180) + 180
    for off in (-300, -100, 100, 300):
        k = round(STATE["price"] / 100) * 100 + off
        mid = f"ab{end}_{k}"
        if mid not in MARKETS and len([m for m in MARKETS.values() if m["kind"] == "above" and m["end"] == end]) < 4:
            MARKETS[mid] = {"id": mid, "kind": "above", "end": end, "strike": float(k), "start": None,
                            "question": f"Will the price of Bitcoin be above ${k:,} at test time?", "slug": f"bitcoin-above-{k}-{end}"}


def gamma_market(m: dict) -> dict:
    now = time.time()
    closed = now > m["end"] + 5
    out = {
        "id": m["id"], "question": m["question"], "slug": m["slug"], "conditionId": "0x" + m["id"],
        "endDate": iso(m["end"]), "closed": closed, "enableOrderBook": True, "acceptingOrders": not closed,
        "clobTokenIds": json.dumps([m["id"] + "-Y", m["id"] + "-N"]),
        "feesEnabled": True, "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True},
        "orderPriceMinTickSize": 0.01, "orderMinSize": 5,
    }
    if m["kind"] == "updown":
        out["outcomes"] = '["Up", "Down"]'
        out["eventStartTime"] = iso(m["start"])
        out["resolutionSource"] = "https://data.chain.link/streams/btc-usd"
    else:
        out["outcomes"] = '["Yes", "No"]'
    if closed:
        final = m.setdefault("final", STATE["hist"].get(int(m["end"] // 60 * 60), STATE["price"]))
        strike = m["strike"] if m["kind"] == "above" else STATE["hist"].get(int(m["start"] // 60 * 60), final)
        yes = final >= strike if m["kind"] == "updown" else final > strike
        out["outcomePrices"] = json.dumps(["1", "0"] if yes else ["0", "1"])
    return out


def fair_yes(m: dict, now: float) -> float:
    if m["kind"] == "updown":
        if now < m["start"]:
            return 0.5
        k = STATE["hist"].get(int(m["start"] // 60 * 60), STATE["price"])
    else:
        k = m["strike"]
    return prob_above(STATE["price"], k, SIGMA, max(m["end"] - now, 0.0))


def book_for(token: str) -> dict:
    mid_id, side = token.rsplit("-", 1)
    m = MARKETS.get(mid_id)
    now = time.time()
    if m is None:
        return {"asset_id": token, "bids": [], "asks": []}
    f = fair_yes(m, now)
    # market makers are noisy: occasionally quote 5-10c off fair
    noise = m.setdefault("noise", rng.choice([0, 0, 0, 0.06, -0.06, 0.1, -0.1]))
    if rng.random() < 0.02:
        m["noise"] = rng.choice([0, 0, 0, 0.06, -0.06, 0.1, -0.1])
    p = min(0.98, max(0.02, f + noise))
    if side == "N":
        p = 1 - p
    bid, ask = round(max(0.01, p - 0.01), 2), round(min(0.99, p + 0.01), 2)
    return {"market": "0x" + mid_id, "asset_id": token, "timestamp": str(int(now * 1000)), "hash": "x",
            "bids": [{"price": f"{bid - i * 0.01:.2f}", "size": "60"} for i in range(4, -1, -1) if bid - i * 0.01 > 0],
            "asks": [{"price": f"{ask + i * 0.01:.2f}", "size": "60"} for i in range(4, -1, -1) if ask + i * 0.01 < 1],
            "min_order_size": "5", "tick_size": "0.01", "neg_risk": False, "last_trade_price": f"{p:.2f}"}


app = FastAPI()


@app.get("/api/v3/klines")
def klines(symbol: str, interval: str, limit: int = 500, startTime: int | None = None):
    if startTime is not None:
        minute = startTime // 1000
        o = STATE["hist"].get(minute)
        return [] if o is None else [[minute * 1000, f"{o:.2f}", "0", "0", f"{o:.2f}", "0", 0, "0", 0, "0", "0", "0"]]
    rows, p, t0 = [], STATE["price"], int(time.time() // 60 * 60) - 60 * limit
    for i in range(limit):
        p *= math.exp(SIGMA * math.sqrt(60 / (365 * 24 * 3600)) * rng.gauss(0, 1))
        rows.append([(t0 + 60 * i) * 1000, f"{p:.2f}", "0", "0", f"{p:.2f}", "0", 0, "0", 0, "0", "0", "0"])
    return rows


@app.get("/events")
def events(request: Request):
    q = dict(request.query_params)
    now = time.time()
    ensure_markets(now)
    if q.get("series_slug") == "btc-up-or-down-5m":
        sel = [m for m in MARKETS.values() if m["kind"] == "updown" and m["end"] > now]
    elif q.get("tag_slug") == "bitcoin":
        sel = [m for m in MARKETS.values() if m["kind"] == "above" and m["end"] > now]
    else:
        return []
    return [{"slug": m["slug"], "title": m["question"], "endDate": iso(m["end"]), "markets": [gamma_market(m)]} for m in sel]


@app.get("/markets/{mid}")
def market(mid: str):
    return gamma_market(MARKETS[mid])


@app.post("/books")
async def books(request: Request):
    body = await request.json()
    return [book_for(b["token_id"]) for b in body]


async def ws_handler(ws, drop_every: float):
    opened = time.time()
    while True:
        tick()
        p = STATE["price"]
        await ws.send(json.dumps({"stream": "btcusdt@bookTicker", "data": {
            "u": int(time.time() * 1000), "s": "BTCUSDT", "b": f"{p - 0.5:.2f}", "B": "1", "a": f"{p + 0.5:.2f}", "A": "1"}}))
        await ws.send(json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "p": f"{p:.2f}"}}))
        if drop_every and time.time() - opened > drop_every:
            await ws.close()  # exercise reconnect logic
            return
        await asyncio.sleep(0.2)


async def main_async(ws_port: int, http_port: int, drop_every: float):
    async with websockets.serve(lambda ws: ws_handler(ws, drop_every), "127.0.0.1", ws_port):
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=http_port, log_level="warning"))
        await server.serve()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-port", type=int, default=9001)
    ap.add_argument("--http-port", type=int, default=9002)
    ap.add_argument("--drop-every", type=float, default=0)
    a = ap.parse_args()
    asyncio.run(main_async(a.ws_port, a.http_port, a.drop_every))
