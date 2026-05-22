# Polymarket × Kalshi Cross-Platform Arbitrage Scanner

A production-grade detection system for arbitrage opportunities between two prediction market platforms (Polymarket on Polygon, Kalshi on the CFTC-regulated exchange). Built over 5 days to test a strategy hypothesis publicly promoted on social media; the system found the hypothesis to be structurally infeasible at retail fee tiers.

## TL;DR Result

Across **890 non-skipped observations** of **112 manually verified market pairs** over **67 hours of continuous live operation**, the scanner found **zero opportunities exceeding 1 bp net edge** after both platforms' actual published fees, slippage from book-walked execution, and Polygon gas. The best observed pricing across the entire run was a consistent **-11 basis points below breakeven**, clustered on the same ~10 pairs throughout the window. This suggests institutional liquidity providers actively cross-price the two platforms at the fee-cap level, leaving no exploitable spread for retail participants in the categories tested.

## What This Project Demonstrates

- **Production data engineering** at non-trivial scale: continuous snapshot collection across 11,044 Polymarket markets and 176,606 Kalshi markets, with formula-based per-market fee modeling and book-walked slippage accounting.
- **Honest hypothesis testing**: built the infrastructure to falsify the strategy, ran it carefully, accepted the negative result. No P&L screenshots, no claims that don't survive scrutiny.
- **Disciplined milestone-based delivery**: 4 sequential milestones (M1, S4-M1, S4-M2, S4-M3), each gated on a diagnostic-then-design-then-implement pattern, with 303 passing tests at project completion.

## Architecture

```
app/
  api/
    gamma.py             # Polymarket Gamma client (market discovery)
    clob.py              # Polymarket CLOB v2 client (orderbook fetch)
    kalshi.py            # Kalshi public API client with circuit breaker
  db/
    schema.py            # SQLite WAL with idempotent migrations (v1 → v5)
    store.py             # Decimal-as-TEXT throughout; no floats for money
  strategies/
    negrisk_scanner.py   # cvxpy LP solver for multi-outcome NegRisk arb
    cross_platform_scanner.py  # Pair-by-pair cross-platform spread detection
  matching/
    search.py            # Jaccard token similarity over 60k+ market corpus
    pairs.py             # Pair confirmation CRUD with full audit snapshots
  fees.py                # Formula-based per-market fee modeling for both platforms
  kelly.py               # Half-Kelly position sizer with 3% portfolio cap
  signing.py             # Polymarket V2 order construction (validated, never submitted)
  run.py                 # CLI: init, discover, snapshot, scan, loop, match-*, etc.
scripts/
  start_loop.sh          # caffeinate-wrapped nohup launch with PID tracking
  stop_loop.sh           # Clean SIGTERM with timeout fallback
  backfill_scan_log.py   # Historical backfill of NegRisk scanner against past snapshots
tests/                   # 303 passing tests across all modules
```

## Key Technical Findings

### Fee Modeling Bugs Caught

1. **Polymarket's stale `/fee-rate` endpoint** returned a flat `1000 bps` for every fees-enabled market regardless of category — a V1 legacy artifact. Replaced with formula-based calculation from `feeSchedule`: `taker_rate = rate × (P × (1-P))^exponent`. Category rates: Politics 0.04, Crypto 0.07, Sports 0.03, Geopolitics 0.
2. **Anti-conservative LP fee approximation**: using constant per-leg fees instead of per-level fees underestimated cost for prices below 0.5, where walking the book moves toward p=0.5 and *raises* the P×(1-P) fee curve. Corrected to per-level fee accounting in the LP solver.
3. **Kalshi orderbook reciprocal interpretation**: Kalshi returns only YES bids and NO bids (never asks); YES ask must be derived as `1 - best_no_bid`. Built three-state validator (valid / identity-violated / single-sided) to handle edge cases without silent corruption.

### Operational Lessons

- **DNS outage caused a 17.5-hour stall** in initial discovery before a circuit breaker was added; the bot's `cmd_kalshi_discover` ground through 53k+ events at 1-second backoff per failure. Fixed with consecutive-failure circuit breaker (aborts after 5 sustained failures, defers to periodic timer).
- **PID tracking footgun**: `start_loop.sh` captured `caffeinate`'s PID via `$!`, not Python's, because `caffeinate -i python ...` exec'd Python as a child. Killing the captured PID killed caffeinate while leaving Python as an orphan. Resolved by checking process tree explicitly.
- **Snapshot coverage gap**: original snapshot strategy was "top-N by volume," which captured ~3% of confirmed-pair markets because confirmed pairs lived in lower-volume long-tail markets. Fixed with dedicated `cmd_cross_snapshot` that targets confirmed pairs explicitly, achieving 100% coverage.

### Strategy Findings

1. **Intra-Polymarket NegRisk arbitrage** (M1 strategy): null result. 0 opportunities across 1,578 (event_id, minute) buckets in 35 hours of historical data. Root cause: top-N-by-volume snapshot strategy captures only high-volume legs; the actual arb surface lives in long-tail outcomes of multi-outcome events that aren't snapshotted.
2. **Cross-platform Polymarket × Kalshi arbitrage** (S4 strategy): null result. 0 opportunities across 890 observations of 112 verified pairs over 67 hours. The persistent -11 bps ceiling across multiple pair clusters indicates active institutional cross-pricing, not random noise.

## Methodology

Watchlist of 112 confirmed pairs was manually curated using a search-and-confirm CLI (`match-search`, `match-candidates`, `match-confirm`). Every pair was verified by reading both market questions, checking end dates, and (for the FIFA batch) directly calling Kalshi's API to retrieve the resolution rules. Every confirmed pair stores full audit snapshots: question text from both sides, end dates at confirmation time, and a required `--note` explaining the equivalence claim.

Watchlist composition:
- 29 pairs: 2028 Democratic presidential nomination
- 32 pairs: 2028 Republican presidential nomination
- 5 pairs: 2026 NBA Finals winner
- 46 pairs: 2026 FIFA World Cup winner

The scanner runs every 5 snapshot cycles (~5 minutes), computing both arbitrage directions (PM YES + Kalshi NO, PM NO + Kalshi YES), applying real fees, real slippage, and real gas, and logging every result to `cross_platform_scan_log` regardless of profitability. Skipped scans (stale data, single-sided Kalshi books, unresolved PM YES/NO tokens) are logged with explicit skip reasons for data-quality tracking.

## Built With

- Python 3.11
- SQLite (WAL mode, Decimal-as-TEXT for monetary precision)
- cvxpy (LP solver for multi-leg arbitrage sizing)
- requests + custom retry/rate-limit/backoff layer
- pytest (303 tests covering API clients, fee models, scanners, matchers, and loop integration)
- Polymarket Gamma + CLOB V2 APIs, Kalshi public REST API

## What I Learned

The visible "easy" arbitrage opportunities described in retail trading content are usually exactly the strategies institutions have already arbed flat. Building the infrastructure to test the hypothesis with discipline produced a stronger result than building the bot in haste: a defensible negative finding with a structural explanation, rather than a working bot that quietly loses money to participants with better infrastructure. The engineering practice — milestone gating, diagnostic-before-design, tests-first, refusing to compute on stale or unverified data — was the substantive value of the project.
