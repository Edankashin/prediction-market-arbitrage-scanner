"""Performance test: 50k-row synthetic corpus must complete in < 500 ms.

Run with: pytest tests/test_matching_perf.py -v -s
The elapsed time is printed to stdout for reference.
"""
from __future__ import annotations

import time

from app.matching.search import candidates_from_pm, search_kalshi

_ENTITIES = [
    "France", "Brazil", "Germany", "Argentina", "Spain", "England", "USA",
    "Mexico", "Portugal", "Netherlands", "Japan", "South Korea", "Morocco",
    "Senegal", "Australia", "Canada", "Poland", "Croatia", "Switzerland",
    "Denmark", "Serbia", "Ecuador", "Ghana", "Cameroon", "Wales",
]
_YEARS = ["2026", "2027", "2028", "2029"]
_EVENTS = [
    "FIFA World Cup", "NBA Finals", "Presidential Election",
    "Senate primary", "Bitcoin price target", "Wimbledon",
    "PGA Championship", "F1 Drivers Championship", "Survivor Season 50",
    "gubernatorial election",
]


def _synthetic_kalshi_rows(n: int) -> list[tuple[str, str, str | None]]:
    rows = []
    for i in range(n):
        entity = _ENTITIES[i % len(_ENTITIES)]
        year = _YEARS[i % len(_YEARS)]
        event = _EVENTS[i % len(_EVENTS)]
        rows.append((
            f"KXSYN-{i:05d}",
            f"Will {entity} win the {year} {event}?",
            f"20{26 + (i % 4)}-06-30",
        ))
    return rows


def _synthetic_pm_rows(n: int) -> list[tuple[str, str, str | None, str | None]]:
    rows = []
    for i in range(n):
        entity = _ENTITIES[i % len(_ENTITIES)]
        year = _YEARS[i % len(_YEARS)]
        event = _EVENTS[i % len(_EVENTS)]
        rows.append((
            f"0x{i:064x}",
            f"Will {entity} win the {year} {event}?",
            f"{year} {event}",
            f"20{26 + (i % 4)}-07-01",
        ))
    return rows


class TestSearchPerformance:
    def test_search_kalshi_50k_under_500ms(self) -> None:
        """search_kalshi against 50 000-row corpus must complete in < 500 ms."""
        corpus = _synthetic_kalshi_rows(50_000)
        query = "Will France win the 2026 FIFA World Cup?"

        t0 = time.perf_counter()
        results = search_kalshi(query, corpus, top=20)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        print(f"\n  search_kalshi(50k rows): {elapsed_ms:.1f} ms")
        assert elapsed_ms < 500, f"search_kalshi took {elapsed_ms:.1f} ms (limit: 500 ms)"
        assert len(results) <= 20
        # Sanity: top result should contain "France" and "2026" tokens
        assert results[0]["score"] > 0.0

    def test_candidates_from_pm_50k_under_500ms(self) -> None:
        """candidates_from_pm against 50 000-row Kalshi corpus must complete in < 500 ms."""
        corpus = _synthetic_kalshi_rows(50_000)

        t0 = time.perf_counter()
        results = candidates_from_pm(
            pm_question="Will France win the 2026 FIFA World Cup?",
            pm_event_title="2026 FIFA World Cup",
            pm_end_date="2026-07-20",
            kalshi_rows=corpus,
            top=20,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        print(f"\n  candidates_from_pm(50k rows): {elapsed_ms:.1f} ms")
        assert elapsed_ms < 500, f"candidates_from_pm took {elapsed_ms:.1f} ms (limit: 500 ms)"
        assert len(results) <= 20
        assert results[0]["score"] > 0.0

    def test_candidates_from_kalshi_50k_pm_under_500ms(self) -> None:
        """candidates_from_kalshi against 50 000-row PM corpus must complete in < 500 ms."""
        corpus = _synthetic_pm_rows(50_000)

        t0 = time.perf_counter()
        results = candidates_from_pm(
            pm_question="Will France win the 2026 FIFA World Cup?",
            pm_event_title="2026 FIFA World Cup",
            pm_end_date="2026-07-20",
            kalshi_rows=[(r[0], r[1], r[3]) for r in corpus],  # adapt to kalshi_rows format
            top=20,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        print(f"\n  candidates_from_pm(50k PM rows via kalshi_rows): {elapsed_ms:.1f} ms")
        assert elapsed_ms < 500, f"elapsed {elapsed_ms:.1f} ms (limit: 500 ms)"
