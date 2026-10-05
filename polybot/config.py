"""Configuration loading.

Non-secret settings come from config.yaml. Secrets come ONLY from environment
variables (loaded from .env). Secrets are never stored on the config object's
repr and are registered with the logging redaction filter.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

SECRET_ENV_VARS = (
    "DASHBOARD_PASSWORD",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)


@dataclass
class BinanceConfig:
    ws_url: str = "wss://data-stream.binance.vision/stream"
    rest_url: str = "https://data-api.binance.vision"
    symbol: str = "BTCUSDT"
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 60.0
    # Binance drops every connection at 24h; reconnect proactively a bit earlier.
    max_connection_age_s: float = 23 * 3600


@dataclass
class PolymarketConfig:
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    max_markets: int = 50
    scan_interval_s: float = 30.0
    book_poll_interval_s: float = 1.0
    # Recurring "Bitcoin Up or Down" series to pull explicitly (events endpoint).
    series_slugs: list[str] = field(
        default_factory=lambda: ["btc-up-or-down-5m", "btc-up-or-down-15m"]
    )
    max_events_per_series: int = 4
    # Generic tag search for strike markets ("above $X", "between", "reach").
    tag_slugs: list[str] = field(default_factory=lambda: ["bitcoin"])
    max_horizon_hours: float = 48.0
    min_seconds_to_expiry: float = 20.0
    # Fallback fee schedule if a market does not publish feeSchedule.
    default_fee_rate: float = 0.07
    default_fee_exponent: float = 1.0


@dataclass
class ModelConfig:
    vol_floor: float = 0.20  # annualised
    vol_cap: float = 2.50
    kline_vol_window_min: int = 120
    short_vol_window_s: int = 900
    short_vol_weight: float = 0.5
    # Binance BTCUSDT is not the resolution source for every market
    # (5m/15m Up/Down settle on Chainlink BTC/USD). Model that basis as extra
    # terminal noise in basis points (1 sd).
    basis_uncertainty_bps: float = 5.0


@dataclass
class SignalConfig:
    min_edge: float = 0.03  # probability points, AFTER fees and spread
    min_price: float = 0.03
    max_price: float = 0.97
    signal_log_cooldown_s: float = 30.0


@dataclass
class RiskConfig:
    starting_balance_usd: float = 1000.0
    max_position_usd: float = 25.0
    max_total_exposure_usd: float = 200.0
    max_positions_per_market: int = 1
    daily_loss_limit_usd: float = 50.0
    max_feed_age_s: float = 3.0
    kill_switch_file: str = "data/KILL"


@dataclass
class ExecutionConfig:
    mode: str = "paper"  # "paper" | "live"
    live_trading_enabled: bool = False
    paper_extra_slippage: float = 0.0  # per share, added to simulated fills
    settle_poll_interval_s: float = 30.0


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    username: str = "admin"


@dataclass
class StorageConfig:
    sqlite_path: str = "data/polybot.sqlite3"


@dataclass
class Secrets:
    """Holds secrets read from the environment. repr never shows values."""

    dashboard_password: str | None = field(default=None, repr=False)
    polymarket_private_key: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Secrets(<redacted>)"


@dataclass
class Config:
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    secrets: Secrets = field(default_factory=Secrets)


def _build(cls, data: dict[str, Any] | None):
    data = data or {}
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys in config.yaml: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | os.PathLike = "config.yaml", env_file: str | None = ".env") -> Config:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    raw: dict[str, Any] = {}
    if Path(path).exists():
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    if "secrets" in raw:
        raise ValueError("Secrets must not be placed in config.yaml; use .env")
    cfg = Config(
        binance=_build(BinanceConfig, raw.get("binance")),
        polymarket=_build(PolymarketConfig, raw.get("polymarket")),
        model=_build(ModelConfig, raw.get("model")),
        signals=_build(SignalConfig, raw.get("signals")),
        risk=_build(RiskConfig, raw.get("risk")),
        execution=_build(ExecutionConfig, raw.get("execution")),
        dashboard=_build(DashboardConfig, raw.get("dashboard")),
        storage=_build(StorageConfig, raw.get("storage")),
        secrets=Secrets(
            dashboard_password=os.environ.get("DASHBOARD_PASSWORD") or None,
            polymarket_private_key=os.environ.get("POLYMARKET_PRIVATE_KEY") or None,
        ),
    )
    # Container deployments bind inside the container; the host port mapping
    # decides real exposure (see docker-compose.yml).
    if host := os.environ.get("POLYBOT_DASHBOARD_HOST"):
        cfg.dashboard.host = host
    if cfg.execution.mode not in ("paper", "live"):
        raise ValueError("execution.mode must be 'paper' or 'live'")
    return cfg


class RedactSecretsFilter(logging.Filter):
    """Replaces any secret env-var value that appears in a log line."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets = [
            v for k in SECRET_ENV_VARS if (v := os.environ.get(k)) and len(v) >= 4
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self._secrets:
            redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactSecretsFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
