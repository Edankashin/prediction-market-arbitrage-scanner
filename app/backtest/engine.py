"""Backtest engine: replay stored book snapshots through a strategy callback.

The engine does NOT know about specific strategies. It accepts a callable
that maps a list of SnapshotRow → list of IntendedTrade, and simulates
execution for each trade by walking the order book.

Key correctness properties:
  - Fill price is computed by walking bids/asks depth, not top-of-book.
  - Fee is applied per-leg on USDC cost (taker_fee_rate * cost_usdc).
  - Gas is applied per-leg (gas_usdc_per_order from config).
  - For NegRisk arb groups (strategy='S1_NEGRISK'), guaranteed payoff is
    computed as min(fill_size_per_leg) * 1.00, independent of resolution.
  - P&L is net of fees AND gas. Gross P&L never nets fees.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.db import store
from app.models import BookLevel, PortfolioSnapshotRow, SnapshotRow, TradeRow

_log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class IntendedTrade:
    """A trade the strategy wants to execute at a given snapshot."""

    condition_id: str
    token_id: str
    side: str   # 'BUY' | 'SELL'
    size: Decimal
    strategy: str
    group_id: str
    leg_index: int


@dataclass
class SimulatedFill:
    """Result of simulating one IntendedTrade against the book."""

    trade: IntendedTrade
    intended_price: Decimal  # best ask/bid at decision time
    fill_price: Decimal      # avg fill price after walking the book
    fill_size: Decimal
    cost_usdc: Decimal
    fee_usdc: Decimal
    gas_usdc: Decimal
    slippage_bps: Decimal
    status: str              # 'filled' | 'partial' | 'no_liquidity'


@dataclass
class GroupPnL:
    """Aggregated P&L for one multi-leg arb group."""

    group_id: str
    strategy: str
    leg_count: int
    ts: str
    total_cost_usdc: Decimal
    expected_payoff: Decimal   # guaranteed for NegRisk arb
    gross_pnl: Decimal         # expected_payoff - total_cost_usdc
    total_fees: Decimal
    total_gas: Decimal
    net_pnl: Decimal           # gross_pnl - total_fees - total_gas
    avg_slippage_bps: Decimal
    all_filled: bool           # False = at least one leg partially filled


@dataclass
class BacktestConfig:
    """Runtime parameters for the backtest engine."""

    initial_capital: Decimal
    gas_usdc_per_order: Decimal
    mode: str = "backtest"
    default_taker_fee: Decimal = field(default_factory=lambda: Decimal("0.01"))


@dataclass
class BacktestResult:
    """Aggregated output from a full backtest run."""

    group_pnls: list[GroupPnL]
    total_gross_pnl: Decimal
    total_fees: Decimal
    total_gas: Decimal
    total_net_pnl: Decimal
    equity_curve: list[PortfolioSnapshotRow]
    fill_count: int
    win_count: int           # groups where net_pnl > 0
    slippage_histogram: dict[str, int]  # "0-5bps", "5-10bps", … → count

    @property
    def sharpe(self) -> Decimal | None:
        from app.backtest.metrics import sharpe_ratio
        returns = [p.total_equity for p in self.equity_curve]
        return sharpe_ratio(returns) if len(returns) >= 2 else None

    @property
    def max_drawdown(self) -> Decimal | None:
        from app.backtest.metrics import max_drawdown
        equities = [p.total_equity for p in self.equity_curve]
        return max_drawdown(equities) if len(equities) >= 2 else None


# ---------------------------------------------------------------------------
# Strategy type alias
# ---------------------------------------------------------------------------

StrategyFn = Callable[[list[SnapshotRow]], list[IntendedTrade]]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def replay(
    conn: sqlite3.Connection,
    strategy_fn: StrategyFn,
    start_ts: str,
    end_ts: str,
    config: BacktestConfig,
) -> BacktestResult:
    """Replay stored snapshots from start_ts to end_ts through strategy_fn.

    For each distinct timestamp in the window:
      1. Load all snapshots.
      2. Call strategy_fn(snapshots) → IntendedTrade list.
      3. Simulate fills by walking the book.
      4. Group fills by group_id; compute P&L per group.
      5. Record all fills and portfolio snapshots in the DB.

    Returns a BacktestResult with aggregated metrics.
    """
    timestamps = store.get_distinct_timestamps(conn, start_ts, end_ts)
    _log.info("backtest_start", extra={
        "start_ts": start_ts, "end_ts": end_ts,
        "ts_count": len(timestamps), "mode": config.mode,
    })

    usdc_balance = config.initial_capital
    position_value = _ZERO
    trade_count = 0
    realized_pnl = _ZERO
    all_group_pnls: list[GroupPnL] = []
    equity_curve: list[PortfolioSnapshotRow] = []
    slippage_counts: dict[str, int] = {}

    for ts in timestamps:
        snaps = store.get_snapshots_at_ts(conn, ts)
        intended = strategy_fn(snaps)
        if not intended:
            continue

        # Simulate fills
        fills = [_simulate_fill(t, snaps, config) for t in intended]
        trade_count += len(fills)

        # Group by group_id
        groups: dict[str, list[SimulatedFill]] = {}
        for fill in fills:
            groups.setdefault(fill.trade.group_id, []).append(fill)

        # Compute P&L per group
        for group_id, group_fills in groups.items():
            gp = _compute_group_pnl(group_id, group_fills, ts)
            all_group_pnls.append(gp)

            # Update portfolio state
            usdc_balance -= gp.total_cost_usdc + gp.total_fees + gp.total_gas
            realized_pnl += gp.net_pnl

            _log.info(
                "backtest_group_pnl",
                extra={
                    "group_id": group_id,
                    "strategy": gp.strategy,
                    "gross_pnl": float(gp.gross_pnl),
                    "net_pnl": float(gp.net_pnl),
                    "all_filled": gp.all_filled,
                },
            )

        # Persist fills to DB
        for fill in fills:
            trade_ref = str(uuid.uuid4())
            trade_row = _fill_to_trade_row(fill, trade_ref, ts, config.mode)
            store.insert_trade(conn, trade_row)

            bkt = _slippage_bucket(fill.slippage_bps)
            slippage_counts[bkt] = slippage_counts.get(bkt, 0) + 1

        # Record equity checkpoint
        total_equity = usdc_balance + position_value + realized_pnl
        snap = PortfolioSnapshotRow(
            mode=config.mode,
            ts=ts,
            usdc_balance=usdc_balance,
            position_value=position_value,
            total_equity=total_equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=_ZERO,
            trade_count=trade_count,
            win_count=sum(1 for g in all_group_pnls if g.net_pnl > _ZERO),
        )
        equity_curve.append(snap)
        store.insert_portfolio_snapshot(conn, snap)

    total_gross = sum(g.gross_pnl for g in all_group_pnls)
    total_fees = sum(g.total_fees for g in all_group_pnls)
    total_gas = sum(g.total_gas for g in all_group_pnls)
    total_net = sum(g.net_pnl for g in all_group_pnls)
    win_count = sum(1 for g in all_group_pnls if g.net_pnl > _ZERO)

    _log.info(
        "backtest_complete",
        extra={
            "groups": len(all_group_pnls),
            "total_net_pnl": float(total_net),
            "total_fees": float(total_fees),
            "total_gas": float(total_gas),
        },
    )
    return BacktestResult(
        group_pnls=all_group_pnls,
        total_gross_pnl=total_gross,
        total_fees=total_fees,
        total_gas=total_gas,
        total_net_pnl=total_net,
        equity_curve=equity_curve,
        fill_count=trade_count,
        win_count=win_count,
        slippage_histogram=slippage_counts,
    )


# ---------------------------------------------------------------------------
# Fill simulation
# ---------------------------------------------------------------------------

def _simulate_fill(
    trade: IntendedTrade,
    snaps: list[SnapshotRow],
    config: BacktestConfig,
) -> SimulatedFill:
    """Walk the book for one IntendedTrade. Returns a SimulatedFill."""
    snap = _find_snap(snaps, trade.token_id)

    if snap is None:
        _log.warning(
            "backtest_no_snapshot",
            extra={"token_id": trade.token_id, "group_id": trade.group_id},
        )
        return SimulatedFill(
            trade=trade,
            intended_price=_ZERO,
            fill_price=_ZERO,
            fill_size=_ZERO,
            cost_usdc=_ZERO,
            fee_usdc=_ZERO,
            gas_usdc=config.gas_usdc_per_order,
            slippage_bps=_ZERO,
            status="no_liquidity",
        )

    levels: list[BookLevel] = snap.asks if trade.side == "BUY" else snap.bids
    intended_price = (snap.best_ask if trade.side == "BUY" else snap.best_bid) or _ZERO

    remaining = trade.size
    total_cost = _ZERO

    for level in levels:
        if remaining <= _ZERO:
            break
        qty = min(remaining, level.size)
        total_cost += qty * level.price
        remaining -= qty

    filled = trade.size - remaining
    if filled <= _ZERO:
        return SimulatedFill(
            trade=trade,
            intended_price=intended_price,
            fill_price=_ZERO,
            fill_size=_ZERO,
            cost_usdc=_ZERO,
            fee_usdc=_ZERO,
            gas_usdc=config.gas_usdc_per_order,
            slippage_bps=_ZERO,
            status="no_liquidity",
        )

    avg_price = total_cost / filled

    slippage_bps = _ZERO
    if intended_price > _ZERO:
        raw_slip = (avg_price - intended_price) / intended_price * Decimal("10000")
        slippage_bps = raw_slip.copy_abs()

    # Taker fee: use snapshot fee if available, else default
    fee_rate = _fee_rate_from_snap(snap, config.default_taker_fee)
    fee_usdc = total_cost * fee_rate

    status = "filled" if remaining == _ZERO else "partial"

    return SimulatedFill(
        trade=trade,
        intended_price=intended_price,
        fill_price=avg_price,
        fill_size=filled,
        cost_usdc=total_cost,
        fee_usdc=fee_usdc,
        gas_usdc=config.gas_usdc_per_order,
        slippage_bps=slippage_bps,
        status=status,
    )


# ---------------------------------------------------------------------------
# Group P&L
# ---------------------------------------------------------------------------

def _compute_group_pnl(
    group_id: str, fills: list[SimulatedFill], ts: str
) -> GroupPnL:
    """Compute P&L for one arb group.

    For S1_NEGRISK: guaranteed payoff = min(fill_size_per_leg) * 1.00.
    The minimum fill size handles partial fills; a full fill set yields
    exactly size * (1 - sum_of_fill_prices) gross profit.

    This is deterministic — no resolution assumption needed.
    """
    strategy = fills[0].trade.strategy if fills else "unknown"
    total_cost = sum(f.cost_usdc for f in fills)
    total_fees = sum(f.fee_usdc for f in fills)
    total_gas = sum(f.gas_usdc for f in fills)

    # Average slippage across legs
    avg_slip = (
        sum(f.slippage_bps for f in fills) / Decimal(len(fills))
        if fills else _ZERO
    )

    all_filled = all(f.status == "filled" for f in fills)

    if strategy == "S1_NEGRISK":
        min_fill = min(f.fill_size for f in fills) if fills else _ZERO
        expected_payoff = min_fill * _ONE
    elif strategy == "S1_INTRA":
        # YES + NO sum < 1: payoff = 1.00 per share of the winner side
        # We bought both; one will pay 1.00. Cost = sum of both prices.
        total_fill = sum(f.fill_size for f in fills)
        expected_payoff = total_fill / Decimal(len(fills)) if fills else _ZERO
    else:
        expected_payoff = _ZERO

    gross_pnl = expected_payoff - total_cost
    net_pnl = gross_pnl - total_fees - total_gas

    return GroupPnL(
        group_id=group_id,
        strategy=strategy,
        leg_count=len(fills),
        ts=ts,
        total_cost_usdc=total_cost,
        expected_payoff=expected_payoff,
        gross_pnl=gross_pnl,
        total_fees=total_fees,
        total_gas=total_gas,
        net_pnl=net_pnl,
        avg_slippage_bps=avg_slip,
        all_filled=all_filled,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_snap(snaps: list[SnapshotRow], token_id: str) -> SnapshotRow | None:
    for s in snaps:
        if s.token_id == token_id:
            return s
    return None


def _fee_rate_from_snap(snap: SnapshotRow, default: Decimal) -> Decimal:
    if snap.taker_fee_bps is None:
        return default
    return Decimal(snap.taker_fee_bps) / Decimal("10000")


def _fill_to_trade_row(
    fill: SimulatedFill, trade_ref: str, ts: str, mode: str
) -> TradeRow:
    return TradeRow(
        trade_ref=trade_ref,
        mode=mode,
        strategy=fill.trade.strategy,
        group_id=fill.trade.group_id,
        leg_index=fill.trade.leg_index,
        condition_id=fill.trade.condition_id,
        token_id=fill.trade.token_id,
        side=fill.trade.side,
        intended_price=fill.intended_price,
        intended_size=fill.trade.size,
        fill_price=fill.fill_price,
        fill_size=fill.fill_size,
        cost_usdc=fill.cost_usdc,
        fee_usdc=fill.fee_usdc,
        gas_usdc=fill.gas_usdc,
        slippage_bps=fill.slippage_bps,
        status=fill.status,
        created_at=ts,
        filled_at=ts if fill.status in ("filled", "partial") else None,
    )


def _slippage_bucket(bps: Decimal) -> str:
    b = float(bps)
    if b < 5:
        return "0-5bps"
    if b < 10:
        return "5-10bps"
    if b < 25:
        return "10-25bps"
    if b < 50:
        return "25-50bps"
    return "50bps+"
