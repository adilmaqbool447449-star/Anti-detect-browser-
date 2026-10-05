import asyncio
import json
import time

import pytest
import websockets

from polybot.config import BinanceConfig, ModelConfig
from polybot.feeds.binance import BinanceFeed


def test_parses_combined_stream_messages():
    f = BinanceFeed(BinanceConfig(), ModelConfig())
    assert f.price is None and f.age() == float("inf")
    f.handle_message(json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "p": "62000.5"}}), now=100.0)
    assert f.price == 62000.5
    f.handle_message(json.dumps({"stream": "btcusdt@bookTicker",
                                 "data": {"u": 1, "s": "BTCUSDT", "b": "62000", "B": "1", "a": "62002", "A": "1"}}), now=101.0)
    assert f.price == 62001.0
    assert f.age(now=104.0) == pytest.approx(3.0)
    # subscription acks / unknown payloads do not refresh freshness
    f.handle_message(json.dumps({"result": None, "id": 1}), now=200.0)
    assert f.last_msg_ts == 101.0


def test_sigma_respects_floor_without_data():
    m = ModelConfig(vol_floor=0.3)
    f = BinanceFeed(BinanceConfig(), m)
    assert f.sigma == 0.3


async def test_reconnects_after_server_drop():
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await ws.send(json.dumps({"stream": "x", "data": {"u": 1, "b": "100", "a": "102", "B": "1", "A": "1"}}))
        if connections == 1:
            await ws.close()  # simulate a drop
            return
        await asyncio.sleep(1)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = BinanceConfig(ws_url=f"ws://127.0.0.1:{port}/stream", reconnect_min_s=0.05, reconnect_max_s=0.2)
        feed = BinanceFeed(cfg, ModelConfig())
        task = asyncio.create_task(feed.run())
        t0 = time.time()
        while connections < 2 and time.time() - t0 < 5:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        feed.stop()
        task.cancel()
        assert connections >= 2
        assert feed.reconnects >= 1
        assert feed.price == 101.0
