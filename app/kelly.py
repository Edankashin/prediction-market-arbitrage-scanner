"""Kelly Criterion position sizer.

Computes a USDC position size from edge, confidence, and portfolio value.

Formula (simplified Kelly for small edges):
    edge_fraction = edge_bps / 10_000
    full_kelly    = edge_fraction * confidence * portfolio_value
    half_kelly    = full_kelly * half_kelly_factor
    capped        = min(half_kelly, portfolio_value * max_position_pct)

'edge_bps' is the raw observed gross edge before fees and gas.
The caller is responsible for verifying that edge > fees + gas before sizing.
This module does not check profitability — it only sizes a claimed edge.

Decimal arithmetic throughout. Returns Decimal('0') for non-positive inputs.
"""
from __future__ import annotations

import logging
from decimal import Decimal

_log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TEN_THOUSAND = Decimal("10000")


def size_position(
    edge_bps: int,
    confidence: float,
    portfolio_value: Decimal,
    half_kelly_factor: float = 0.5,
    max_position_pct: float = 0.03,
) -> Decimal:
    """Compute a sized USDC position from Kelly Criterion.

    Args:
        edge_bps: Expected gross edge in basis points (e.g., 50 = 0.5%).
        confidence: Scaling factor in [0, 1] representing model certainty.
                    0 = no confidence (returns 0), 1 = full model confidence.
        portfolio_value: Current total equity in USDC (Decimal).
        half_kelly_factor: Multiplier on full Kelly (default 0.5 = half-Kelly).
        max_position_pct: Hard cap as a fraction of portfolio (default 0.03).

    Returns:
        USDC amount to deploy, rounded to 2 decimal places.
        Returns Decimal('0') if any input is non-positive.
    """
    if edge_bps <= 0 or confidence <= 0 or portfolio_value <= _ZERO:
        return _ZERO

    conf = Decimal(str(confidence)).max(_ZERO).min(_ONE)
    hkf = Decimal(str(half_kelly_factor)).max(_ZERO).min(_ONE)
    cap_frac = Decimal(str(max_position_pct)).max(_ZERO)

    edge_fraction = Decimal(edge_bps) / _TEN_THOUSAND
    full_kelly = edge_fraction * conf * portfolio_value
    half_kelly = full_kelly * hkf
    hard_cap = portfolio_value * cap_frac

    result = min(half_kelly, hard_cap)
    result = result.max(_ZERO)

    _log.debug(
        "kelly_sizing",
        extra={
            "edge_bps": edge_bps,
            "confidence": float(conf),
            "portfolio_usdc": float(portfolio_value),
            "full_kelly": float(full_kelly),
            "half_kelly": float(half_kelly),
            "hard_cap": float(hard_cap),
            "result": float(result),
        },
    )
    return result.quantize(Decimal("0.01"))


def min_edge_bps_to_trade(
    taker_fee_rate: Decimal,
    gas_usdc: Decimal,
    position_size_usdc: Decimal,
    leg_count: int = 1,
) -> int:
    """Compute the minimum gross edge in bps to break even after costs.

    Useful for pre-filtering: skip any arb whose edge_bps < this threshold.

    Args:
        taker_fee_rate: Per-leg taker fee as a Decimal fraction (e.g., 0.01).
        gas_usdc: Per-order gas cost in USDC.
        position_size_usdc: Total USDC deployed across all legs.
        leg_count: Number of legs in the arb group.

    Returns:
        Minimum edge in basis points (rounded up to nearest int).
    """
    if position_size_usdc <= _ZERO:
        return 10_000  # can't trade nothing

    total_fees = taker_fee_rate * position_size_usdc
    total_gas = gas_usdc * leg_count
    total_cost = total_fees + total_gas

    min_edge_fraction = total_cost / position_size_usdc
    min_edge_bps = int((min_edge_fraction * _TEN_THOUSAND).to_integral_value()) + 1
    return max(1, min_edge_bps)
