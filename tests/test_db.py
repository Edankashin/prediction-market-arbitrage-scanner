"""Tests for db/schema.py and db/store.py.

All tests use :memory: SQLite — no disk access, no network.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from app.db import schema, store
from app.models import (
    BookLevel,
    DataApiPositionRow,
    FeeRateRow,
    KalshiMarketRow,
    KalshiSnapshotRow,
    MarketRow,
    PortfolioSnapshotRow,
    PositionRow,
    SnapshotRow,
    TradeRow,
)


def _market(condition_id: str = "cond_1", event_id: str | None = "evt_1") -> MarketRow:
    return MarketRow(
        condition_id=condition_id,
        question="Will X happen?",
        event_id=event_id,
        event_slug=None,
        event_title="Test event",
        neg_risk=True,
        active=True,
        closed=False,
        archived=False,
        end_date="2026-12-31",
        clob_token_ids=json.dumps(["tok_yes", "tok_no"]),
        outcomes=json.dumps(["Yes", "No"]),
        outcome_prices=json.dumps(["0.55", "0.45"]),
        volume=Decimal("10000"),
        liquidity=Decimal("5000"),
        spread=Decimal("0.01"),
        fees_enabled=True,
        taker_fee_cap_bps=100,
        maker_rebate_cap_bps=0,
        fee_formula='{"makerBaseFee":0,"takerBaseFee":100}',
        gamma_updated_at="2026-05-14T00:00:00+00:00",
        synced_at="2026-05-14T12:00:00+00:00",
    )


def _snap(
    token_id: str = "tok_yes",
    condition_id: str = "cond_1",
    ts: str = "2026-05-14T12:00:00+00:00",
    best_ask: str = "0.55",
    fee_bps: int | None = 100,
) -> SnapshotRow:
    ask_price = Decimal(best_ask)
    bid_price = ask_price - Decimal("0.01")
    return SnapshotRow(
        token_id=token_id,
        condition_id=condition_id,
        ts=ts,
        bids=[BookLevel(price=bid_price, size=Decimal("200"))],
        asks=[BookLevel(price=ask_price, size=Decimal("200"))],
        best_bid=bid_price,
        best_ask=ask_price,
        mid=(bid_price + ask_price) / Decimal("2"),
        taker_fee_bps=fee_bps,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_migrate_creates_tables(mem_db: sqlite3.Connection) -> None:
    """migrate() creates all expected tables."""
    cur = mem_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cur.fetchall()}
    expected = {
        "markets", "book_snapshots", "fee_rate_cache",
        "trades", "positions", "portfolio_snapshots",
        "data_api_positions", "scan_log", "schema_version",
        "kalshi_markets", "kalshi_book_snapshots",
        "cross_platform_pairs", "cross_platform_scan_log",
    }
    assert expected.issubset(tables)


def test_migrate_idempotent(mem_db: sqlite3.Connection) -> None:
    """migrate() can be called multiple times without error."""
    schema.migrate(mem_db)
    schema.migrate(mem_db)
    cur = mem_db.execute("SELECT version FROM schema_version")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == schema.CURRENT_VERSION


def test_schema_version_set(mem_db: sqlite3.Connection) -> None:
    cur = mem_db.execute("SELECT version FROM schema_version")
    assert cur.fetchone()[0] == 5


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------

def test_upsert_and_get_market(mem_db: sqlite3.Connection) -> None:
    m = _market()
    store.upsert_market(mem_db, m)
    fetched = store.get_market(mem_db, "cond_1")
    assert fetched is not None
    assert fetched.condition_id == "cond_1"
    assert fetched.neg_risk is True
    assert fetched.taker_fee_cap_bps == 100
    assert fetched.volume == Decimal("10000")
    assert fetched.fee_formula is not None


def test_upsert_market_updates_on_conflict(mem_db: sqlite3.Connection) -> None:
    m = _market()
    store.upsert_market(mem_db, m)
    updated = MarketRow(
        **{**m.__dict__, "question": "Updated question", "volume": Decimal("99999")}
    )
    store.upsert_market(mem_db, updated)
    fetched = store.get_market(mem_db, "cond_1")
    assert fetched is not None
    assert fetched.question == "Updated question"
    assert fetched.volume == Decimal("99999")


def test_get_market_missing_returns_none(mem_db: sqlite3.Connection) -> None:
    assert store.get_market(mem_db, "nonexistent") is None


def test_get_active_markets(mem_db: sqlite3.Connection) -> None:
    store.upsert_market(mem_db, _market("cond_a"))
    store.upsert_market(mem_db, _market("cond_b"))
    closed = MarketRow(**{**_market("cond_c").__dict__, "closed": True})
    store.upsert_market(mem_db, closed)

    actives = store.get_active_markets(mem_db)
    ids = {m.condition_id for m in actives}
    assert "cond_a" in ids
    assert "cond_b" in ids
    assert "cond_c" not in ids


def test_get_neg_risk_markets_for_event(mem_db: sqlite3.Connection) -> None:
    store.upsert_market(mem_db, _market("cond_1", event_id="evt_x"))
    store.upsert_market(mem_db, _market("cond_2", event_id="evt_x"))
    non_neg = MarketRow(**{**_market("cond_3", event_id="evt_x").__dict__, "neg_risk": False})
    store.upsert_market(mem_db, non_neg)

    results = store.get_neg_risk_markets_for_event(mem_db, "evt_x")
    ids = {m.condition_id for m in results}
    assert "cond_1" in ids
    assert "cond_2" in ids
    assert "cond_3" not in ids


# ---------------------------------------------------------------------------
# book_snapshots
# ---------------------------------------------------------------------------

def test_insert_and_get_snapshot(mem_db: sqlite3.Connection) -> None:
    store.upsert_market(mem_db, _market())
    snap = _snap()
    store.insert_snapshot(mem_db, snap)

    fetched = store.get_snapshots_for_token(
        mem_db, "tok_yes",
        "2026-05-14T11:00:00+00:00",
        "2026-05-14T13:00:00+00:00",
    )
    assert len(fetched) == 1
    assert fetched[0].token_id == "tok_yes"
    assert fetched[0].best_ask == Decimal("0.55")
    assert fetched[0].taker_fee_bps == 100
    assert len(fetched[0].asks) == 1
    assert fetched[0].asks[0].size == Decimal("200")


def test_get_distinct_timestamps(mem_db: sqlite3.Connection) -> None:
    store.upsert_market(mem_db, _market())
    store.insert_snapshot(mem_db, _snap(ts="2026-05-14T12:00:00+00:00"))
    store.insert_snapshot(mem_db, _snap(ts="2026-05-14T12:00:30+00:00"))
    store.insert_snapshot(mem_db, _snap(ts="2026-05-14T12:01:00+00:00"))

    ts_list = store.get_distinct_timestamps(
        mem_db,
        "2026-05-14T12:00:00+00:00",
        "2026-05-14T12:01:00+00:00",
    )
    assert len(ts_list) == 3


def test_delete_snapshots_before(mem_db: sqlite3.Connection) -> None:
    store.upsert_market(mem_db, _market())
    store.insert_snapshot(mem_db, _snap(ts="2026-05-01T00:00:00+00:00"))
    store.insert_snapshot(mem_db, _snap(ts="2026-05-14T12:00:00+00:00"))

    deleted = store.delete_snapshots_before(mem_db, "2026-05-10T00:00:00+00:00")
    assert deleted == 1

    remaining = store.get_snapshots_for_token(
        mem_db, "tok_yes", "2000-01-01", "2099-01-01"
    )
    assert len(remaining) == 1


# ---------------------------------------------------------------------------
# fee_rate_cache
# ---------------------------------------------------------------------------

def test_upsert_and_get_fee_rate(mem_db: sqlite3.Connection) -> None:
    store.upsert_fee_rate(mem_db, "tok_yes", maker_bps=0, taker_bps=100)
    row = store.get_fee_rate(mem_db, "tok_yes")
    assert row is not None
    assert row.taker_bps == 100
    assert row.maker_bps == 0


def test_get_fee_rate_missing(mem_db: sqlite3.Connection) -> None:
    assert store.get_fee_rate(mem_db, "nonexistent") is None


def test_upsert_fee_rate_updates(mem_db: sqlite3.Connection) -> None:
    store.upsert_fee_rate(mem_db, "tok_yes", 0, 100)
    store.upsert_fee_rate(mem_db, "tok_yes", 0, 180)
    row = store.get_fee_rate(mem_db, "tok_yes")
    assert row is not None
    assert row.taker_bps == 180


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------

def test_insert_and_get_trades_for_group(mem_db: sqlite3.Connection) -> None:
    store.upsert_market(mem_db, _market())
    t = TradeRow(
        trade_ref="ref_1",
        mode="backtest",
        strategy="S1_NEGRISK",
        condition_id="cond_1",
        token_id="tok_yes",
        side="BUY",
        intended_price=Decimal("0.30"),
        intended_size=Decimal("100"),
        created_at="2026-05-14T12:00:00+00:00",
        group_id="grp_1",
        leg_index=0,
        fill_price=Decimal("0.30"),
        fill_size=Decimal("100"),
        cost_usdc=Decimal("30.00"),
        fee_usdc=Decimal("0.30"),
        gas_usdc=Decimal("0.005"),
        slippage_bps=Decimal("0"),
        status="filled",
        filled_at="2026-05-14T12:00:00+00:00",
    )
    store.insert_trade(mem_db, t)
    legs = store.get_trades_for_group(mem_db, "grp_1")
    assert len(legs) == 1
    assert legs[0].fill_price == Decimal("0.30")
    assert legs[0].fee_usdc == Decimal("0.30")


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

def test_upsert_and_get_position(mem_db: sqlite3.Connection) -> None:
    store.upsert_market(mem_db, _market())
    pos = PositionRow(
        mode="paper",
        condition_id="cond_1",
        token_id="tok_yes",
        shares=Decimal("100"),
        avg_cost=Decimal("0.30"),
        realized_pnl=Decimal("0"),
        opened_at="2026-05-14T12:00:00+00:00",
        updated_at="2026-05-14T12:00:00+00:00",
    )
    store.upsert_position(mem_db, pos)
    fetched = store.get_position(mem_db, "paper", "tok_yes")
    assert fetched is not None
    assert fetched.shares == Decimal("100")


# ---------------------------------------------------------------------------
# portfolio_snapshots
# ---------------------------------------------------------------------------

def test_insert_and_get_equity_curve(mem_db: sqlite3.Connection) -> None:
    snap = PortfolioSnapshotRow(
        mode="backtest",
        ts="2026-05-14T12:00:00+00:00",
        usdc_balance=Decimal("1000"),
        position_value=Decimal("0"),
        total_equity=Decimal("1000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        trade_count=0,
        win_count=0,
    )
    store.insert_portfolio_snapshot(mem_db, snap)
    curve = store.get_equity_curve(
        mem_db, "backtest",
        "2026-05-14T11:00:00+00:00",
        "2026-05-14T13:00:00+00:00",
    )
    assert len(curve) == 1
    assert curve[0].total_equity == Decimal("1000")


# ---------------------------------------------------------------------------
# data_api_positions
# ---------------------------------------------------------------------------

def test_upsert_and_get_data_api_position(mem_db: sqlite3.Connection) -> None:
    pos = DataApiPositionRow(
        wallet="0xDEAD",
        condition_id="cond_1",
        token_id="tok_yes",
        outcome="Yes",
        shares=Decimal("50"),
        avg_price=Decimal("0.55"),
        realized_pnl=Decimal("10"),
        unrealized_pnl=Decimal("5"),
        fetched_at="2026-05-14T12:00:00+00:00",
    )
    store.upsert_data_api_position(mem_db, pos)
    rows = store.get_data_api_positions(mem_db, "0xDEAD")
    assert len(rows) == 1
    assert rows[0].shares == Decimal("50")
    assert rows[0].avg_price == Decimal("0.55")


# ---------------------------------------------------------------------------
# kalshi_markets
# ---------------------------------------------------------------------------

def _kalshi_market(ticker: str = "KXTEST-1") -> KalshiMarketRow:
    return KalshiMarketRow(
        ticker=ticker,
        event_ticker="KXTEST",
        title="Will X happen?",
        category="World",
        status="active",
        end_date="2026-12-31T00:00:00Z",
        yes_bid=Decimal("0.45"),
        volume=Decimal("5000"),
        taker_fee_coeff=Decimal("0.07"),
        synced_at="2026-05-16T12:00:00+00:00",
    )


def test_upsert_and_get_kalshi_market(mem_db: sqlite3.Connection) -> None:
    m = _kalshi_market()
    store.upsert_kalshi_market(mem_db, m)
    rows = store.get_active_kalshi_markets(mem_db)
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker == "KXTEST-1"
    assert r.event_ticker == "KXTEST"
    assert r.yes_bid == Decimal("0.45")
    assert r.volume == Decimal("5000")
    assert r.taker_fee_coeff == Decimal("0.07")


def test_kalshi_market_upsert_updates_on_conflict(mem_db: sqlite3.Connection) -> None:
    m = _kalshi_market()
    store.upsert_kalshi_market(mem_db, m)
    updated = KalshiMarketRow(
        **{**m.__dict__, "yes_bid": Decimal("0.48"), "volume": Decimal("9999")}
    )
    store.upsert_kalshi_market(mem_db, updated)
    rows = store.get_active_kalshi_markets(mem_db)
    assert len(rows) == 1
    assert rows[0].yes_bid == Decimal("0.48")
    assert rows[0].volume == Decimal("9999")


def test_get_active_kalshi_markets_excludes_closed(mem_db: sqlite3.Connection) -> None:
    store.upsert_kalshi_market(mem_db, _kalshi_market("KXOPEN"))
    closed = KalshiMarketRow(**{**_kalshi_market("KXCLOSED").__dict__, "status": "closed"})
    store.upsert_kalshi_market(mem_db, closed)
    rows = store.get_active_kalshi_markets(mem_db)
    tickers = {r.ticker for r in rows}
    assert "KXOPEN" in tickers
    assert "KXCLOSED" not in tickers


# ---------------------------------------------------------------------------
# kalshi_book_snapshots
# ---------------------------------------------------------------------------

def _kalshi_snap(ticker: str = "KXTEST-1", ts: str | None = None) -> KalshiSnapshotRow:
    from datetime import datetime, timezone
    actual_ts = ts if ts is not None else datetime.now(tz=timezone.utc).isoformat()
    return KalshiSnapshotRow(
        ticker=ticker,
        ts=actual_ts,
        yes_bids=[BookLevel(price=Decimal("0.55"), size=Decimal("100"))],
        no_bids=[BookLevel(price=Decimal("0.42"), size=Decimal("150"))],
        best_yes_ask=Decimal("0.58"),
        best_no_ask=Decimal("0.45"),
        single_sided=False,
    )


def test_insert_and_get_kalshi_snapshot(mem_db: sqlite3.Connection) -> None:
    store.upsert_kalshi_market(mem_db, _kalshi_market())
    snap = _kalshi_snap()  # uses datetime.now() — always within 90s window
    store.insert_kalshi_snapshot(mem_db, snap)

    rows = store.get_latest_kalshi_snapshots_in_window(mem_db, window_sec=90)
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker == "KXTEST-1"
    assert r.best_yes_ask == Decimal("0.58")
    assert r.best_no_ask == Decimal("0.45")
    assert r.single_sided is False
    assert len(r.yes_bids) == 1
    assert r.yes_bids[0].price == Decimal("0.55")


def test_kalshi_snapshot_single_sided(mem_db: sqlite3.Connection) -> None:
    from datetime import datetime, timezone
    store.upsert_kalshi_market(mem_db, _kalshi_market())
    snap = KalshiSnapshotRow(
        ticker="KXTEST-1",
        ts=datetime.now(tz=timezone.utc).isoformat(),
        yes_bids=[BookLevel(price=Decimal("0.55"), size=Decimal("100"))],
        no_bids=[],
        best_yes_ask=None,
        best_no_ask=Decimal("0.45"),
        single_sided=True,
    )
    store.insert_kalshi_snapshot(mem_db, snap)
    rows = store.get_latest_kalshi_snapshots_in_window(mem_db, window_sec=90)
    assert len(rows) == 1
    assert rows[0].single_sided is True
    assert rows[0].best_yes_ask is None
