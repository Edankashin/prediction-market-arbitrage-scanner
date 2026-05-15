"""Tests for fees.py — cache logic and staleness detection."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db import store
from app.fees import _is_stale, fee_bps_from_formula, fee_from_snapshot, fee_for_token
from app.models import BookLevel, MarketRow, SnapshotRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(
    fees_enabled: bool = True,
    fee_formula: str | None = None,
    condition_id: str = "cond_test",
) -> MarketRow:
    return MarketRow(
        condition_id=condition_id,
        question="Test?",
        event_id=None,
        event_slug=None,
        event_title=None,
        neg_risk=False,
        active=True,
        closed=False,
        archived=False,
        end_date=None,
        clob_token_ids='["tok_yes","tok_no"]',
        outcomes=None,
        outcome_prices=None,
        volume=None,
        liquidity=None,
        spread=None,
        fees_enabled=fees_enabled,
        taker_fee_cap_bps=None,
        maker_rebate_cap_bps=None,
        fee_formula=fee_formula,
        gamma_updated_at=None,
        synced_at="2026-05-14T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# fee_bps_from_formula
# ---------------------------------------------------------------------------

class TestFeeBpsFromFormula:
    def _sched(self, rate: float, exponent: float = 1.0) -> str:
        return json.dumps({"rate": rate, "exponent": exponent, "takerOnly": True})

    def test_crypto_at_mid(self) -> None:
        """crypto_fees_v2: rate=0.07, exp=1, price=0.5 → 0.07×0.25×10000=175 bps."""
        m = _market(fee_formula=self._sched(0.07))
        result = fee_bps_from_formula(m, Decimal("0.5"))
        assert result == 175

    def test_sports_at_mid(self) -> None:
        """sports_fees_v2: rate=0.03, exp=1, price=0.5 → 0.03×0.25×10000=75 bps."""
        m = _market(fee_formula=self._sched(0.03))
        result = fee_bps_from_formula(m, Decimal("0.5"))
        assert result == 75

    def test_general_at_mid(self) -> None:
        """general_fees: rate=0.05, exp=1, price=0.5 → 0.05×0.25×10000=125 bps."""
        m = _market(fee_formula=self._sched(0.05))
        result = fee_bps_from_formula(m, Decimal("0.5"))
        assert result == 125

    def test_fee_decreases_away_from_mid(self) -> None:
        """Fee at price=0.1 must be lower than at price=0.5 (price×(1-price) smaller)."""
        m = _market(fee_formula=self._sched(0.07))
        fee_at_mid = fee_bps_from_formula(m, Decimal("0.5"))
        fee_at_otm = fee_bps_from_formula(m, Decimal("0.1"))
        assert fee_at_otm < fee_at_mid

    def test_fees_disabled_returns_zero(self) -> None:
        """feesEnabled=False markets always return 0 bps regardless of schedule."""
        m = _market(fees_enabled=False, fee_formula=self._sched(0.07))
        assert fee_bps_from_formula(m, Decimal("0.5")) == 0

    def test_missing_fee_formula_returns_zero(self) -> None:
        m = _market(fee_formula=None)
        assert fee_bps_from_formula(m, Decimal("0.5")) == 0

    def test_zero_rate_returns_zero(self) -> None:
        m = _market(fee_formula=self._sched(0.0))
        assert fee_bps_from_formula(m, Decimal("0.5")) == 0

    def test_price_at_boundary_zero(self) -> None:
        """At price=0 or price=1, fee is 0 (probability at limit → no fee)."""
        m = _market(fee_formula=self._sched(0.07))
        assert fee_bps_from_formula(m, Decimal("0")) == 0
        assert fee_bps_from_formula(m, Decimal("1")) == 0

    def test_returns_int(self) -> None:
        m = _market(fee_formula=self._sched(0.07))
        result = fee_bps_from_formula(m, Decimal("0.5"))
        assert isinstance(result, int)


class TestIsStale:
    def test_fresh_not_stale(self) -> None:
        now_str = datetime.now(tz=timezone.utc).isoformat()
        assert _is_stale(now_str, ttl_sec=300) is False

    def test_old_is_stale(self) -> None:
        old = (datetime.now(tz=timezone.utc) - timedelta(seconds=400)).isoformat()
        assert _is_stale(old, ttl_sec=300) is True

    def test_exactly_at_boundary(self) -> None:
        at_boundary = (datetime.now(tz=timezone.utc) - timedelta(seconds=300)).isoformat()
        # 300s elapsed, TTL is 300 → stale (age > ttl)
        assert _is_stale(at_boundary, ttl_sec=300) is True

    def test_invalid_timestamp_is_stale(self) -> None:
        assert _is_stale("not-a-date", ttl_sec=300) is True


class TestFeeFromSnapshot:
    def _snap(self, fee_bps: int | None) -> SnapshotRow:
        return SnapshotRow(
            token_id="tok",
            condition_id="cond",
            ts="2026-05-14T12:00:00+00:00",
            bids=[],
            asks=[BookLevel(Decimal("0.50"), Decimal("100"))],
            best_bid=None,
            best_ask=Decimal("0.50"),
            mid=None,
            taker_fee_bps=fee_bps,
        )

    def test_returns_fraction(self) -> None:
        result = fee_from_snapshot(self._snap(100))
        assert result == Decimal("0.0100")

    def test_zero_fee(self) -> None:
        result = fee_from_snapshot(self._snap(0))
        assert result == Decimal("0")

    def test_none_fee_returns_none(self) -> None:
        result = fee_from_snapshot(self._snap(None))
        assert result is None

    def test_180bps(self) -> None:
        result = fee_from_snapshot(self._snap(180))
        assert result == Decimal("0.0180")


class TestFeeForToken:
    def test_uses_cache_when_fresh(self, mem_db: object, mocker: object = None) -> None:  # type: ignore[assignment]
        """When cache is fresh, no HTTP call is made."""
        from unittest.mock import MagicMock

        import sqlite3

        conn: sqlite3.Connection = mem_db  # type: ignore[assignment]
        store.upsert_fee_rate(conn, "tok_cached", maker_bps=0, taker_bps=75)

        # Mock CLOB client — should not be called
        mock_clob = MagicMock()
        mock_clob.get_fee_rate_bps.return_value = 999  # would be wrong

        result = fee_for_token("tok_cached", conn, mock_clob, cache_ttl_sec=300)
        mock_clob.get_fee_rate_bps.assert_not_called()
        assert result == Decimal("0.0075")

    def test_fetches_when_stale(self, mem_db: object) -> None:
        """When cache is stale, calls CLOB client and updates cache."""
        from datetime import timedelta
        from unittest.mock import MagicMock

        import sqlite3

        conn: sqlite3.Connection = mem_db  # type: ignore[assignment]

        # Insert an old entry
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=400)).isoformat()
        conn.execute(
            "INSERT INTO fee_rate_cache(token_id, maker_bps, taker_bps, fetched_at) VALUES(?,?,?,?)",
            ("tok_stale", 0, 50, old_ts),
        )
        conn.commit()

        mock_clob = MagicMock()
        mock_clob.get_fee_rate_bps.return_value = 180

        result = fee_for_token("tok_stale", conn, mock_clob, cache_ttl_sec=300)
        mock_clob.get_fee_rate_bps.assert_called_once_with("tok_stale")
        assert result == Decimal("0.0180")

        # Verify cache was updated
        cached = store.get_fee_rate(conn, "tok_stale")
        assert cached is not None
        assert cached.taker_bps == 180

    def test_fetches_when_missing(self, mem_db: object) -> None:
        """When token is not in cache at all, calls CLOB client."""
        from unittest.mock import MagicMock

        import sqlite3

        conn: sqlite3.Connection = mem_db  # type: ignore[assignment]
        mock_clob = MagicMock()
        mock_clob.get_fee_rate_bps.return_value = 100

        result = fee_for_token("tok_new", conn, mock_clob, cache_ttl_sec=300)
        assert result == Decimal("0.0100")
        mock_clob.get_fee_rate_bps.assert_called_once_with("tok_new")
