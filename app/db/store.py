"""Database read/write functions.

All SQL lives here. No raw SQL outside this module.
Monetary values enter/exit as Decimal; stored as TEXT.
JSON blobs (bids/asks levels, token ID arrays) serialized here.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app.models import (
    BookLevel,
    DataApiPositionRow,
    FeeRateRow,
    MarketRow,
    PortfolioSnapshotRow,
    PositionRow,
    SnapshotRow,
    TradeRow,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _d(v: str | None) -> Decimal | None:
    return Decimal(v) if v is not None else None


def _s(v: Decimal | None) -> str | None:
    return str(v) if v is not None else None


def _levels_to_json(levels: list[BookLevel]) -> str:
    return json.dumps([{"price": str(lv.price), "size": str(lv.size)} for lv in levels])


def _json_to_levels(raw: str) -> list[BookLevel]:
    return [BookLevel(price=Decimal(d["price"]), size=Decimal(d["size"])) for d in json.loads(raw)]


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------

def upsert_market(conn: sqlite3.Connection, row: MarketRow) -> None:
    """Insert or replace a market row."""
    conn.execute(
        """
        INSERT INTO markets (
            condition_id, question, event_id, event_slug, event_title,
            neg_risk, active, closed, archived, end_date,
            clob_token_ids, outcomes, outcome_prices,
            volume, liquidity, spread,
            fees_enabled, taker_fee_cap_bps, maker_rebate_cap_bps, fee_formula,
            gamma_updated_at, synced_at
        ) VALUES (
            :condition_id, :question, :event_id, :event_slug, :event_title,
            :neg_risk, :active, :closed, :archived, :end_date,
            :clob_token_ids, :outcomes, :outcome_prices,
            :volume, :liquidity, :spread,
            :fees_enabled, :taker_fee_cap_bps, :maker_rebate_cap_bps, :fee_formula,
            :gamma_updated_at, :synced_at
        )
        ON CONFLICT(condition_id) DO UPDATE SET
            question=excluded.question, event_id=excluded.event_id,
            event_slug=excluded.event_slug, event_title=excluded.event_title,
            neg_risk=excluded.neg_risk, active=excluded.active,
            closed=excluded.closed, archived=excluded.archived,
            end_date=excluded.end_date, clob_token_ids=excluded.clob_token_ids,
            outcomes=excluded.outcomes, outcome_prices=excluded.outcome_prices,
            volume=excluded.volume, liquidity=excluded.liquidity,
            spread=excluded.spread, fees_enabled=excluded.fees_enabled,
            taker_fee_cap_bps=excluded.taker_fee_cap_bps,
            maker_rebate_cap_bps=excluded.maker_rebate_cap_bps,
            fee_formula=excluded.fee_formula,
            gamma_updated_at=excluded.gamma_updated_at,
            synced_at=excluded.synced_at
        """,
        {
            "condition_id": row.condition_id,
            "question": row.question,
            "event_id": row.event_id,
            "event_slug": row.event_slug,
            "event_title": row.event_title,
            "neg_risk": int(row.neg_risk),
            "active": int(row.active),
            "closed": int(row.closed),
            "archived": int(row.archived),
            "end_date": row.end_date,
            "clob_token_ids": row.clob_token_ids,
            "outcomes": row.outcomes,
            "outcome_prices": row.outcome_prices,
            "volume": _s(row.volume),
            "liquidity": _s(row.liquidity),
            "spread": _s(row.spread),
            "fees_enabled": int(row.fees_enabled),
            "taker_fee_cap_bps": row.taker_fee_cap_bps,
            "maker_rebate_cap_bps": row.maker_rebate_cap_bps,
            "fee_formula": row.fee_formula,
            "gamma_updated_at": row.gamma_updated_at,
            "synced_at": row.synced_at,
        },
    )
    conn.commit()


def get_market(conn: sqlite3.Connection, condition_id: str) -> MarketRow | None:
    """Fetch one market by condition_id."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
    )
    row = cur.fetchone()
    return _market_from_row(row) if row else None


