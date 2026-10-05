"""Password-protected web dashboard (FastAPI + static page that polls JSON)."""
from __future__ import annotations

import math
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

if TYPE_CHECKING:
    from ..bot import Bot

STATIC = Path(__file__).parent / "static"
MAX_FAILS, FAIL_WINDOW_S = 10, 300


def _clean(o):
    """Make floats JSON-safe (inf/nan -> None)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def create_app(bot: "Bot", username: str, password: str) -> FastAPI:
    if not password:
        raise ValueError("dashboard password required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    security = HTTPBasic(realm="polybot")
    fails: dict[str, deque] = defaultdict(deque)

    def auth(request: Request, creds: HTTPBasicCredentials = Depends(security)) -> None:
        ip = request.client.host if request.client else "?"
        q = fails[ip]
        now = time.time()
        while q and now - q[0] > FAIL_WINDOW_S:
            q.popleft()
        if len(q) >= MAX_FAILS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed logins; try later")
        ok_user = secrets.compare_digest(creds.username.encode(), username.encode())
        ok_pass = secrets.compare_digest(creds.password.encode(), password.encode())
        if not (ok_user and ok_pass):
            q.append(now)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized", headers={"WWW-Authenticate": "Basic"})

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.get("/healthz")
    def healthz():
        # Unauthenticated liveness probe; reveals nothing sensitive.
        return {"ok": True, "feed_age_s": _clean(bot.binance.age())}

    @app.get("/", dependencies=[Depends(auth)])
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state", dependencies=[Depends(auth)])
    def state():
        return JSONResponse(_clean(bot.snapshot()))

    @app.post("/api/kill", dependencies=[Depends(auth)])
    def kill():
        bot.risk.engage_kill_switch("dashboard")
        bot.storage.log_event("WARN", "kill_switch", "engaged from dashboard")
        return {"kill_switch": True}

    return app


async def serve_dashboard(bot: "Bot") -> None:
    import asyncio
    import contextlib

    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # the bot owns SIGINT/SIGTERM handling
            yield

    cfg = bot.cfg.dashboard
    app = create_app(bot, cfg.username, bot.cfg.secrets.dashboard_password or "")
    server = _Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning",
                                    access_log=False, proxy_headers=True))

    async def stop_with_bot():
        await bot.stopped()
        server.should_exit = True

    watcher = asyncio.create_task(stop_with_bot())
    try:
        await server.serve()
    finally:
        watcher.cancel()
