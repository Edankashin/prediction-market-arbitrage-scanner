"""Tests for ClobPublicClient — parsing and fee rate logic.

HTTP tests would use VCR cassettes. Parsing tests run offline.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.api.clob import ClobPublicClient, _parse_levels
from app.core.config import ClobConfig
from app.models import BookLevel

_CFG = ClobConfig(
    host="https://clob.polymarket.com",
    requests_per_second=4.0,
    timeout_sec=15,
    max_retries=3,
    backoff_factor=1.0,
    chain_id=137,
)


def _client() -> ClobPublicClient:
    return ClobPublicClient(_CFG)


# ---------------------------------------------------------------------------
# _parse_levels
# ---------------------------------------------------------------------------

class TestParseLevels:
    def test_basic(self) -> None:
        raw = [{"price": "0.55", "size": "100"}, {"price": "0.56", "size": "50"}]
        levels = _parse_levels(raw)
        assert len(levels) == 2
        assert levels[0].price == Decimal("0.55")
        assert levels[1].size == Decimal("50")

    def test_zero_size_excluded(self) -> None:
        raw = [{"price": "0.55", "size": "0"}, {"price": "0.56", "size": "100"}]
        levels = _parse_levels(raw)
        assert len(levels) == 1

    def test_zero_price_excluded(self) -> None:
        raw = [{"price": "0", "size": "100"}, {"price": "0.55", "size": "50"}]
        levels = _parse_levels(raw)
        assert len(levels) == 1

    def test_empty_input(self) -> None:
        assert _parse_levels([]) == []

    def test_bad_entry_skipped(self) -> None:
        raw = [{"price": "bad", "size": "100"}, {"price": "0.55", "size": "50"}]
        levels = _parse_levels(raw)
        assert len(levels) == 1


# ---------------------------------------------------------------------------
# _parse_book
# ---------------------------------------------------------------------------

class TestParseBook:
    def _raw_book(self) -> dict[str, object]:
        return {
            "asset_id": "tok_abc",
            "market": "cond_xyz",
            "bids": [{"price": "0.44", "size": "500"}, {"price": "0.43", "size": "300"}],
            "asks": [{"price": "0.45", "size": "400"}, {"price": "0.46", "size": "200"}],
        }

    def test_parse_snapshot(self) -> None:
        client = _client()
        snap = client._parse_book(self._raw_book(), ts="2026-05-14T12:00:00+00:00")
        assert snap.token_id == "tok_abc"
        assert snap.condition_id == "cond_xyz"
        assert snap.best_bid == Decimal("0.44")
        assert snap.best_ask == Decimal("0.45")
        assert snap.mid == Decimal("0.445")
        assert len(snap.bids) == 2
        assert len(snap.asks) == 2

    def test_mid_computed(self) -> None:
        client = _client()
        snap = client._parse_book(self._raw_book(), ts="2026-05-14T12:00:00+00:00")
        expected_mid = (Decimal("0.44") + Decimal("0.45")) / Decimal("2")
        assert snap.mid == expected_mid

    def test_empty_book(self) -> None:
        client = _client()
        raw = {"asset_id": "tok_empty", "market": "cond_empty", "bids": [], "asks": []}
        snap = client._parse_book(raw, "2026-05-14T12:00:00+00:00")
        assert snap.best_bid is None
        assert snap.best_ask is None
        assert snap.mid is None


# ---------------------------------------------------------------------------
# get_order_books (mocked HTTP)
# ---------------------------------------------------------------------------

class TestGetOrderBooks:
    def test_empty_token_list_returns_empty(self) -> None:
        client = _client()
        result = client.get_order_books([])
        assert result == []

    def test_http_failure_returns_empty(self) -> None:
        """On POST /books failure: log and return [] (no fallback)."""
        import requests

        client = _client()
        with patch.object(client._session, "post", side_effect=requests.ConnectionError("down")):
            result = client.get_order_books(["tok_1", "tok_2"])
        assert result == []


# ---------------------------------------------------------------------------
# get_fee_rate_bps (mocked HTTP)
# ---------------------------------------------------------------------------

class TestGetFeeRateBps:
    def test_returns_base_fee(self) -> None:
        client = _client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"base_fee": 100}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.get_fee_rate_bps("tok_x")
        assert result == 100

    def test_returns_zero_on_failure(self) -> None:
        import requests

        client = _client()
        with patch.object(
            client._session, "get",
            side_effect=requests.ConnectionError("down")
        ):
            result = client.get_fee_rate_bps("tok_x")
        assert result == 0

    def test_geopolitics_zero_fee(self) -> None:
        """Geopolitics category has 0% fee — verify 0 is returned correctly."""
        client = _client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"base_fee": 0}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.get_fee_rate_bps("tok_geo")
        assert result == 0
