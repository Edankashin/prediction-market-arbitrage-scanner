"""SQLite schema DDL and migration.

Schema version 1 — M1 milestone.
All monetary values stored as TEXT (Decimal string).
JSON blobs stored as TEXT (SQLite has no native JSON type but supports JSON functions).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_MARKETS = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id          TEXT PRIMARY KEY,
    question              TEXT NOT NULL,
    event_id              TEXT,
    event_slug            TEXT,
    event_title           TEXT,
    neg_risk              INTEGER NOT NULL DEFAULT 0,
    active                INTEGER NOT NULL DEFAULT 1,
    closed                INTEGER NOT NULL DEFAULT 0,
    archived              INTEGER NOT NULL DEFAULT 0,
    end_date              TEXT,
    clob_token_ids        TEXT NOT NULL,
    outcomes              TEXT,
    outcome_prices        TEXT,
    volume                TEXT,
    liquidity             TEXT,
    spread                TEXT,
    fees_enabled          INTEGER NOT NULL DEFAULT 1,
    taker_fee_cap_bps     INTEGER,
    maker_rebate_cap_bps  INTEGER,
    fee_formula           TEXT,
    gamma_updated_at      TEXT,
    synced_at             TEXT NOT NULL
)
"""

_IDX_MARKETS_EVENT = """
CREATE INDEX IF NOT EXISTS idx_markets_event
    ON markets(event_id, neg_risk)
"""

_IDX_MARKETS_ACTIVE = """
CREATE INDEX IF NOT EXISTS idx_markets_active
    ON markets(active, closed, archived)
"""

_BOOK_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS book_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    ts            TEXT NOT NULL,
    bids          TEXT NOT NULL,
    asks          TEXT NOT NULL,
    best_bid      TEXT,
    best_ask      TEXT,
    mid           TEXT,
    taker_fee_bps INTEGER,
    FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
)
"""

_IDX_SNAP_TOKEN_TS = """
CREATE INDEX IF NOT EXISTS idx_snapshots_token_ts
    ON book_snapshots(token_id, ts)
"""

_IDX_SNAP_COND_TS = """
CREATE INDEX IF NOT EXISTS idx_snapshots_condition_ts
    ON book_snapshots(condition_id, ts)
"""

_FEE_RATE_CACHE = """
CREATE TABLE IF NOT EXISTS fee_rate_cache (
    token_id   TEXT PRIMARY KEY,
    maker_bps  INTEGER NOT NULL,
    taker_bps  INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
)
"""

_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_ref      TEXT UNIQUE NOT NULL,
    mode           TEXT NOT NULL CHECK(mode IN ('paper','shadow','live','backtest')),
    strategy       TEXT NOT NULL,
    group_id       TEXT,
    leg_index      INTEGER,
    condition_id   TEXT NOT NULL,
    token_id       TEXT NOT NULL,
    side           TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    intended_price TEXT NOT NULL,
    intended_size  TEXT NOT NULL,
    fill_price     TEXT,
    fill_size      TEXT,
    cost_usdc      TEXT,
    fee_usdc       TEXT NOT NULL DEFAULT '0',
    gas_usdc       TEXT NOT NULL DEFAULT '0',
    slippage_bps   TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','filled','partial','cancelled','failed')),
    created_at     TEXT NOT NULL,
    filled_at      TEXT,
    FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
)
"""

_IDX_TRADES_GROUP = """
CREATE INDEX IF NOT EXISTS idx_trades_group
    ON trades(group_id)
"""

_IDX_TRADES_MODE = """
CREATE INDEX IF NOT EXISTS idx_trades_mode_strategy
    ON trades(mode, strategy, created_at)
"""

_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT NOT NULL CHECK(mode IN ('paper','shadow','live')),
    condition_id  TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    shares        TEXT NOT NULL DEFAULT '0',
    avg_cost      TEXT NOT NULL DEFAULT '0',
    realized_pnl  TEXT NOT NULL DEFAULT '0',
    opened_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(mode, token_id),
    FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
)
"""

_PORTFOLIO_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    usdc_balance    TEXT NOT NULL,
    position_value  TEXT NOT NULL,
    total_equity    TEXT NOT NULL,
    realized_pnl    TEXT NOT NULL,
    unrealized_pnl  TEXT NOT NULL,
    trade_count     INTEGER NOT NULL DEFAULT 0,
    win_count       INTEGER NOT NULL DEFAULT 0
)
"""

