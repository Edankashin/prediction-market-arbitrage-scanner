"""Tests for KalshiClient.parse_and_validate_snapshot — three-state validator.

All tests are offline: no network, no DB, no VCR.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.api.kalshi import KalshiClient
from app.core.config import KalshiConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _client() -> KalshiClient:
    cfg = MagicMock(spec=KalshiConfig)
    cfg.base_url = "https://external-api.kalshi.com/trade-api/v2"
    cfg.timeout_sec = 15
    cfg.max_retries = 3
    cfg.backoff_factor = 1.0
    return KalshiClient(cfg)


def _ob(yes_dollars=None, no_dollars=None) -> dict:
    """Build a minimal orderbook_fp dict."""
    return {
        "orderbook_fp": {
            "yes_dollars": yes_dollars or [],
            "no_dollars": no_dollars or [],
        }
    }


# ---------------------------------------------------------------------------
# State (a): valid both-sided book
# ---------------------------------------------------------------------------

class TestValidBook:
    def test_returns_snapshot_not_none(self) -> None:
        """Valid book (sum ≤ 1.01) returns a KalshiSnapshotRow."""
        raw = _ob(
            yes_dollars=[["0.5500", "100.00"], ["0.5000", "200.00"]],
            no_dollars=[["0.4200", "150.00"], ["0.3000", "300.00"]],
        )
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None

    def test_single_sided_is_false(self) -> None:
        raw = _ob(
            yes_dollars=[["0.5500", "100.00"]],
            no_dollars=[["0.4200", "100.00"]],
        )
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        assert snap.single_sided is False

    def test_best_yes_ask_derived_from_no_bid(self) -> None:
        """best_yes_ask = 1 − best_no_bid."""
        raw = _ob(
            yes_dollars=[["0.5500", "100.00"]],
            no_dollars=[["0.4200", "100.00"], ["0.3000", "50.00"]],
        )
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        # best_no_bid = 0.42 → best_yes_ask = 0.58
        assert snap.best_yes_ask == Decimal("1") - Decimal("0.4200")

    def test_best_no_ask_derived_from_yes_bid(self) -> None:
        """best_no_ask = 1 − best_yes_bid."""
        raw = _ob(
            yes_dollars=[["0.5500", "100.00"], ["0.4000", "50.00"]],
            no_dollars=[["0.4200", "100.00"]],
        )
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        # best_yes_bid = 0.55 → best_no_ask = 0.45
        assert snap.best_no_ask == Decimal("1") - Decimal("0.5500")

    def test_bids_sorted_descending(self) -> None:
        """yes_bids and no_bids are returned best-bid-first (descending price)."""
        raw = _ob(
            yes_dollars=[["0.3000", "50.00"], ["0.5500", "100.00"], ["0.4000", "75.00"]],
            no_dollars=[["0.1000", "10.00"], ["0.4200", "100.00"]],
        )
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        yes_prices = [lv.price for lv in snap.yes_bids]
        no_prices = [lv.price for lv in snap.no_bids]
        assert yes_prices == sorted(yes_prices, reverse=True)
        assert no_prices == sorted(no_prices, reverse=True)

    def test_ticker_stored(self) -> None:
        raw = _ob(yes_dollars=[["0.55", "100"]], no_dollars=[["0.42", "100"]])
        snap = _client().parse_and_validate_snapshot("KXELONMARS-99", raw)
        assert snap is not None
        assert snap.ticker == "KXELONMARS-99"

    def test_at_boundary_1_01_is_valid(self) -> None:
        """Sum exactly 1.01 is still valid (boundary is exclusive)."""
        raw = _ob(
            yes_dollars=[["0.5900", "100"]],
            no_dollars=[["0.4200", "100"]],
        )
        # 0.59 + 0.42 = 1.01 → exactly at boundary, should be valid
        snap = _client().parse_and_validate_snapshot("T", raw)
        assert snap is not None


# ---------------------------------------------------------------------------
# State (b): crossed book
# ---------------------------------------------------------------------------

class TestCrossedBook:
    def test_returns_none_for_crossed_book(self) -> None:
        """yes_bid + no_bid > 1.01 → None (state b)."""
        raw = _ob(
            yes_dollars=[["0.6500", "100"]],
            no_dollars=[["0.5000", "100"]],
        )
        # 0.65 + 0.50 = 1.15 > 1.01 → crossed
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is None

    def test_just_above_boundary_returns_none(self) -> None:
        """0.6000 + 0.4200 = 1.02 > 1.01 → crossed."""
        raw = _ob(
            yes_dollars=[["0.6000", "100"]],
            no_dollars=[["0.4200", "100"]],
        )
        snap = _client().parse_and_validate_snapshot("T", raw)
        assert snap is None


# ---------------------------------------------------------------------------
# State (c): single-sided book
# ---------------------------------------------------------------------------

class TestSingleSidedBook:
    def test_yes_only_is_single_sided(self) -> None:
        """No no_bids → single_sided=True."""
        raw = _ob(yes_dollars=[["0.5500", "100"]], no_dollars=[])
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        assert snap.single_sided is True

    def test_no_only_is_single_sided(self) -> None:
        """No yes_bids → single_sided=True."""
        raw = _ob(yes_dollars=[], no_dollars=[["0.4200", "100"]])
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        assert snap.single_sided is True

    def test_yes_only_best_yes_ask_is_none(self) -> None:
        """No no_bids → best_yes_ask is None (can't derive from missing side)."""
        raw = _ob(yes_dollars=[["0.5500", "100"]], no_dollars=[])
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        assert snap.best_yes_ask is None

    def test_yes_only_best_no_ask_derived(self) -> None:
        """best_no_ask = 1 − best_yes_bid even when single-sided."""
        raw = _ob(yes_dollars=[["0.5500", "100"]], no_dollars=[])
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        assert snap.best_no_ask == Decimal("1") - Decimal("0.5500")

    def test_empty_both_sides_is_single_sided(self) -> None:
        """Both sides empty → single_sided=True, no best prices."""
        raw = _ob(yes_dollars=[], no_dollars=[])
        snap = _client().parse_and_validate_snapshot("TEST-1", raw)
        assert snap is not None
        assert snap.single_sided is True
        assert snap.best_yes_ask is None
        assert snap.best_no_ask is None