def get_active_markets(conn: sqlite3.Connection) -> list[MarketRow]:
    """Fetch all active, non-closed, non-archived markets."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM markets WHERE active=1 AND closed=0 AND archived=0"
    )
    return [_market_from_row(r) for r in cur.fetchall()]


def get_neg_risk_markets_for_event(
    conn: sqlite3.Connection, event_id: str
) -> list[MarketRow]:
    """Fetch all neg_risk markets belonging to one event."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM markets WHERE event_id=? AND neg_risk=1 AND active=1 AND closed=0",
        (event_id,),
    )
    return [_market_from_row(r) for r in cur.fetchall()]


def _market_from_row(row: sqlite3.Row) -> MarketRow:
    return MarketRow(
        condition_id=row["condition_id"],
        question=row["question"],
        event_id=row["event_id"],
        event_slug=row["event_slug"],
        event_title=row["event_title"],
        neg_risk=bool(row["neg_risk"]),
        active=bool(row["active"]),
        closed=bool(row["closed"]),
        archived=bool(row["archived"]),
        end_date=row["end_date"],
        clob_token_ids=row["clob_token_ids"],
        outcomes=row["outcomes"],
        outcome_prices=row["outcome_prices"],
        volume=_d(row["volume"]),
        liquidity=_d(row["liquidity"]),
        spread=_d(row["spread"]),
        fees_enabled=bool(row["fees_enabled"]),
        taker_fee_cap_bps=row["taker_fee_cap_bps"],
        maker_rebate_cap_bps=row["maker_rebate_cap_bps"],
        fee_formula=row["fee_formula"],
        gamma_updated_at=row["gamma_updated_at"],
        synced_at=row["synced_at"],
    )


# ---------------------------------------------------------------------------
# book_snapshots
# ---------------------------------------------------------------------------

def insert_snapshot(conn: sqlite3.Connection, snap: SnapshotRow) -> None:
    """Append one orderbook snapshot."""
    conn.execute(
        """
        INSERT INTO book_snapshots
            (token_id, condition_id, ts, bids, asks,
             best_bid, best_ask, mid, taker_fee_bps)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.token_id,
            snap.condition_id,
            snap.ts,
            _levels_to_json(snap.bids),
            _levels_to_json(snap.asks),
            _s(snap.best_bid),
            _s(snap.best_ask),
            _s(snap.mid),
            snap.taker_fee_bps,
        ),
    )
    conn.commit()


def get_snapshots_for_token(
    conn: sqlite3.Connection,
    token_id: str,
    start_ts: str,
    end_ts: str,
) -> list[SnapshotRow]:
    """Time-range query for one token — used by backtest engine."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT * FROM book_snapshots
        WHERE token_id=? AND ts >= ? AND ts <= ?
        ORDER BY ts
        """,
        (token_id, start_ts, end_ts),
    )
    return [_snap_from_row(r) for r in cur.fetchall()]


def get_distinct_timestamps(
    conn: sqlite3.Connection, start_ts: str, end_ts: str
) -> list[str]:
    """Ordered list of distinct snapshot timestamps in the given window."""
    cur = conn.execute(
        """
        SELECT DISTINCT ts FROM book_snapshots
        WHERE ts >= ? AND ts <= ?
        ORDER BY ts
        """,
        (start_ts, end_ts),
    )
    return [row[0] for row in cur.fetchall()]


def get_snapshots_at_ts(
    conn: sqlite3.Connection, ts: str
) -> list[SnapshotRow]:
    """All tokens' snapshots for one exact timestamp."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM book_snapshots WHERE ts=?", (ts,)
    )
    return [_snap_from_row(r) for r in cur.fetchall()]


def delete_snapshots_before(conn: sqlite3.Connection, ts: str) -> int:
    """Prune snapshots older than ts. Returns number of rows deleted."""
    cur = conn.execute("DELETE FROM book_snapshots WHERE ts < ?", (ts,))
    conn.commit()
    return cur.rowcount


def _snap_from_row(row: sqlite3.Row) -> SnapshotRow:
    return SnapshotRow(
        token_id=row["token_id"],
        condition_id=row["condition_id"],
        ts=row["ts"],
        bids=_json_to_levels(row["bids"]),
        asks=_json_to_levels(row["asks"]),
        best_bid=_d(row["best_bid"]),
        best_ask=_d(row["best_ask"]),
        mid=_d(row["mid"]),
        taker_fee_bps=row["taker_fee_bps"],
    )


# ---------------------------------------------------------------------------
# fee_rate_cache
# ---------------------------------------------------------------------------

