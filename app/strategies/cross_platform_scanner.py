"""Cross-platform arbitrage scanner (S4-M3).

Scans all confirmed PM ↔ Kalshi pairs for spread opportunities.
Pure computation — no DB writes.  Callers (app/run.py) handle persistence.

Direction A: Buy PM YES + Buy Kalshi NO
Direction B: Buy PM NO  + Buy Kalshi YES

Net profit at size:
    gross_per_unit × max_units
    − pm_fee_total          (fee_bps_from_formula(market, pm_ask) × pm_ask × max_units)
    − kalshi_fee_total      (0.07 × kalshi_ask × (1 − kalshi_ask) × max_units)
    − gas_flat              (one PM on-chain order, fixed regardless of size)

Net edge bps:
    (net_profit_at_size / total_cost_at_size) × 10000
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db import store
from app.fees import fee_bps_from_formula
from app.models import CrossScanRow, MarketRow

_log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")


@dataclass(frozen=True)
class DirectionResult:
    """Economics for one direction of a cross-platform arb at top-of-book size."""

    gross_per_unit: Decimal
    pm_fee_total: Decimal
    kalshi_fee_total: Decimal
    net_at_size: Decimal
    net_per_unit_excl_gas: Decimal
    max_units: Decimal
    net_edge_bps: int


def _resolve_pm_tokens(
    clob_token_ids: str,
    outcomes: str | None,
) -> tuple[str, str] | None:
    """Return (yes_token_id, no_token_id) by matching outcomes strings.

    Resolves by case-insensitive string match, NOT positional index.
    The Polymarket API does not guarantee index 0 = YES.
    Returns None if outcomes is missing or lacks both 'Yes' and 'No'.
    """
    if not outcomes:
        return None
    try:
        tokens: list[str] = json.loads(clob_token_ids)
        outcms: list[str] = json.loads(outcomes)
    except (json.JSONDecodeError, TypeError):
        return None

    if len(tokens) != len(outcms):
        return None

    yes_token: str | None = None
    no_token: str | None = None
    for tok, out in zip(tokens, outcms):
        if isinstance(out, str) and out.lower() == "yes":
            yes_token = tok
        elif isinstance(out, str) and out.lower() == "no":
            no_token = tok

    if yes_token is None or no_token is None:
        return None
    return (yes_token, no_token)


def _compute_direction(
    pm_ask: Decimal,
    pm_ask_size: Decimal,
    kalshi_ask: Decimal,
    kalshi_bid_size: Decimal,
    market_row: MarketRow | None,
    kalshi_fee_coeff: Decimal,
    gas_flat: Decimal,
) -> DirectionResult:
    """Compute economics for one arb direction at top-of-book size.

    pm_ask:          best ask price for the PM leg (YES or NO token price)
    pm_ask_size:     top-of-book ask size for that PM token
    kalshi_ask:      derived Kalshi ask (1 − opposing best bid)
    kalshi_bid_size: opposing Kalshi bid size (limits how much we can buy)
    market_row:      PM MarketRow for fee formula; None → 0 PM fee
    kalshi_fee_coeff: 0.07 for standard Kalshi taker fee
    gas_flat:        fixed per-PM-order gas cost, charged once regardless of size
    """
    gross_per_unit = _ONE - pm_ask - kalshi_ask
    max_units = min(pm_ask_size, kalshi_bid_size)

    if max_units <= _ZERO:
        return DirectionResult(
            gross_per_unit=gross_per_unit,
            pm_fee_total=_ZERO,
            kalshi_fee_total=_ZERO,
            net_at_size=_ZERO,
            net_per_unit_excl_gas=gross_per_unit,
            max_units=_ZERO,
            net_edge_bps=0,
        )

    gross_total = gross_per_unit * max_units

    if market_row is not None:
        pm_fee_rate = Decimal(fee_bps_from_formula(market_row, pm_ask)) / _BPS
    else:
        pm_fee_rate = _ZERO
    pm_fee_total = pm_fee_rate * pm_ask * max_units

    # Kalshi taker fee: 0.07 × P × (1 − P) per unit, where P is the purchased price
    kalshi_fee_total = kalshi_fee_coeff * kalshi_ask * (_ONE - kalshi_ask) * max_units

    net_at_size = gross_total - pm_fee_total - kalshi_fee_total - gas_flat
    net_per_unit_excl_gas = (gross_total - pm_fee_total - kalshi_fee_total) / max_units

    total_cost_at_size = (pm_ask + kalshi_ask) * max_units
    if total_cost_at_size > _ZERO:
        net_edge_bps = int(
            (net_at_size / total_cost_at_size * _BPS).to_integral_value()
        )
    else:
        net_edge_bps = 0

    return DirectionResult(
        gross_per_unit=gross_per_unit,
        pm_fee_total=pm_fee_total,
        kalshi_fee_total=kalshi_fee_total,
        net_at_size=net_at_size,
        net_per_unit_excl_gas=net_per_unit_excl_gas,
        max_units=max_units,
        net_edge_bps=net_edge_bps,
    )


def _snap_age_sec(ts: str, now: datetime) -> int:
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int((now - t).total_seconds())


def _make_skip_row(
    pair_id: int,
    scanned_at: str,
    skip_reason: str,
    gas_flat: Decimal,
    *,
    direction: str = "A",
    pm_yes_ask: Decimal | None = None,
    pm_no_ask: Decimal | None = None,
    kalshi_yes_ask: Decimal | None = None,
    kalshi_no_ask: Decimal | None = None,
    pm_snapshot_age_sec: int | None = None,
    kalshi_snapshot_age_sec: int | None = None,
) -> CrossScanRow:
    return CrossScanRow(
        pair_id=pair_id,
        scanned_at=scanned_at,
        direction=direction,
        pm_yes_ask=pm_yes_ask,
        pm_no_ask=pm_no_ask,
        kalshi_yes_ask=kalshi_yes_ask,
        kalshi_no_ask=kalshi_no_ask,
        gross_profit_per_unit=None,
        pm_fee_total=None,
        kalshi_fee_total=None,
        gas_flat=gas_flat,
        net_profit_at_size=None,
        net_profit_per_unit_excl_gas=None,
        net_edge_bps=None,
        max_profitable_units=None,
        pm_snapshot_age_sec=pm_snapshot_age_sec,
        kalshi_snapshot_age_sec=kalshi_snapshot_age_sec,
        skipped=True,
        skip_reason=skip_reason,
    )


def scan_all_pairs(
    conn: sqlite3.Connection,
    gas_usdc_per_order: Decimal,
    window_sec: int = 120,
) -> list[CrossScanRow]:
    """Scan all confirmed pairs. Returns one CrossScanRow per pair.

    Skip reasons (skipped=True):
      pm_outcomes_unresolvable — outcomes missing or lacks 'Yes'/'No'
      stale_pm                 — no PM snapshot within window_sec
      stale_kalshi             — no Kalshi snapshot within window_sec
      kalshi_single_sided      — Kalshi book has only one side
      missing_price            — best_ask is None on a fresh snapshot
      pm_market_crossed        — both directions show positive gross profit

    For non-skipped pairs, logs the better direction (higher net_at_size).
    """
    now = datetime.now(tz=timezone.utc)
    scanned_at = now.isoformat()

    pairs = store.get_confirmed_pairs_full(conn)
    if not pairs:
        return []

    market_map: dict[str, MarketRow] = {
        m.condition_id: m for m in store.get_active_markets(conn)
    }

    pm_snaps = {
        s.token_id: s
        for s in store.get_latest_snapshots_in_window(conn, window_sec=window_sec)
    }
    kal_snaps = {
        s.ticker: s
        for s in store.get_latest_kalshi_snapshots_in_window(conn, window_sec=window_sec)
    }

    results: list[CrossScanRow] = []

    for p in pairs:
        pair_id = int(p["pair_id"])
        pm_cid: str = p["pm_condition_id"]
        kal_ticker: str = p["kalshi_ticker"]
        kal_fee_coeff = Decimal(str(p["taker_fee_coeff"]))

        token_pair = _resolve_pm_tokens(p["clob_token_ids"], p["outcomes"])
        if token_pair is None:
            results.append(_make_skip_row(pair_id, scanned_at, "pm_outcomes_unresolvable", gas_usdc_per_order))
            continue
        yes_token_id, no_token_id = token_pair

        pm_snap_yes = pm_snaps.get(yes_token_id)
        pm_snap_no = pm_snaps.get(no_token_id)
        kal_snap = kal_snaps.get(kal_ticker)

        if pm_snap_yes is None or pm_snap_no is None:
            results.append(_make_skip_row(pair_id, scanned_at, "stale_pm", gas_usdc_per_order))
            continue
        if kal_snap is None:
            results.append(_make_skip_row(pair_id, scanned_at, "stale_kalshi", gas_usdc_per_order))
            continue
        if kal_snap.single_sided:
            results.append(_make_skip_row(pair_id, scanned_at, "kalshi_single_sided", gas_usdc_per_order))
            continue

        pm_yes_ask = pm_snap_yes.best_ask
        pm_no_ask = pm_snap_no.best_ask
        kal_yes_ask = kal_snap.best_yes_ask
        kal_no_ask = kal_snap.best_no_ask

        if None in (pm_yes_ask, pm_no_ask, kal_yes_ask, kal_no_ask):
            results.append(_make_skip_row(pair_id, scanned_at, "missing_price", gas_usdc_per_order))
            continue

        pm_yes_age = _snap_age_sec(pm_snap_yes.ts, now)
        kal_age = _snap_age_sec(kal_snap.ts, now)
        market = market_map.get(pm_cid)

        pm_yes_size = pm_snap_yes.asks[0].size if pm_snap_yes.asks else _ZERO
        pm_no_size = pm_snap_no.asks[0].size if pm_snap_no.asks else _ZERO
        # Direction A buys Kalshi NO; counterparty is best YES bidder
        kal_no_available = kal_snap.yes_bids[0].size if kal_snap.yes_bids else _ZERO
        # Direction B buys Kalshi YES; counterparty is best NO bidder
        kal_yes_available = kal_snap.no_bids[0].size if kal_snap.no_bids else _ZERO

        dir_a = _compute_direction(
            pm_ask=pm_yes_ask,
            pm_ask_size=pm_yes_size,
            kalshi_ask=kal_no_ask,
            kalshi_bid_size=kal_no_available,
            market_row=market,
            kalshi_fee_coeff=kal_fee_coeff,
            gas_flat=gas_usdc_per_order,
        )
        dir_b = _compute_direction(
            pm_ask=pm_no_ask,
            pm_ask_size=pm_no_size,
            kalshi_ask=kal_yes_ask,
            kalshi_bid_size=kal_yes_available,
            market_row=market,
            kalshi_fee_coeff=kal_fee_coeff,
            gas_flat=gas_usdc_per_order,
        )

        # Both directions positive → PM ask side is crossed; not a real arb
        if dir_a.gross_per_unit > _ZERO and dir_b.gross_per_unit > _ZERO:
            results.append(_make_skip_row(
                pair_id, scanned_at, "pm_market_crossed", gas_usdc_per_order,
                direction="BOTH",
                pm_yes_ask=pm_yes_ask, pm_no_ask=pm_no_ask,
                kalshi_yes_ask=kal_yes_ask, kalshi_no_ask=kal_no_ask,
                pm_snapshot_age_sec=pm_yes_age, kalshi_snapshot_age_sec=kal_age,
            ))
            continue

        if dir_a.net_at_size >= dir_b.net_at_size:
            chosen_dir, chosen = "A", dir_a
        else:
            chosen_dir, chosen = "B", dir_b

        results.append(CrossScanRow(
            pair_id=pair_id,
            scanned_at=scanned_at,
            direction=chosen_dir,
            pm_yes_ask=pm_yes_ask,
            pm_no_ask=pm_no_ask,
            kalshi_yes_ask=kal_yes_ask,
            kalshi_no_ask=kal_no_ask,
            gross_profit_per_unit=chosen.gross_per_unit,
            pm_fee_total=chosen.pm_fee_total,
            kalshi_fee_total=chosen.kalshi_fee_total,
            gas_flat=gas_usdc_per_order,
            net_profit_at_size=chosen.net_at_size,
            net_profit_per_unit_excl_gas=chosen.net_per_unit_excl_gas,
            net_edge_bps=chosen.net_edge_bps,
            max_profitable_units=chosen.max_units,
            pm_snapshot_age_sec=pm_yes_age,
            kalshi_snapshot_age_sec=kal_age,
            skipped=False,
            skip_reason=None,
        ))

    return results
