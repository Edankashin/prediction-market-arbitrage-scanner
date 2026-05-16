#!/usr/bin/env python3
"""Backfill historical book_snapshots through the NegRisk arb scanner.

Writes hits to scan_log with discovered_at='BACKFILL' so they are
distinguishable from live-scan rows.

Usage (from project root):
    python scripts/backfill_scan_log.py

Idempotent: exits early if any BACKFILL rows already exist in scan_log.
To re-run: DELETE FROM scan_log WHERE discovered_at='BACKFILL'
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import schema, store
from app.strategies.negrisk_scanner import ScanConfig, scan_event

_DEFAULT_DB = "data/polymarket.db"
_SCAN_CFG = ScanConfig(gas_usdc_per_order=Decimal("0.005"), min_net_edge_bps=1)
_PROGRESS_EVERY = 25


def run_backfill(db_path: str = _DEFAULT_DB) -> dict:
    """Run the backfill against db_path and return a summary dict.

    Safe to call concurrently with the live loop: only writes to scan_log;
    SQLite WAL mode allows interleaved reads and writes without locking.
    """
    conn = sqlite3.connect(db_path)
    schema.migrate(conn)

    existing = conn.execute(
        "SELECT COUNT(*) FROM scan_log WHERE discovered_at='BACKFILL'"
    ).fetchone()[0]
    if existing > 0:
        print(
            f"Backfill already ran: {existing} BACKFILL rows exist in scan_log.\n"
            "To re-run: DELETE FROM scan_log WHERE discovered_at='BACKFILL'"
        )
        conn.close()
        return {"skipped": True, "existing_backfill_rows": existing}

    all_markets = store.get_all_markets(conn)
    markets_map = {m.condition_id: m for m in all_markets}
    print(f"Loaded {len(markets_map)} markets.")

    print("Loading NegRisk snapshots...")
    all_snaps = store.get_all_negrisk_snapshots(conn)
    print(f"  {len(all_snaps)} NegRisk snapshot rows.")

    # Keep the latest snapshot per (event_id, minute_bucket, token_id)
    latest: dict[tuple[str, str, str], tuple] = {}
    for snap, event_id, event_title in all_snaps:
        bucket = snap.ts[:16]
        key = (event_id, bucket, snap.token_id)
        prev = latest.get(key)
        if prev is None or snap.ts > prev[0].ts:
            latest[key] = (snap, event_title)

    # Collect per-bucket snapshot lists and filter to ≥2 distinct tokens
    buckets_snaps: dict[tuple[str, str], list] = defaultdict(list)
    event_titles: dict[tuple[str, str], str] = {}
    for (event_id, bucket, _tok), (snap, event_title) in latest.items():
        bkey = (event_id, bucket)
        buckets_snaps[bkey].append(snap)
        event_titles[bkey] = event_title

    eligible = [
        (bkey, snaps, event_titles[bkey])
        for bkey, snaps in buckets_snaps.items()
        if len(snaps) >= 2
    ]
    eligible.sort(key=lambda x: min(s.ts for s in x[1]))

    total_buckets = len(eligible)
    print(f"  {total_buckets} (event_id, minute) buckets with ≥2 tokens.\n")

    start = time.monotonic()
    opps_found = 0
    all_opps = []

    for i, (bkey, snaps, event_title) in enumerate(eligible, 1):
        event_id, _bucket = bkey
        opp = scan_event(snaps, markets_map, _SCAN_CFG)
        if opp is not None:
            store.insert_scan_log_entry(conn, opp, event_title, discovered_at="BACKFILL")
            opps_found += 1
            all_opps.append(opp)

        if i % _PROGRESS_EVERY == 0 or i == total_buckets:
            elapsed = time.monotonic() - start
            print(
                f"[{i}/{total_buckets}] "
                f"event_id={event_id}  "
                f"opps_found_so_far={opps_found}  "
                f"elapsed={elapsed:.1f}s"
            )

    conn.close()

    summary: dict = {
        "skipped": False,
        "total_buckets": total_buckets,
        "opps_found": opps_found,
    }
    print(f"\n=== Backfill complete ===")
    print(f"Buckets scanned:      {total_buckets}")
    print(f"Opportunities found:  {opps_found}")

    if all_opps:
        edges = sorted(o.net_edge_bps for o in all_opps)
        n = len(edges)
        p90_idx = min(int(n * 0.9), n - 1)
        med = median(edges)
        print(f"\nnet_edge_bps:  min={edges[0]}  max={edges[-1]}  median={med}  p90={edges[p90_idx]}")
        summary["net_edge_bps"] = {"min": edges[0], "max": edges[-1], "median": med, "p90": edges[p90_idx]}

        cost_counts: dict[str, int] = defaultdict(int)
        for o in all_opps:
            cost_counts[o.dominant_cost] += 1
        print(f"dominant_cost: {dict(cost_counts)}")
        summary["dominant_cost"] = dict(cost_counts)

        avg_legs = sum(len(o.legs) for o in all_opps) / n
        print(f"avg legs/opp:  {avg_legs:.1f}")
        summary["avg_legs"] = avg_legs

    return summary


if __name__ == "__main__":
    run_backfill()
