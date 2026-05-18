"""Shared data model dataclasses.

All monetary fields use Decimal (never float). Stored in SQLite as TEXT.
No imports from other app modules — safe to import from anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class MarketRow:
    """Represents one row in the markets table."""

    condition_id: str
    question: str
    event_id: str | None
    event_slug: str | None
    event_title: str | None
    neg_risk: bool
    active: bool
    closed: bool
    archived: bool
    end_date: str | None
    clob_token_ids: str  # JSON: ["yes_token_id", "no_token_id"]
    outcomes: str | None  # JSON: ["Yes","No"] or candidate names
    outcome_prices: str | None  # JSON: ["0.55","0.45"] last known
    volume: Decimal | None
    liquidity: Decimal | None
    spread: Decimal | None
    fees_enabled: bool
    taker_fee_cap_bps: int | None  # category cap from Gamma feeSchedule
    maker_rebate_cap_bps: int | None  # rebate cap from Gamma feeSchedule
    fee_formula: str | None  # full feeSchedule JSON blob for backtest replay
    gamma_updated_at: str | None
    synced_at: str


@dataclass(frozen=True)
class BookLevel:
    """A single price level in an order book."""

    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class SnapshotRow:
    """One order-book snapshot for one token at one timestamp."""

    token_id: str
    condition_id: str
    ts: str  # ISO 8601 UTC
    bids: list[BookLevel]  # descending price
    asks: list[BookLevel]  # ascending price
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    taker_fee_bps: int | None  # None = not yet fetched; 0 = fees disabled


@dataclass(frozen=True)
class FeeRateRow:
    """Cached per-token fee rates from /fee-rate endpoint."""

    token_id: str
    maker_bps: int
    taker_bps: int
    fetched_at: str  # ISO 8601 UTC


@dataclass
class TradeRow:
    """One trade leg — immutable after filled_at is set."""

    trade_ref: str  # UUID (paper/shadow) or CLOB order_id (live)
    mode: str  # 'paper' | 'shadow' | 'live' | 'backtest'
    strategy: str  # 'S1_NEGRISK' | 'S1_INTRA' | 'S2_LP' | 'MANUAL'
    condition_id: str
    token_id: str
    side: str  # 'BUY' | 'SELL'
    intended_price: Decimal
    intended_size: Decimal
    created_at: str
    group_id: str | None = None  # links legs of one multi-leg arb
    leg_index: int | None = None
    fill_price: Decimal | None = None
    fill_size: Decimal | None = None
    cost_usdc: Decimal | None = None
    fee_usdc: Decimal = field(default_factory=lambda: Decimal("0"))
    gas_usdc: Decimal = field(default_factory=lambda: Decimal("0"))
    slippage_bps: Decimal | None = None
    status: str = "pending"
    filled_at: str | None = None
    id: int | None = None


@dataclass
class PositionRow:
    """Current holding for one (mode, token_id) pair.

    side is not stored — derive from index in markets.clob_token_ids.
    """

    mode: str
    condition_id: str
    token_id: str
    shares: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal
    opened_at: str
    updated_at: str
    id: int | None = None


@dataclass
class PortfolioSnapshotRow:
    """Point-in-time equity curve entry."""

    mode: str
    ts: str
    usdc_balance: Decimal
    position_value: Decimal
    total_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    trade_count: int
    win_count: int
    id: int | None = None


@dataclass
class DataApiPositionRow:
    """On-chain position read from data-api.polymarket.com."""

    wallet: str
    condition_id: str
    token_id: str
    outcome: str | None
    shares: Decimal
    avg_price: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    fetched_at: str
    id: int | None = None


@dataclass(frozen=True)
class ScanLogRow:
    """One arb opportunity recorded by the NegRisk scanner."""

    ts: str                          # snapshot ts, SQLite-comparable UTC (space separator)
    discovered_at: str               # wall clock when the scan ran
    event_id: str
    event_title: str
    leg_count: int
    gross_edge_bps: int
    net_edge_bps: int
    dominant_cost: str               # 'fee' | 'slippage' | 'gas'
    total_cost_usdc: Decimal
    guaranteed_payoff_usdc: Decimal
    optimal_shares: Decimal
    legs_json: str                   # JSON list of ArbLeg field dicts
    id: int | None = None


@dataclass(frozen=True)
class KalshiMarketRow:
    """One row in the kalshi_markets table."""

    ticker: str
    event_ticker: str
    title: str
    category: str | None
    status: str              # 'active' | 'closed' | 'settled'
    end_date: str | None
    yes_bid: Decimal | None
    volume: Decimal | None
    taker_fee_coeff: Decimal  # always Decimal("0.07") for taker
    synced_at: str


@dataclass(frozen=True)
class KalshiSnapshotRow:
    """One Kalshi orderbook snapshot for one market ticker."""

    ticker: str
    ts: str                    # ISO 8601 UTC
    yes_bids: list[BookLevel]  # descending by price (best bid first)
    no_bids: list[BookLevel]   # descending by price (best bid first)
    best_yes_ask: Decimal | None  # = 1 − best_no_bid
    best_no_ask: Decimal | None   # = 1 − best_yes_bid
    single_sided: bool
    id: int | None = None


@dataclass(frozen=True)
class CrossScanRow:
    """One scan-log entry for a confirmed PM ↔ Kalshi pair."""

    pair_id: int
    scanned_at: str           # ISO-8601 UTC
    direction: str            # 'A' (pm_yes+kal_no) | 'B' (pm_no+kal_yes) | 'BOTH' (crossed)
    pm_yes_ask: Decimal | None
    pm_no_ask: Decimal | None
    kalshi_yes_ask: Decimal | None
    kalshi_no_ask: Decimal | None
    gross_profit_per_unit: Decimal | None
    pm_fee_total: Decimal | None
    kalshi_fee_total: Decimal | None
    gas_flat: Decimal | None
    net_profit_at_size: Decimal | None
    net_profit_per_unit_excl_gas: Decimal | None
    net_edge_bps: int | None
    max_profitable_units: Decimal | None
    pm_snapshot_age_sec: int | None
    kalshi_snapshot_age_sec: int | None
    skipped: bool
    skip_reason: str | None
    id: int | None = None


@dataclass(frozen=True)
class CrossPlatformPairRow:
    """One confirmed (or rejected) PM ↔ Kalshi market pair."""

    pm_condition_id: str
    kalshi_ticker: str
    status: str              # 'confirmed' | 'rejected'
    note: str
    pm_question_snapshot: str    # PM question text captured at confirmation time
    pm_end_date_snapshot: str | None
    kalshi_title_snapshot: str   # Kalshi title captured at confirmation time
    kalshi_end_date_snapshot: str | None
    confirmed_at: str        # ISO 8601 UTC
    rejected_at: str | None
    rejected_reason: str | None
    id: int | None = None
