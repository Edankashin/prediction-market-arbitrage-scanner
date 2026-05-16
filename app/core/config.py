"""Configuration loader.

Reads config.toml using stdlib tomllib (Python 3.11+).
Secrets (private key, API credentials) are loaded from environment variables
via python-dotenv — never from the TOML file.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class GammaConfig:
    base_url: str
    requests_per_second: float
    timeout_sec: int
    max_retries: int
    backoff_factor: float


@dataclass(frozen=True)
class ClobConfig:
    host: str
    requests_per_second: float
    timeout_sec: int
    max_retries: int
    backoff_factor: float
    chain_id: int


@dataclass(frozen=True)
class DataApiConfig:
    base_url: str
    timeout_sec: int
    max_retries: int
    backoff_factor: float


@dataclass(frozen=True)
class DbConfig:
    path: str
    snapshot_retention_days: int


@dataclass(frozen=True)
class FeesConfig:
    cache_ttl_sec: int


@dataclass(frozen=True)
class GasConfig:
    usdc_per_order: float


@dataclass(frozen=True)
class KellyConfig:
    half_kelly_factor: float
    max_position_pct: float


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    log_dir: str


@dataclass(frozen=True)
class SnapshotConfig:
    interval_sec: int


@dataclass(frozen=True)
class KalshiConfig:
    base_url: str
    requests_per_second: float
    timeout_sec: int
    max_retries: int
    backoff_factor: float
    kalshi_top: int  # max markets to snapshot per cycle


@dataclass(frozen=True)
class Config:
    gamma: GammaConfig
    clob: ClobConfig
    data_api: DataApiConfig
    db: DbConfig
    fees: FeesConfig
    gas: GasConfig
    kelly: KellyConfig
    logging: LoggingConfig
    snapshot: SnapshotConfig
    kalshi: KalshiConfig


def load(config_path: str = "config.toml") -> Config:
    """Load config.toml and .env, return a frozen Config.

    Call once at program startup; pass the returned Config to all components.
    """
    load_dotenv()

    path = Path(config_path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    g = raw["gamma"]
    c = raw["clob"]
    d = raw["data_api"]
    db = raw["db"]
    f = raw["fees"]
    gas = raw["gas"]
    k = raw["kelly"]
    lo = raw["logging"]
    sn = raw["snapshot"]
    ka = raw["kalshi"]

    return Config(
        gamma=GammaConfig(
            base_url=g["base_url"],
            requests_per_second=float(g["requests_per_second"]),
            timeout_sec=int(g["timeout_sec"]),
            max_retries=int(g["max_retries"]),
            backoff_factor=float(g["backoff_factor"]),
        ),
        clob=ClobConfig(
            host=c["host"],
            requests_per_second=float(c["requests_per_second"]),
            timeout_sec=int(c["timeout_sec"]),
            max_retries=int(c["max_retries"]),
            backoff_factor=float(c["backoff_factor"]),
            chain_id=int(c["chain_id"]),
        ),
        data_api=DataApiConfig(
            base_url=d["base_url"],
            timeout_sec=int(d["timeout_sec"]),
            max_retries=int(d["max_retries"]),
            backoff_factor=float(d["backoff_factor"]),
        ),
        db=DbConfig(
            path=db["path"],
            snapshot_retention_days=int(db["snapshot_retention_days"]),
        ),
        fees=FeesConfig(cache_ttl_sec=int(f["cache_ttl_sec"])),
        gas=GasConfig(usdc_per_order=float(gas["usdc_per_order"])),
        kelly=KellyConfig(
            half_kelly_factor=float(k["half_kelly_factor"]),
            max_position_pct=float(k["max_position_pct"]),
        ),
        logging=LoggingConfig(level=lo["level"], log_dir=lo["log_dir"]),
        snapshot=SnapshotConfig(interval_sec=int(sn["interval_sec"])),
        kalshi=KalshiConfig(
            base_url=ka["base_url"],
            requests_per_second=float(ka["requests_per_second"]),
            timeout_sec=int(ka["timeout_sec"]),
            max_retries=int(ka["max_retries"]),
            backoff_factor=float(ka["backoff_factor"]),
            kalshi_top=int(ka["kalshi_top"]),
        ),
    )


def private_key() -> str:
    """Read the Polygon wallet private key from the environment.

    Only call from signing.py. Never log the returned value.
    """
    key = os.environ.get("POLYGON_WALLET_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError(
            "POLYGON_WALLET_PRIVATE_KEY is not set. "
            "Add it to .env (never commit that file)."
        )
    return key
