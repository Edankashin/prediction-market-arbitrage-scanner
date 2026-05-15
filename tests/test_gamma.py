"""Tests for GammaClient — event reference extraction and market parsing.

Network-dependent tests use @pytest.mark.vcr and require cassettes
in tests/cassettes/. Cassette-free tests run offline.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.api.gamma import GammaClient, _dec_or_none, _int_or_none
from app.core.config import GammaConfig

_CFG = GammaConfig(
    base_url="https://gamma-api.polymarket.com",
    requests_per_second=1.0,
    timeout_sec=15,
    max_retries=3,
    backoff_factor=1.0,
)


def _client() -> GammaClient:
    return GammaClient(_CFG)


# ---------------------------------------------------------------------------
# _extract_event_reference (offline)
# ---------------------------------------------------------------------------

class TestExtractEventReference:
    def test_events_array_shape(self) -> None:
        """Shape 1: events array nested under market."""
        client = _client()
        market = {
            "events": [{"id": "99", "slug": "test-slug", "title": "Test Event"}]
        }
        eid, eslug, etitle = client._extract_event_reference(market)
        assert eid == "99"
        assert eslug == "test-slug"
        assert etitle == "Test Event"

    def test_flat_event_id_shape(self) -> None:
        """Shape 2: flat eventId / eventSlug fields."""
        client = _client()
        market = {
            "eventId": "42",
            "eventSlug": "flat-slug",
            "eventTitle": "Flat Event",
        }
        eid, eslug, etitle = client._extract_event_reference(market)
        assert eid == "42"
        assert eslug == "flat-slug"
        assert etitle == "Flat Event"

    def test_nested_event_object_shape(self) -> None:
        """Shape 3: nested event object."""
        client = _client()
        market = {
            "event": {"id": "77", "slug": "nested-slug", "title": "Nested"}
        }
        eid, eslug, etitle = client._extract_event_reference(market)
        assert eid == "77"
        assert eslug == "nested-slug"
        assert etitle == "Nested"

    def test_no_event_returns_none_tuple(self) -> None:
        client = _client()
        eid, eslug, etitle = client._extract_event_reference({"question": "X?"})
        assert eid is None
        assert eslug is None
        assert etitle is None

    def test_prefers_events_array_over_flat(self) -> None:
        """When both events array and flat fields present, prefer events array."""
        client = _client()
        market = {
            "events": [{"id": "from_array", "slug": "arr"}],
            "eventId": "from_flat",
        }
        eid, _, _ = client._extract_event_reference(market)
        assert eid == "from_array"


# ---------------------------------------------------------------------------
# parse_market_row (offline)
# ---------------------------------------------------------------------------

class TestParseMarketRow:
    def _raw(self) -> dict[str, object]:
        return {
            "conditionId": "cond_abc",
            "question": "Will it rain?",
            "negRisk": True,
            "active": True,
            "closed": False,
            "archived": False,
            "endDate": "2026-12-31",
            "clobTokenIds": ["tok_yes", "tok_no"],
            "outcomePrices": ["0.60", "0.40"],
            "outcomes": ["Yes", "No"],
            "volume": "150000.5",
            "liquidity": "50000",
            "spread": "0.02",
            "feesEnabled": True,
            "feeSchedule": {
                "makerBaseFee": 0,
                "takerBaseFee": 100,
                "maxFee": 180,
            },
            "events": [{"id": "evt_1", "slug": "rain-event", "title": "Rain?"}],
        }

    def test_basic_fields(self) -> None:
        row = _client().parse_market_row(self._raw(), synced_at="2026-05-14T00:00:00+00:00")
        assert row.condition_id == "cond_abc"
        assert row.neg_risk is True
        assert row.event_id == "evt_1"
        assert row.fees_enabled is True

    def test_fee_schedule_parsing(self) -> None:
        row = _client().parse_market_row(self._raw(), synced_at="2026-05-14T00:00:00+00:00")
        assert row.taker_fee_cap_bps == 180  # max of takerBaseFee and maxFee
        assert row.maker_rebate_cap_bps == 0
        assert row.fee_formula is not None
        parsed = json.loads(row.fee_formula)
        assert parsed["takerBaseFee"] == 100

    def test_volume_is_decimal(self) -> None:
        row = _client().parse_market_row(self._raw(), synced_at="2026-05-14T00:00:00+00:00")
        assert isinstance(row.volume, Decimal)
        assert row.volume == Decimal("150000.5")

    def test_clob_token_ids_json_string(self) -> None:
        row = _client().parse_market_row(self._raw(), synced_at="2026-05-14T00:00:00+00:00")
        ids = json.loads(row.clob_token_ids)
        assert ids == ["tok_yes", "tok_no"]

    def test_handles_missing_fee_schedule(self) -> None:
        raw = self._raw()
        del raw["feeSchedule"]
        row = _client().parse_market_row(raw, synced_at="2026-05-14T00:00:00+00:00")
        assert row.fee_formula is None
        assert row.taker_fee_cap_bps is None

    def test_handles_string_clob_token_ids(self) -> None:
        raw = self._raw()
        raw["clobTokenIds"] = '["tok_a","tok_b"]'
        row = _client().parse_market_row(raw, synced_at="2026-05-14T00:00:00+00:00")
        ids = json.loads(row.clob_token_ids)
        assert ids == ["tok_a", "tok_b"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_dec_or_none_valid(self) -> None:
        assert _dec_or_none("123.45") == Decimal("123.45")

    def test_dec_or_none_none(self) -> None:
        assert _dec_or_none(None) is None

    def test_dec_or_none_invalid(self) -> None:
        assert _dec_or_none("not-a-number") is None

    def test_int_or_none_valid(self) -> None:
        assert _int_or_none(100) == 100

    def test_int_or_none_none(self) -> None:
        assert _int_or_none(None) is None
