"""DB CRUD for the cross_platform_pairs table.

Only this module and app/db/store.py write to the database.
All pair lifecycle operations (confirm, reject, list) live here.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.models import CrossPlatformPairRow


def insert_pair(
    conn: sqlite3.Connection,
    pm_condition_id: str,
    kalshi_ticker: str,
    note: str,
    pm_question_snapshot: str,
    pm_end_date_snapshot: str | None,
    kalshi_title_snapshot: str,
    kalshi_end_date_snapshot: str | None,
) -> int:
    """Insert a confirmed pair. Returns the new row id.

    Raises sqlite3.IntegrityError if (pm_condition_id, kalshi_ticker) already
    exists — the UNIQUE constraint intentionally prevents re-confirming a
    rejected pair via a new INSERT.
    """
    confirmed_at = datetime.now(tz=timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO cross_platform_pairs (
            pm_condition_id, kalshi_ticker, status, note,
            pm_question_snapshot, pm_end_date_snapshot,
            kalshi_title_snapshot, kalshi_end_date_snapshot,
            confirmed_at
        ) VALUES (?, ?, 'confirmed', ?, ?, ?, ?, ?, ?)
        """,
        (
            pm_condition_id, kalshi_ticker, note,
            pm_question_snapshot, pm_end_date_snapshot,
            kalshi_title_snapshot, kalshi_end_date_snapshot,
            confirmed_at,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def reject_pair(conn: sqlite3.Connection, pair_id: int, reason: str) -> bool:
    """Set status='rejected' on an existing pair.

    Returns True if a row was updated, False if pair_id was not found.
    No-op (returns True) if the pair is already rejected.
    """
    rejected_at = datetime.now(tz=timezone.utc).isoformat()
    cur = conn.execute(
        """
        UPDATE cross_platform_pairs
        SET status='rejected', rejected_at=?, rejected_reason=?
        WHERE id=?
        """,
        (rejected_at, reason, pair_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_confirmed_pairs(conn: sqlite3.Connection) -> list[CrossPlatformPairRow]:
    """Fetch all confirmed pairs (used by the scanner)."""
    return _fetch(conn, status="confirmed")


def list_pairs(
    conn: sqlite3.Connection,
    status: str | None = None,
) -> list[CrossPlatformPairRow]:
    """Fetch all pairs, optionally filtered by status."""
    return _fetch(conn, status=status)


def _fetch(
    conn: sqlite3.Connection,
    status: str | None,
) -> list[CrossPlatformPairRow]:
    conn.row_factory = sqlite3.Row
    if status is not None:
        cur = conn.execute(
            "SELECT * FROM cross_platform_pairs WHERE status=? ORDER BY id",
            (status,),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM cross_platform_pairs ORDER BY id"
        )
    return [_row_to_model(r) for r in cur.fetchall()]


def _row_to_model(row: sqlite3.Row) -> CrossPlatformPairRow:
    return CrossPlatformPairRow(
        id=row["id"],
        pm_condition_id=row["pm_condition_id"],
        kalshi_ticker=row["kalshi_ticker"],
        status=row["status"],
        note=row["note"],
        pm_question_snapshot=row["pm_question_snapshot"],
        pm_end_date_snapshot=row["pm_end_date_snapshot"],
        kalshi_title_snapshot=row["kalshi_title_snapshot"],
        kalshi_end_date_snapshot=row["kalshi_end_date_snapshot"],
        confirmed_at=row["confirmed_at"],
        rejected_at=row["rejected_at"],
        rejected_reason=row["rejected_reason"],
    )
