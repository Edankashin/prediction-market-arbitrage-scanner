"""Tests for scripts/backfill_scan_log.py.

Uses tmp_path (file-backed SQLite) so the backfill can open its own connection.
Never touches data/polymarket.db.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import schema, store
from app.models import BookLevel, MarketRow, SnapshotRow


# ---------------------------------------------------------------------------
# Load backfill module from scripts/
# ---------------------------------------------------------------------------

def _load_backfill():
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "backfill_scan_log.py"
    spec = importlib.util.spec_from_file_location("backfill_scan_log", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_backfill = _load_backfill()
run_backfill = _backfill.run_backfill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_A = "2026-05-15T12:00:05.000000+00:00"
_TS_B = "2026-05-15T12:00:30.000000+00:00"


def _seed(db: sqlite3.Connection, event_id: str = "evt_test") -> None:
    """Plant two NegRisk legs with ask=0.30 each (sum=0.60 < 1.0 → arb)."""
    now = datetime.now(tz=timezone.utc).isoformat()
    for cid, tok, ts in [("cond_A", "tok_A", _TS_A), ("cond_B", "tok_B", _TS_B)]:
        store.upsert_market(db, MarketRow(
            condition_id=cid,
            question=f"Q {cid}",
            event_id=event_id,
            event_slug=None,
            event_title="Test Event",
            neg_risk=True,
            active=True,
            closed=False,
            archived=False,
            end_date=None,
            clob_token_ids=json.dumps([tok]),
            outcomes=None,
            outcome_prices=None,
            volume=Decimal("1000"),
            liquidity=Decimal("500"),
            spread=Decimal("0.01"),
            fees_enabled=True,
            taker_fee_cap_bps=None,
            maker_rebate_cap_bps=None,
            fee_formula=None,
            gamma_updated_at=None,
            synced_at=now,
        ))
        p = Decimal("0.30")
        bid = p - Decimal("0.01")
        store.insert_snapshot(db, SnapshotRow(
            token_id=tok,
            condition_id=cid,
            ts=ts,
            bids=[BookLevel(price=bid, size=Decimal("500"))],
            asks=[BookLevel(price=p, size=Decimal("500"))],
            best_bid=bid,
            best_ask=p,
            mid=(bid + p) / Decimal("2"),
            taker_fee_bps=0,
        ))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_backfill_finds_opportunities(tmp_path: Path) -> None:
    """Backfill inserts scan_log rows with discovered_at='BACKFILL' for planted arb."""
    db_path = str(tmp_path / "test.db")
    db = sqlite3.connect(db_path)
    schema.migrate(db)
    _seed(db)
    db.close()

    result = run_backfill(db_path)

    assert result["skipped"] is False
    assert result["opps_found"] >= 1

    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT discovered_at, net_edge_bps FROM scan_log").fetchall()
    db.close()

    assert len(rows) >= 1
    assert all(r[0] == "BACKFILL" for r in rows)
    assert all(r[1] >= 1 for r in rows)


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    """Running backfill twice produces no duplicate scan_log rows."""
    db_path = str(tmp_path / "test.db")
    db = sqlite3.connect(db_path)
    schema.migrate(db)
    _seed(db)
    db.close()

    r1 = run_backfill(db_path)
    assert r1["skipped"] is False
    count_after_first = r1["opps_found"]
    assert count_after_first >= 1

    r2 = run_backfill(db_path)
    assert r2["skipped"] is True

    db = sqlite3.connect(db_path)
    total = db.execute(
        "SELECT COUNT(*) FROM scan_log WHERE discovered_at='BACKFILL'"
    ).fetchone()[0]
    db.close()

    assert total == count_after_first
