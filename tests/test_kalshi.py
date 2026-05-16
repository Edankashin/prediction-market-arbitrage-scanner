"""Tests for app/api/kalshi.py — offline parsing and mocked HTTP.

No network, no DB.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.api.kalshi import KalshiClient
from app.core.config import KalshiConfig
from app.models import KalshiMarketRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> KalshiClient:
    cfg = MagicMock(spec=KalshiConfig)
    cfg.base_url = "https://external-api.kalshi.com/trade-api/v2"
    cfg.timeout_sec = 15
    cfg.max_retries = 0  # no retries in tests
    cfg.backoff_factor = 0.0
    cfg.kalshi_top = 100
    return KalshiClient(cfg)


def _fake_market_raw(
    ticker: str = "KXELONMARS-99",
    event_ticker: str = "KXELONMARS",
    status: str = "active",
    volume_fp: str | None = "12345.50",
    yes_bid_dollars: str | None = "0.0300",
) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "title": "Will Elon Musk visit Mars before Aug 1, 2099?",
        "status": status,
        "close_time": "2099-08-01T00:00:00Z",
        "expiration_time": "2099-08-01T00:00:00Z",
        "volume_fp": volume_fp,
        "yes_bid_dollars": yes_bid_dollars,
        "no_bid_dollars": "0.9600",
        "yes_ask_dollars": "0.0400",
        "no_ask_dollars": "0.9700",
    }


# ---------------------------------------------------------------------------
# parse_market_row
# ---------------------------------------------------------------------------

class TestParseMarketRow:
    def test_full_row(self) -> None:
        """All fields populated in the raw dict → KalshiMarketRow with correct values."""
        raw = _fake_market_raw()
        row = _client().parse_market_row(raw, category="World", synced_at="2026-05-16T00:00:00+00:00")
        assert isinstance(row, KalshiMarketRow)
        assert row.ticker == "KXELONMARS-99"
        assert row.event_ticker == "KXELONMARS"
        assert row.category == "World"
        assert row.status == "active"
        assert row.volume == Decimal("12345.50")
        assert row.yes_bid == Decimal("0.0300")
        assert row.taker_fee_coeff == Decimal("0.07")
        assert row.end_date == "2099-08-01T00:00:00Z"

    def test_no_volume(self) -> None:
        """volume_fp=None → volume=None."""
        raw = _fake_market_raw(volume_fp=None)
        row = _client().parse_market_row(raw, category=None, synced_at="2026-05-16T00:00:00+00:00")
        assert row.volume is None

    def test_no_yes_bid(self) -> None:
        """yes_bid_dollars=None → yes_bid=None (illiquid market)."""
        raw = _fake_market_raw(yes_bid_dollars=None)
        row = _client().parse_market_row(raw, category=None, synced_at="2026-05-16T00:00:00+00:00")
        assert row.yes_bid is None

    def test_taker_fee_coeff_always_007(self) -> None:
        """taker_fee_coeff is always 0.07 regardless of market type."""
        raw = _fake_market_raw()
        row = _client().parse_market_row(raw, category="Politics", synced_at="2026-05-16T00:00:00+00:00")
        assert row.taker_fee_coeff == Decimal("0.07")

    def test_category_none(self) -> None:
        raw = _fake_market_raw()
        row = _client().parse_market_row(raw, category=None, synced_at="2026-05-16T00:00:00+00:00")
        assert row.category is None

    def test_status_preserved(self) -> None:
        raw = _fake_market_raw(status="closed")
        row = _client().parse_market_row(raw, category=None, synced_at="2026-05-16T00:00:00+00:00")
        assert row.status == "closed"


# ---------------------------------------------------------------------------
# get_all_open_events — mocked HTTP
# ---------------------------------------------------------------------------

class TestGetAllOpenEvents:
    def _make_response(self, events: list, cursor: str | None = None) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"events": events, "cursor": cursor}
        return resp

    def _sample_event(self, ticker: str) -> dict:
        return {
            "event_ticker": ticker,
            "category": "World",
            "title": f"Event {ticker}",
        }

    def test_single_page(self) -> None:
        """Single page of results (no cursor) returns all events."""
        events = [self._sample_event(f"EVT-{i}") for i in range(3)]
        resp = self._make_response(events, cursor=None)

        with patch("requests.Session.get", return_value=resp):
            result = _client().get_all_open_events()

        assert len(result) == 3
        assert result[0]["event_ticker"] == "EVT-0"

    def test_pagination_via_cursor(self) -> None:
        """Two pages: first returns cursor, second returns no cursor → 5 total events."""
        page1 = [self._sample_event(f"EVT-{i}") for i in range(3)]
        page2 = [self._sample_event(f"EVT-{i}") for i in range(3, 5)]
        resp1 = self._make_response(page1, cursor="cursor_abc")
        resp2 = self._make_response(page2, cursor=None)

        with patch("requests.Session.get", side_effect=[resp1, resp2]):
            result = _client().get_all_open_events()

        assert len(result) == 5
        tickers = [e["event_ticker"] for e in result]
        assert tickers == [f"EVT-{i}" for i in range(5)]

    def test_empty_response(self) -> None:
        """Empty events list returns empty list."""
        resp = self._make_response([], cursor=None)
        with patch("requests.Session.get", return_value=resp):
            result = _client().get_all_open_events()
        assert result == []


# ---------------------------------------------------------------------------
# get_markets_for_event — mocked HTTP
# ---------------------------------------------------------------------------

class TestGetMarketsForEvent:
    def test_returns_markets_list(self) -> None:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "markets": [_fake_market_raw("EVT-1-YES"), _fake_market_raw("EVT-1-NO")],
            "cursor": None,
        }
        with patch("requests.Session.get", return_value=resp):
            result = _client().get_markets_for_event("EVT-1")
        assert len(result) == 2

    def test_returns_empty_when_no_markets(self) -> None:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"markets": [], "cursor": None}
        with patch("requests.Session.get", return_value=resp):
            result = _client().get_markets_for_event("EVT-EMPTY")
        assert result == []
