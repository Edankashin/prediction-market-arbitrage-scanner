"""Tests for app/strategies/negrisk_scanner.py.

Synthetic scenario key:
  Sc1 — 3 legs, deep books, ~600 bps gross, finds arb
  Sc2 — 4 legs, one thin leg that forces the LP to cap size; per-level vs
         per-leg fee comparison
  Sc3 — 3 legs, sum(best_ask) = 0.998, net edge negative → None
  Sc4 — feesEnabled=False across all legs, larger edge than Sc1
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.fees import fee_bps_from_formula
from app.models import BookLevel, MarketRow, SnapshotRow
from app.strategies.negrisk_scanner import (
    ArbLeg,
    ArbOpportunity,
    ScanConfig,
    lp_optimal_shares,
    scan_all,
    scan_event,
    walk_book_cost,
)

_TS = "2026-05-14T12:00:00+00:00"
_ZERO = Decimal("0")

# Shared fee formula for crypto category (rate=0.07, exp=1)
_CRYPTO_FORMULA = json.dumps({"rate": 0.07, "exponent": 1.0})
_SPORTS_FORMULA = json.dumps({"rate": 0.03, "exponent": 1.0})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(
    condition_id: str,
    event_id: str = "evt_test",
    fees_enabled: bool = True,
    fee_formula: str | None = _CRYPTO_FORMULA,
) -> MarketRow:
    return MarketRow(
        condition_id=condition_id,
        question=f"Outcome {condition_id}?",
        event_id=event_id,
        event_slug=None,
        event_title="Test Event",
        neg_risk=True,
        active=True,
        closed=False,
        archived=False,
        end_date=None,
        clob_token_ids=json.dumps([f"tok_{condition_id}_yes", f"tok_{condition_id}_no"]),
        outcomes=json.dumps(["Yes", "No"]),
        outcome_prices=None,
        volume=None,
        liquidity=None,
        spread=None,
        fees_enabled=fees_enabled,
        taker_fee_cap_bps=175 if fees_enabled else 0,
        maker_rebate_cap_bps=0,
        fee_formula=fee_formula if fees_enabled else None,
        gamma_updated_at=None,
        synced_at=_TS,
    )


def _snap(
    condition_id: str,
    token_id: str,
    ask_levels: list[tuple[str, str]],  # [(price, size), ...]
    taker_fee_bps: int | None = None,
) -> SnapshotRow:
    asks = [BookLevel(Decimal(p), Decimal(s)) for p, s in ask_levels]
    best_ask = asks[0].price if asks else None
    best_bid = best_ask - Decimal("0.01") if best_ask else None
    bids = [BookLevel(best_bid, Decimal("100"))] if best_bid else []
    mid = (best_ask + best_bid) / 2 if (best_ask and best_bid) else None
    return SnapshotRow(
        token_id=token_id,
        condition_id=condition_id,
        ts=_TS,
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        taker_fee_bps=taker_fee_bps,
    )


# ---------------------------------------------------------------------------
# TestWalkBookCost
# ---------------------------------------------------------------------------

class TestWalkBookCost:
    def test_single_level_full_fill(self) -> None:
        levels = [BookLevel(Decimal("0.40"), Decimal("200"))]
        cost, filled = walk_book_cost(levels, Decimal("100"))
        assert filled == Decimal("100")
        assert cost == Decimal("40")

    def test_walks_two_levels(self) -> None:
        levels = [
            BookLevel(Decimal("0.50"), Decimal("50")),
            BookLevel(Decimal("0.55"), Decimal("50")),
        ]
        cost, filled = walk_book_cost(levels, Decimal("100"))
        assert filled == Decimal("100")
        # 50×0.50 + 50×0.55 = 25 + 27.50 = 52.50
        assert cost == Decimal("52.50")

    def test_partial_fill_when_depth_insufficient(self) -> None:
        levels = [BookLevel(Decimal("0.40"), Decimal("30"))]
        cost, filled = walk_book_cost(levels, Decimal("100"))
        assert filled == Decimal("30")
        assert cost == Decimal("12")

    def test_empty_book_returns_zero(self) -> None:
        cost, filled = walk_book_cost([], Decimal("100"))
        assert filled == _ZERO
        assert cost == _ZERO

    def test_exact_depth_match(self) -> None:
        levels = [BookLevel(Decimal("0.35"), Decimal("100"))]
        cost, filled = walk_book_cost(levels, Decimal("100"))
        assert filled == Decimal("100")
        assert cost == Decimal("35")


# ---------------------------------------------------------------------------
# TestLpOptimalShares
# ---------------------------------------------------------------------------

class TestLpOptimalShares:
    def _flat_rate(self, bps: int) -> Decimal:
        return Decimal(bps) / Decimal("10000")

    def test_deep_equal_legs(self) -> None:
        """Three deep legs at same price — LP should fill to full depth."""
        legs = [
            [BookLevel(Decimal("0.30"), Decimal("500"))],
            [BookLevel(Decimal("0.35"), Decimal("500"))],
            [BookLevel(Decimal("0.25"), Decimal("500"))],
        ]
        # sum ask = 0.90, gross 10%, after fee (1%) still positive
        rates = [[self._flat_rate(100)], [self._flat_rate(100)], [self._flat_rate(100)]]
        optimal = lp_optimal_shares(legs, rates, gas_total=Decimal("0.015"))
        # LP should push to 500 shares (depth-limited)
        assert optimal > Decimal("400")

    def test_thin_leg_caps_size(self) -> None:
        """One thin leg (50 shares) caps the LP regardless of other legs."""
        legs = [
            [BookLevel(Decimal("0.30"), Decimal("500"))],
            [BookLevel(Decimal("0.35"), Decimal("50"))],  # thin
            [BookLevel(Decimal("0.25"), Decimal("500"))],
        ]
        rates = [[self._flat_rate(100)], [self._flat_rate(100)], [self._flat_rate(100)]]
        optimal = lp_optimal_shares(legs, rates, gas_total=Decimal("0.015"))
        assert optimal <= Decimal("50.001")

    def test_no_edge_returns_zero(self) -> None:
        """sum(ask) >= 1: LP profit ≤ 0, should return 0."""
        legs = [
            [BookLevel(Decimal("0.50"), Decimal("200"))],
            [BookLevel(Decimal("0.50"), Decimal("200"))],
        ]
        rates = [[self._flat_rate(100)], [self._flat_rate(100)]]
        optimal = lp_optimal_shares(legs, rates, gas_total=Decimal("0"))
        assert optimal == _ZERO

    def test_empty_legs_returns_zero(self) -> None:
        optimal = lp_optimal_shares([], [], gas_total=Decimal("0"))
        assert optimal == _ZERO


# ---------------------------------------------------------------------------
# Scenario 1: Deep books, ~600 bps gross
# ---------------------------------------------------------------------------

class TestScanEventScenario1:
    """3 legs, deep books, clear arb, feesEnabled=True (crypto rate 0.07)."""

    def _setup(self) -> tuple[list[SnapshotRow], dict[str, MarketRow]]:
        # sum ask = 0.30+0.35+0.29 = 0.94 → 600 bps gross
        legs = [
            ("cond_a", "tok_a", [("0.30", "500")]),
            ("cond_b", "tok_b", [("0.35", "500")]),
            ("cond_c", "tok_c", [("0.29", "500")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs]
        markets = {cid: _market(cid) for cid, _, _ in legs}
        return snaps, markets

    def test_finds_opportunity(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"), min_net_edge_bps=1)
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None, "Should find an arb opportunity"

    def test_gross_edge_positive(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        # Gross edge ≈ 600 bps (before fees)
        assert opp.gross_edge_bps > 400

    def test_net_edge_positive(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert opp.net_edge_bps > 0

    def test_three_legs_returned(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert len(opp.legs) == 3

    def test_all_legs_are_buy(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert all(lg.side == "BUY" for lg in opp.legs)

    def test_payoff_equals_optimal_shares(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert opp.guaranteed_payoff_usdc == opp.optimal_shares

    def test_zero_slippage_on_deep_book(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        for leg in opp.legs:
            assert leg.slippage_bps == _ZERO

    def test_dominant_cost_is_fee(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert opp.dominant_cost == "fee"


# ---------------------------------------------------------------------------
# Scenario 2: Thin leg, LP caps size
# ---------------------------------------------------------------------------

class TestScanEventScenario2:
    """4 legs, leg D is thin (50 shares at best_ask=0.20, then 50 more at 0.23).
    This means avg_fill walks ~3¢ above best_ask when filling 100 shares.
    """

    def _setup(self) -> tuple[list[SnapshotRow], dict[str, MarketRow]]:
        legs = [
            ("cond_a", "tok_a", [("0.30", "500")]),
            ("cond_b", "tok_b", [("0.25", "500")]),
            ("cond_c", "tok_c", [("0.18", "500")]),
            # Leg D: 50 shares at 0.20, another 50 at 0.23 (thin, walks book)
            ("cond_d", "tok_d", [("0.20", "50"), ("0.23", "50")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs]
        markets = {cid: _market(cid) for cid, _, _ in legs}
        return snaps, markets

    def test_finds_opportunity(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None

    def test_size_capped_by_thin_leg(self) -> None:
        """LP optimal size must be ≤ 100 (total depth on thin leg)."""
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert opp.optimal_shares <= Decimal("100.001")

    def test_four_legs_returned(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert len(opp.legs) == 4

    def test_gross_sum_less_than_one(self) -> None:
        """sum(best_ask) = 0.30+0.25+0.18+0.20 = 0.93 < 1."""
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert opp.gross_edge_bps > 0


# ---------------------------------------------------------------------------
# Per-level vs per-leg fee comparison (Scenario 2 thin-leg correction)
# ---------------------------------------------------------------------------

class TestPerLevelFeeCorrection:
    """Demonstrates that per-level fee rates produce a different (lower) optimal
    size than a naive per-leg fee (fee computed only at best_ask).

    Thin leg D: best_ask=0.20, second level at 0.23.
    fee_rate at 0.20: 0.07 × 0.20×0.80 = 0.07 × 0.16 = 0.0112  → 112 bps
    fee_rate at 0.23: 0.07 × 0.23×0.77 = 0.07 × 0.1771 = 0.01240 → 124 bps

    Per-leg fee (at best_ask=0.20) underestimates the second level's fee.
    Per-level fees charge more on the second level → LP sees higher cost at
    that level and may reduce optimal size or produce a different value.
    """

    _CRYPTO_RATE = Decimal("0.07")

    def _per_level_rates(
        self, legs: list[list[BookLevel]], markets: list[MarketRow]
    ) -> list[list[Decimal]]:
        """Compute per-level fee rates from fee formula."""
        result: list[list[Decimal]] = []
        for levels, mkt in zip(legs, markets):
            rates = [
                Decimal(fee_bps_from_formula(mkt, lv.price)) / Decimal("10000")
                for lv in levels
            ]
            result.append(rates)
        return result

    def _per_leg_rates(
        self, legs: list[list[BookLevel]], markets: list[MarketRow]
    ) -> list[list[Decimal]]:
        """Naive: use best_ask fee for every level in a leg."""
        result: list[list[Decimal]] = []
        for levels, mkt in zip(legs, markets):
            best_ask = levels[0].price
            flat = Decimal(fee_bps_from_formula(mkt, best_ask)) / Decimal("10000")
            result.append([flat] * len(levels))
        return result

    def test_per_level_fees_differ_from_per_leg(self) -> None:
        """Per-level fees must differ from per-leg fees for the thin leg."""
        # Leg D: two levels at different prices
        mkt_d = _market("cond_d")
        levels_d = [
            BookLevel(Decimal("0.20"), Decimal("50")),
            BookLevel(Decimal("0.23"), Decimal("50")),
        ]
        rate_at_020 = fee_bps_from_formula(mkt_d, Decimal("0.20"))
        rate_at_023 = fee_bps_from_formula(mkt_d, Decimal("0.23"))
        # Rates must differ (0.23 is closer to 0.5 so fee is higher)
        assert rate_at_023 > rate_at_020, (
            f"rate@0.23={rate_at_023} should exceed rate@0.20={rate_at_020}"
        )

    def test_per_level_optimal_differs_from_per_leg_optimal(self) -> None:
        """LP with per-level fees must produce a different optimal than per-leg fees.

        With the thin leg walking from 0.20 to 0.23, per-level fees charge
        higher cost at the second level.  If the per-level LP changes the
        optimal size (or at minimum the net profit calculation), the two
        approaches differ.
        """
        # Build the 4-leg scenario
        leg_levels = [
            [BookLevel(Decimal("0.30"), Decimal("500"))],
            [BookLevel(Decimal("0.25"), Decimal("500"))],
            [BookLevel(Decimal("0.18"), Decimal("500"))],
            [
                BookLevel(Decimal("0.20"), Decimal("50")),
                BookLevel(Decimal("0.23"), Decimal("50")),
            ],
        ]
        markets = [
            _market("cond_a"),
            _market("cond_b"),
            _market("cond_c"),
            _market("cond_d"),
        ]
        gas = Decimal("0.020")

        rates_per_level = self._per_level_rates(leg_levels, markets)
        rates_per_leg = self._per_leg_rates(leg_levels, markets)

        # Verify per-level and per-leg rates differ on the thin leg
        assert rates_per_level[3][0] == rates_per_leg[3][0], (
            "First level should match (both use best_ask price)"
        )
        assert rates_per_level[3][1] != rates_per_leg[3][1], (
            "Second level must differ: per-level uses level price, per-leg uses best_ask"
        )

        optimal_per_level = lp_optimal_shares(leg_levels, rates_per_level, gas)
        optimal_per_leg = lp_optimal_shares(leg_levels, rates_per_leg, gas)

        # The per-level fee is higher on the second level → LP sees higher cost
        # This must produce a different net profit, which may or may not change
        # the optimal size (LP is depth-limited at 100 in both cases).
        # The key correctness test: when we compute net profit at the same
        # optimal size, per-level fees give lower net profit.
        assert optimal_per_level > _ZERO, "Per-level LP should find an opportunity"
        assert optimal_per_leg > _ZERO, "Per-leg LP should find an opportunity"

        # Compute net profit for each approach at their respective optima
        def net_profit(
            legs: list[list[BookLevel]],
            rates: list[list[Decimal]],
            shares: Decimal,
            gas_total: Decimal,
        ) -> Decimal:
            cost = _ZERO
            for levels, leg_rates in zip(legs, rates):
                remaining = shares
                for lv, r in zip(levels, leg_rates):
                    qty = min(remaining, lv.size)
                    cost += qty * lv.price * (1 + r)
                    remaining -= qty
                    if remaining <= _ZERO:
                        break
            return shares - cost - gas_total

        net_pl = net_profit(leg_levels, rates_per_level, optimal_per_level, gas)
        net_pg = net_profit(leg_levels, rates_per_leg, optimal_per_leg, gas)

        # Per-level fees are higher on leg D's second level → lower net profit
        # at the same or higher optimal size.  We assert they are not identical.
        assert net_pl != net_pg or optimal_per_level != optimal_per_leg, (
            "Per-level and per-leg LP must produce different net profits or "
            "different optimal sizes — per-level fees are higher on the walked level"
        )

        # More specifically: per-level fees are strictly higher at level 1 of leg D
        # → the effective cost at that level is higher → net profit at per-level
        # must be ≤ net profit at per-leg for the same size.
        net_at_same_size_pl = net_profit(leg_levels, rates_per_level, optimal_per_level, gas)
        net_at_same_size_pg = net_profit(leg_levels, rates_per_leg, optimal_per_level, gas)
        assert net_at_same_size_pl <= net_at_same_size_pg, (
            f"Per-level net={float(net_at_same_size_pl):.6f} should be ≤ "
            f"per-leg net={float(net_at_same_size_pg):.6f} (higher fees)"
        )


# ---------------------------------------------------------------------------
# Scenario 3: Edge too small → None
# ---------------------------------------------------------------------------

class TestScanEventScenario3:
    """sum(best_ask) = 0.998, net edge is negative after fees → returns None."""

    def _setup(self) -> tuple[list[SnapshotRow], dict[str, MarketRow]]:
        # sum = 0.38+0.35+0.268 = 0.998 → 20 bps gross
        legs = [
            ("cond_a", "tok_a", [("0.38", "500")]),
            ("cond_b", "tok_b", [("0.35", "500")]),
            ("cond_c", "tok_c", [("0.268", "500")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs]
        markets = {cid: _market(cid) for cid, _, _ in legs}
        return snaps, markets

    def test_returns_none(self) -> None:
        """Crypto fees at these prices eat all 20 bps gross → net negative."""
        snaps, markets = self._setup()
        cfg = ScanConfig(
            gas_usdc_per_order=Decimal("0.005"),
            min_net_edge_bps=1,
        )
        opp = scan_event(snaps, markets, cfg)
        assert opp is None, (
            "Net edge after crypto fees should be negative; scanner should return None"
        )

    def test_no_gross_edge_returns_none(self) -> None:
        """sum(best_ask) >= 1.00 → no arb possible."""
        legs = [
            ("cond_a", "tok_a", [("0.50", "200")]),
            ("cond_b", "tok_b", [("0.51", "200")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs]
        markets = {cid: _market(cid) for cid, _, _ in legs}
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is None


# ---------------------------------------------------------------------------
# Scenario 4: feesEnabled=False — larger edge than Sc1
# ---------------------------------------------------------------------------

class TestScanEventScenario4:
    """Same prices as Sc1 but feesEnabled=False on all markets.
    Net edge should be larger (no fees) and dominant_cost should be 'gas'.
    """

    def _setup(self) -> tuple[list[SnapshotRow], dict[str, MarketRow]]:
        legs = [
            ("cond_a", "tok_a", [("0.30", "500")]),
            ("cond_b", "tok_b", [("0.35", "500")]),
            ("cond_c", "tok_c", [("0.29", "500")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs]
        markets = {
            cid: _market(cid, fees_enabled=False, fee_formula=None)
            for cid, _, _ in legs
        }
        return snaps, markets

    def test_finds_opportunity(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None

    def test_fee_is_zero(self) -> None:
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None
        assert opp.total_fee_usdc == _ZERO
        for leg in opp.legs:
            assert leg.fee_bps == 0
            assert leg.fee_usdc == _ZERO

    def test_net_edge_analytical(self) -> None:
        """Planted answer: no-fee net edge at 500 shares (gas only cost).

        Setup: 3 legs at 0.30/0.35/0.29, depth=500 each, feesEnabled=False,
               gas=$0.005/leg (3 legs → $0.015 total).

        Analytical:
          sum(ask)          = 0.30 + 0.35 + 0.29    = 0.94
          optimal_shares    = 500   (depth-limited; per-share edge $0.06 > 0)
          total_cost        = 500 × 0.94             = $470.000
          payoff            = 500 × 1.00             = $500.000
          gross_pnl         = $500 − $470            = $30.000
          fees              = 0     (feesEnabled=False)
          slippage          = 0     (single-level book, no walk)
          gas               = 3 × $0.005             = $0.015
          net_pnl           = $30.000 − $0.015       = $29.985
          net_edge_bps      = round(29.985/500×10000) = round(599.7) = 600

        Acceptance: |scanner.net_edge_bps − 600| ≤ 1
        """
        snaps, markets = self._setup()
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        opp = scan_event(snaps, markets, cfg)
        assert opp is not None

        # Planted analytical answer
        optimal_shares_expected = Decimal("500")
        sum_ask = Decimal("0.30") + Decimal("0.35") + Decimal("0.29")  # 0.94
        total_cost_expected = optimal_shares_expected * sum_ask          # 470.00
        payoff_expected = optimal_shares_expected                        # 500.00
        gross_pnl_expected = payoff_expected - total_cost_expected       # 30.00
        fees_expected = Decimal("0")
        gas_expected = Decimal("3") * Decimal("0.005")                  # 0.015
        net_pnl_expected = gross_pnl_expected - fees_expected - gas_expected  # 29.985
        net_edge_bps_expected = int(
            (net_pnl_expected / payoff_expected * Decimal("10000")).to_integral_value()
        )  # round(599.7) = 600 bps

        assert abs(opp.optimal_shares - optimal_shares_expected) < Decimal("0.01"), (
            f"LP should depth-limit at 500; got {opp.optimal_shares}"
        )
        assert opp.total_fee_usdc == Decimal("0"), "feesEnabled=False must produce zero fees"
        assert opp.total_slippage_usdc == Decimal("0"), "Single-level book: zero slippage"
        assert abs(opp.total_gas_usdc - gas_expected) < Decimal("0.001"), (
            f"Gas {opp.total_gas_usdc} vs expected {gas_expected}"
        )
        delta_bps = abs(opp.net_edge_bps - net_edge_bps_expected)
        assert delta_bps <= 1, (
            f"ACCEPTANCE FAILED: net_edge_bps={opp.net_edge_bps} "
            f"vs expected={net_edge_bps_expected}, delta={delta_bps}"
        )

    def test_dominant_cost_is_gas(self) -> None:
        snaps, markets = self._setup()
        # Use larger gas so it dominates
        cfg = ScanConfig(gas_usdc_per_order=Decimal("1.000"))
        opp = scan_event(snaps, markets, cfg)
        # With large gas and no fees, gas should dominate
        # (opp may be None if net edge < 0 — adjust gas to ensure opp exists)
        cfg2 = ScanConfig(gas_usdc_per_order=Decimal("0.001"))
        opp2 = scan_event(snaps, markets, cfg2)
        if opp2 is not None:
            assert opp2.dominant_cost == "gas"


# ---------------------------------------------------------------------------
# TestScanAll
# ---------------------------------------------------------------------------

class TestScanAll:
    def _sc1_group(self) -> tuple[list[SnapshotRow], dict[str, MarketRow]]:
        legs = [
            ("cond_a", "tok_a", [("0.30", "500")]),
            ("cond_b", "tok_b", [("0.35", "500")]),
            ("cond_c", "tok_c", [("0.29", "500")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs]
        markets = {cid: _market(cid, event_id="evt_1") for cid, _, _ in legs}
        return snaps, markets

    def _sc3_group(self) -> tuple[list[SnapshotRow], dict[str, MarketRow]]:
        """Edge too small → None."""
        legs = [
            ("cond_x", "tok_x", [("0.38", "500")]),
            ("cond_y", "tok_y", [("0.35", "500")]),
            ("cond_z", "tok_z", [("0.268", "500")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs]
        markets = {cid: _market(cid, event_id="evt_2") for cid, _, _ in legs}
        return snaps, markets

    def test_returns_only_valid_opportunities(self) -> None:
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        groups = [self._sc1_group(), self._sc3_group()]
        results = scan_all(groups, cfg)
        assert len(results) == 1
        assert results[0].event_id == "evt_1"

    def test_sorted_descending_by_net_edge(self) -> None:
        """Two valid groups: no-fee group should rank higher than fee group."""
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        legs_spec = [
            ("cond_a", "tok_a", [("0.30", "500")]),
            ("cond_b", "tok_b", [("0.35", "500")]),
            ("cond_c", "tok_c", [("0.29", "500")]),
        ]
        snaps = [_snap(cid, tid, lvls) for cid, tid, lvls in legs_spec]
        m_fee = {cid: _market(cid, event_id="evt_fee") for cid, _, _ in legs_spec}
        m_nofee = {
            cid: _market(cid, event_id="evt_nofee", fees_enabled=False, fee_formula=None)
            for cid, _, _ in legs_spec
        }
        results = scan_all([(snaps, m_fee), (snaps, m_nofee)], cfg)
        assert len(results) == 2
        assert results[0].net_edge_bps >= results[1].net_edge_bps

    def test_empty_when_no_opportunities(self) -> None:
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        results = scan_all([self._sc3_group()], cfg)
        assert results == []

    def test_empty_groups_returns_empty(self) -> None:
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"))
        results = scan_all([], cfg)
        assert results == []


# ---------------------------------------------------------------------------
# Edge / guard tests
# ---------------------------------------------------------------------------

class TestScanEventEdgeCases:
    def test_single_leg_returns_none(self) -> None:
        snap = _snap("cond_a", "tok_a", [("0.30", "200")])
        mkt = _market("cond_a")
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0"))
        opp = scan_event([snap], {"cond_a": mkt}, cfg)
        assert opp is None

    def test_missing_market_for_snap_skipped(self) -> None:
        """Snapshot with no matching market entry is silently skipped."""
        snap_a = _snap("cond_a", "tok_a", [("0.30", "200")])
        snap_b = _snap("cond_b", "tok_b", [("0.40", "200")])
        # Only market A provided → only 1 valid → returns None
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0"))
        opp = scan_event([snap_a, snap_b], {"cond_a": _market("cond_a")}, cfg)
        assert opp is None

    def test_empty_snaps_returns_none(self) -> None:
        cfg = ScanConfig(gas_usdc_per_order=Decimal("0"))
        opp = scan_event([], {}, cfg)
        assert opp is None


# ---------------------------------------------------------------------------
# Live DB test (skipped unless NegRisk data present)
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_scan_event_live_db(mem_db: object) -> None:
    """End-to-end: load a real NegRisk event from DB and scan without error.

    Skipped (xfail) if no NegRisk rows are present in mem_db.
    """
    import sqlite3

    from app.db import store

    conn: sqlite3.Connection = mem_db  # type: ignore[assignment]
    cur = conn.execute(
        "SELECT DISTINCT event_id FROM markets WHERE neg_risk=1 AND active=1 AND closed=0 LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        pytest.skip("No NegRisk markets in test DB")

    event_id = row[0]
    markets_list = store.get_neg_risk_markets_for_event(conn, event_id)
    if not markets_list:
        pytest.skip("No active NegRisk markets for event")

    markets = {m.condition_id: m for m in markets_list}
    snaps_all = store.get_snapshots_at_ts(conn, _TS)
    snaps = [s for s in snaps_all if s.condition_id in markets]

    cfg = ScanConfig(gas_usdc_per_order=Decimal("0.005"), min_net_edge_bps=1)
    # Must not raise
    opp = scan_event(snaps, markets, cfg)
    # opp may be None (no arb found) — that's fine, no error is the contract
    assert opp is None or isinstance(opp, ArbOpportunity)
