"""Tests for scan_log: DB operations, cmd_scan, cmd_scan_log, loop integration.

All DB work uses in-memory SQLite (mem_db fixture) or pytest tmp_path for
tests that need cmd_scan to open its own file connection.  The live DB at
data/polymarket.db is never touched.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import types
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest

from app.db import schema, store
from app.models import BookLevel, MarketRow, SnapshotRow
from app.run import cmd_loop, cmd_scan, cmd_scan_log
from app.strategies.negrisk_scanner import ArbLeg, ArbOpportunity


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _iso_ago(seconds: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _make_market(
    condition_id: str,
    event_id: str,
    *,
    neg_risk: bool = True,
    event_title: str = "Test Event",
) -> MarketRow:
    return MarketRow(
        condition_id=condition_id,
        question=f"Will {condition_id} resolve Yes?",
        event_id=event_id,
        event_slug=None,
        event_title=event_title,
        neg_risk=neg_risk,
        active=True,
        closed=False,
        archived=False,
        end_date=None,
        clob_token_ids=json.dumps([f"tok_{condition_id}"]),
        outcomes=None,
        outcome_prices=None,
        volume=Decimal("1000"),
        liquidity=Decimal("500"),
        spread=Decimal("0.01"),
        fees_enabled=True,
        taker_fee_cap_bps=None,
        maker_rebate_cap_bps=None,
        fee_formula=None,          # fee_bps_from_formula returns 0 when None
        gamma_updated_at=None,
        synced_at=_now_iso(),
    )


def _make_snap(
    condition_id: str,
    token_id: str,
    ts: str,
    ask_price: str = "0.30",
) -> SnapshotRow:
    p = Decimal(ask_price)
    return SnapshotRow(
        token_id=token_id,
        condition_id=condition_id,
        ts=ts,
        bids=[],
        asks=[BookLevel(price=p, size=Decimal("1000"))],
        best_bid=None,
        best_ask=p,
        mid=p,
        taker_fee_bps=None,
    )


def _make_opp(event_id: str, ts: str) -> ArbOpportunity:
    """Minimal ArbOpportunity for round-trip and row-level tests."""
    leg = ArbLeg(
        condition_id="cond_a",
        token_id="tok_a",
        side="BUY",
        best_ask=Decimal("0.30"),
        avg_fill_price=Decimal("0.30"),
        fill_shares=Decimal("10"),
        fee_bps=0,
        fee_usdc=Decimal("0"),
        slippage_bps=Decimal("0"),
        slippage_usdc=Decimal("0"),
        gas_usdc=Decimal("0.005"),
    )
    return ArbOpportunity(
        event_id=event_id,
        ts=ts,
        legs=(leg, leg),
        optimal_shares=Decimal("10"),
        total_cost_usdc=Decimal("6.00"),
        guaranteed_payoff_usdc=Decimal("10.00"),
        gross_edge_bps=4000,
        net_edge_bps=3990,
        total_fee_usdc=Decimal("0"),
        total_slippage_usdc=Decimal("0"),
        total_gas_usdc=Decimal("0.010"),
        dominant_cost="gas",
    )


def _make_cfg(db_path: str) -> object:
    cfg = types.SimpleNamespace()
    cfg.db = types.SimpleNamespace(path=db_path)
    cfg.gas = types.SimpleNamespace(usdc_per_order=0.005)
    return cfg


def _loop_cfg() -> object:
    cfg = types.SimpleNamespace()
    cfg.db = types.SimpleNamespace(path=":memory:")
    cfg.gas = types.SimpleNamespace(usdc_per_order=0.005)
    return cfg


# ---------------------------------------------------------------------------
# 1. TestInsertScanLogEntry — round-trip insert → fetch
# ---------------------------------------------------------------------------

class TestInsertScanLogEntry:
    def test_round_trip_fields(self, mem_db: sqlite3.Connection) -> None:
        opp = _make_opp("evt_001", _iso_ago(5))
        store.insert_scan_log_entry(mem_db, opp, "My Event Title")
        rows = store.get_scan_log_last_24h(mem_db)

        assert len(rows) == 1
        r = rows[0]
        assert r.event_id == "evt_001"
        assert r.event_title == "My Event Title"
        assert r.leg_count == 2
        assert r.gross_edge_bps == 4000
        assert r.net_edge_bps == 3990
        assert r.dominant_cost == "gas"
        assert r.optimal_shares == Decimal("10")
        assert r.total_cost_usdc == Decimal("6.00")
        assert r.guaranteed_payoff_usdc == Decimal("10.00")
        assert r.id is not None

    def test_legs_json_is_valid_and_preserves_decimals(self, mem_db: sqlite3.Connection) -> None:
        opp = _make_opp("evt_002", _iso_ago(3))
        store.insert_scan_log_entry(mem_db, opp, "")
        rows = store.get_scan_log_last_24h(mem_db)

        legs = json.loads(rows[0].legs_json)
        assert len(legs) == 2
        assert legs[0]["condition_id"] == "cond_a"
        assert Decimal(legs[0]["best_ask"]) == Decimal("0.30")
        assert Decimal(legs[0]["gas_usdc"]) == Decimal("0.005")
        assert legs[0]["side"] == "BUY"


# ---------------------------------------------------------------------------
# 2. TestInsertScanLogEntryEventTitle — empty and non-empty title
# ---------------------------------------------------------------------------

class TestInsertScanLogEntryEventTitle:
    def test_empty_title_stored_as_empty_string(self, mem_db: sqlite3.Connection) -> None:
        store.insert_scan_log_entry(mem_db, _make_opp("evt_a", _iso_ago(1)), "")
        rows = store.get_scan_log_last_24h(mem_db)
        assert rows[0].event_title == ""

    def test_long_title_preserved_verbatim(self, mem_db: sqlite3.Connection) -> None:
        title = "A" * 200
        store.insert_scan_log_entry(mem_db, _make_opp("evt_b", _iso_ago(1)), title)
        rows = store.get_scan_log_last_24h(mem_db)
        assert rows[0].event_title == title


# ---------------------------------------------------------------------------
# 3. TestGetScanLogLast24h — windowing and ordering
# ---------------------------------------------------------------------------

class TestGetScanLogLast24h:
    def test_returns_rows_within_24h(self, mem_db: sqlite3.Connection) -> None:
        store.insert_scan_log_entry(mem_db, _make_opp("evt_new", _iso_ago(60)), "new")
        rows = store.get_scan_log_last_24h(mem_db)
        assert len(rows) == 1
        assert rows[0].event_id == "evt_new"

    def test_excludes_rows_older_than_24h(self, mem_db: sqlite3.Connection) -> None:
        old_ts = "2020-01-01T00:00:00+00:00"
        recent_ts = _iso_ago(30)
        store.insert_scan_log_entry(mem_db, _make_opp("evt_old", old_ts), "old")
        store.insert_scan_log_entry(mem_db, _make_opp("evt_new", recent_ts), "new")
        rows = store.get_scan_log_last_24h(mem_db)
        assert len(rows) == 1
        assert rows[0].event_id == "evt_new"

    def test_sorted_ts_desc(self, mem_db: sqlite3.Connection) -> None:
        store.insert_scan_log_entry(mem_db, _make_opp("evt_a", _iso_ago(120)), "a")
        store.insert_scan_log_entry(mem_db, _make_opp("evt_b", _iso_ago(60)), "b")
        store.insert_scan_log_entry(mem_db, _make_opp("evt_c", _iso_ago(10)), "c")
        rows = store.get_scan_log_last_24h(mem_db)
        assert [r.event_id for r in rows] == ["evt_c", "evt_b", "evt_a"]


# ---------------------------------------------------------------------------
# 4. TestGetScanLogLast24hEmpty
# ---------------------------------------------------------------------------

class TestGetScanLogLast24hEmpty:
    def test_returns_empty_list_on_no_rows(self, mem_db: sqlite3.Connection) -> None:
        assert store.get_scan_log_last_24h(mem_db) == []


# ---------------------------------------------------------------------------
# 5. TestCmdScan — one-shot scan finds a real opportunity
# ---------------------------------------------------------------------------

class TestCmdScan:
    def test_finds_opportunity_and_inserts_to_scan_log(
        self, tmp_path: object
    ) -> None:
        db_path = str(tmp_path / "test.db")  # type: ignore[operator]
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)

        m1 = _make_market("cond_a", "evt_x", event_title="Event X")
        m2 = _make_market("cond_b", "evt_x", event_title="Event X")
        store.upsert_market(conn, m1)
        store.upsert_market(conn, m2)

        # Two legs, sum_best_ask = 0.60 → gross edge 40%
        ts = _iso_ago(5)
        store.insert_snapshot(conn, _make_snap("cond_a", "tok_a", ts, "0.30"))
        store.insert_snapshot(conn, _make_snap("cond_b", "tok_b", ts, "0.30"))
        conn.close()

        found = cmd_scan(_make_cfg(db_path))
        assert found == 1

        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute("SELECT * FROM scan_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["event_id"] == "evt_x"
        assert int(rows[0]["net_edge_bps"]) > 0
        conn2.close()


# ---------------------------------------------------------------------------
# 6. TestCmdScanNoOpportunity — sum of asks ≥ 1.0 → nothing inserted
# ---------------------------------------------------------------------------

class TestCmdScanNoOpportunity:
    def test_no_edge_inserts_nothing(self, tmp_path: object) -> None:
        db_path = str(tmp_path / "test.db")  # type: ignore[operator]
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)

        store.upsert_market(conn, _make_market("cond_c", "evt_y"))
        store.upsert_market(conn, _make_market("cond_d", "evt_y"))

        ts = _iso_ago(5)
        # sum_best_ask = 0.50 + 0.50 = 1.00 → no gross edge
        store.insert_snapshot(conn, _make_snap("cond_c", "tok_c", ts, "0.50"))
        store.insert_snapshot(conn, _make_snap("cond_d", "tok_d", ts, "0.50"))
        conn.close()

        found = cmd_scan(_make_cfg(db_path))
        assert found == 0

        conn2 = sqlite3.connect(db_path)
        count = conn2.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
        assert count == 0
        conn2.close()


# ---------------------------------------------------------------------------
# 7. TestCmdScanLog — stdout output and footer
# ---------------------------------------------------------------------------

class TestCmdScanLog:
    def test_prints_header_rows_and_footer(self, tmp_path: object) -> None:
        db_path = str(tmp_path / "test.db")  # type: ignore[operator]
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)

        for i, dominant in enumerate(["fee", "fee", "gas"]):
            opp = ArbOpportunity(
                event_id=f"evt_{i}",
                ts=_iso_ago(10 + i),
                legs=(_make_opp("x", _iso_ago(1)).legs[0],) * 2,
                optimal_shares=Decimal("5"),
                total_cost_usdc=Decimal("3"),
                guaranteed_payoff_usdc=Decimal("5"),
                gross_edge_bps=4000,
                net_edge_bps=3900 - i * 10,
                total_fee_usdc=Decimal("0.10") if dominant == "fee" else Decimal("0"),
                total_slippage_usdc=Decimal("0"),
                total_gas_usdc=Decimal("0.01"),
                dominant_cost=dominant,  # type: ignore[arg-type]
            )
            store.insert_scan_log_entry(conn, opp, f"Title {i}")
        conn.close()

        buf = StringIO()
        with redirect_stdout(buf):
            cmd_scan_log(_make_cfg(db_path))
        output = buf.getvalue()

        assert "ts" in output
        assert "event_title" in output
        assert "Total last 24h: 3" in output
        assert "avg net_edge_bps:" in output
        assert "fee:" in output
        assert "gas:" in output

    def test_prints_no_opportunities_when_empty(self, tmp_path: object) -> None:
        db_path = str(tmp_path / "test.db")  # type: ignore[operator]
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        conn.close()

        buf = StringIO()
        with redirect_stdout(buf):
            cmd_scan_log(_make_cfg(db_path))
        assert "No arb opportunities" in buf.getvalue()


# ---------------------------------------------------------------------------
# 8. TestLoopScanEvery5 — cmd_scan fires on cycle 5, not before
# ---------------------------------------------------------------------------

class TestLoopScanEvery5:
    def test_scan_called_exactly_on_cycle_5(self) -> None:
        stop = threading.Event()
        snapshot_cycle: list[int] = [0]
        scan_called_on_cycles: list[int] = []

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_cycle[0] += 1
            if snapshot_cycle[0] >= 5:
                stop.set()
            return 100

        def fake_scan(cfg):
            scan_called_on_cycles.append(snapshot_cycle[0])
            return 0

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_scan", side_effect=fake_scan),
            patch("time.monotonic", side_effect=[0.0] * 6),
        ):
            cmd_loop(_loop_cfg(), interval=0, _stop=stop)

        assert scan_called_on_cycles == [5], (
            f"cmd_scan should fire only on cycle 5, fired on: {scan_called_on_cycles}"
        )

    def test_scan_not_called_on_cycles_1_through_4(self) -> None:
        stop = threading.Event()
        snapshot_count: list[int] = [0]

        def fake_snapshot(cfg, top=None, by="volume_24h"):
            snapshot_count[0] += 1
            if snapshot_count[0] >= 4:
                stop.set()
            return 50

        with (
            patch("app.run.cmd_discover"),
            patch("app.run.cmd_snapshot", side_effect=fake_snapshot),
            patch("app.run.cmd_scan") as mock_scan,
            patch("time.monotonic", side_effect=[0.0] * 5),
        ):
            cmd_loop(_loop_cfg(), interval=0, _stop=stop)

        mock_scan.assert_not_called()


# ---------------------------------------------------------------------------
# 9. TestCmdScanCycleBoundary — legs with different ts values are grouped correctly
# ---------------------------------------------------------------------------

class TestCmdScanCycleBoundary:
    def test_two_events_with_staggered_leg_timestamps_both_found(
        self, tmp_path: object
    ) -> None:
        """EventA legs are 5 s apart in ts; EventB legs are 10 s offset from EventA.
        Both events are within the 90-second look-back window.
        Both opportunities must be scanned and inserted into scan_log.
        """
        db_path = str(tmp_path / "test.db")  # type: ignore[operator]
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)

        # EventA — two legs 5 seconds apart
        store.upsert_market(conn, _make_market("cond_a1", "evt_A", event_title="Event A"))
        store.upsert_market(conn, _make_market("cond_a2", "evt_A", event_title="Event A"))
        store.insert_snapshot(conn, _make_snap("cond_a1", "tok_a1", _iso_ago(35), "0.30"))
        store.insert_snapshot(conn, _make_snap("cond_a2", "tok_a2", _iso_ago(30), "0.30"))

        # EventB — two legs with ts values 10–20 s newer than EventA
        store.upsert_market(conn, _make_market("cond_b1", "evt_B", event_title="Event B"))
        store.upsert_market(conn, _make_market("cond_b2", "evt_B", event_title="Event B"))
        store.insert_snapshot(conn, _make_snap("cond_b1", "tok_b1", _iso_ago(20), "0.30"))
        store.insert_snapshot(conn, _make_snap("cond_b2", "tok_b2", _iso_ago(10), "0.30"))

        # Also insert an old snapshot for tok_a1 to verify most-recent-per-token semantics:
        # old ask = 0.55 would make sum = 0.85 (still arb), but the recent one is selected.
        store.insert_snapshot(conn, _make_snap("cond_a1", "tok_a1", _iso_ago(80), "0.55"))
        conn.close()

        found = cmd_scan(_make_cfg(db_path))
        assert found == 2, f"Expected 2 opportunities, got {found}"

        conn2 = sqlite3.connect(db_path)
        rows = conn2.execute(
            "SELECT event_id FROM scan_log ORDER BY event_id"
        ).fetchall()
        event_ids = [r[0] for r in rows]
        assert "evt_A" in event_ids
        assert "evt_B" in event_ids
        conn2.close()
