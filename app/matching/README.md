# app/matching — Cross-Platform Market Matcher (S4-M2)

## Purpose

Provides a CLI-driven search-and-confirm workflow for pairing Polymarket
markets with semantically equivalent Kalshi markets.  Confirmed pairs are
stored in `cross_platform_pairs` and consumed by the arb scanner (S4-M3+).

## Modules

- **search.py** — Pure tokenization and Jaccard-similarity ranking.
  No DB access.  All functions are O(N·T) with a single corpus pre-tokenization
  pass per invocation.
- **pairs.py** — DB CRUD for `cross_platform_pairs` (confirm, reject, list).

## Known Limitations

### Polymarket has no category field

The `markets` table contains no `category` column.  Bulk operations such as
"show me all unconfirmed PM Politics markets" are not supported in M2.

The workaround is keyword-based search:

```
python -m app.run match-search "senate texas 2026"
```

If category-level filtering is needed in M3+, add a `category TEXT` column
to `markets` and backfill from the Gamma API's `feeSchedule.category` field.
**Do not add this column or migration in M2** — defer to M3+ when the need
is confirmed by actual usage patterns.

## CLI Commands

```
# Find PM markets matching a keyword query
python -m app.run match-search <query> [--top N]

# Find Kalshi candidates for a PM market (or PM candidates for a Kalshi market)
python -m app.run match-candidates --pm-condition-id <id>    [--top N]
python -m app.run match-candidates --kalshi-ticker   <ticker> [--top N]

# Confirm a pair (--note is required)
python -m app.run match-confirm --pm-condition-id <id> --kalshi-ticker <ticker> --note "<text>"

# Reject a confirmed pair (stored for audit; never deleted)
python -m app.run match-reject --pair-id <id> --reason "<text>"

# List confirmed (default) or all pairs
python -m app.run match-list [--status confirmed|rejected|all]
```

## Ranking Algorithm

Titles/questions are tokenized to lowercase alphanumeric tokens with a
~30-word English stopword list applied.  Similarity is Jaccard:
`|A ∩ B| / |A ∪ B|`.  When scores are tied, end-date proximity breaks the
tie (ascending absolute day difference), with diffs > 730 days capped at
99 999 to prevent Kalshi sentinel placeholder dates from distorting results.

## Pair Lifecycle

```
(not yet in DB) → confirmed → rejected
```

Rejected pairs are retained for audit.  A rejected pair cannot be re-confirmed
(UNIQUE constraint on `(pm_condition_id, kalshi_ticker)` blocks it).  If
rehabilitation is ever needed, it requires a direct SQL UPDATE — intentionally
not exposed as a CLI command.
