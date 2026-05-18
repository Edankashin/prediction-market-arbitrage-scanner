"""Tests for cmd_cross_snapshot and the store functions it depends on.

All DB work uses tmp_path file DBs or in-memory SQLite.
Network calls are mocked via unittest.mock.
"""
from __future__ import annotations

import json
import sqlite3
import types
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from app.db import schema, store
from app.models import BookLevel, KalshiMarketRow, KalshiSnapshotRow, MarketRow, SnapshotRow
from app.run import cmd_cross_scan, cmd_cross_scan_log, cmd_cross_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _make_cfg(db_path: str, *, with_kalshi: bool = True) -> object:
    cfg = types.SimpleNamespace()
    cfg.db = types.SimpleNamespace(path=db_path)
    cfg.gas = types.SimpleNamespace(usdc_per_order=0.005)
    cfg.clob = types.SimpleNamespace(
        host="https://clob.polymarket.com",
        requests_per_second=4.0, timeout_sec=5,
        max_retries=1, backoff_factor=1.0,
    )
    if with_kalshi:
        cfg.kalshi = types.SimpleNamespace(
            base_url="https://external-api.kalshi.com/trade-api/v2",
            requests_per_second=999.0, timeout_sec=5,
            max_retries=0, backoff_factor=1.0,
        )
    return cfg


def _make_market(condition_id: str, yes_tok: str, no_tok: str) -> MarketRow:
    return MarketRow(
        condition_id=condition_id,
        question=f"Will {condition_id} happen?",
        event_id=None, event_slug=None, event_title=None,
        neg_risk=False, active=True, closed=False, archived=False,
        end_date=None,
        clob_token_ids=json.dumps([yes_tok, no_tok]),
        outcomes=json.dumps(["Yes", "No"]),
        outcome_prices=None, volume=Decimal("100000"), liquidity=Decimal("10000"),
        spread=Decimal("0.02"), fees_enabled=False,
        taker_fee_cap_bps=None, maker_rebate_cap_bps=None,
        fee_formula=None, gamma_updated_at=None, synced_at=_now_iso(),
    )


def _make_kalshi_market(ticker: str) -> KalshiMarketRow:
    return KalshiMarketRow(
        ticker=ticker, event_ticker="EVT", title=f"Kalshi {ticker}",
        category=None, status="active", end_date=None,
        yes_bid=Decimal("0.45"), volume=Decimal("50000"),
        taker_fee_coeff=Decimal("0.07"), synced_at=_now_iso(),
    )