def get_fee_rate(conn: sqlite3.Connection, token_id: str) -> FeeRateRow | None:
    """Return cached fee rate, or None if not cached."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM fee_rate_cache WHERE token_id=?", (token_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    return FeeRateRow(
        token_id=row["token_id"],
        maker_bps=row["maker_bps"],
        taker_bps=row["taker_bps"],
        fetched_at=row["fetched_at"],
    )


def upsert_fee_rate(
    conn: sqlite3.Connection, token_id: str, maker_bps: int, taker_bps: int
) -> None:
    """Cache or update fee rates for a token."""
    conn.execute(
        """
        INSERT INTO fee_rate_cache(token_id, maker_bps, taker_bps, fetched_at)
        VALUES(?,?,?,?)
        ON CONFLICT(token_id) DO UPDATE SET
            maker_bps=excluded.maker_bps,
            taker_bps=excluded.taker_bps,
            fetched_at=excluded.fetched_at
        """,
        (token_id, maker_bps, taker_bps, _now()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------

def insert_trade(conn: sqlite3.Connection, trade: TradeRow) -> None:
    """Append a new trade record."""
    conn.execute(
        """
        INSERT INTO trades (
            trade_ref, mode, strategy, group_id, leg_index,
            condition_id, token_id, side,
            intended_price, intended_size,
            fill_price, fill_size, cost_usdc,
            fee_usdc, gas_usdc, slippage_bps,
            status, created_at, filled_at
        ) VALUES (
            :trade_ref, :mode, :strategy, :group_id, :leg_index,
            :condition_id, :token_id, :side,
            :intended_price, :intended_size,
            :fill_price, :fill_size, :cost_usdc,
            :fee_usdc, :gas_usdc, :slippage_bps,
            :status, :created_at, :filled_at
        )
        """,
        {
            "trade_ref": trade.trade_ref,
            "mode": trade.mode,
            "strategy": trade.strategy,
            "group_id": trade.group_id,
            "leg_index": trade.leg_index,
            "condition_id": trade.condition_id,
            "token_id": trade.token_id,
            "side": trade.side,
            "intended_price": str(trade.intended_price),
            "intended_size": str(trade.intended_size),
            "fill_price": _s(trade.fill_price),
            "fill_size": _s(trade.fill_size),
            "cost_usdc": _s(trade.cost_usdc),
            "fee_usdc": str(trade.fee_usdc),
            "gas_usdc": str(trade.gas_usdc),
            "slippage_bps": _s(trade.slippage_bps),
            "status": trade.status,
            "created_at": trade.created_at,
            "filled_at": trade.filled_at,
        },
    )
    conn.commit()


def get_trades_for_group(
    conn: sqlite3.Connection, group_id: str
) -> list[TradeRow]:
    """All trade legs for one arb group."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM trades WHERE group_id=? ORDER BY leg_index",
        (group_id,),
    )
    return [_trade_from_row(r) for r in cur.fetchall()]


def _trade_from_row(row: sqlite3.Row) -> TradeRow:
    return TradeRow(
        id=row["id"],
        trade_ref=row["trade_ref"],
        mode=row["mode"],
        strategy=row["strategy"],
        group_id=row["group_id"],
        leg_index=row["leg_index"],
        condition_id=row["condition_id"],
        token_id=row["token_id"],
        side=row["side"],
        intended_price=Decimal(row["intended_price"]),
        intended_size=Decimal(row["intended_size"]),
        fill_price=_d(row["fill_price"]),
        fill_size=_d(row["fill_size"]),
        cost_usdc=_d(row["cost_usdc"]),
        fee_usdc=Decimal(row["fee_usdc"]),
        gas_usdc=Decimal(row["gas_usdc"]),
        slippage_bps=_d(row["slippage_bps"]),
        status=row["status"],
        created_at=row["created_at"],
        filled_at=row["filled_at"],
    )


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

def upsert_position(conn: sqlite3.Connection, pos: PositionRow) -> None:
    """Insert or update a position."""
    conn.execute(
        """
        INSERT INTO positions
            (mode, condition_id, token_id, shares, avg_cost, realized_pnl,
             opened_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(mode, token_id) DO UPDATE SET
            shares=excluded.shares, avg_cost=excluded.avg_cost,
            realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at
        """,
        (
            pos.mode, pos.condition_id, pos.token_id,
            str(pos.shares), str(pos.avg_cost), str(pos.realized_pnl),
            pos.opened_at, pos.updated_at,
        ),
    )
    conn.commit()


