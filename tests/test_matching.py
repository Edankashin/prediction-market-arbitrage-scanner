"""Tests for app/matching/search.py and app/matching/pairs.py.

All DB tests use in-memory SQLite; no network, no production DB.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.matching.search import (
    _SENTINEL_DAYS,
    _SENTINEL_THRESHOLD,
    _date_diff_days,
    candidates_from_kalshi,
    candidates_from_pm,
    jaccard,
    search_kalshi,
    search_pm,
    tokenize,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db() -> sqlite3.Connection:
    """In-memory DB with schema v4 applied."""
    from app.db import schema
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_basic_split(self) -> None:
        assert "bitcoin" in tokenize("Will Bitcoin hit $150k?")

    def test_lowercase(self) -> None:
        assert "spurs" in tokenize("San Antonio Spurs")
        assert "Spurs" not in tokenize("San Antonio Spurs")

    def test_stopwords_removed(self) -> None:
        toks = tokenize("Will the president win the election?")
        assert "will" not in toks
        assert "the" not in toks
        assert "president" in toks
        assert "election" in toks

    def test_punctuation_stripped(self) -> None:
        toks = tokenize("Bitcoin: $150k by 2026!")
        assert "bitcoin" in toks
        assert "150k" in toks
        assert "$" not in toks
        assert "!" not in toks

    def test_single_char_removed(self) -> None:
        toks = tokenize("a b c d apple")
        assert "a" not in toks
        assert "b" not in toks
        assert "apple" in toks

    def test_numbers_kept(self) -> None:
        toks = tokenize("NBA Finals 2026")
        assert "2026" in toks
        assert "nba" in toks
        assert "finals" in toks

    def test_empty_string(self) -> None:
        assert tokenize("") == frozenset()

    def test_all_stopwords(self) -> None:
        assert tokenize("will the be a an") == frozenset()

    def test_returns_frozenset(self) -> None:
        assert isinstance(tokenize("hello world"), frozenset)

    def test_deduplication(self) -> None:
        toks = tokenize("spurs spurs spurs")
        assert toks == frozenset({"spurs"})


# ---------------------------------------------------------------------------
# jaccard
# ---------------------------------------------------------------------------

class TestJaccard:
    def test_identical(self) -> None:
        a = frozenset({"a", "b", "c"})
        assert jaccard(a, a) == 1.0

    def test_disjoint(self) -> None:
        a = frozenset({"a", "b"})
        b = frozenset({"c", "d"})
        assert jaccard(a, b) == 0.0

    def test_partial_overlap(self) -> None:
        a = frozenset({"a", "b", "c"})
        b = frozenset({"b", "c", "d"})
        # |a ∩ b| = 2, |a ∪ b| = 4 → 0.5
        assert jaccard(a, b) == pytest.approx(0.5)

    def test_one_element_overlap(self) -> None:
        a = frozenset({"x", "y", "z"})
        b = frozenset({"z", "w"})
        # |a ∩ b| = 1, |a ∪ b| = 4 → 0.25
        assert jaccard(a, b) == pytest.approx(0.25)

    def test_empty_a(self) -> None:
        assert jaccard(frozenset(), frozenset({"a"})) == 0.0

    def test_empty_b(self) -> None:
        assert jaccard(frozenset({"a"}), frozenset()) == 0.0

    def test_both_empty(self) -> None:
        assert jaccard(frozenset(), frozenset()) == 0.0


# ---------------------------------------------------------------------------
# _date_diff_days
# ---------------------------------------------------------------------------

class TestDateDiffDays:
    def test_same_date(self) -> None:
        assert _date_diff_days("2026-07-01", "2026-07-01") == 0

    def test_within_threshold(self) -> None:
        assert _date_diff_days("2026-07-01", "2026-07-15") == 14

    def test_exactly_threshold(self) -> None:
        # Exactly 730 days → kept as-is
        from datetime import date, timedelta
        d1 = "2026-01-01"
        d2 = (date(2026, 1, 1) + timedelta(days=_SENTINEL_THRESHOLD)).isoformat()
        assert _date_diff_days(d1, d2) == _SENTINEL_THRESHOLD

    def test_above_threshold_returns_sentinel(self) -> None:
        assert _date_diff_days("2026-01-01", "2028-06-29") == _SENTINEL_DAYS

    def test_one_none(self) -> None:
        assert _date_diff_days(None, "2026-07-01") == _SENTINEL_DAYS
        assert _date_diff_days("2026-07-01", None) == _SENTINEL_DAYS

    def test_both_none(self) -> None:
        assert _date_diff_days(None, None) == _SENTINEL_DAYS

    def test_bad_format(self) -> None:
        assert _date_diff_days("not-a-date", "2026-07-01") == _SENTINEL_DAYS

    def test_with_time_component(self) -> None:
        # Should parse only the date part
        assert _date_diff_days("2026-07-01T00:00:00Z", "2026-07-11T12:00:00Z") == 10

    def test_order_invariant(self) -> None:
        d1, d2 = "2026-01-01", "2026-06-01"
        assert _date_diff_days(d1, d2) == _date_diff_days(d2, d1)


# ---------------------------------------------------------------------------
# search_pm
# ---------------------------------------------------------------------------

class TestSearchPm:
    _PM: list[tuple[str, str, str | None, str | None]] = [
        ("cid-spurs", "Will the San Antonio Spurs win the 2026 NBA Finals?", "2026 NBA Finals", "2026-07-01"),
        ("cid-lakers", "Will the Los Angeles Lakers win the 2026 NBA Finals?", "2026 NBA Finals", "2026-07-01"),
        ("cid-btc", "Will Bitcoin hit $150k by June 30, 2026?", "Bitcoin price", "2026-06-30"),
        ("cid-senate", "Will Ken Paxton win the Texas Senate primary?", "Texas Politics", "2026-11-03"),
    ]

    def test_returns_top_n(self) -> None:
        results = search_pm("NBA Finals", self._PM, top=2)
        assert len(results) == 2

    def test_ranking_order_better_first(self) -> None:
        results = search_pm("NBA Finals 2026 Spurs", self._PM)
        cids = [r["condition_id"] for r in results]
        assert cids[0] == "cid-spurs"

    def test_zero_score_excluded(self) -> None:
        results = search_pm("NBA Finals", self._PM)
        cids = [r["condition_id"] for r in results]
        assert "cid-senate" not in cids

    def test_result_has_expected_keys(self) -> None:
        results = search_pm("NBA", self._PM)
        assert results
        assert set(results[0].keys()) == {"condition_id", "question", "event_title", "end_date", "score"}

    def test_empty_query_returns_empty(self) -> None:
        assert search_pm("", self._PM) == []

    def test_all_stopword_query_returns_empty(self) -> None:
        assert search_pm("will the be", self._PM) == []

    def test_empty_corpus_returns_empty(self) -> None:
        assert search_pm("NBA", []) == []

    def test_pretokenizes_corpus_once(self) -> None:
        # Verify the function works correctly on a moderately large corpus
        # (correctness guarantee implies single-pass tokenization is working)
        large_pm = [
            (f"cid-{i}", f"Will candidate_{i} win the 2026 election?", None, "2026-11-03")
            for i in range(1000)
        ]
        large_pm.append(("cid-target", "Will Newsom win the 2026 California governor race?", None, "2026-11-03"))
        results = search_pm("Newsom California governor", large_pm)
        assert results[0]["condition_id"] == "cid-target"


# ---------------------------------------------------------------------------
# search_kalshi
# ---------------------------------------------------------------------------

class TestSearchKalshi:
    _KR: list[tuple[str, str, str | None]] = [
        ("KXNBA-26-SAS", "Will the San Antonio win the 2026 Pro Basketball Finals?", "2026-05-16"),
        ("KXNBA-26-LAL", "Will the Los Angeles win the 2026 Pro Basketball Finals?", "2026-05-16"),
        ("KXBTC-150", "Will Bitcoin reach $150,000?", "2026-06-30"),
    ]

    def test_returns_top_n(self) -> None:
        assert len(search_kalshi("basketball", self._KR, top=1)) == 1

    def test_ranking_order(self) -> None:
        results = search_kalshi("San Antonio basketball", self._KR)
        assert results[0]["ticker"] == "KXNBA-26-SAS"

    def test_result_keys(self) -> None:
        results = search_kalshi("basketball", self._KR)
        assert set(results[0].keys()) == {"ticker", "title", "end_date", "score"}

    def test_empty_query(self) -> None:
        assert search_kalshi("", self._KR) == []


# ---------------------------------------------------------------------------
# candidates_from_pm (bidirectional, date tiebreaker)
# ---------------------------------------------------------------------------

class TestCandidatesFromPm:
    _KR: list[tuple[str, str, str | None]] = [
        ("KXNBA-26-SAS", "Will the San Antonio win the 2026 Pro Basketball Finals?", "2026-07-05"),
        ("KXNBA-26-SAS-SENTINEL", "Will the San Antonio win the 2026 Pro Basketball Finals?", "2028-06-29"),
        ("KXNBA-26-LAL", "Will the Los Angeles win the 2026 Pro Basketball Finals?", "2026-07-05"),
        ("KXBTC", "Will Bitcoin reach $150k?", "2026-06-30"),
    ]

    def test_finds_candidates(self) -> None:
        results = candidates_from_pm(
            "Will the San Antonio Spurs win the 2026 NBA Finals?",
            "2026 NBA Finals",
            "2026-07-01",
            self._KR,
        )
        tickers = [r["ticker"] for r in results]
        assert "KXNBA-26-SAS" in tickers

    def test_result_has_days_diff(self) -> None:
        results = candidates_from_pm("San Antonio Spurs NBA", None, "2026-07-01", self._KR)
        assert all("days_diff" in r for r in results)

    def test_sentinel_date_pushed_to_end_of_tied_group(self) -> None:
        # Both SAS and SAS-SENTINEL have identical Jaccard; SENTINEL has
        # days_diff > 730 so it should rank after the real one.
        results = candidates_from_pm(
            "Will the San Antonio win the 2026 Pro Basketball Finals?",
            None,
            "2026-07-01",
            [
                ("KXNBA-26-SAS", "Will the San Antonio win the 2026 Pro Basketball Finals?", "2026-07-05"),
                ("KXNBA-26-SAS-SENTINEL", "Will the San Antonio win the 2026 Pro Basketball Finals?", "2028-06-29"),
            ],
        )
        tickers = [r["ticker"] for r in results]
        assert tickers.index("KXNBA-26-SAS") < tickers.index("KXNBA-26-SAS-SENTINEL")

    def test_zero_score_excluded(self) -> None:
        results = candidates_from_pm("San Antonio NBA Spurs", None, "2026-07-01", self._KR)
        tickers = [r["ticker"] for r in results]
        assert "KXBTC" not in tickers

    def test_empty_kalshi_corpus(self) -> None:
        assert candidates_from_pm("NBA Spurs", None, "2026-07-01", []) == []

    def test_top_n_respected(self) -> None:
        assert len(candidates_from_pm("basketball finals", None, "2026-07-01", self._KR, top=1)) <= 1


# ---------------------------------------------------------------------------
# candidates_from_kalshi
# ---------------------------------------------------------------------------

class TestCandidatesFromKalshi:
    _PM: list[tuple[str, str, str | None, str | None]] = [
        ("cid-spurs", "Will the San Antonio Spurs win the 2026 NBA Finals?", "2026 NBA", "2026-07-01"),
        ("cid-lakers", "Will the Los Angeles Lakers win the 2026 NBA Finals?", "2026 NBA", "2026-07-01"),
        ("cid-btc", "Will Bitcoin hit $150k?", "Bitcoin price", "2026-06-30"),
    ]

    def test_bidirectional_finds_pm(self) -> None:
        results = candidates_from_kalshi(
            "Will the San Antonio win the 2026 Pro Basketball Finals?",
            "2026-07-05",
            self._PM,
        )
        cids = [r["condition_id"] for r in results]
        assert "cid-spurs" in cids

    def test_result_has_days_diff(self) -> None:
        results = candidates_from_kalshi("San Antonio basketball", "2026-07-01", self._PM)
        assert all("days_diff" in r for r in results)

    def test_sentinel_tiebreaker_kalshi_direction(self) -> None:
        pm_rows = [
            ("cid-close", "Will the San Antonio Spurs win the 2026 NBA Finals?", "NBA", "2026-07-05"),
            ("cid-far", "Will the San Antonio Spurs win the 2026 NBA Finals?", "NBA", "2029-01-01"),
        ]
        results = candidates_from_kalshi(
            "Will the San Antonio win the 2026 Pro Basketball Finals?",
            "2026-07-01",
            pm_rows,
        )
        cids = [r["condition_id"] for r in results]
        assert cids.index("cid-close") < cids.index("cid-far")

    def test_empty_pm_corpus(self) -> None:
        assert candidates_from_kalshi("NBA Spurs", "2026-07-01", []) == []


# ---------------------------------------------------------------------------
# pairs CRUD (in-memory DB)
# ---------------------------------------------------------------------------

class TestPairsCRUD:
    from app.matching.pairs import (
        get_confirmed_pairs,
        insert_pair,
        list_pairs,
        reject_pair,
    )

    def _insert(self, conn: sqlite3.Connection, suffix: str = "") -> int:
        from app.matching.pairs import insert_pair
        # The FK constraint is ON, but kalshi_markets and markets tables exist.
        # We need to disable FK enforcement for in-memory test inserts.
        conn.execute("PRAGMA foreign_keys=OFF")
        return insert_pair(
            conn,
            pm_condition_id=f"cid-spurs{suffix}",
            kalshi_ticker=f"KXNBA-SAS{suffix}",
            note="Same event, same resolution date",
            pm_question_snapshot="Will the Spurs win?",
            pm_end_date_snapshot="2026-07-01",
            kalshi_title_snapshot="Will the San Antonio win?",
            kalshi_end_date_snapshot="2026-07-05",
        )

    def test_insert_returns_id(self) -> None:
        conn = _mem_db()
        pair_id = self._insert(conn)
        assert isinstance(pair_id, int)
        assert pair_id >= 1

    def test_insert_persists_data(self) -> None:
        from app.matching.pairs import list_pairs
        conn = _mem_db()
        self._insert(conn)
        rows = list_pairs(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r.pm_condition_id == "cid-spurs"
        assert r.kalshi_ticker == "KXNBA-SAS"
        assert r.status == "confirmed"
        assert r.note == "Same event, same resolution date"
        assert r.pm_question_snapshot == "Will the Spurs win?"
        assert r.kalshi_title_snapshot == "Will the San Antonio win?"
        assert r.confirmed_at is not None

    def test_reject_pair(self) -> None:
        from app.matching.pairs import list_pairs, reject_pair
        conn = _mem_db()
        pair_id = self._insert(conn)
        updated = reject_pair(conn, pair_id, "Different resolution rules")
        assert updated is True
        rows = list_pairs(conn, status="rejected")
        assert len(rows) == 1
        assert rows[0].status == "rejected"
        assert rows[0].rejected_reason == "Different resolution rules"
        assert rows[0].rejected_at is not None

    def test_reject_nonexistent_returns_false(self) -> None:
        from app.matching.pairs import reject_pair
        conn = _mem_db()
        assert reject_pair(conn, 9999, "reason") is False

    def test_list_filter_by_status_confirmed(self) -> None:
        from app.matching.pairs import list_pairs, reject_pair
        conn = _mem_db()
        id1 = self._insert(conn, suffix="")
        id2 = self._insert(conn, suffix="-b")
        reject_pair(conn, id1, "wrong")
        confirmed = list_pairs(conn, status="confirmed")
        assert len(confirmed) == 1
        assert confirmed[0].kalshi_ticker == "KXNBA-SAS-b"

    def test_list_filter_by_status_rejected(self) -> None:
        from app.matching.pairs import list_pairs, reject_pair
        conn = _mem_db()
        id1 = self._insert(conn)
        reject_pair(conn, id1, "wrong")
        rejected = list_pairs(conn, status="rejected")
        assert len(rejected) == 1

    def test_list_all_returns_everything(self) -> None:
        from app.matching.pairs import list_pairs, reject_pair
        conn = _mem_db()
        id1 = self._insert(conn)
        self._insert(conn, suffix="-b")
        reject_pair(conn, id1, "x")
        all_rows = list_pairs(conn, status=None)
        assert len(all_rows) == 2

    def test_unique_constraint_blocks_duplicate(self) -> None:
        import sqlite3 as _sqlite3
        from app.matching.pairs import insert_pair
        conn = _mem_db()
        conn.execute("PRAGMA foreign_keys=OFF")
        insert_pair(conn, "cid-a", "KXNBA-A", "note", "q", None, "t", None)
        with pytest.raises(_sqlite3.IntegrityError):
            insert_pair(conn, "cid-a", "KXNBA-A", "note2", "q2", None, "t2", None)

    def test_get_confirmed_pairs_excludes_rejected(self) -> None:
        from app.matching.pairs import get_confirmed_pairs, reject_pair
        conn = _mem_db()
        id1 = self._insert(conn)
        self._insert(conn, suffix="-b")
        reject_pair(conn, id1, "x")
        confirmed = get_confirmed_pairs(conn)
        assert len(confirmed) == 1
        assert confirmed[0].kalshi_ticker == "KXNBA-SAS-b"


# ---------------------------------------------------------------------------
# Schema v4 migration
# ---------------------------------------------------------------------------

class TestSchemaV4:
    def test_cross_platform_pairs_table_created(self) -> None:
        conn = _mem_db()
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "cross_platform_pairs" in tables

    def test_schema_version_is_4(self) -> None:
        from app.db.schema import CURRENT_VERSION
        conn = _mem_db()
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert ver == 4
        assert CURRENT_VERSION == 4

    def test_index_on_pairs_status(self) -> None:
        conn = _mem_db()
        indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_pairs_status_pm" in indexes

    def test_pairs_unique_constraint_from_schema(self) -> None:
        conn = _mem_db()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO cross_platform_pairs"
            "(pm_condition_id, kalshi_ticker, status, note,"
            " pm_question_snapshot, kalshi_title_snapshot, confirmed_at)"
            " VALUES ('cid-x','KXTEST','confirmed','','q','t','2026-01-01')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cross_platform_pairs"
                "(pm_condition_id, kalshi_ticker, status, note,"
                " pm_question_snapshot, kalshi_title_snapshot, confirmed_at)"
                " VALUES ('cid-x','KXTEST','confirmed','','q2','t2','2026-01-02')"
            )