_IDX_PORTFOLIO_MODE_TS = """
CREATE INDEX IF NOT EXISTS idx_portfolio_mode_ts
    ON portfolio_snapshots(mode, ts)
"""

_DATA_API_POSITIONS = """
CREATE TABLE IF NOT EXISTS data_api_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    outcome         TEXT,
    shares          TEXT NOT NULL,
    avg_price       TEXT,
    realized_pnl    TEXT,
    unrealized_pnl  TEXT,
    fetched_at      TEXT NOT NULL,
    UNIQUE(wallet, token_id)
)
"""

_SCAN_LOG = """
CREATE TABLE IF NOT EXISTS scan_log (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                     TEXT NOT NULL,
    discovered_at          TEXT NOT NULL,
    event_id               TEXT NOT NULL,
    event_title            TEXT NOT NULL DEFAULT '',
    leg_count              INTEGER NOT NULL,
    gross_edge_bps         INTEGER NOT NULL,
    net_edge_bps           INTEGER NOT NULL,
    dominant_cost          TEXT NOT NULL,
    total_cost_usdc        TEXT NOT NULL,
    guaranteed_payoff_usdc TEXT NOT NULL,
    optimal_shares         TEXT NOT NULL,
    legs_json              TEXT NOT NULL
)
"""

_IDX_SCAN_LOG_TS_NET = """
CREATE INDEX IF NOT EXISTS idx_scan_log_ts_net
    ON scan_log(ts, net_edge_bps DESC)
"""

_KALSHI_MARKETS = """
CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker           TEXT PRIMARY KEY,
    event_ticker     TEXT NOT NULL,
    title            TEXT NOT NULL,
    category         TEXT,
    status           TEXT NOT NULL DEFAULT 'active',
    end_date         TEXT,
    yes_bid          TEXT,
    volume           TEXT,
    taker_fee_coeff  TEXT NOT NULL DEFAULT '0.07',
    synced_at        TEXT NOT NULL
)
"""

_IDX_KALSHI_MARKETS_EVENT = """
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_event
    ON kalshi_markets(event_ticker)
"""

_IDX_KALSHI_MARKETS_STATUS = """
CREATE INDEX IF NOT EXISTS idx_kalshi_markets_status
    ON kalshi_markets(status)
"""

_KALSHI_BOOK_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS kalshi_book_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    yes_bids     TEXT NOT NULL,
    no_bids      TEXT NOT NULL,
    best_yes_ask TEXT,
    best_no_ask  TEXT,
    single_sided INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (ticker) REFERENCES kalshi_markets(ticker)
)
"""

_IDX_KALSHI_SNAP_TICKER_TS = """
CREATE INDEX IF NOT EXISTS idx_kalshi_snapshots_ticker_ts
    ON kalshi_book_snapshots(ticker, ts)
"""

_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL
)
"""

_ALL_DDL = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    _MARKETS,
    _IDX_MARKETS_EVENT,
    _IDX_MARKETS_ACTIVE,
    _BOOK_SNAPSHOTS,
    _IDX_SNAP_TOKEN_TS,
    _IDX_SNAP_COND_TS,
    _FEE_RATE_CACHE,
    _TRADES,
    _IDX_TRADES_GROUP,
    _IDX_TRADES_MODE,
    _POSITIONS,
    _PORTFOLIO_SNAPSHOTS,
    _IDX_PORTFOLIO_MODE_TS,
    _DATA_API_POSITIONS,
    _SCAN_LOG,
    _IDX_SCAN_LOG_TS_NET,
    _KALSHI_MARKETS,
    _IDX_KALSHI_MARKETS_EVENT,
    _IDX_KALSHI_MARKETS_STATUS,
    _KALSHI_BOOK_SNAPSHOTS,
    _IDX_KALSHI_SNAP_TICKER_TS,
    _SCHEMA_VERSION,
]

CURRENT_VERSION = 3


def migrate(conn: sqlite3.Connection) -> None:
    """Apply all DDL statements and set schema_version = CURRENT_VERSION.

    Idempotent: safe to call on an already-migrated database.
    DELETE + INSERT keeps version current across upgrades.
    """
    for ddl in _ALL_DDL:
        conn.execute(ddl)
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
        (CURRENT_VERSION, _now()),
    )
    conn.commit()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
