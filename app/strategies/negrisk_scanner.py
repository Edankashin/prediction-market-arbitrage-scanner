"""NegRisk multi-outcome arbitrage scanner.

A NegRisk event has N mutually exclusive outcomes.  Buying one YES token per
outcome at ask prices p_1, …, p_N guarantees a payoff of exactly $1.00 per
share — the winning outcome pays 1.00, all others pay 0.00.

Gross edge  = 1 - sum(p_i)
Net edge    = gross_edge - sum(fee_i) - gas_total

The LP chooses the number of shares to buy across all legs so as to maximise
net profit subject to the orderbook depth constraint.  To avoid re-examining
the same snapshot twice, scan_event() is pure: it does not write to the DB.

Design notes
------------
* Per-level fee rates: each book level at price p_ij has its own fee constant
  fee_rate_ij = fee_bps_from_formula(market, p_ij) / 10000.  This corrects the
  anti-conservative error that arises when a leg walks the book above best_ask
  (moving p closer to 0.5 raises p(1-p) and therefore the dynamic fee rate).

* All Decimal arithmetic is preserved up to the cvxpy boundary.  LP variables
  and coefficients are cast to float there and results are cast back to Decimal.

* Returns None if gross edge ≤ 0 or net edge (at LP-optimal size) < min_net_edge_bps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.fees import fee_bps_from_formula
from app.models import BookLevel, MarketRow, SnapshotRow

_log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")


# ---------------------------------------------------------------------------
# Public configuration and result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScanConfig:
    gas_usdc_per_order: Decimal
    min_net_edge_bps: int = 1


@dataclass(frozen=True)
class ArbLeg:
    condition_id: str
    token_id: str
    side: Literal["BUY"]
    best_ask: Decimal
    avg_fill_price: Decimal
    fill_shares: Decimal
    fee_bps: int          # effective fee in bps at avg_fill_price
    fee_usdc: Decimal
    slippage_bps: Decimal
    slippage_usdc: Decimal
    gas_usdc: Decimal


@dataclass(frozen=True)
class ArbOpportunity:
    event_id: str
    ts: str
    legs: tuple[ArbLeg, ...]
    optimal_shares: Decimal
    total_cost_usdc: Decimal
    guaranteed_payoff_usdc: Decimal
    gross_edge_bps: int
    net_edge_bps: int
    total_fee_usdc: Decimal
    total_slippage_usdc: Decimal
    total_gas_usdc: Decimal
    dominant_cost: Literal["fee", "slippage", "gas"]


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def walk_book_cost(levels: list[BookLevel], shares: Decimal) -> tuple[Decimal, Decimal]:
    """Fill `shares` by walking `levels` in order.  Returns (total_cost, filled_shares).

    If total depth < shares, returns cost for however many were available.
    levels must be sorted ascending by price (as CLOB ask side is).
    """
    remaining = shares
    total_cost = _ZERO
    for lv in levels:
        if remaining <= _ZERO:
            break
        qty = min(remaining, lv.size)
        total_cost += qty * lv.price
        remaining -= qty
    filled = shares - remaining
    return total_cost, filled


def lp_optimal_shares(
    leg_levels: list[list[BookLevel]],
    fee_rates_per_level: list[list[Decimal]],
    gas_total: Decimal,
) -> Decimal:
    """LP sizing: maximise net profit subject to depth constraints.

    Objective (linear):
        maximise  m  -  Σ_i Σ_j  x_ij × p_ij × (1 + fee_rate_ij)  -  gas_total
    Subject to:
        Σ_j x_ij  ==  s_i            (leg i fills add up to s_i shares)
        m  ≤  s_i  ∀ i               (m is the min across legs)
        0 ≤ x_ij ≤ depth_ij          (can't exceed book depth at level j)

    leg_levels[i]        : list of BookLevel for leg i, ascending price
    fee_rates_per_level[i][j] : Decimal fee rate for level j of leg i
    gas_total            : flat gas cost (Decimal), treated as a constant offset

    Returns 0 if LP is infeasible, unbounded, or optimal value ≤ 0.
    Float conversion happens only at the cvxpy boundary.
    """
    import cvxpy as cp

    n_legs = len(leg_levels)
    if n_legs == 0:
        return _ZERO

    # Variables: x[i][j] = shares bought at level j of leg i
    x_vars: list[list[cp.Variable]] = []
    for i, levels in enumerate(leg_levels):
        x_vars.append([cp.Variable(nonneg=True) for _ in levels])

    # m = guaranteed payoff shares = min of leg totals
    m = cp.Variable(nonneg=True)

    # Build cost expression: Σ_i Σ_j x_ij × p_ij × (1 + fee_rate_ij)
    cost_terms = []
    for i, levels in enumerate(leg_levels):
        for j, lv in enumerate(levels):
            coeff = float(lv.price * (1 + fee_rates_per_level[i][j]))
            cost_terms.append(coeff * x_vars[i][j])

    objective = cp.Maximize(m - cp.sum(cost_terms) - float(gas_total))

    constraints = []
    for i, levels in enumerate(leg_levels):
        # leg total s_i
        s_i = cp.sum(x_vars[i])
        # m <= s_i
        constraints.append(m <= s_i)
        # depth caps
        for j, lv in enumerate(levels):
            constraints.append(x_vars[i][j] <= float(lv.size))

    prob = cp.Problem(objective, constraints)
    prob.solve(solver=cp.CLARABEL, verbose=False)

    if prob.status not in ("optimal", "optimal_inaccurate"):
        _log.warning("lp_nonfeasible", extra={"status": prob.status})
        return _ZERO

    optimal_m = m.value
    if optimal_m is None or optimal_m <= 0:
        return _ZERO

    # Round down to nearest integer share to stay conservative
    return Decimal(str(round(float(optimal_m), 8))).quantize(Decimal("0.000001"))


def scan_event(
    snaps: list[SnapshotRow],
    markets: dict[str, MarketRow],
    cfg: ScanConfig,
) -> ArbOpportunity | None:
    """Scan one NegRisk event for an arb opportunity.

    snaps   : all YES-token snapshots for this event (one per outcome)
    markets : {condition_id: MarketRow} for these outcomes
    cfg     : scanner configuration

    Returns None if:
      - fewer than 2 outcomes
      - sum(best_ask) >= 1.0 (no gross edge)
      - LP optimal size == 0
      - net_edge_bps < cfg.min_net_edge_bps
    """
    if len(snaps) < 2:
        return None

    # Ensure all snaps have a matching market and a usable ask side
    valid: list[tuple[SnapshotRow, MarketRow]] = []
    for snap in snaps:
        mkt = markets.get(snap.condition_id)
        if mkt is None or not snap.asks or snap.best_ask is None:
            continue
        valid.append((snap, mkt))

    if len(valid) < 2:
        return None

    sum_best_ask = sum(s.best_ask for s, _ in valid)
    if sum_best_ask >= _ONE:
        return None

    ts = valid[0][0].ts
    event_id = valid[0][1].event_id or ""
    gas_total = cfg.gas_usdc_per_order * len(valid)

    # Build per-level fee rates for the LP
    leg_levels: list[list[BookLevel]] = []
    fee_rates_per_level: list[list[Decimal]] = []
    for snap, mkt in valid:
        leg_levels.append(snap.asks)
        rates = [
            Decimal(fee_bps_from_formula(mkt, lv.price)) / _BPS
            for lv in snap.asks
        ]
        fee_rates_per_level.append(rates)

    optimal = lp_optimal_shares(leg_levels, fee_rates_per_level, gas_total)
    if optimal <= _ZERO:
        return None

    # Build legs using walk_book_cost at the LP-optimal size
    legs: list[ArbLeg] = []
    total_cost = _ZERO
    total_fee = _ZERO
    total_slip = _ZERO

    for (snap, mkt), levels, rates in zip(valid, leg_levels, fee_rates_per_level):
        cost, filled = walk_book_cost(levels, optimal)
        if filled <= _ZERO:
            return None  # depth disappeared

        avg_fill = cost / filled
        # Fee on realized cost using avg_fill_price fee rate
        effective_fee_bps = fee_bps_from_formula(mkt, avg_fill)
        effective_fee_rate = Decimal(effective_fee_bps) / _BPS
        fee_usdc = cost * effective_fee_rate

        slip_bps = _ZERO
        if snap.best_ask and snap.best_ask > _ZERO:
            raw = (avg_fill - snap.best_ask) / snap.best_ask * _BPS
            slip_bps = max(_ZERO, raw)
        slip_usdc = (avg_fill - snap.best_ask) * filled if snap.best_ask else _ZERO
        slip_usdc = max(_ZERO, slip_usdc)

        leg = ArbLeg(
            condition_id=snap.condition_id,
            token_id=snap.token_id,
            side="BUY",
            best_ask=snap.best_ask,
            avg_fill_price=avg_fill,
            fill_shares=filled,
            fee_bps=effective_fee_bps,
            fee_usdc=fee_usdc,
            slippage_bps=slip_bps,
            slippage_usdc=slip_usdc,
            gas_usdc=cfg.gas_usdc_per_order,
        )
        legs.append(leg)
        total_cost += cost
        total_fee += fee_usdc
        total_slip += slip_usdc

    payoff = optimal * _ONE  # guaranteed $1.00/share
    gross_pnl = payoff - total_cost
    net_pnl = gross_pnl - total_fee - gas_total

    gross_edge_bps = int((gross_pnl / payoff * _BPS).to_integral_value()) if payoff > _ZERO else 0
    net_edge_bps = int((net_pnl / payoff * _BPS).to_integral_value()) if payoff > _ZERO else 0

    if net_edge_bps < cfg.min_net_edge_bps:
        return None

    dominant: Literal["fee", "slippage", "gas"]
    if total_fee >= gas_total and total_fee >= total_slip:
        dominant = "fee"
    elif total_slip >= gas_total:
        dominant = "slippage"
    else:
        dominant = "gas"

    return ArbOpportunity(
        event_id=event_id,
        ts=ts,
        legs=tuple(legs),
        optimal_shares=optimal,
        total_cost_usdc=total_cost,
        guaranteed_payoff_usdc=payoff,
        gross_edge_bps=gross_edge_bps,
        net_edge_bps=net_edge_bps,
        total_fee_usdc=total_fee,
        total_slippage_usdc=total_slip,
        total_gas_usdc=gas_total,
        dominant_cost=dominant,
    )


def scan_all(
    event_groups: list[tuple[list[SnapshotRow], dict[str, MarketRow]]],
    cfg: ScanConfig,
) -> list[ArbOpportunity]:
    """Scan a list of (snaps, markets) event groups.  Returns opportunities
    sorted descending by net_edge_bps (best first).
    """
    results: list[ArbOpportunity] = []
    for snaps, markets in event_groups:
        opp = scan_event(snaps, markets, cfg)
        if opp is not None:
            results.append(opp)
    results.sort(key=lambda o: o.net_edge_bps, reverse=True)
    return results
