"""Polymarket Data API client — read-only, wallet-parameterized.

Fetches on-chain positions, trade history, and portfolio P&L
for any wallet address. No wallet is hardcoded.

Base URL: https://data-api.polymarket.com
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import requests

from app.core.config import DataApiConfig
from app.models import DataApiPositionRow
from app.utils.helpers import RateLimiter, make_session

_log = logging.getLogger(__name__)


class DataApiClient:
    """Read-only client for the Polymarket Data API."""

    def __init__(self, cfg: DataApiConfig) -> None:
        self._cfg = cfg
        self._session = make_session(
            max_retries=cfg.max_retries,
            backoff_factor=cfg.backoff_factor,
        )
        # Data API has no published rate limit; use a conservative 2 req/sec.
        self._limiter = RateLimiter(2.0)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_positions(self, wallet: str) -> list[DataApiPositionRow]:
        """Fetch open positions for a wallet address.

        Returns an empty list on API failure (logged at WARNING).
        """
        raw = self._get("/positions", {"user": wallet, "sizeThreshold": "0.01"})
        if not isinstance(raw, list):
            _log.warning(
                "data_api_positions_unexpected_shape",
                extra={"wallet": wallet, "type": type(raw).__name__},
            )
            return []

        fetched_at = _now_ts()
        rows: list[DataApiPositionRow] = []
        for item in raw:
            row = self._parse_position(item, wallet, fetched_at)
            if row is not None:
                rows.append(row)
        return rows

    def get_activity(
        self, wallet: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch recent trade activity for a wallet (raw dicts, no model)."""
        raw = self._get("/activity", {"user": wallet, "limit": limit})
        if not isinstance(raw, list):
            _log.warning(
                "data_api_activity_unexpected_shape",
                extra={"wallet": wallet},
            )
            return []
        return raw  # type: ignore[return-value]

    def get_portfolio(self, wallet: str) -> dict[str, Any]:
        """Fetch portfolio summary (P&L, realized/unrealized) for a wallet."""
        raw = self._get("/portfolio", {"user": wallet})
        if not isinstance(raw, dict):
            _log.warning(
                "data_api_portfolio_unexpected_shape",
                extra={"wallet": wallet},
            )
            return {}
        return raw  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_position(
        self, item: dict[str, Any], wallet: str, fetched_at: str
    ) -> DataApiPositionRow | None:
        """Parse one position dict into a DataApiPositionRow."""
        token_id = item.get("asset") or item.get("tokenId") or item.get("token_id")
        condition_id = item.get("conditionId") or item.get("market") or ""
        if not token_id:
            return None

        shares = _dec_or_zero(item.get("size") or item.get("shares"))
        avg_price = _dec_or_none(item.get("avgPrice") or item.get("avg_price"))
        realized_pnl = _dec_or_none(item.get("realizedPnl") or item.get("realized_pnl"))
        unrealized_pnl = _dec_or_none(
            item.get("unrealizedPnl") or item.get("unrealized_pnl")
        )
        outcome = item.get("outcome") or item.get("side")

        return DataApiPositionRow(
            wallet=wallet,
            condition_id=str(condition_id),
            token_id=str(token_id),
            outcome=str(outcome) if outcome else None,
            shares=shares,
            avg_price=avg_price,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            fetched_at=fetched_at,
        )

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._cfg.base_url}{path}"
        self._limiter.wait()

        for attempt in range(self._cfg.max_retries):
            try:
                resp = self._session.get(
                    url, params=params, timeout=self._cfg.timeout_sec
                )
                if resp.status_code == 429:
                    delay = min(
                        float(resp.headers.get("Retry-After",
                                               1.0 * (2 ** attempt))),
                        30.0,
                    )
                    _log.warning(
                        "data_api_rate_limited",
                        extra={"attempt": attempt, "delay_s": delay},
                    )
                    if attempt < self._cfg.max_retries - 1:
                        time.sleep(delay)
                        continue
                resp.raise_for_status()
                return resp.json()
            except requests.ConnectionError as exc:
                _log.warning(
                    "data_api_connection_error",
                    extra={"attempt": attempt, "error": str(exc)},
                )
                if attempt == self._cfg.max_retries - 1:
                    raise
                time.sleep(min(1.0 * (2 ** attempt), 30.0))
            except requests.Timeout as exc:
                _log.warning(
                    "data_api_timeout",
                    extra={"attempt": attempt, "error": str(exc)},
                )
                if attempt == self._cfg.max_retries - 1:
                    raise

        raise RuntimeError("retry loop exhausted without returning")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dec_or_none(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v is not None else None
    except Exception:
        return None


def _dec_or_zero(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v is not None else Decimal("0")
    except Exception:
        return Decimal("0")


def _now_ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()
