"""Gamma metadata API client.

Fetches market and event metadata from https://gamma-api.polymarket.com.
Rate-limited to 1 req/sec. All calls carry a 15-second timeout.
No private key or auth — view-only.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

import requests

from app.core.config import GammaConfig
from app.models import MarketRow
from app.utils.helpers import RateLimiter, make_session

_log = logging.getLogger(__name__)


class GammaClient:
    """Read-only client for the Polymarket Gamma API."""

    def __init__(self, cfg: GammaConfig) -> None:
        self._cfg = cfg
        self._session = make_session(
            max_retries=cfg.max_retries,
            backoff_factor=cfg.backoff_factor,
        )
        self._limiter = RateLimiter(cfg.requests_per_second)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_markets(
        self,
        limit: int = 100,
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch a page of markets from the Gamma /markets endpoint."""
        return self._get(  # type: ignore[return-value]
            "/markets",
            params={
                "limit": limit,
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "archived": str(archived).lower(),
                "offset": offset,
            },
        )

    def get_all_active_markets(self, page_size: int = 100) -> list[dict[str, Any]]:
        """Paginate through all active markets.

        Stops when a page returns fewer results than requested (normal end-of-
        data) or when the API responds with 422 (offset beyond available rows).
        """
        import requests as _requests

        all_markets: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                batch = self.get_markets(
                    limit=page_size, active=True, closed=False,
                    archived=False, offset=offset,
                )
            except _requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 422:
                    _log.info(
                        "gamma_pagination_end",
                        extra={"offset": offset, "total": len(all_markets)},
                    )
                    break
                raise
            all_markets.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        _log.info("gamma_discover_complete", extra={"total": len(all_markets)})
        return all_markets

    def get_events(
        self,
        limit: int = 100,
        active: bool = True,
        closed: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch events (which contain nested markets)."""
        return self._get(  # type: ignore[return-value]
            "/events",
            params={
                "limit": limit,
                "active": str(active).lower(),
                "closed": str(closed).lower(),
            },
        )

    def parse_market_row(self, raw: dict[str, Any], synced_at: str) -> MarketRow:
        """Convert a raw Gamma market dict to a MarketRow.

        Handles both flat payloads (from /markets) and nested event payloads.
        Robust to missing or null fields.
        """
        event_id, event_slug, event_title = self._extract_event_reference(raw)

        clob_ids = raw.get("clobTokenIds", "[]")
        if isinstance(clob_ids, list):
            clob_ids = json.dumps([str(t) for t in clob_ids])

        outcome_prices = raw.get("outcomePrices", None)
        if isinstance(outcome_prices, list):
            outcome_prices = json.dumps([str(p) for p in outcome_prices])

        outcomes = raw.get("outcomes", None)
        if isinstance(outcomes, list):
            outcomes = json.dumps(outcomes)

        fee_schedule = raw.get("feeSchedule")
        fee_formula: str | None = json.dumps(fee_schedule) if fee_schedule else None
        taker_cap: int | None = None
        maker_cap: int | None = None
        if isinstance(fee_schedule, dict):
            taker_cap = _int_or_none(fee_schedule.get("maxFee") or fee_schedule.get("takerBaseFee"))
            maker_cap = _int_or_none(fee_schedule.get("makerBaseFee"))

        return MarketRow(
            condition_id=str(raw.get("conditionId") or raw.get("id") or ""),
            question=raw.get("question", ""),
            event_id=event_id,
            event_slug=event_slug,
            event_title=event_title,
            neg_risk=bool(raw.get("negRisk", False)),
            active=bool(raw.get("active", True)),
            closed=bool(raw.get("closed", False)),
            archived=bool(raw.get("archived", False)),
            end_date=raw.get("endDate"),
            clob_token_ids=clob_ids,
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            volume=_dec_or_none(raw.get("volume")),
            liquidity=_dec_or_none(raw.get("liquidity")),
            spread=_dec_or_none(raw.get("spread")),
            fees_enabled=bool(raw.get("feesEnabled", True)),
            taker_fee_cap_bps=taker_cap,
            maker_rebate_cap_bps=maker_cap,
            fee_formula=fee_formula,
            gamma_updated_at=raw.get("updatedAt"),
            synced_at=synced_at,
        )

    # ------------------------------------------------------------------
    # Event grouping (adapted from polyterm _extract_event_reference)
    # ------------------------------------------------------------------

    def _extract_event_reference(
        self, market: dict[str, Any]
    ) -> tuple[str | None, str | None, str | None]:
        """Return (event_id, event_slug, event_title) from a flat market dict.

        Handles the varied shapes Gamma returns depending on the endpoint.
        """
        # Shape 1: events array nested under market
        events_list = market.get("events")
        if isinstance(events_list, list) and events_list:
            first = events_list[0]
            if isinstance(first, dict):
                eid = str(first.get("id") or first.get("slug") or "")
                return (
                    eid or None,
                    first.get("slug"),
                    first.get("title"),
                )

        # Shape 2: flat eventId / eventSlug fields
        eid = market.get("eventId") or market.get("event_id")
        eslug = market.get("eventSlug") or market.get("event_slug")
        etitle = market.get("eventTitle") or market.get("event_title")
        if eid:
            return str(eid), eslug, etitle

        # Shape 3: nested event object
        event_obj = market.get("event")
        if isinstance(event_obj, dict):
            eid = str(event_obj.get("id") or event_obj.get("slug") or "")
            return (
                eid or None,
                event_obj.get("slug"),
                event_obj.get("title"),
            )

        return None, None, None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> list[Any] | dict[str, Any]:
        """Rate-limited GET with retry (transport layer handles most retries)."""
        url = f"{self._cfg.base_url}{path}"
        self._limiter.wait()

        for attempt in range(self._cfg.max_retries):
            try:
                resp = self._session.get(
                    url, params=params, timeout=self._cfg.timeout_sec
                )
                if resp.status_code == 429:
                    retry_after = float(
                        resp.headers.get("Retry-After",
                                         self._cfg.backoff_factor * (2 ** attempt))
                    )
                    delay = min(retry_after, 30.0)
                    _log.warning(
                        "gamma_rate_limited",
                        extra={"attempt": attempt, "delay_s": delay},
                    )
                    if attempt < self._cfg.max_retries - 1:
                        time.sleep(delay)
                        continue
                resp.raise_for_status()
                return resp.json()  # type: ignore[return-value]
            except requests.ConnectionError as exc:
                _log.warning(
                    "gamma_connection_error",
                    extra={"attempt": attempt, "error": str(exc)},
                )
                if attempt == self._cfg.max_retries - 1:
                    raise
                time.sleep(min(self._cfg.backoff_factor * (2 ** attempt), 30.0))
            except requests.Timeout as exc:
                _log.warning(
                    "gamma_timeout",
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


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except Exception:
        return None
