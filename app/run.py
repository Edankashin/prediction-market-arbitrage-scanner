"""CLI entry point.

Usage (from repo root):
    python -m app.run init
    python -m app.run discover
    python -m app.run snapshot [--top N] [--by volume_24h|liquidity|spread]
    python -m app.run stats
    python -m app.run scan
    python -m app.run scan-log
    python -m app.run kalshi-discover
    python -m app.run kalshi-snapshot [--top N] [--by volume_desc|volume_asc|recent]
    python -m app.run backtest --start 2026-05-01T00:00:00 --end 2026-05-14T00:00:00
    python -m app.run loop [--interval 60] [--top 50] [--by volume_24h] [--discover-every 900]
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

from app.core.config import load
from app.utils.helpers import configure_logging

_log = logging.getLogger(__name__)

_BY_CHOICES = ["volume_24h", "liquidity", "spread"]
_BY_KALSHI_CHOICES = ["volume_desc", "volume_asc", "recent"]
_SCAN_EVERY = 5  # run cmd_scan every N snapshot cycles inside the loop

# ---------------------------------------------------------------------------
# SIGTERM / stop-flag machinery
# ---------------------------------------------------------------------------

_loop_stop = threading.Event()


def _install_sigterm_handler(stop: threading.Event) -> None:
    def _handler(signum: int, frame: object) -> None:
        _log.info("loop_sigterm_received; finishing current cycle then exiting")
        stop.set()

    signal.signal(signal.SIGTERM, _handler)


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(cfg: object) -> None:  # type: ignore[type-arg]
    """Create DB and run schema migrations."""
    from app.db import schema

    db_cfg = cfg.db  # type: ignore[attr-defined]
    conn = _open_db(db_cfg.path)
    schema.migrate(conn)
    print(f"Database initialised at {db_cfg.path}")


def cmd_discover(cfg: object) -> None:  # type: ignore[type-arg]
    """Fetch all active markets from Gamma and upsert to DB."""
    from datetime import datetime, timezone

    from app.api.gamma import GammaClient
    from app.db import schema, store

    gamma = GammaClient(cfg.gamma)  # type: ignore[attr-defined]
    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    schema.migrate(conn)

    synced_at = datetime.now(tz=timezone.utc).isoformat()
    raw_markets = gamma.get_all_active_markets()

    count = 0
    for raw in raw_markets:
        row = gamma.parse_market_row(raw, synced_at)
        if row.condition_id:
            store.upsert_market(conn, row)
            count += 1

    print(f"Discovered {count} markets.")


def cmd_snapshot(
    cfg: object,
    top: int | None = None,
    by: str = "volume_24h",
) -> int:  # type: ignore[type-arg]
    """Fetch one round of orderbook snapshots for active markets.

    Markets are sorted by `by` (descending for volume/liquidity, ascending for
    spread) and capped at `top` before snapshot fetch.  Returns the number of
    snapshots stored.
    """
    import dataclasses

    from app.api.clob import ClobPublicClient
    from app.db import schema, store
    from app.fees import fee_bps_from_formula

    clob = ClobPublicClient(cfg.clob)  # type: ignore[attr-defined]
    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    schema.migrate(conn)

    markets = store.get_active_markets(conn)

    # Sort by the requested field, Nones sort last
    if by == "volume_24h":
        markets.sort(key=lambda m: (m.volume is None, -(m.volume or Decimal("0"))))
    elif by == "liquidity":
        markets.sort(key=lambda m: (m.liquidity is None, -(m.liquidity or Decimal("0"))))
    elif by == "spread":
        markets.sort(
            key=lambda m: (m.spread is None, m.spread if m.spread is not None else Decimal("999"))
        )

    if top is not None:
        markets = markets[:top]

    token_ids: list[str] = []
    token_to_condition: dict[str, str] = {}
    token_to_market: dict[str, object] = {}

    for m in markets:
        ids = json.loads(m.clob_token_ids)
        for tid in ids:
            token_ids.append(tid)
            token_to_condition[tid] = m.condition_id
            token_to_market[tid] = m

    if not token_ids:
        print("No active markets found. Run 'discover' first.")
        return 0

    snaps = clob.get_order_books(token_ids)
    stored = 0
    for snap in snaps:
        if not snap.condition_id and snap.token_id in token_to_condition:
            snap = dataclasses.replace(
                snap, condition_id=token_to_condition[snap.token_id]
            )
        if not snap.condition_id:
            continue

        market = token_to_market.get(snap.token_id)
        if market is not None and snap.mid is not None:
            fee_bps = fee_bps_from_formula(market, snap.mid)  # type: ignore[arg-type]
            snap = dataclasses.replace(snap, taker_fee_bps=fee_bps)

        store.insert_snapshot(conn, snap)
        stored += 1

    print(f"Stored {stored} snapshots for {len(token_ids)} tokens.")
    return stored


def cmd_stats(cfg: object) -> None:  # type: ignore[type-arg]
    """Print DB stats."""
    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    cur = conn.execute("SELECT COUNT(*) FROM markets")
    n_markets = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM book_snapshots")
    n_snaps = cur.fetchone()[0]
    cur = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE neg_risk=1 AND active=1 AND closed=0"
    )
    n_neg = cur.fetchone()[0]

    print(f"Markets:              {n_markets}")
    print(f"  of which NegRisk:   {n_neg}")
    print(f"Book snapshots:       {n_snaps}")


def cmd_backtest(cfg: object, start: str, end: str) -> None:  # type: ignore[type-arg]
    """Run the NegRisk arb strategy against stored snapshots and print results."""
    from app.backtest.engine import BacktestConfig, IntendedTrade, replay
    from app.backtest.metrics import print_report
    from app.db import schema, store

    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    schema.migrate(conn)

    config = BacktestConfig(
        initial_capital=Decimal("1000"),
        gas_usdc_per_order=Decimal(str(cfg.gas.usdc_per_order)),  # type: ignore[attr-defined]
    )

    def negrisk_scan(snaps: list) -> list:  # type: ignore[type-arg]
        by_cond: dict[str, object] = {}
        for s in snaps:
            mkt = store.get_market(conn, s.condition_id)
            if mkt and mkt.neg_risk and s.best_ask is not None:
                eid = mkt.event_id or ""
                by_cond.setdefault(eid, []).append((s, mkt))  # type: ignore[union-attr]

        trades = []
        for eid, items in by_cond.items():  # type: ignore[union-attr]
            if len(items) < 2:
                continue
            total_ask = sum(s.best_ask for s, _ in items)  # type: ignore[union-attr]
            if total_ask >= Decimal("0.98"):
                continue
            group_id = f"bt_{eid[:8]}"
            for i, (snap, mkt) in enumerate(items):  # type: ignore[misc]
                trades.append(
                    IntendedTrade(
                        condition_id=snap.condition_id,
                        token_id=snap.token_id,
                        side="BUY",
                        size=Decimal("10"),
                        strategy="S1_NEGRISK",
                        group_id=group_id,
                        leg_index=i,
                    )
                )
        return trades

    result = replay(conn, negrisk_scan, start_ts=start, end_ts=end, config=config)
    print_report(result)


def cmd_scan(cfg: object) -> int:  # type: ignore[type-arg]
    """Scan the latest snapshot window for NegRisk arb; write hits to scan_log.

    Fetches the most recent snapshot per token within the last 90 seconds,
    groups by event_id, and calls scan_event() on each NegRisk event with
    ≥ 2 legs.  Opportunities are appended to scan_log.  Returns the count found.
    """
    from collections import defaultdict

    from app.db import schema, store
    from app.strategies.negrisk_scanner import ScanConfig, scan_event

    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    schema.migrate(conn)

    snaps = store.get_latest_snapshots_in_window(conn, window_sec=90)
    if not snaps:
        _log.info("scan_no_snapshots")
        return 0

    all_markets = store.get_active_markets(conn)
    market_map = {m.condition_id: m for m in all_markets}

    event_snaps: dict[str, list] = defaultdict(list)
    for snap in snaps:
        mkt = market_map.get(snap.condition_id)
        if mkt and mkt.neg_risk and mkt.event_id:
            event_snaps[mkt.event_id].append(snap)

    scan_cfg = ScanConfig(
        gas_usdc_per_order=Decimal(str(cfg.gas.usdc_per_order))  # type: ignore[attr-defined]
    )

    opps_found = 0
    events_scanned = 0
    for event_id, group_snaps in event_snaps.items():
        if len(group_snaps) < 2:
            continue
        events_scanned += 1
        event_markets = {s.condition_id: market_map[s.condition_id] for s in group_snaps}
        event_title = next(
            (m.event_title or "" for m in event_markets.values()), ""
        )
        opp = scan_event(group_snaps, event_markets, scan_cfg)
        if opp is not None:
            store.insert_scan_log_entry(conn, opp, event_title)
            opps_found += 1

    _log.info(
        "scan_complete",
        extra={"opps_found": opps_found, "events_scanned": events_scanned},
    )
    return opps_found


def cmd_kalshi_discover(cfg: object) -> int:  # type: ignore[type-arg]
    """Fetch all open Kalshi events+markets and upsert to kalshi_markets table.

    Two-level fetch: events endpoint does not include nested markets, so each
    event requires a separate /markets?event_ticker=X call.
    Returns number of markets stored.  Returns 0 if cfg.kalshi is absent.
    """
    if not hasattr(cfg, "kalshi"):
        return 0

    from datetime import datetime, timezone

    from app.api.kalshi import KalshiClient
    from app.db import schema, store

    kalshi = KalshiClient(cfg.kalshi)  # type: ignore[attr-defined]
    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    schema.migrate(conn)

    synced_at = datetime.now(tz=timezone.utc).isoformat()
    events = kalshi.get_all_open_events()

    count = 0
    for event in events:
        event_ticker = event.get("event_ticker", "")
        category = event.get("category")
        try:
            markets = kalshi.get_markets_for_event(event_ticker)
        except Exception as exc:
            _log.warning(
                "kalshi_discover_event_error",
                extra={"event_ticker": event_ticker, "error": str(exc)},
            )
            continue
        for mkt_raw in markets:
            row = kalshi.parse_market_row(mkt_raw, category, synced_at)
            store.upsert_kalshi_market(conn, row)
            count += 1

    _log.info("kalshi_discover_complete", extra={"markets_stored": count})
    return count


def cmd_kalshi_snapshot(
    cfg: object,
    top: int | None = None,
    by: str = "volume_desc",
) -> int:  # type: ignore[type-arg]
    """Fetch one round of Kalshi orderbook snapshots.

    Markets are sorted by `by` and capped at `top` before fetch.
    When `top` is None, falls back to cfg.kalshi.kalshi_top (if present).
    Returns number of snapshots stored.  Returns 0 if cfg.kalshi is absent.
    """
    if not hasattr(cfg, "kalshi"):
        return 0

    from app.api.kalshi import KalshiClient
    from app.db import schema, store

    kalshi = KalshiClient(cfg.kalshi)  # type: ignore[attr-defined]
    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    schema.migrate(conn)

    markets = store.get_active_kalshi_markets(conn)

    if by == "volume_desc":
        markets.sort(key=lambda m: (m.volume is None, -(m.volume or Decimal("0"))))
    elif by == "volume_asc":
        markets.sort(key=lambda m: (m.volume is not None, m.volume or Decimal("0")))
    elif by == "recent":
        markets.sort(key=lambda m: m.synced_at, reverse=True)

    # Resolve top: explicit arg wins; fall back to cfg.kalshi.kalshi_top.
    if top is None:
        kalshi_cfg = getattr(cfg, "kalshi", None)
        top = getattr(kalshi_cfg, "kalshi_top", None)

    if top is not None:
        markets = markets[:top]

    stored = 0
    for mkt in markets:
        try:
            raw = kalshi.get_orderbook(mkt.ticker)
            snap = kalshi.parse_and_validate_snapshot(mkt.ticker, raw)
            if snap is not None:
                store.insert_kalshi_snapshot(conn, snap)
                stored += 1
        except Exception as exc:
            _log.warning(
                "kalshi_snapshot_error",
                extra={"ticker": mkt.ticker, "error": str(exc)},
            )

    _log.info("kalshi_snapshot_complete", extra={"stored": stored})
    return stored


def cmd_scan_log(cfg: object) -> None:  # type: ignore[type-arg]
    """Print the last 24 h of scan_log entries with a summary footer."""
    from app.db import store

    conn = _open_db(cfg.db.path)  # type: ignore[attr-defined]
    rows = store.get_scan_log_last_24h(conn)

    if not rows:
        print("No arb opportunities logged in the last 24 hours.")
        return

    header = (
        f"{'ts':<26}  {'event_title':<40}  {'legs':>4}  "
        f"{'gross':>6}  {'net':>6}  {'dominant':<9}  {'shares':>12}"
    )
    print(header)
    print("-" * len(header))

    for r in rows:
        title = (r.event_title[:37] + "...") if len(r.event_title) > 40 else r.event_title
        print(
            f"{r.ts:<26}  {title:<40}  {r.leg_count:>4}  "
            f"{r.gross_edge_bps:>6}  {r.net_edge_bps:>6}  "
            f"{r.dominant_cost:<9}  {str(r.optimal_shares):>12}"
        )

    print()
    total = len(rows)
    avg_net = sum(r.net_edge_bps for r in rows) // total
    cost_counts: dict[str, int] = {}
    for r in rows:
        cost_counts[r.dominant_cost] = cost_counts.get(r.dominant_cost, 0) + 1
    dist = "  ".join(
        f"{k}: {v * 100 // total}%"
        for k, v in sorted(cost_counts.items())
    )
    print(f"Total last 24h: {total}  |  avg net_edge_bps: {avg_net}  |  dominant_cost: {dist}")


def cmd_loop(
    cfg: object,
    interval: int = 60,
    top: int = 50,
    by: str = "volume_24h",
    discover_every: int = 900,
    _stop: threading.Event | None = None,
) -> None:  # type: ignore[type-arg]
    """Run discover + snapshot in an infinite loop until stopped.

    Uses a monotonic clock to re-run discover every `discover_every` seconds
    regardless of how many snapshot cycles have completed.  Exceptions in the
    snapshot cycle are caught, logged at ERROR, and the loop continues.
    Only KeyboardInterrupt causes a clean exit.

    Args:
        cfg: Loaded configuration object.
        interval: Seconds to sleep between snapshot rounds.
        top: Maximum number of markets to snapshot per round.
        by: Sort key for market selection ('volume_24h', 'liquidity', 'spread').
        discover_every: Minimum seconds between full market re-discovery runs.
        _stop: Optional stop event; defaults to the module-level _loop_stop
               (which is set by the SIGTERM handler).  Pass a custom event
               in tests to control loop termination.
    """
    stop = _stop if _stop is not None else _loop_stop
    _install_sigterm_handler(stop)

    _log.info(
        "loop_start",
        extra={
            "interval_sec": interval,
            "top": top,
            "by": by,
            "discover_every_sec": discover_every,
        },
    )

    # Initial market discovery before the first snapshot round.
    # If it fails we log the error but still enter the loop — the re-discover
    # mechanism will retry after discover_every seconds.
    _log.info("loop_initial_discover")
    try:
        cmd_discover(cfg)
    except Exception as init_exc:
        _log.error(
            "loop_initial_discover_error",
            extra={"error": str(init_exc)},
            exc_info=True,
        )
    try:
        cmd_kalshi_discover(cfg)
    except Exception as init_exc:
        _log.error(
            "loop_initial_kalshi_discover_error",
            extra={"error": str(init_exc)},
            exc_info=True,
        )
    last_discover_at = time.monotonic()
    cycle_count = 0

    try:
        while not stop.is_set():
            cycle_start = time.time()
            sleep_sec = float(interval)  # default: full interval if cycle raises
            try:
                # Re-discover if discover_every seconds have elapsed.
                elapsed = time.monotonic() - last_discover_at
                if elapsed >= discover_every:
                    _log.info(
                        "loop_rediscover_triggered",
                        extra={"elapsed_sec": int(elapsed)},
                    )
                    try:
                        cmd_discover(cfg)
                        last_discover_at = time.monotonic()
                    except Exception as discover_exc:
                        _log.error(
                            "loop_discover_error",
                            extra={"error": str(discover_exc)},
                            exc_info=True,
                        )
                        # last_discover_at intentionally not updated so the
                        # next cycle will retry discover
                    try:
                        cmd_kalshi_discover(cfg)
                    except Exception as k_exc:
                        _log.error(
                            "loop_kalshi_discover_error",
                            extra={"error": str(k_exc)},
                            exc_info=True,
                        )

                # Snapshot round always runs, even if discover above failed
                stored = cmd_snapshot(cfg, top=top, by=by)

                # Kalshi snapshot round (top resolved inside cmd_kalshi_snapshot)
                k_stored = cmd_kalshi_snapshot(cfg)

                cycle_count += 1

                # Wall-clock adjusted sleep: subtract work time from interval
                cycle_elapsed = time.time() - cycle_start
                sleep_sec = max(0.0, interval - cycle_elapsed)
                if interval > 0 and sleep_sec == 0.0:
                    _log.warning(
                        "loop_cycle_overran",
                        extra={"elapsed_sec": cycle_elapsed},
                    )

                _log.info(
                    "loop_cycle_complete",
                    extra={
                        "snapshots_stored": stored,
                        "kalshi_snapshots_stored": k_stored,
                        "sleep_sec": sleep_sec,
                    },
                )

                # Arb scan every _SCAN_EVERY cycles; isolated so it cannot
                # interrupt the sleep or cause the loop to exit.
                if cycle_count % _SCAN_EVERY == 0:
                    try:
                        found = cmd_scan(cfg)
                        _log.info("loop_scan_complete", extra={"opps_found": found})
                    except Exception as scan_exc:
                        _log.error(
                            "loop_scan_error",
                            extra={"error": str(scan_exc)},
                            exc_info=True,
                        )

            except Exception as exc:
                _log.error(
                    "loop_cycle_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )

            # Interruptible sleep: stop.wait() returns immediately when set
            stop.wait(sleep_sec)

    except KeyboardInterrupt:
        _log.info("loop_keyboard_interrupt")

    _log.info("loop_stopped")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command."""
    parser = argparse.ArgumentParser(prog="polymarket-bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create/migrate database")
    sub.add_parser("discover", help="Fetch markets from Gamma API")

    snap_parser = sub.add_parser("snapshot", help="Take one round of orderbook snapshots")
    snap_parser.add_argument(
        "--top", type=int, default=None,
        help="Limit to top N markets (default: all)",
    )
    snap_parser.add_argument(
        "--by", choices=_BY_CHOICES, default="volume_24h",
        help="Sort key for market selection (default: volume_24h)",
    )

    sub.add_parser("stats", help="Print database statistics")
    sub.add_parser("scan", help="Scan latest snapshots for NegRisk arb opportunities")
    sub.add_parser("scan-log", help="Print last 24 h of arb scan results")
    sub.add_parser("kalshi-discover", help="Fetch Kalshi open markets and upsert to DB")

    k_snap_parser = sub.add_parser(
        "kalshi-snapshot", help="Take one round of Kalshi orderbook snapshots"
    )
    k_snap_parser.add_argument(
        "--top", type=int, default=None,
        help="Limit to top N markets (default: all)",
    )
    k_snap_parser.add_argument(
        "--by", choices=_BY_KALSHI_CHOICES, default="volume_desc",
        help="Sort key for market selection (default: volume_desc)",
    )

    bt_parser = sub.add_parser("backtest", help="Run backtest on stored snapshots")
    bt_parser.add_argument("--start", required=True, help="Start timestamp ISO 8601")
    bt_parser.add_argument("--end", required=True, help="End timestamp ISO 8601")

    loop_parser = sub.add_parser("loop", help="Run discover+snapshot in a continuous loop")
    loop_parser.add_argument(
        "--interval", type=int, default=60,
        help="Seconds between snapshot rounds (default: 60)",
    )
    loop_parser.add_argument(
        "--top", type=int, default=50,
        help="Markets to snapshot per round (default: 50)",
    )
    loop_parser.add_argument(
        "--by", choices=_BY_CHOICES, default="volume_24h",
        help="Sort key for market selection (default: volume_24h)",
    )
    loop_parser.add_argument(
        "--discover-every", type=int, default=900,
        help="Seconds between full market re-discovery (default: 900)",
    )

    args = parser.parse_args()
    cfg = load()
    configure_logging(cfg.logging.level, cfg.logging.log_dir)  # type: ignore[attr-defined]

    if args.command == "init":
        cmd_init(cfg)
    elif args.command == "discover":
        cmd_discover(cfg)
    elif args.command == "snapshot":
        cmd_snapshot(cfg, top=args.top, by=args.by)
    elif args.command == "stats":
        cmd_stats(cfg)
    elif args.command == "scan":
        cmd_scan(cfg)
    elif args.command == "scan-log":
        cmd_scan_log(cfg)
    elif args.command == "kalshi-discover":
        cmd_kalshi_discover(cfg)
    elif args.command == "kalshi-snapshot":
        cmd_kalshi_snapshot(cfg, top=args.top, by=args.by)
    elif args.command == "backtest":
        cmd_backtest(cfg, args.start, args.end)
    elif args.command == "loop":
        cmd_loop(
            cfg,
            interval=args.interval,
            top=args.top,
            by=args.by,
            discover_every=args.discover_every,
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
