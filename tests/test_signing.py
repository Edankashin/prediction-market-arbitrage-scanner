"""Tests for signing.py — V2 order construction.

These tests verify that build_v2_order() returns a non-empty dict
with the correct V2 structure (no feeRateBps, no nonce, no taker).
The ClobClient's internal caches are pre-seeded so no HTTP calls are made.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.signing import build_v2_order, make_test_client

# Known harmless test private key (Hardhat/anvil account 0 — no real funds)
_TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_TEST_TOKEN = "99999999999999999999999999999999999999999999999999999999999999999"


class TestBuildV2Order:
    def test_returns_dict(self) -> None:
        """build_v2_order returns a non-empty dict."""
        order = build_v2_order(
            token_id=_TEST_TOKEN,
            price=Decimal("0.45"),
            size=Decimal("10"),
            side="BUY",
            chain_id=137,
            private_key=_TEST_KEY,
        )
        assert isinstance(order, dict)
        assert len(order) > 0

    def test_no_v1_fields(self) -> None:
        """V2 orders must not contain feeRateBps, nonce, or taker."""
        order = build_v2_order(
            token_id=_TEST_TOKEN,
            price=Decimal("0.30"),
            size=Decimal("50"),
            side="BUY",
            chain_id=137,
            private_key=_TEST_KEY,
        )
        # V1 fields that must be absent in V2 orders
        assert "feeRateBps" not in order
        assert "nonce" not in order
        assert "taker" not in order

    def test_sell_side(self) -> None:
        """SELL orders are also V2-compliant."""
        order = build_v2_order(
            token_id=_TEST_TOKEN,
            price=Decimal("0.70"),
            size=Decimal("5"),
            side="SELL",
            chain_id=137,
            private_key=_TEST_KEY,
        )
        assert isinstance(order, dict)
        assert len(order) > 0

    def test_neg_risk_flag(self) -> None:
        """neg_risk=True produces a signed order without error."""
        order = build_v2_order(
            token_id=_TEST_TOKEN,
            price=Decimal("0.20"),
            size=Decimal("25"),
            side="BUY",
            chain_id=137,
            private_key=_TEST_KEY,
            neg_risk=True,
        )
        assert isinstance(order, dict)

    def test_make_test_client_offline(self) -> None:
        """make_test_client pre-seeds caches so no HTTP is needed."""
        client = make_test_client(token_id=_TEST_TOKEN)
        # Verify caches are set (name-mangled attributes)
        tick_cache = client._ClobClient__tick_sizes  # type: ignore[attr-defined]
        neg_cache = client._ClobClient__neg_risk      # type: ignore[attr-defined]
        assert _TEST_TOKEN in tick_cache
        assert _TEST_TOKEN in neg_cache
