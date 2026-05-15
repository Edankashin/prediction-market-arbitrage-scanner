"""CLOB V2 public API client.

Wraps public (no-auth) endpoints:
  POST /books  — bulk orderbook fetch (never falls back to sequential)
  GET  /fee-rate?token_id=  — per-token dynamic taker fee

Uses requests (not httpx) so vcrpy cassettes work in tests.
The ClobClient from py-clob-client-v2 is used only by signing.py.

Per the approved spec: on /books failure, log the error and skip the cycle.
No sequential fallback.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

import requests

from app.core.config import ClobConfig
from app.models import BookLevel, SnapshotRow
from app.utils.helpers import RateLimiter, make_session

_log = logging.getLogger(__name__)

_BOOKS_ENDPOINT = "/books"
_FEE_RATE_ENDPOINT = "/fee-rate"


class ClobPublicClient:
    """Read-only client for the Polymarket CLOB V2 public endpoints."""

    def __init__(self, cfg: ClobConfig) -> None:
        self._cfg = cfg
        self._session = make_session(
            max_retries=cfg.max_retries,
            backoff_factor=cfg.backoff_factor,
        )
        self._limiter = RateLimiter(cfg.requests_per_second)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_order_books(self, token_ids: list[str]) -> list[SnapshotRow]:
        """Fetch orderbooks for multiple tokens in one POST /books call.

        Returns a SnapshotRow per token with ts=now.
        On any failure: logs at WARNING and returns an empty list (no fallback).
        """
        if not token_ids:
            return []

        ts = _now_ts()
        payload = [{"token_id": tid} for tid in token_ids]

        self._limiter.wait()
        try:
            resp = self._session.post(
                f"{self._cfg.host}{_BOOKS_ENDPOINT}",
                json=payload,
                timeout=self._cfg.timeout_sec,
            )
            resp.raise_for_status()
            raw_list: list[dict[str, Any]] = resp.json()
        except requests.RequestException as exc:
            _log.warning(
                "clob_books_failed",
                extra={"token_count": len(token_ids), "error": str(exc)},
            )
            return []

        return [self._parse_book(raw, ts) for raw in raw_list if raw]

    def get_fee_rate_bps(self, token_id: str) -> int:
        """Fetch taker fee rate in basis points from /fee-rate endpoint.

        Returns 0 if the call fails (conservative: assumes no fee).
        """
        self._limiter.wait()
        for attempt in range(self._cfg.max_retries):
            try:
                resp = self._session.get(
                    f"{self._cfg.host}{_FEE_RATE_ENDPOINT}",
                    params={"token_id": token_id},
                    timeout=self._cfg.timeout_sec,
                )
                if resp.status_code == 429:
                    delay = min(
                        float(resp.headers.get("Retry-After",
                                               self._cfg.backoff_factor * (2 ** attempt))),
                        30.0,
                    )
                    _log.warning(
                        "clob_fee_rate_limited",
                        extra={"attempt": attempt, "delay_s": delay},
                    )
                    if attempt < self._cfg.max_retries - 1:
                        time.sleep(delay)
                        continue
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                return int(data.get("base_fee") or 0)
            except requests.RequestException as exc:
                if attempt == self._cfg.max_retries - 1:
                    _log.warning(
                        "clob_fee_rate_failed",
                        extra={"token_id": token_id, "error": str(exc)},
                    )
                    return 0
                time.sleep(
                    min(self._cfg.backoff_factor * (2 ** attempt), 30.0)
                )
        return 0

    # ------------------------------------------------------------------
    # Internal parsing
    # ------------------------------------------------------------------

    def _parse_book(self, raw: dict[str, Any], ts: str) -> SnapshotRow:
        """Convert a raw /books response entry to a SnapshotRow."""
        token_id: str = raw.get("asset_id") or raw.get("token_id") or ""
        condition_id: str = raw.get("market") or ""

        bids = _parse_levels(raw.get("bids", []))
        asks = _parse_levels(raw.get("asks", []))

        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        mid: Decimal | None = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / Decimal("2")

        return SnapshotRow(
            token_id=token_id,
            condition_id=condition_id,
            ts=ts,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            taker_fee_bps=None,  # populated by fees.py before storage
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_levels(raw: list[dict[str, Any]]) -> list[BookLevel]:
    levels = []
    for item in raw:
        try:
            price = Decimal(str(item.get("price", "0")))
            size = Decimal(str(item.get("size", "0")))
            if price > 0 and size > 0:
                levels.append(BookLevel(price=price, size=size))
        except Exception:
            continue
    return levels


def _now_ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()
