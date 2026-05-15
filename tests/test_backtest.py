"""Backtest engine tests — including the M1 acceptance test.

ACCEPTANCE TEST (test_negrisk_arb_acceptance):
  Plant a known $5 NegRisk arb. The harness must report post-fee,
  post-gas, post-slippage net P&L within $0.01 of the analytical answer.

  Setup:
    3 markets in the same NegRisk event
    YES prices: 0.30, 0.40, 0.25  (sum = 0.95 → $5.00 gross on 100 shares)
    Taker fee:  1.00% (100 bps) per leg
    Gas:        $0.005 per leg × 3 = $0.015
    Book depth: 200 shares at stated ask — no slippage

  Analytical answer:
    Gross P&L  = 100 × (1.00 - 0.95)           = $5.000
    Fee A      = 1% × (100 × 0.30)             = $0.300
    Fee B      = 1% × (100 × 0.40)             = $0.400
    Fee C      = 1% × (100 × 0.25)             = $0.250
    Total fees                                  = $0.950
    Total gas  = $0.005 × 3                    = $0.015
    NET P&L                                     = $4.035

  Acceptance criterion: |harness_result - $4.035| < $0.01
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from decimal import Decimal

import pytest

from app.backtest.engine import (
    BacktestConfig,
    IntendedTrade,
    SnapshotRow,
    replay,
)
from app.backtest.metrics import pnl_breakdown, print_report
from app.db import schema, store
from app.models import BookLevel, MarketRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2026-05-14T12:00:00+00:00"
_DEPTH = Decimal("200")   # shares available at each ask level
_SHARES = Decimal("100")  # shares bought per leg

_LEGS = [
    {"condition_id": "mkt_a", "token_id": "tok_a", "ask": "0.30"},
    {"condition_id": "mkt_b", "token_id": "tok_b", "ask": "0.40"},
    {"condition_id": "mkt_c", "token_id": "tok_c", "ask": "0.25"},
]

_EVENT_ID = "evt_synthetic_001"
_FEE_BPS = 100   # 1% taker fee


def _insert_synthetic_markets(conn: sqlite3.Connection) -> None:
    for leg in _LEGS:
        row = MarketRow(
            condition_id=leg["condition_id"],
            question=f"Synthetic outcome {leg['condition_id']}?",
            event_id=_EVENT_ID,
            event_slug=None,
            event_title="Synthetic NegRisk Event",
            neg_risk=True,
            active=True,
            closed=False,
            archived=False,
            end_date="2026-12-31",
            clob_token_ids=json.dumps([leg["token_id"], f"tok_{leg['condition_id']}_no"]),
            outcomes=json.dumps(["Yes", "No"]),
            outcome_prices=json.dumps([leg["ask"], str(1 - Decimal(leg["ask"]))]),
            volume=Decimal("50000"),
            liquidity=Decimal("10000"),
            spread=Decimal("0.01"),
            fees_enabled=True,
            taker_fee_cap_bps=_FEE_BPS,
            maker_rebate_cap_bps=0,
            fee_formula=json.dumps({"takerBaseFee": _FEE_BPS}),
            gamma_updated_at=_TS,
            synced_at=_TS,
        )
        store.upsert_market(conn, row)


def _insert_synthetic_snapshots(conn: sqlite3.Connection) -> None:
    for leg in _LEGS:
        ask = Decimal(leg["ask"])
        bid = ask - Decimal("0.01")
        snap = SnapshotRow(
            token_id=leg["token_id"],
            condition_id=leg["condition_id"],
            ts=_TS,
            bids=[BookLevel(price=bid, size=_DEPTH)],
            asks=[BookLevel(price=ask, size=_DEPTH)],
            best_bid=bid,
            best_ask=ask,
            mid=(bid + ask) / Decimal("2"),
            taker_fee_bps=_FEE_BPS,
        )
        store.insert_snapshot(conn, snap)


def _arb_strategy(snaps: list[SnapshotRow]) -> list[IntendedTrade]:
    """Strategy stub: always buys 100 shares of each YES token."""
    group_id = f"grp_{_EVENT_ID}"
    return [
        IntendedTrade(
            condition_id=leg["condition_id"],
            token_id=leg["token_id"],
            side="BUY",
            size=_SHARES,
            strategy="S1_NEGRISK",
            group_id=group_id,
            leg_index=i,
        )
        for i, leg in enumerate(_LEGS)
    ]


# ---------------------------------------------------------------------------
# Acceptance test
# ---------------------------------------------------------------------------

def test_negrisk_arb_acceptance(mem_db: sqlite3.Connection) -> None:
    """M1 acceptance: $5 synthetic NegRisk arb P&L within $0.01 of $4.035."""
    _insert_synthetic_markets(mem_db)
    _insert_synthetic_snapshots(mem_db)

    config = BacktestConfig(
        initial_capital=Decimal("1000"),
        gas_usdc_per_order=Decimal("0.005"),
        default_taker_fee=Decimal("0.01"),
    )

    result = replay(
        conn=mem_db,
        strategy_fn=_arb_strategy,
        start_ts=_TS,
        end_ts=_TS,
        config=config,
    )

    # Analytical answer
    gross_expected = Decimal("5.000")  # 100 × (1.00 - 0.95)
    fees_expected = Decimal("0.950")   # 1% × (30+40+25)
    gas_expected = Decimal("0.015")    # 3 legs × $0.005
    net_expected = Decimal("4.035")    # gross - fees - gas

    assert len(result.group_pnls) == 1, "Expected exactly one arb group"
    gp = result.group_pnls[0]

    assert gp.all_filled, "All three legs must fill completely (ample depth)"
    assert gp.avg_slippage_bps == Decimal("0"), "Single-level book: zero slippage"
    assert abs(gp.gross_pnl - gross_expected) < Decimal("0.001"), (
        f"Gross P&L {gp.gross_pnl} vs expected {gross_expected}"
    )
    assert abs(gp.total_fees - fees_expected) < Decimal("0.001"), (
        f"Total fees {gp.total_fees} vs expected {fees_expected}"
    )
    assert abs(gp.total_gas - gas_expected) < Decimal("0.001"), (
        f"Total gas {gp.total_gas} vs expected {gas_expected}"
    )

    # The acceptance criterion: within $0.01
    delta = abs(result.total_net_pnl - net_expected)
    assert delta < Decimal("0.01"), (
        f"ACCEPTANCE FAILED: net P&L {result.total_net_pnl} "
        f"vs expected {net_expected}, delta={delta}"
    )

    # Equity curve: one checkpoint per distinct timestamp processed
    assert len(result.equity_curve) == 1, (
        f"Expected 1 equity checkpoint (one timestamp), got {len(result.equity_curve)}"
    )
    eq = result.equity_curve[0]
    assert eq.ts == _TS
    # total_equity = initial_capital - cost - fees - gas + expected_payoff
    # We don't assert exact value here (covered by total_net_pnl above);
    # just confirm it is a real positive number greater than zero.
    assert eq.total_equity > Decimal("0"), "Equity curve checkpoint must be positive"
    assert eq.trade_count == 3, "Three legs filled → trade_count = 3"
    assert eq.win_count == 1, "One arb group with positive net P&L → win_count = 1"

    # Equity curve is readable back from the DB
    eq_from_db = store.get_equity_curve(
        mem_db, mode="backtest", start_ts=_TS, end_ts=_TS
    )
    assert len(eq_from_db) == 1
    assert abs(eq_from_db[0].total_equity - eq.total_equity) < Decimal("0.001")

    # Print the report for the M1 milestone report
    print("\n--- M1 Acceptance Test Output ---")
    print_report(result)
    breakdown = pnl_breakdown(result)
    print(f"\nAnalytical expected net P&L: ${float(net_expected):.4f}")
    print(f"Harness reported net P&L:    ${float(result.total_net_pnl):.4f}")
    print(f"Delta (must be < $0.01):     ${float(delta):.6f}")
    print(f"RESULT: {'PASS ✓' if delta < Decimal('0.01') else 'FAIL ✗'}")
    print(f"Equity curve: {len(result.equity_curve)} checkpoint(s), "
          f"total_equity=${float(eq.total_equity):.4f}")


# ---------------------------------------------------------------------------
# Additional engine unit tests
# ---------------------------------------------------------------------------

def test_simulate_fill_walks_book(mem_db: sqlite3.Connection) -> None:
    """Fill price walks the book when depth is insufficient at best level."""
    store.upsert_market(
        mem_db,
        MarketRow(
            condition_id="mkt_walk",
            question="Walk test?",
            event_id="evt_walk",
            event_slug=None,
            event_title="Walk",
            neg_risk=False,
            active=True,
            closed=False,
            archived=False,
            end_date=None,
            clob_token_ids=json.dumps(["tok_walk_yes", "tok_walk_no"]),
            outcomes=None,
            outcome_prices=None,
            volume=None,
            liquidity=None,
            spread=None,
            fees_enabled=True,
            taker_fee_cap_bps=50,
            maker_rebate_cap_bps=0,
            fee_formula=None,
            gamma_updated_at=None,
            synced_at=_TS,
        ),
    )
    # Two price levels: first 50 shares at 0.50, then 50 shares at 0.55
    snap = SnapshotRow(
        token_id="tok_walk_yes",
        condition_id="mkt_walk",
        ts=_TS,
        bids=[],
        asks=[
            BookLevel(price=Decimal("0.50"), size=Decimal("50")),
            BookLevel(price=Decimal("0.55"), size=Decimal("50")),
        ],
        best_bid=None,
        best_ask=Decimal("0.50"),
        mid=None,
        taker_fee_bps=50,
    )
    store.insert_snapshot(mem_db, snap)

    def walk_strategy(snaps: list[SnapshotRow]) -> list[IntendedTrade]:
        return [
            IntendedTrade(
                condition_id="mkt_walk",
                token_id="tok_walk_yes",
                side="BUY",
                size=Decimal("100"),  # needs both levels
                strategy="S1_INTRA",
                group_id="grp_walk",
                leg_index=0,
            )
        ]

    config = BacktestConfig(
        initial_capital=Decimal("1000"),
        gas_usdc_per_order=Decimal("0"),
    )
    result = replay(mem_db, walk_strategy, _TS, _TS, config)

    assert result.fill_count == 1
    gp = result.group_pnls[0]
    # avg fill price = (50×0.50 + 50×0.55)/100 = 52.50/100 = 0.525
    # slippage = (0.525 - 0.50)/0.50 × 10000 = 500 bps
    assert gp.avg_slippage_bps > Decimal("0"), "Should have nonzero slippage"


def test_partial_fill_status(mem_db: sqlite3.Connection) -> None:
    """When book depth < order size, status='partial'."""
    store.upsert_market(
        mem_db,
        MarketRow(
            condition_id="mkt_thin",
            question="Thin?",
            event_id="evt_thin",
            event_slug=None,
            event_title="Thin",
            neg_risk=False,
            active=True,
            closed=False,
            archived=False,
            end_date=None,
            clob_token_ids=json.dumps(["tok_thin", "tok_thin_no"]),
            outcomes=None,
            outcome_prices=None,
            volume=None,
            liquidity=None,
            spread=None,
            fees_enabled=True,
            taker_fee_cap_bps=100,
            maker_rebate_cap_bps=0,
            fee_formula=None,
            gamma_updated_at=None,
            synced_at=_TS,
        ),
    )
    snap = SnapshotRow(
        token_id="tok_thin",
        condition_id="mkt_thin",
        ts=_TS,
        bids=[],
        asks=[BookLevel(price=Decimal("0.50"), size=Decimal("30"))],  # only 30 available
        best_bid=None,
        best_ask=Decimal("0.50"),
        mid=None,
        taker_fee_bps=100,
    )
    store.insert_snapshot(mem_db, snap)

    def thin_strategy(snaps: list[SnapshotRow]) -> list[IntendedTrade]:
        return [
            IntendedTrade(
                condition_id="mkt_thin",
                token_id="tok_thin",
                side="BUY",
                size=Decimal("100"),
                strategy="S1_NEGRISK",
                group_id="grp_thin",
                leg_index=0,
            )
        ]

    config = BacktestConfig(
        initial_capital=Decimal("1000"),
        gas_usdc_per_order=Decimal("0"),
    )
    result = replay(mem_db, thin_strategy, _TS, _TS, config)

    legs = store.get_trades_for_group(mem_db, "grp_thin")
    assert len(legs) == 1
    assert legs[0].status == "partial"
    assert legs[0].fill_size == Decimal("30")


def test_no_strategy_output_no_trades(mem_db: sqlite3.Connection) -> None:
    """If strategy returns empty list, no trades are recorded."""
    result = replay(
        mem_db,
        lambda snaps: [],
        "2026-05-14T12:00:00+00:00",
        "2026-05-14T13:00:00+00:00",
        BacktestConfig(Decimal("1000"), Decimal("0.005")),
    )
    assert result.fill_count == 0
    assert result.total_net_pnl == Decimal("0")
