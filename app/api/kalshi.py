"""Read-only Kalshi API client (S4-M1).

Public API only — no authentication required for market data.
Endpoint base: https://external-api.kalshi.com/trade-api/v2

Discovery requires two-level fetching:
  1. GET /events?status=open  → event metadata (no nested markets)
  2. GET /markets?event_ticker={X} → markets for each event

Orderbook response shape:
  {"orderbook_fp": {"yes_dollars": [[price, size], ...], "no_dollars": [...]}}
  Prices are bid prices in ascending order; we sort to descending (best first).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests

from app.core.config import KalshiConfig
from app.models import BookLevel, KalshiMarketRow, KalshiSnapshotRow

_log = logging.getLogger(__name__)

_CIRCUIT_FAIL_LIMIT = 5  # consecutive per-event failures before aborting discover


class KalshiDiscoverAbortedError(Exception):
    """Raised by discover_all_markets when the circuit breaker trips."""

    def __init__(self, consecutive_failures: int) -> None:
        self.consecutive_failures = consecutive_failures
        super().__init__(
            f"Kalshi discover aborted after {consecutive_failures} consecutive failures"
        )


def _is_circuit_breaker_error(exc: Exception) -> bool:
    """Return True for errors that indicate a sustained outage (DNS, connection, 5xx)."""
    if isinstance(exc, requests.HTTPError):
        resp = exc.response
        return resp is not None and resp.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


class KalshiClient:
    def __init__(self, cfg: KalshiConfig) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def get_all_open_events(self, page_size: int = 200) -> list[dict[str, Any]]:
        """Paginate GET /events?status=open via cursor until exhausted."""
        events: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"status": "open", "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/events", params)
            batch = data.get("events", [])
            events.extend(batch)
            cursor = data.get("cursor") or None
            if not cursor or not batch:
                break
        return events

    def get_markets_for_event(self, event_ticker: str) -> list[dict[str, Any]]:
        """GET /markets?event_ticker={ticker} — returns markets for one event."""
        data = self._get("/markets", {"event_ticker": event_ticker, "limit": 200})
        return data.get("markets", [])

    def discover_all_markets(
        self, consecutive_fail_limit: int = _CIRCUIT_FAIL_LIMIT
    ) -> list[tuple[dict[str, Any], str | None]]:
        """Fetch all open events then their markets, with a circuit breaker.

        Iterates every event returned by get_all_open_events() and fetches its
        markets.  Per-event errors that are NOT connection/DNS/5xx (e.g. 404) are
        logged as warnings and skipped.  Connection, DNS, and 5xx errors count
        toward a consecutive-failure counter; if that counter reaches
        consecutive_fail_limit without an intervening success the method raises
        KalshiDiscoverAbortedError so the caller can abort quickly rather than
        grinding through tens of thousands of failing requests.

        Returns a flat list of (market_raw, category) tuples for all successfully
        fetched markets.
        """
        events = self.get_all_open_events()
        results: list[tuple[dict[str, Any], str | None]] = []
        consecutive_failures = 0
        for event in events:
            event_ticker = event.get("event_ticker", "")
            category: str | None = event.get("category")
            try:
                markets = self.get_markets_for_event(event_ticker)
                consecutive_failures = 0
                results.extend((m, category) for m in markets)
            except Exception as exc:
                if _is_circuit_breaker_error(exc):
                    consecutive_failures += 1
                    if consecutive_failures >= consecutive_fail_limit:
                        raise KalshiDiscoverAbortedError(consecutive_failures) from exc
                _log.warning(
                    "kalshi_discover_event_error",
                    extra={"event_ticker": event_ticker, "error": str(exc)},
                )
        return results

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """GET /markets/{ticker}/orderbook."""
        return self._get(f"/markets/{ticker}/orderbook", {})

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_market_row(
        self,
        market_raw: dict[str, Any],
        category: str | None,
        synced_at: str,
    ) -> KalshiMarketRow:
        """Convert raw market dict from /markets endpoint to KalshiMarketRow."""
        vol_raw = market_raw.get("volume_fp")
        yes_bid_raw = market_raw.get("yes_bid_dollars")
        end_date = market_raw.get("close_time") or market_raw.get("expiration_time")
        return KalshiMarketRow(
            ticker=market_raw["ticker"],
            event_ticker=market_raw["event_ticker"],
            title=market_raw.get("title") or "",
            category=category,
            status=market_raw.get("status") or "active",
            end_date=end_date,
            yes_bid=Decimal(str(yes_bid_raw)) if yes_bid_raw is not None else None,
            volume=Decimal(str(vol_raw)) if vol_raw is not None else None,
            taker_fee_coeff=Decimal("0.07"),
            synced_at=synced_at,
        )

    def parse_and_validate_snapshot(
        self,
        ticker: str,
        raw: dict[str, Any],
    ) -> KalshiSnapshotRow | None:
        """Parse orderbook_fp and validate.

        Three states:
          (a) valid: yes_bid + no_bid <= 1.01 — return KalshiSnapshotRow
          (b) crossed: yes_bid + no_bid > 1.01 — log ERROR, return None
          (c) single-sided: one side empty — log INFO, return with single_sided=True
        """
        ob = raw.get("orderbook_fp") or {}
        yes_raw: list[list[str]] = ob.get("yes_dollars") or []
        no_raw: list[list[str]] = ob.get("no_dollars") or []

        yes_bids = sorted(
            self._parse_levels(yes_raw),
            key=lambda x: x.price,
            reverse=True,
        )
        no_bids = sorted(
            self._parse_levels(no_raw),
            key=lambda x: x.price,
            reverse=True,
        )

        best_yes_bid = yes_bids[0].price if yes_bids else None
        best_no_bid = no_bids[0].price if no_bids else None

        # State (b): crossed book
        if best_yes_bid is not None and best_no_bid is not None:
            if best_yes_bid + best_no_bid > Decimal("1.01"):
                _log.error(
                    "kalshi_crossed_book",
                    extra={
                        "ticker": ticker,
                        "yes_bid": str(best_yes_bid),
                        "no_bid": str(best_no_bid),
                    },
                )
                return None

        # State (c): single-sided
        single_sided = best_yes_bid is None or best_no_bid is None
        if single_sided:
            _log.info("kalshi_single_sided_book", extra={"ticker": ticker})

        best_yes_ask = (Decimal("1") - best_no_bid) if best_no_bid is not None else None
        best_no_ask = (Decimal("1") - best_yes_bid) if best_yes_bid is not None else None

        ts = datetime.now(tz=timezone.utc).isoformat()
        return KalshiSnapshotRow(
            ticker=ticker,
            ts=ts,
            yes_bids=yes_bids,
            no_bids=no_bids,
            best_yes_ask=best_yes_ask,
            best_no_ask=best_no_ask,
            single_sided=single_sided,
        )

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        # Enforce caller-configured rate limit between every outgoing request.
        time.sleep(1.0 / self._cfg.requests_per_second)

        url = self._cfg.base_url.rstrip("/") + path
        for attempt in range(self._cfg.max_retries + 1):
            try:
                resp = self._session.get(
                    url, params=params, timeout=self._cfg.timeout_sec
                )
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
            except requests.HTTPError as exc:
                # 429 Too Many Requests: raise immediately — retrying would
                # only deepen the rate-limit hole. Callers should skip and move on.
                if exc.response is not None and exc.response.status_code == 429:
                    _log.warning(
                        "kalshi_rate_limited",
                        extra={"path": path, "status": 429},
                    )
                    raise
                if attempt == self._cfg.max_retries:
                    raise
                backoff = self._cfg.backoff_factor * (2 ** attempt)
                _log.warning(
                    "kalshi_http_retry",
                    extra={"path": path, "attempt": attempt + 1, "backoff": backoff},
                )
                time.sleep(backoff)
            except requests.RequestException as exc:
                if attempt == self._cfg.max_retries:
                    raise
                backoff = self._cfg.backoff_factor * (2 ** attempt)
                _log.warning(
                    "kalshi_http_retry",
                    extra={"path": path, "attempt": attempt + 1, "backoff": backoff},
                )
                time.sleep(backoff)
        return {}  # unreachable

    def _parse_levels(self, raw: list[list[str]]) -> list[BookLevel]:
        return [BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in raw]