def get_position(
    conn: sqlite3.Connection, mode: str, token_id: str
) -> PositionRow | None:
    """Fetch current position for one (mode, token_id) pair."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM positions WHERE mode=? AND token_id=?", (mode, token_id)
    )
    row = cur.fetchone()
    if row is None:
        return None
    return PositionRow(
        id=row["id"],
        mode=row["mode"],
        condition_id=row["condition_id"],
        token_id=row["token_id"],
        shares=Decimal(row["shares"]),
        avg_cost=Decimal(row["avg_cost"]),
        realized_pnl=Decimal(row["realized_pnl"]),
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# portfolio_snapshots
# ---------------------------------------------------------------------------

def insert_portfolio_snapshot(
    conn: sqlite3.Connection, snap: PortfolioSnapshotRow
) -> None:
    """Append an equity curve data point."""
    conn.execute(
        """
        INSERT INTO portfolio_snapshots
            (mode, ts, usdc_balance, position_value, total_equity,
             realized_pnl, unrealized_pnl, trade_count, win_count)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            snap.mode, snap.ts,
            str(snap.usdc_balance), str(snap.position_value),
            str(snap.total_equity), str(snap.realized_pnl),
            str(snap.unrealized_pnl),
            snap.trade_count, snap.win_count,
        ),
    )
    conn.commit()


def get_equity_curve(
    conn: sqlite3.Connection, mode: str, start_ts: str, end_ts: str
) -> list[PortfolioSnapshotRow]:
    """Ordered equity curve for one mode in the given time window."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT * FROM portfolio_snapshots
        WHERE mode=? AND ts>=? AND ts<=?
        ORDER BY ts
        """,
        (mode, start_ts, end_ts),
    )
    return [
        PortfolioSnapshotRow(
            id=row["id"],
            mode=row["mode"],
            ts=row["ts"],
            usdc_balance=Decimal(row["usdc_balance"]),
            position_value=Decimal(row["position_value"]),
            total_equity=Decimal(row["total_equity"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            unrealized_pnl=Decimal(row["unrealized_pnl"]),
            trade_count=row["trade_count"],
            win_count=row["win_count"],
        )
        for row in cur.fetchall()
    ]


# ---------------------------------------------------------------------------
# data_api_positions
# ---------------------------------------------------------------------------

def upsert_data_api_position(
    conn: sqlite3.Connection, pos: DataApiPositionRow
) -> None:
    """Cache on-chain position data from the Data API."""
    conn.execute(
        """
        INSERT INTO data_api_positions
            (wallet, condition_id, token_id, outcome, shares,
             avg_price, realized_pnl, unrealized_pnl, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(wallet, token_id) DO UPDATE SET
            condition_id=excluded.condition_id, outcome=excluded.outcome,
            shares=excluded.shares, avg_price=excluded.avg_price,
            realized_pnl=excluded.realized_pnl,
            unrealized_pnl=excluded.unrealized_pnl,
            fetched_at=excluded.fetched_at
        """,
        (
            pos.wallet, pos.condition_id, pos.token_id, pos.outcome,
            str(pos.shares), _s(pos.avg_price), _s(pos.realized_pnl),
            _s(pos.unrealized_pnl), pos.fetched_at,
        ),
    )
    conn.commit()


def get_data_api_positions(
    conn: sqlite3.Connection, wallet: str
) -> list[DataApiPositionRow]:
    """All cached on-chain positions for a wallet address."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM data_api_positions WHERE wallet=?", (wallet,)
    )
    return [
        DataApiPositionRow(
            id=row["id"],
            wallet=row["wallet"],
            condition_id=row["condition_id"],
            token_id=row["token_id"],
            outcome=row["outcome"],
            shares=Decimal(row["shares"]),
            avg_price=_d(row["avg_price"]),
            realized_pnl=_d(row["realized_pnl"]),
            unrealized_pnl=_d(row["unrealized_pnl"]),
            fetched_at=row["fetched_at"],
        )
        for row in cur.fetchall()
    ]
