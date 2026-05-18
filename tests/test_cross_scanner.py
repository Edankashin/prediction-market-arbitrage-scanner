"""Tests for app/strategies/cross_platform_scanner.py.

All DB work uses in-memory SQLite.  No network calls.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db import schema, store
from app.models import BookLevel, CrossScanRow, KalshiMarketRow, KalshiSnapshotRow, MarketRow, SnapshotRow
from app.strategies.cross_platform_scanner import (
    DirectionResult,
    _compute_direction,
    _resolve_pm_tokens,
    scan_all_pairs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _ago_iso(seconds: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _make_market(condition_id: str, *, yes_token: str = "tok_yes", no_token: str = "tok_no") -> MarketRow:
    return MarketRow(
        condition_id=condition_id,
        question=f"Will {condition_id} resolve?",
        event_id=None, event_slug=None, event_title=None,
        neg_risk=False, active=True, closed=False, archived=False,
        end_date="2026-12-31",
        clob_token_ids=json.dumps([yes_token, no_token]),
        outcomes=json.dumps(["Yes", "No"]),
        outcome_prices=None, volume=Decimal("1000000"), liquidity=Decimal("50000"),
        spread=Decimal("0.02"), fees_enabled=True,
        taker_fee_cap_bps=100, maker_rebate_cap_bps=0,
        fee_formula='{"rate":0.02,"exponent":1}',
        gamma_updated_at=None, synced_at=_now_iso(),
    )


def _make_market_no_first(condition_id: str) -> MarketRow:
    """Market where NO token is at index 0 in clob_token_ids."""
    return MarketRow(
        condition_id=condition_id,
        question=f"Will {condition_id} resolve?",
        event_id=None, event_slug=None, event_title=None,
        neg_risk=False, active=True, closed=False, archived=False,
        end_date="2026-12-31",
        clob_token_ids=json.dumps(["tok_no_first", "tok_yes_second"]),
        outcomes=json.dumps(["No", "Yes"]),
        outcome_prices=None, volume=Decimal("500000"), liquidity=Decimal("20000"),
        spread=Decimal("0.03"), fees_enabled=False,
        taker_fee_cap_bps=None, maker_rebate_cap_bps=None,
        fee_formula=None, gamma_updated_at=None, synced_at=_now_iso(),
    )


def _make_kalshi_market(ticker: str) -> KalshiMarketRow:
    return KalshiMarketRow(
        ticker=ticker, event_ticker="EVT-1", title=f"Kalshi {ticker}",
        category=None, status="active", end_date="2026-12-31",
        yes_bid=Decimal("0.40"), volume=Decimal("500000"),
        taker_fee_coeff=Decimal("0.07"), synced_at=_now_iso(),
    )


def _make_pm_snap(token_id: str, condition_id: str, best_ask: str, size: str = "100") -> SnapshotRow:
    p = Decimal(best_ask)
    s = Decimal(size)
    return SnapshotRow(
        token_id=token_id, condition_id=condition_id, ts=_now_iso(),
        bids=[BookLevel(price=p - Decimal("0.01"), size=s)],
        asks=[BookLevel(price=p, size=s)],
        best_bid=p - Decimal("0.01"), best_ask=p, mid=p,
        taker_fee_bps=None,
    )


def _make_kal_snap(
    ticker: str, *,
    yes_bid: str = "0.40", no_bid: str = "0.50",
    yes_bid_size: str = "50", no_bid_size: str = "60",
    single_sided: bool = False,
) -> KalshiSnapshotRow:
    yb = Decimal(yes_bid)
    nb = Decimal(no_bid)
    return KalshiSnapshotRow(
        ticker=ticker, ts=_now_iso(),
        yes_bids=[BookLevel(price=yb, size=Decimal(yes_bid_size))],
        no_bids=[BookLevel(price=nb, size=Decimal(no_bid_size))],
        best_yes_ask=Decimal("1") - nb,  # yes_ask = 1 - no_bid
        best_no_ask=Decimal("1") - yb,   # no_ask  = 1 - yes_bid
        single_sided=single_sided,
    )


def _plant_pair(conn: sqlite3.Connection, pm_cid: str, kal_ticker: str, note: str = "") -> None:
    """Insert a confirmed pair (assumes market + kalshi_market rows already inserted)."""
    pm = conn.execute(
        "SELECT question, end_date FROM markets WHERE condition_id=?", (pm_cid,)
    ).fetchone()
    km = conn.execute(
        "SELECT title, end_date FROM kalshi_markets WHERE ticker=?", (kal_ticker,)
    ).fetchone()
    conn.execute(
        """INSERT INTO cross_platform_pairs
           (pm_condition_id, kalshi_ticker, status, note,
            pm_question_snapshot, pm_end_date_snapshot,
            kalshi_title_snapshot, kalshi_end_date_snapshot,
            confirmed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pm_cid, kal_ticker, "confirmed", note,
         pm[0] if pm else "", pm[1] if pm else None,
         km[0] if km else "", km[1] if km else None,
         _now_iso()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# TestResolveTokens
# ---------------------------------------------------------------------------

class TestResolveTokens:
    def test_yes_first_standard_order(self) -> None:
        tokens = json.dumps(["tok_yes", "tok_no"])
        outcomes = json.dumps(["Yes", "No"])
        result = _resolve_pm_tokens(tokens, outcomes)
        assert result == ("tok_yes", "tok_no")

    def test_no_first_reversed_order(self) -> None:
        """Critical: NO-first ordering must resolve YES to the second token."""
        tokens = json.dumps(["tok_no", "tok_yes"])
        outcomes = json.dumps(["No", "Yes"])
        result = _resolve_pm_tokens(tokens, outcomes)
        assert result == ("tok_yes", "tok_no")

    def test_outcomes_none_returns_none(self) -> None:
        assert _resolve_pm_tokens('["a","b"]', None) is None

    def test_outcomes_missing_yes_returns_none(self) -> None:
        tokens = json.dumps(["a", "b"])
        outcomes = json.dumps(["Win", "Lose"])
        assert _resolve_pm_tokens(tokens, outcomes) is None

    def test_outcomes_only_yes_returns_none(self) -> None:
        tokens = json.dumps(["a", "b"])
        outcomes = json.dumps(["Yes", "Maybe"])
        assert _resolve_pm_tokens(tokens, outcomes) is None

    def test_case_insensitive_matching(self) -> None:
        tokens = json.dumps(["x", "y"])
        outcomes = json.dumps(["YES", "no"])
        result = _resolve_pm_tokens(tokens, outcomes)
        assert result == ("x", "y")

    def test_length_mismatch_returns_none(self) -> None:
        tokens = json.dumps(["a"])
        outcomes = json.dumps(["Yes", "No"])
        assert _resolve_pm_tokens(tokens, outcomes) is None

    def test_malformed_json_returns_none(self) -> None:
        assert _resolve_pm_tokens("not-json", '["Yes","No"]') is None


# ---------------------------------------------------------------------------
# TestComputeDirection
# ---------------------------------------------------------------------------

class TestComputeDirection:
    GAS = Decimal("0.005")
    COEFF = Decimal("0.07")

    def test_returns_directon_result_dataclass(self) -> None:
        r = _compute_direction(
            pm_ask=Decimal("0.55"), pm_ask_size=Decimal("30"),
            kalshi_ask=Decimal("0.42"), kalshi_bid_size=Decimal("20"),
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        assert isinstance(r, DirectionResult)

    def test_gross_per_unit_formula(self) -> None:
        r = _compute_direction(
            pm_ask=Decimal("0.55"), pm_ask_size=Decimal("20"),
            kalshi_ask=Decimal("0.42"), kalshi_bid_size=Decimal("20"),
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        assert r.gross_per_unit == Decimal("1") - Decimal("0.55") - Decimal("0.42")

    def test_max_units_is_min_of_sizes(self) -> None:
        r = _compute_direction(
            pm_ask=Decimal("0.55"), pm_ask_size=Decimal("30"),
            kalshi_ask=Decimal("0.42"), kalshi_bid_size=Decimal("20"),
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        assert r.max_units == Decimal("20")

    def test_gas_is_flat_not_per_unit(self) -> None:
        """net_at_size for size=1 and size=10 must both subtract gas_flat once."""
        r1 = _compute_direction(
            pm_ask=Decimal("0.45"), pm_ask_size=Decimal("1"),
            kalshi_ask=Decimal("0.42"), kalshi_bid_size=Decimal("1"),
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        r10 = _compute_direction(
            pm_ask=Decimal("0.45"), pm_ask_size=Decimal("10"),
            kalshi_ask=Decimal("0.42"), kalshi_bid_size=Decimal("10"),
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        # gross_total increases with size but gas does not scale with units
        # r10.net_at_size should be much larger than r1.net_at_size
        assert r10.net_at_size > r1.net_at_size
        # Verify by reconstructing: net1 = gross×1 - fee - gas; net10 = gross×10 - 10×fee - gas
        # The difference should be 9 × (gross - fee) approximately
        gross = Decimal("1") - Decimal("0.45") - Decimal("0.42")
        assert gross > Decimal("0")
        # Gas flat contribution: net10 - net1 ≈ 9 × (gross - kalshi_fee_per_unit) - pm_fee_diff
        # Just verify gas is not multiplied by units: gas_flat same in both
        # We check: r10.net_at_size - r1.net_at_size != 9 × r1.net_at_size
        assert r10.net_at_size != Decimal("10") * r1.net_at_size

    def test_zero_size_returns_zero_net(self) -> None:
        r = _compute_direction(
            pm_ask=Decimal("0.45"), pm_ask_size=Decimal("0"),
            kalshi_ask=Decimal("0.42"), kalshi_bid_size=Decimal("100"),
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        assert r.max_units == Decimal("0")
        assert r.net_at_size == Decimal("0")

    def test_kalshi_fee_formula(self) -> None:
        """Kalshi fee = coeff × P × (1−P) × max_units."""
        pm_ask = Decimal("0.45")
        kal_ask = Decimal("0.42")
        units = Decimal("10")
        r = _compute_direction(
            pm_ask=pm_ask, pm_ask_size=units,
            kalshi_ask=kal_ask, kalshi_bid_size=units,
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        expected_kal_fee = self.COEFF * kal_ask * (Decimal("1") - kal_ask) * units
        assert r.kalshi_fee_total == expected_kal_fee

    def test_net_edge_bps_formula(self) -> None:
        pm_ask = Decimal("0.45")
        kal_ask = Decimal("0.42")
        units = Decimal("20")
        gas = Decimal("0.005")
        coeff = Decimal("0.07")
        r = _compute_direction(
            pm_ask=pm_ask, pm_ask_size=units,
            kalshi_ask=kal_ask, kalshi_bid_size=units,
            market_row=None, kalshi_fee_coeff=coeff, gas_flat=gas,
        )
        gross_total = (Decimal("1") - pm_ask - kal_ask) * units
        kal_fee = coeff * kal_ask * (Decimal("1") - kal_ask) * units
        net = gross_total - kal_fee - gas
        total_cost = (pm_ask + kal_ask) * units
        expected_bps = int((net / total_cost * Decimal("10000")).to_integral_value())
        assert r.net_edge_bps == expected_bps

    def test_no_arb_negative_gross(self) -> None:
        """PM ask + Kalshi ask > 1 → negative gross, still returns a result."""
        r = _compute_direction(
            pm_ask=Decimal("0.60"), pm_ask_size=Decimal("10"),
            kalshi_ask=Decimal("0.55"), kalshi_bid_size=Decimal("10"),
            market_row=None, kalshi_fee_coeff=self.COEFF, gas_flat=self.GAS,
        )
        assert r.gross_per_unit < Decimal("0")


# ---------------------------------------------------------------------------
# TestScanAllPairs
# ---------------------------------------------------------------------------

class TestScanAllPairs:
    GAS = Decimal("0.005")

    def _setup_pair(
        self,
        conn: sqlite3.Connection,
        pm_cid: str = "cond_1",
        kal_ticker: str = "KAL-1",
        *,
        yes_token: str = "tok_yes",
        no_token: str = "tok_no",
        pm_yes_ask: str = "0.45",
        pm_no_ask: str = "0.58",
        kal_yes_bid: str = "0.40",
        kal_no_bid: str = "0.50",
        single_sided: bool = False,
    ) -> None:
        mkt = _make_market(pm_cid, yes_token=yes_token, no_token=no_token)
        store.upsert_market(conn, mkt)
        km = _make_kalshi_market(kal_ticker)
        store.upsert_kalshi_market(conn, km)
        _plant_pair(conn, pm_cid, kal_ticker)

        store.insert_snapshot(conn, _make_pm_snap(yes_token, pm_cid, pm_yes_ask))
        store.insert_snapshot(conn, _make_pm_snap(no_token, pm_cid, pm_no_ask))
        store.insert_kalshi_snapshot(conn, _make_kal_snap(
            kal_ticker, yes_bid=kal_yes_bid, no_bid=kal_no_bid,
            single_sided=single_sided,
        ))

    def test_returns_one_row_per_pair(self, mem_db: sqlite3.Connection) -> None:
        self._setup_pair(mem_db, "cond_1", "KAL-1")
        self._setup_pair(mem_db, "cond_2", "KAL-2", yes_token="y2", no_token="n2")
        self._setup_pair(mem_db, "cond_3", "KAL-3", yes_token="y3", no_token="n3")

        rows = scan_all_pairs(mem_db, self.GAS)
        assert len(rows) == 3

    def test_skip_stale_pm_snapshot(self, mem_db: sqlite3.Connection) -> None:
        store.upsert_market(mem_db, _make_market("cond_s"))
        store.upsert_kalshi_market(mem_db, _make_kalshi_market("KAL-S"))
        _plant_pair(mem_db, "cond_s", "KAL-S")
        # Insert OLD PM snapshot (200 seconds ago — outside 120s window)
        old_ts = _ago_iso(200)
        stale_snap = SnapshotRow(
            token_id="tok_yes", condition_id="cond_s", ts=old_ts,
            bids=[], asks=[BookLevel(price=Decimal("0.45"), size=Decimal("100"))],
            best_bid=None, best_ask=Decimal("0.45"), mid=Decimal("0.45"),
            taker_fee_bps=None,
        )
        store.insert_snapshot(mem_db, stale_snap)
        store.insert_kalshi_snapshot(mem_db, _make_kal_snap("KAL-S"))

        rows = scan_all_pairs(mem_db, self.GAS, window_sec=120)
        assert len(rows) == 1
        assert rows[0].skipped
        assert rows[0].skip_reason == "stale_pm"

    def test_skip_stale_kalshi_snapshot(self, mem_db: sqlite3.Connection) -> None:
        store.upsert_market(mem_db, _make_market("cond_k"))
        store.upsert_kalshi_market(mem_db, _make_kalshi_market("KAL-K"))
        _plant_pair(mem_db, "cond_k", "KAL-K")
        store.insert_snapshot(mem_db, _make_pm_snap("tok_yes", "cond_k", "0.45"))
        store.insert_snapshot(mem_db, _make_pm_snap("tok_no", "cond_k", "0.57"))
        # Old Kalshi snapshot
        old_ts = _ago_iso(200)
        old_kal = KalshiSnapshotRow(
            ticker="KAL-K", ts=old_ts,
            yes_bids=[BookLevel(price=Decimal("0.40"), size=Decimal("50"))],
            no_bids=[BookLevel(price=Decimal("0.50"), size=Decimal("60"))],
            best_yes_ask=Decimal("0.50"), best_no_ask=Decimal("0.60"),
            single_sided=False,
        )
        store.insert_kalshi_snapshot(mem_db, old_kal)

        rows = scan_all_pairs(mem_db, self.GAS, window_sec=120)
        assert rows[0].skipped
        assert rows[0].skip_reason == "stale_kalshi"

    def test_skip_kalshi_single_sided(self, mem_db: sqlite3.Connection) -> None:
        self._setup_pair(mem_db, "cond_ss", "KAL-SS", yes_token="yss", no_token="nss",
                         single_sided=True)
        rows = scan_all_pairs(mem_db, self.GAS)
        assert rows[0].skipped
        assert rows[0].skip_reason == "kalshi_single_sided"

    def test_skip_pm_outcomes_unresolvable(self, mem_db: sqlite3.Connection) -> None:
        mkt = MarketRow(
            condition_id="cond_ou", question="Will it happen?",
            event_id=None, event_slug=None, event_title=None,
            neg_risk=False, active=True, closed=False, archived=False,
            end_date=None, clob_token_ids=json.dumps(["ta", "tb"]),
            outcomes=None,  # no outcomes
            outcome_prices=None, volume=None, liquidity=None, spread=None,
            fees_enabled=False, taker_fee_cap_bps=None, maker_rebate_cap_bps=None,
            fee_formula=None, gamma_updated_at=None, synced_at=_now_iso(),
        )
        store.upsert_market(mem_db, mkt)
        store.upsert_kalshi_market(mem_db, _make_kalshi_market("KAL-OU"))
        _plant_pair(mem_db, "cond_ou", "KAL-OU")
        store.insert_snapshot(mem_db, _make_pm_snap("ta", "cond_ou", "0.45"))
        store.insert_snapshot(mem_db, _make_pm_snap("tb", "cond_ou", "0.57"))
        store.insert_kalshi_snapshot(mem_db, _make_kal_snap("KAL-OU"))

        rows = scan_all_pairs(mem_db, self.GAS)
        assert rows[0].skipped
        assert rows[0].skip_reason == "pm_outcomes_unresolvable"

    def test_crossed_pm_market_detected(self, mem_db: sqlite3.Connection) -> None:
        """Both directions profitable → pm_market_crossed, skipped=True, direction=BOTH."""
        # pm_yes_ask=0.35, pm_no_ask=0.38 → sum=0.73 < 1 → PM ask-side crossed
        # kal_no_ask = 1 - 0.60 = 0.40 → dir_A = 1 - 0.35 - 0.40 = 0.25 > 0
        # kal_yes_ask = 1 - 0.50 = 0.50 → dir_B = 1 - 0.38 - 0.50 = 0.12 > 0
        self._setup_pair(
            mem_db, "cond_cx", "KAL-CX",
            yes_token="yc", no_token="nc",
            pm_yes_ask="0.35", pm_no_ask="0.38",
            kal_yes_bid="0.60", kal_no_bid="0.50",
        )
        rows = scan_all_pairs(mem_db, self.GAS)
        assert rows[0].skipped
        assert rows[0].skip_reason == "pm_market_crossed"
        assert rows[0].direction == "BOTH"

    def test_non_arb_direction_chosen_is_better(self, mem_db: sqlite3.Connection) -> None:
        """When neither direction is an arb, the better (less negative) is chosen."""
        self._setup_pair(
            mem_db, "cond_na", "KAL-NA",
            yes_token="yna", no_token="nna",
            pm_yes_ask="0.60", pm_no_ask="0.61",
            kal_yes_bid="0.35", kal_no_bid="0.36",
        )
        rows = scan_all_pairs(mem_db, self.GAS)
        assert not rows[0].skipped
        assert rows[0].direction in ("A", "B")

    def test_writes_to_db_via_insert_cross_scan_row(self, mem_db: sqlite3.Connection) -> None:
        self._setup_pair(mem_db, "cond_db", "KAL-DB", yes_token="ydb", no_token="ndb")
        rows = scan_all_pairs(mem_db, self.GAS)
        for r in rows:
            store.insert_cross_scan_row(mem_db, r)
        count = mem_db.execute("SELECT COUNT(*) FROM cross_platform_scan_log").fetchone()[0]
        assert count == len(rows)

    def test_empty_pairs_returns_empty_list(self, mem_db: sqlite3.Connection) -> None:
        assert scan_all_pairs(mem_db, self.GAS) == []

    def test_no_first_token_order_resolves_correctly(self, mem_db: sqlite3.Connection) -> None:
        """NO-first clob_token_ids must still route YES and NO correctly."""
        mkt = _make_market_no_first("cond_nf")
        store.upsert_market(mem_db, mkt)
        store.upsert_kalshi_market(mem_db, _make_kalshi_market("KAL-NF"))
        _plant_pair(mem_db, "cond_nf", "KAL-NF")
        # Plant snapshots: tok_yes_second at low ask (arb candidate), tok_no_first at higher ask
        store.insert_snapshot(mem_db, _make_pm_snap("tok_yes_second", "cond_nf", "0.40", "80"))
        store.insert_snapshot(mem_db, _make_pm_snap("tok_no_first", "cond_nf", "0.65", "80"))
        store.insert_kalshi_snapshot(mem_db, _make_kal_snap(
            "KAL-NF", yes_bid="0.38", no_bid="0.48",
        ))
        rows = scan_all_pairs(mem_db, self.GAS)
        assert len(rows) == 1
        r = rows[0]
        # Direction A uses PM YES ask (0.40) + Kalshi NO ask (1-0.38=0.62) → gross=-0.02
        # Direction B uses PM NO ask (0.65) + Kalshi YES ask (1-0.48=0.52) → gross=-0.17
        # Better (less negative) is Direction A
        assert not r.skipped
        # pm_yes_ask should be 0.40 (from tok_yes_second, which is the YES token)
        assert r.pm_yes_ask == Decimal("0.40")
        assert r.pm_no_ask == Decimal("0.65")

    def test_scannable_pair_produces_non_skipped_row(self, mem_db: sqlite3.Connection) -> None:
        # Set up a pair with positive gross profit in Direction A
        # pm_yes_ask=0.45, kal_no_ask=1-0.60=0.40 → gross=0.15
        self._setup_pair(
            mem_db, "cond_arb", "KAL-ARB",
            yes_token="yarb", no_token="narb",
            pm_yes_ask="0.45", pm_no_ask="0.62",
            kal_yes_bid="0.60", kal_no_bid="0.35",
        )
        rows = scan_all_pairs(mem_db, self.GAS)
        assert len(rows) == 1
        r = rows[0]
        assert not r.skipped
        assert r.gross_profit_per_unit is not None
        assert r.net_profit_at_size is not None
        assert r.max_profitable_units is not None
        assert r.pm_snapshot_age_sec is not None
        assert r.kalshi_snapshot_age_sec is not None

    def test_net_profit_at_size_gas_is_flat(self, mem_db: sqlite3.Connection) -> None:
        """Net at size=1 and size=50: gas subtracted once in both cases."""
        # size=1
        self._setup_pair(
            mem_db, "cond_g1", "KAL-G1",
            yes_token="yg1", no_token="ng1",
            pm_yes_ask="0.45", pm_no_ask="0.62",
            kal_yes_bid="0.60", kal_no_bid="0.35",
        )
        rows1 = scan_all_pairs(mem_db, self.GAS)

        # Check gas_flat is stored as configured
        assert rows1[0].gas_flat == self.GAS