def _plant_pair(conn: sqlite3.Connection, pm_cid: str, kal_ticker: str) -> None:
    conn.execute(
        """INSERT INTO cross_platform_pairs
           (pm_condition_id, kalshi_ticker, status, note,
            pm_question_snapshot, pm_end_date_snapshot,
            kalshi_title_snapshot, kalshi_end_date_snapshot,
            confirmed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pm_cid, kal_ticker, "confirmed", "test",
         "question", None, "title", None, _now_iso()),
    )
    conn.commit()


def _make_pm_snap_response(token_id: str, condition_id: str) -> SnapshotRow:
    p = Decimal("0.50")
    return SnapshotRow(
        token_id=token_id, condition_id=condition_id, ts=_now_iso(),
        bids=[BookLevel(price=p - Decimal("0.01"), size=Decimal("100"))],
        asks=[BookLevel(price=p, size=Decimal("100"))],
        best_bid=p - Decimal("0.01"), best_ask=p, mid=p, taker_fee_bps=None,
    )


def _make_kal_snap_response(ticker: str) -> KalshiSnapshotRow:
    return KalshiSnapshotRow(
        ticker=ticker, ts=_now_iso(),
        yes_bids=[BookLevel(price=Decimal("0.45"), size=Decimal("50"))],
        no_bids=[BookLevel(price=Decimal("0.48"), size=Decimal("60"))],
        best_yes_ask=Decimal("0.52"), best_no_ask=Decimal("0.55"),
        single_sided=False,
    )


# ---------------------------------------------------------------------------
# TestCmdCrossSnapshotNoKalshiCfg
# ---------------------------------------------------------------------------

class TestCmdCrossSnapshotNoKalshiCfg:
    def test_returns_zero_zero_when_no_kalshi(self, tmp_path) -> None:
        cfg = _make_cfg(str(tmp_path / "test.db"), with_kalshi=False)
        result = cmd_cross_snapshot(cfg)
        assert result == (0, 0)


# ---------------------------------------------------------------------------
# TestCmdCrossSnapshotNoPairs
# ---------------------------------------------------------------------------

class TestCmdCrossSnapshotNoPairs:
    def test_returns_zero_zero_when_no_pairs(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        conn.close()

        cfg = _make_cfg(db_path)
        with (
            patch("app.api.clob.ClobPublicClient") as mock_clob_cls,
            patch("app.api.kalshi.KalshiClient") as mock_kal_cls,
        ):
            result = cmd_cross_snapshot(cfg)

        assert result == (0, 0)
        mock_clob_cls.assert_not_called()
        mock_kal_cls.assert_not_called()


# ---------------------------------------------------------------------------
# TestCmdCrossSnapshotPmBulkFetch
# ---------------------------------------------------------------------------

class TestCmdCrossSnapshotPmBulkFetch:
    def test_stores_pm_snapshots_for_both_tokens(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        store.upsert_market(conn, _make_market("cond_1", "tok_yes_1", "tok_no_1"))
        store.upsert_kalshi_market(conn, _make_kalshi_market("KAL-1"))
        _plant_pair(conn, "cond_1", "KAL-1")
        conn.close()

        mock_clob = MagicMock()
        mock_clob.get_order_books.return_value = [
            _make_pm_snap_response("tok_yes_1", "cond_1"),
            _make_pm_snap_response("tok_no_1", "cond_1"),
        ]
        mock_kal = MagicMock()
        mock_kal.get_orderbook.return_value = {}
        mock_kal.parse_and_validate_snapshot.return_value = _make_kal_snap_response("KAL-1")

        cfg = _make_cfg(db_path)
        with (
            patch("app.api.clob.ClobPublicClient", return_value=mock_clob),
            patch("app.api.kalshi.KalshiClient", return_value=mock_kal),
        ):
            pm_stored, kal_stored = cmd_cross_snapshot(cfg)

        assert pm_stored == 2
        assert kal_stored == 1
        conn2 = sqlite3.connect(db_path)
        pm_count = conn2.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
        kal_count = conn2.execute("SELECT COUNT(*) FROM kalshi_book_snapshots").fetchone()[0]
        assert pm_count == 2
        assert kal_count == 1
        conn2.close()

    def test_single_pm_bulk_call_for_all_tokens(self, tmp_path) -> None:
        """All PM tokens fetched in one POST /books call regardless of pair count."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        store.upsert_market(conn, _make_market("cond_a", "ya", "na"))
        store.upsert_market(conn, _make_market("cond_b", "yb", "nb"))
        store.upsert_kalshi_market(conn, _make_kalshi_market("KAL-A"))
        store.upsert_kalshi_market(conn, _make_kalshi_market("KAL-B"))
        _plant_pair(conn, "cond_a", "KAL-A")
        _plant_pair(conn, "cond_b", "KAL-B")
        conn.close()

        mock_clob = MagicMock()
        mock_clob.get_order_books.return_value = [
            _make_pm_snap_response("ya", "cond_a"),
            _make_pm_snap_response("na", "cond_a"),
            _make_pm_snap_response("yb", "cond_b"),
            _make_pm_snap_response("nb", "cond_b"),
        ]
        mock_kal = MagicMock()
        mock_kal.get_orderbook.return_value = {}
        mock_kal.parse_and_validate_snapshot.side_effect = [
            _make_kal_snap_response("KAL-A"),
            _make_kal_snap_response("KAL-B"),
        ]

        cfg = _make_cfg(db_path)
        with (
            patch("app.api.clob.ClobPublicClient", return_value=mock_clob),
            patch("app.api.kalshi.KalshiClient", return_value=mock_kal),
        ):
            pm_stored, kal_stored = cmd_cross_snapshot(cfg)

        # Only one bulk PM call
        assert mock_clob.get_order_books.call_count == 1
        assert len(mock_clob.get_order_books.call_args[0][0]) == 4  # 4 tokens
        assert pm_stored == 4
        assert kal_stored == 2


# ---------------------------------------------------------------------------
# TestCmdCrossSnapshotKalshiFailure
# ---------------------------------------------------------------------------

class TestCmdCrossSnapshotKalshiFailure:
    def test_partial_kalshi_failure_continues(self, tmp_path) -> None:
        """One Kalshi ticker raising exception doesn't abort the rest."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        store.upsert_market(conn, _make_market("cond_f1", "yf1", "nf1"))
        store.upsert_market(conn, _make_market("cond_f2", "yf2", "nf2"))
        store.upsert_kalshi_market(conn, _make_kalshi_market("KAL-F1"))
        store.upsert_kalshi_market(conn, _make_kalshi_market("KAL-F2"))
        _plant_pair(conn, "cond_f1", "KAL-F1")
        _plant_pair(conn, "cond_f2", "KAL-F2")
        conn.close()

        mock_clob = MagicMock()
        mock_clob.get_order_books.return_value = []
        mock_kal = MagicMock()

        def _side_effect(ticker):
            if ticker == "KAL-F1":
                raise ConnectionError("timeout")
            return {}

        mock_kal.get_orderbook.side_effect = _side_effect
        mock_kal.parse_and_validate_snapshot.return_value = _make_kal_snap_response("KAL-F2")

        cfg = _make_cfg(db_path)
        with (
            patch("app.api.clob.ClobPublicClient", return_value=mock_clob),
            patch("app.api.kalshi.KalshiClient", return_value=mock_kal),
        ):
            pm_stored, kal_stored = cmd_cross_snapshot(cfg)

        # KAL-F1 failed, KAL-F2 succeeded
        assert kal_stored == 1

    def test_duplicate_kalshi_ticker_fetched_once(self, tmp_path) -> None:
        """Same Kalshi ticker shared by two PM pairs → fetched only once."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        store.upsert_market(conn, _make_market("cond_d1", "yd1", "nd1"))
        store.upsert_market(conn, _make_market("cond_d2", "yd2", "nd2"))
        store.upsert_kalshi_market(conn, _make_kalshi_market("KAL-SHARED"))
        _plant_pair(conn, "cond_d1", "KAL-SHARED")
        _plant_pair(conn, "cond_d2", "KAL-SHARED")
        conn.close()

        mock_clob = MagicMock()
        mock_clob.get_order_books.return_value = []
        mock_kal = MagicMock()
        mock_kal.get_orderbook.return_value = {}
        mock_kal.parse_and_validate_snapshot.return_value = _make_kal_snap_response("KAL-SHARED")

        cfg = _make_cfg(db_path)
        with (
            patch("app.api.clob.ClobPublicClient", return_value=mock_clob),
            patch("app.api.kalshi.KalshiClient", return_value=mock_kal),
        ):
            _, kal_stored = cmd_cross_snapshot(cfg)

        assert mock_kal.get_orderbook.call_count == 1
        assert kal_stored == 1


# ---------------------------------------------------------------------------
# TestCmdCrossScan
# ---------------------------------------------------------------------------

class TestCmdCrossScan:
    def test_returns_zero_when_no_pairs(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        conn.close()

        cfg = _make_cfg(db_path)
        assert cmd_cross_scan(cfg) == 0

    def test_writes_scan_rows_to_db(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        store.upsert_market(conn, _make_market("cond_cs", "ycs", "ncs"))
        store.upsert_kalshi_market(conn, _make_kalshi_market("KAL-CS"))
        _plant_pair(conn, "cond_cs", "KAL-CS")
        # No snapshots → stale_pm skip
        conn.close()

        cfg = _make_cfg(db_path)
        cmd_cross_scan(cfg)

        conn2 = sqlite3.connect(db_path)
        count = conn2.execute("SELECT COUNT(*) FROM cross_platform_scan_log").fetchone()[0]
        assert count == 1
        conn2.close()


# ---------------------------------------------------------------------------
# TestCmdCrossScanLog
# ---------------------------------------------------------------------------

class TestCmdCrossScanLog:
    def test_prints_no_results_when_empty(self, tmp_path) -> None:
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        schema.migrate(conn)
        conn.close()

        buf = StringIO()
        with redirect_stdout(buf):
            cmd_cross_scan_log(_make_cfg(db_path))
        assert "No cross-platform scan results" in buf.getvalue()
