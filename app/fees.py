"""Dynamic fee resolution.

Two paths:
  1. fee_bps_from_formula() — computes the price-dependent taker fee from the
     Gamma feeSchedule formula: rate × (price × (1-price))^exponent.
     This is the V2 dynamic fee model (March 2026+) and is the authoritative
     source for snapshot-time fee stamping.

  2. fee_for_token() — legacy path using CLOB /fee-rate base_fee. Kept for
     the fee_rate_cache TTL machinery but note that base_fee=1000 is a flat
     constant that does NOT vary by category; use fee_bps_from_formula()
     for economic calculations instead.

Fee rates are stored as integer basis points (bps): 100 bps = 1.00%.
This module returns Decimal fractions (e.g., Decimal("0.0100") for 1%).

IMPORTANT: The spec requires fees to be subtracted from gross expected value,
never from net. All callers receive the raw taker rate; do not pre-net it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.api.clob import ClobPublicClient
from app.db import store
from app.models import MarketRow, SnapshotRow

_log = logging.getLogger(__name__)


def fee_bps_from_formula(
    market: MarketRow,
    price: Decimal,
) -> int:
    """Compute the taker fee in bps using the Gamma feeSchedule formula.

    Formula: taker_rate = rate × (price × (1 - price))^exponent
    Result in bps: int(taker_rate × 10000)

    Returns 0 for feesEnabled=False markets or unparseable schedules.
    The price argument should be the mid price (or fill price) at snapshot time.
    """
    if not market.fees_enabled:
        return 0
    if not market.fee_formula:
        return 0
    try:
        sched = json.loads(market.fee_formula)
        rate = float(sched.get("rate") or 0)
        exp = float(sched.get("exponent") or 1)
        p = float(price)
        taker_rate = rate * (p * (1 - p)) ** exp
        return int(taker_rate * 10000)
    except Exception as exc:
        _log.warning("fee_formula_parse_error", extra={"error": str(exc)})
        return 0


def fee_for_token(
    token_id: str,
    conn: sqlite3.Connection,
    clob: ClobPublicClient,
    cache_ttl_sec: int,
) -> Decimal:
    """Return current taker fee rate as a Decimal fraction via CLOB /fee-rate.

    NOTE: As of V2 (April 2026), CLOB /fee-rate returns base_fee=1000 as a
    flat constant for all feesEnabled=True markets — it does not encode the
    dynamic per-category rate. Use fee_bps_from_formula() for snapshot-time
    fee stamping. This function is kept for the cache TTL machinery and for
    feesEnabled=False detection (returns 0).

    E.g., 1% fee → Decimal("0.0100").
    """
    cached = store.get_fee_rate(conn, token_id)
    if cached is not None and not _is_stale(cached.fetched_at, cache_ttl_sec):
        return Decimal(cached.taker_bps) / Decimal("10000")

    taker_bps = clob.get_fee_rate_bps(token_id)
    store.upsert_fee_rate(conn, token_id, maker_bps=0, taker_bps=taker_bps)
    _log.info(
        "fee_rate_refreshed",
        extra={"token_id": token_id, "taker_bps": taker_bps},
    )
    return Decimal(taker_bps) / Decimal("10000")


def fee_from_snapshot(snap: SnapshotRow) -> Decimal | None:
    """Return the fee recorded at snapshot time, or None if not captured.

    Useful in the backtest engine where we replay historical snapshots and
    want to use the fee that was live when the snapshot was taken.
    """
    if snap.taker_fee_bps is None:
        return None
    return Decimal(snap.taker_fee_bps) / Decimal("10000")


def fee_for_group(
    token_ids: list[str],
    conn: sqlite3.Connection,
    clob: ClobPublicClient,
    cache_ttl_sec: int,
) -> dict[str, Decimal]:
    """Batch fee lookup: returns {token_id: taker_rate} for each token."""
    return {
        tid: fee_for_token(tid, conn, clob, cache_ttl_sec)
        for tid in token_ids
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_stale(fetched_at: str, ttl_sec: int) -> bool:
    """Return True if fetched_at is older than ttl_sec seconds."""
    try:
        fetched = datetime.fromisoformat(fetched_at)
        now = datetime.now(tz=timezone.utc)
        # Make fetched timezone-aware if needed
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age_sec = (now - fetched).total_seconds()
        return age_sec > ttl_sec
    except ValueError:
        return True  # unparseable timestamp → treat as stale
