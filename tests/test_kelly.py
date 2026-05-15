"""Tests for kelly.py — pure arithmetic, no network."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.kelly import min_edge_bps_to_trade, size_position

_PORTFOLIO = Decimal("10000")


class TestSizePosition:
    def test_basic_half_kelly(self) -> None:
        """50bps edge, 100% confidence, $10k portfolio → half-Kelly = $25."""
        # edge_fraction = 0.0050; full_kelly = 50; half = 25; cap = 300
        result = size_position(
            edge_bps=50,
            confidence=1.0,
            portfolio_value=_PORTFOLIO,
        )
        assert result == Decimal("25.00")

    def test_cap_applies(self) -> None:
        """Large edge still hits 3% portfolio cap."""
        result = size_position(
            edge_bps=5000,
            confidence=1.0,
            portfolio_value=_PORTFOLIO,
            max_position_pct=0.03,
        )
        assert result == Decimal("300.00")

    def test_confidence_scales_down(self) -> None:
        """50% confidence halves the position."""
        full = size_position(50, 1.0, _PORTFOLIO)
        half = size_position(50, 0.5, _PORTFOLIO)
        assert half == full / Decimal("2")

    def test_zero_edge_returns_zero(self) -> None:
        assert size_position(0, 1.0, _PORTFOLIO) == Decimal("0")

    def test_negative_edge_returns_zero(self) -> None:
        assert size_position(-10, 1.0, _PORTFOLIO) == Decimal("0")

    def test_zero_confidence_returns_zero(self) -> None:
        assert size_position(100, 0.0, _PORTFOLIO) == Decimal("0")

    def test_zero_portfolio_returns_zero(self) -> None:
        assert size_position(100, 1.0, Decimal("0")) == Decimal("0")

    def test_custom_half_kelly(self) -> None:
        """Quarter-Kelly (factor=0.25) yields 25% of full-Kelly."""
        full_k = size_position(50, 1.0, _PORTFOLIO, half_kelly_factor=1.0)
        quarter_k = size_position(50, 1.0, _PORTFOLIO, half_kelly_factor=0.25)
        assert quarter_k == (full_k * Decimal("0.25")).quantize(Decimal("0.01"))

    def test_returns_decimal(self) -> None:
        result = size_position(100, 0.8, _PORTFOLIO)
        assert isinstance(result, Decimal)

    def test_two_decimal_places(self) -> None:
        """Result is always rounded to 2 decimal places."""
        result = size_position(37, 0.71, _PORTFOLIO)
        assert result == result.quantize(Decimal("0.01"))


class TestMinEdgeBps:
    def test_1pct_fee_100_usdc(self) -> None:
        """1% fee on $100 position = $1 fee; at $100 position = 100bps min edge."""
        result = min_edge_bps_to_trade(
            taker_fee_rate=Decimal("0.01"),
            gas_usdc=Decimal("0"),
            position_size_usdc=Decimal("100"),
        )
        assert result == 101  # fee = $1 → 100bps + 1 for rounding up

    def test_gas_adds_to_minimum(self) -> None:
        """Gas of $0.015 on $100 = 1.5bps added to minimum."""
        no_gas = min_edge_bps_to_trade(
            taker_fee_rate=Decimal("0.01"),
            gas_usdc=Decimal("0"),
            position_size_usdc=Decimal("100"),
        )
        with_gas = min_edge_bps_to_trade(
            taker_fee_rate=Decimal("0.01"),
            gas_usdc=Decimal("0.005"),
            position_size_usdc=Decimal("100"),
            leg_count=3,
        )
        assert with_gas > no_gas

    def test_zero_position_returns_max(self) -> None:
        result = min_edge_bps_to_trade(
            Decimal("0.01"), Decimal("0"), Decimal("0")
        )
        assert result == 10_000
