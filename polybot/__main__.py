"""Entry point: python -m polybot [--config config.yaml]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .config import load_config, setup_logging

log = logging.getLogger("polybot")


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC: Binance vs Polymarket mispricing bot")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    setup_logging(args.log_level)
    cfg = load_config(args.config, args.env_file)

    if cfg.execution.mode == "live" or cfg.execution.live_trading_enabled:
        log.error("Live trading is not implemented in this build. Set execution.mode: paper.")
        return 2
    if cfg.dashboard.enabled and not cfg.secrets.dashboard_password:
        log.error("DASHBOARD_PASSWORD is not set in .env; refusing to start an unprotected dashboard.")
        return 2

    from .bot import Bot

    bot = Bot(cfg)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop)
        await bot.run()

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    sys.exit(main())
