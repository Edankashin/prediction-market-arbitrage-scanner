"""Tests for app/api/kalshi.py — offline parsing and mocked HTTP.

No network, no DB.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests.exceptions

from app.api.kalshi import KalshiClient, KalshiDiscoverAbortedError
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
    cfg.requests_per_second = 999.0  # effectively no sleep in tests
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


# ---------------------------------------------------------------------------
# discover_all_markets — circuit breaker
# ---------------------------------------------------------------------------

def _make_events(n: int) -> list[dict]:
    return [{"event_ticker": f"EVT-{i}", "category": "Sports"} for i in range(n)]


def _conn_error() -> requests.exceptions.ConnectionError:
    return requests.exceptions.ConnectionError("DNS failure")


def _timeout_error() -> requests.exceptions.Timeout:
    return requests.exceptions.Timeout("timed out")


def _http_5xx() -> requests.exceptions.HTTPError:
    resp = MagicMock()
    resp.status_code = 503
    return requests.exceptions.HTTPError(response=resp)


class TestDiscoverAllMarkets:
    def _setup(self, client: KalshiClient, events: list[dict], market_side_effects: list):
        """Patch get_all_open_events and get_markets_for_event on the client."""
        client.get_all_open_events = MagicMock(return_value=events)  # type: ignore[method-assign]
        client.get_markets_for_event = MagicMock(side_effect=market_side_effects)  # type: ignore[method-assign]

    def test_four_consecutive_failures_then_success_no_abort(self) -> None:
        """4 consecutive circuit-breaker errors followed by 1 success → no abort,
        results contain markets from the successful event only."""
        client = _client()
        events = _make_events(5)
        markets_for_evt4 = [_fake_market_raw(f"MKT-{i}") for i in range(2)]
        self._setup(client, events, [
            _conn_error(),
            _conn_error(),
            _conn_error(),
            _conn_error(),
            markets_for_evt4,  # 5th succeeds
        ])

        result = client.discover_all_markets(consecutive_fail_limit=5)

        assert len(result) == 2
        assert result[0][1] == "Sports"  # category preserved
        assert client.get_markets_for_event.call_count == 5

    def test_five_consecutive_failures_raises_abort(self) -> None:
        """5 consecutive circuit-breaker errors → KalshiDiscoverAbortedError."""
        client = _client()
        events = _make_events(10)
        self._setup(client, events, [_conn_error()] * 10)

        with pytest.raises(KalshiDiscoverAbortedError) as exc_info:
            client.discover_all_markets(consecutive_fail_limit=5)

        assert exc_info.value.consecutive_failures == 5
        assert client.get_markets_for_event.call_count == 5

    def test_mixed_error_types_count_toward_same_threshold(self) -> None:
        """ConnectionError, Timeout, and HTTPError(5xx) all increment the same counter."""
        client = _client()
        events = _make_events(10)
        self._setup(client, events, [
            _conn_error(),
            _timeout_error(),
            _http_5xx(),
            _conn_error(),
            _conn_error(),
        ])

        with pytest.raises(KalshiDiscoverAbortedError) as exc_info:
            client.discover_all_markets(consecutive_fail_limit=5)

        assert exc_info.value.consecutive_failures == 5

    def test_success_then_failure_tail_aborts_mid_pagination(self) -> None:
        """3 successful events then 5 consecutive failures → abort mid-run,
        partial results from the successful events are discarded (exception raised)."""
        client = _client()
        events = _make_events(8)
        success_markets = [[_fake_market_raw(f"MKT-{e}-0")] for e in range(3)]
        fail_side_effects = [_conn_error()] * 5
        self._setup(client, events, [*success_markets, *fail_side_effects])

        with pytest.raises(KalshiDiscoverAbortedError) as exc_info:
            client.discover_all_markets(consecutive_fail_limit=5)

        assert exc_info.value.consecutive_failures == 5
        assert client.get_markets_for_event.call_count == 8  # 3 success + 5 failures
