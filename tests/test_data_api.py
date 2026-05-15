"""Tests for DataApiClient — parsing logic (offline)."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.api.data_api import DataApiClient, _dec_or_none, _dec_or_zero
from app.core.config import DataApiConfig

_CFG = DataApiConfig(
    base_url="https://data-api.polymarket.com",
    timeout_sec=15,
    max_retries=3,
    backoff_factor=1.0,
)


def _client() -> DataApiClient:
    return DataApiClient(_CFG)


# ---------------------------------------------------------------------------
# _parse_position
# ---------------------------------------------------------------------------

class TestParsePosition:
    def _raw(self) -> dict[str, object]:
        return {
            "asset": "tok_abc",
            "conditionId": "cond_xyz",
            "outcome": "Yes",
            "size": "50.5",
            "avgPrice": "0.45",
            "realizedPnl": "12.00",
            "unrealizedPnl": "3.50",
        }

    def test_basic_parse(self) -> None:
        client = _client()
        row = client._parse_position(self._raw(), wallet="0xDEAD", fetched_at="2026-05-14T00:00:00+00:00")
        assert row is not None
        assert row.token_id == "tok_abc"
        assert row.condition_id == "cond_xyz"
        assert row.shares == Decimal("50.5")
        assert row.avg_price == Decimal("0.45")
        assert row.realized_pnl == Decimal("12.00")
        assert row.unrealized_pnl == Decimal("3.50")
        assert row.wallet == "0xDEAD"

    def test_missing_token_id_returns_none(self) -> None:
        client = _client()
        row = client._parse_position({}, wallet="0xDEAD", fetched_at="2026-05-14T00:00:00+00:00")
        assert row is None

    def test_alternative_field_names(self) -> None:
        """Handles tokenId, avg_price, etc. as alternative keys."""
        client = _client()
        raw = {
            "tokenId": "tok_alt",
            "market": "cond_alt",
            "size": "10",
            "avg_price": "0.55",
        }
        row = client._parse_position(raw, wallet="0xBEEF", fetched_at="2026-05-14T00:00:00+00:00")
        assert row is not None
        assert row.token_id == "tok_alt"
        assert row.avg_price == Decimal("0.55")


# ---------------------------------------------------------------------------
# get_positions (mocked HTTP)
# ---------------------------------------------------------------------------

class TestGetPositions:
    def test_success(self) -> None:
        client = _client()
        raw_list = [
            {
                "asset": "tok_1",
                "conditionId": "cond_1",
                "outcome": "Yes",
                "size": "100",
                "avgPrice": "0.30",
            }
        ]
        with patch.object(client, "_get", return_value=raw_list):
            rows = client.get_positions("0xWALLET")
        assert len(rows) == 1
        assert rows[0].shares == Decimal("100")

    def test_non_list_returns_empty(self) -> None:
        client = _client()
        with patch.object(client, "_get", return_value={"error": "bad"}):
            rows = client.get_positions("0xWALLET")
        assert rows == []

    def test_general_wallet_parameterized(self) -> None:
        """No default wallet — get_positions(wallet) takes any address."""
        import inspect
        sig = inspect.signature(DataApiClient.get_positions)
        params = list(sig.parameters.keys())
        assert "wallet" in params
        # Verify no default value for wallet (required argument)
        assert sig.parameters["wallet"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_dec_or_none_valid(self) -> None:
        assert _dec_or_none("99.5") == Decimal("99.5")

    def test_dec_or_none_none(self) -> None:
        assert _dec_or_none(None) is None

    def test_dec_or_zero_none(self) -> None:
        assert _dec_or_zero(None) == Decimal("0")

    def test_dec_or_zero_value(self) -> None:
        assert _dec_or_zero("42.5") == Decimal("42.5")
