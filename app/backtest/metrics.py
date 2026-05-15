"""Backtest metrics: Sharpe ratio, max drawdown, slippage histogram, P&L breakdown.

All inputs and outputs use Decimal.
No pandas in the live path.
"""
from __future__ import annotations

import math
from decimal import Decimal

from app.backtest.engine import BacktestResult, GroupPnL

_ZERO = Decimal("0")
_ANNUALISE_FACTOR = Decimal("365")  # daily returns → annualised


def sharpe_ratio(equity_curve: list[Decimal]) -> Decimal | None:
    """Compute Sharpe ratio from a series of equity values (not returns).

    Assumes each entry is one trading period (no specific time unit assumed).
    Returns None if fewer than 2 data points or zero standard deviation.
    """
    if len(equity_curve) < 2:
        return None

    returns = [
        equity_curve[i] / equity_curve[i - 1] - Decimal("1")
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] != _ZERO
    ]
    if not returns:
        return None

    n = Decimal(len(returns))
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / n

    if variance <= _ZERO:
        return None

    std = Decimal(str(math.sqrt(float(variance))))
    if std == _ZERO:
        return None

    return (mean / std * _ANNUALISE_FACTOR.sqrt()).quantize(Decimal("0.0001"))


def max_drawdown(equity_curve: list[Decimal]) -> Decimal:
    """Compute maximum peak-to-trough drawdown as a positive fraction.

    E.g., 0.20 means the portfolio fell 20% from its peak at some point.
    Returns 0 if the curve never falls below a prior peak.
    """
    if not equity_curve:
        return _ZERO

    peak = equity_curve[0]
    max_dd = _ZERO

    for equity in equity_curve:
        if equity > peak:
            peak = equity
        if peak > _ZERO:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd

    return max_dd.quantize(Decimal("0.0001"))


def slippage_histogram(result: BacktestResult) -> dict[str, int]:
    """Return the slippage bucket counts from a BacktestResult."""
    return dict(result.slippage_histogram)


def pnl_breakdown(result: BacktestResult) -> dict[str, Decimal]:
    """Summarise gross/net P&L and cost components."""
    return {
        "gross_pnl": result.total_gross_pnl.quantize(Decimal("0.01")),
        "total_fees": result.total_fees.quantize(Decimal("0.01")),
        "total_gas": result.total_gas.quantize(Decimal("0.01")),
        "net_pnl": result.total_net_pnl.quantize(Decimal("0.01")),
        "fee_drag_pct": (
            result.total_fees / result.total_gross_pnl * Decimal("100")
            if result.total_gross_pnl > _ZERO
            else _ZERO
        ).quantize(Decimal("0.01")),
    }


def hit_rate(result: BacktestResult) -> Decimal:
    """Fraction of arb groups that were net-profitable."""
    total = len(result.group_pnls)
    if total == 0:
        return _ZERO
    return (Decimal(result.win_count) / Decimal(total)).quantize(Decimal("0.0001"))


def print_report(result: BacktestResult) -> None:
    """Print a human-readable summary of a BacktestResult to stdout."""
    breakdown = pnl_breakdown(result)
    slip = slippage_histogram(result)

    print("=" * 60)
    print("BACKTEST REPORT")
    print("=" * 60)
    print(f"  Groups (arb opportunities):  {len(result.group_pnls)}")
    print(f"  Fill count (legs):           {result.fill_count}")
    print(f"  Win rate:                    {float(hit_rate(result)) * 100:.1f}%")
    print()
    print("  P&L breakdown:")
    print(f"    Gross P&L:   ${float(breakdown['gross_pnl']):>10.4f}")
    print(f"    Fees:       -${float(breakdown['total_fees']):>10.4f}")
    print(f"    Gas:        -${float(breakdown['total_gas']):>10.4f}")
    print(f"    Net P&L:     ${float(breakdown['net_pnl']):>10.4f}")
    print(f"    Fee drag:    {float(breakdown['fee_drag_pct']):>8.2f}%")
    print()
    if result.equity_curve:
        equities = [p.total_equity for p in result.equity_curve]
        print(f"  Sharpe ratio:    {result.sharpe}")
        print(f"  Max drawdown:    {float(result.max_drawdown or 0) * 100:.2f}%")
    print()
    print("  Slippage histogram:")
    for bucket, count in sorted(slip.items()):
        print(f"    {bucket:>12}: {count}")
    print("=" * 60)


def group_pnl_table(groups: list[GroupPnL]) -> list[dict[str, object]]:
    """Convert GroupPnL list to a list of plain dicts for tabular display."""
    return [
        {
            "group_id": g.group_id[:8],
            "strategy": g.strategy,
            "legs": g.leg_count,
            "ts": g.ts,
            "gross_pnl": float(g.gross_pnl.quantize(Decimal("0.0001"))),
            "fees": float(g.total_fees.quantize(Decimal("0.0001"))),
            "gas": float(g.total_gas.quantize(Decimal("0.0001"))),
            "net_pnl": float(g.net_pnl.quantize(Decimal("0.0001"))),
            "avg_slip_bps": float(g.avg_slippage_bps.quantize(Decimal("0.01"))),
            "all_filled": g.all_filled,
        }
        for g in groups
    ]
