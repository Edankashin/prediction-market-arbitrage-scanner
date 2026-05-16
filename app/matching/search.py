"""Token-overlap ranking for cross-platform market matching.

All functions are pure (no DB access, no side effects). The caller pre-loads
rows from the DB and passes them as lists of tuples; this module only
tokenizes and scores.

Algorithmic complexity
----------------------
tokenize(text)                  O(W)  where W = words in text
jaccard(a, b)                   O(min(|a|, |b|))
search_pm / search_kalshi       O(N * T)
    N = number of candidate rows, T = average tokens per text.
    The corpus is pre-tokenized in a single pass at function entry; each
    subsequent Jaccard call operates on already-computed frozensets.
    For N=57 000, T≈8, elapsed is well under 500 ms in CPython 3.11.
candidates_from_pm / _from_kalshi   O(N * T)  — same pre-tokenization pattern.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "in", "of", "on", "be", "to", "by", "for",
    "before", "after", "above", "below", "is", "are", "was", "were", "at",
    "win", "wins", "winning", "won", "who", "what", "when", "than",
    "their", "its", "and", "or", "not", "no", "yes", "do", "does", "did",
    "s", "t",
})

# Diffs beyond this threshold are capped to push Kalshi sentinel dates
# (e.g. 2028-06-29 on current 2026 sports markets) to the end of tied groups.
_SENTINEL_DAYS = 99_999
_SENTINEL_THRESHOLD = 730


def tokenize(text: str) -> frozenset[str]:
    """Lowercase, strip non-alphanumeric, remove stopwords and 1-char tokens."""
    return frozenset(
        w
        for w in re.sub(r"[^a-z0-9\s]", " ", text.lower()).split()
        if w not in _STOPWORDS and len(w) > 1
    )


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity |a ∩ b| / |a ∪ b|. Returns 0.0 for empty inputs."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _date_diff_days(d1: str | None, d2: str | None) -> int:
    """Absolute day difference between two ISO-8601 date strings.

    Returns _SENTINEL_DAYS if either date is missing, unparseable, or if the
    difference exceeds _SENTINEL_THRESHOLD days.  The threshold guards against
    Kalshi placeholder dates (e.g. 2028-06-29 on current-season sports
    markets) distorting the tiebreaker ranking.
    """
    if not d1 or not d2:
        return _SENTINEL_DAYS
    try:
        p1 = datetime.fromisoformat(d1[:10]).date()
        p2 = datetime.fromisoformat(d2[:10]).date()
        diff = abs((p1 - p2).days)
        return diff if diff <= _SENTINEL_THRESHOLD else _SENTINEL_DAYS
    except (ValueError, TypeError):
        return _SENTINEL_DAYS


def search_pm(
    query: str,
    pm_rows: list[tuple[str, str, str | None, str | None]],
    top: int = 20,
) -> list[dict[str, Any]]:
    """Rank PM markets by token overlap with *query*.

    Pre-tokenizes all rows in a single pass (O(N*T)), then scores each.

    Args:
        query: Free-text search string.
        pm_rows: List of (condition_id, question, event_title, end_date).
        top: Maximum results to return.

    Returns:
        Dicts with keys: condition_id, question, event_title, end_date, score.
        Sorted by score DESC; zero-score rows excluded.
    """
    query_toks = tokenize(query)
    if not query_toks:
        return []

    # Single tokenization pass over corpus
    tokenized = [
        (cid, q, et, ed, tokenize((q or "") + " " + (et or "")))
        for cid, q, et, ed in pm_rows
    ]

    scored: list[dict[str, Any]] = []
    for cid, q, et, ed, toks in tokenized:
        s = jaccard(query_toks, toks)
        if s > 0.0:
            scored.append(
                {"condition_id": cid, "question": q, "event_title": et,
                 "end_date": ed, "score": s}
            )

    scored.sort(key=lambda x: -x["score"])
    return scored[:top]


def search_kalshi(
    query: str,
    kalshi_rows: list[tuple[str, str, str | None]],
    top: int = 20,
) -> list[dict[str, Any]]:
    """Rank Kalshi markets by token overlap with *query*.

    Pre-tokenizes all rows in a single pass (O(N*T)), then scores each.

    Args:
        query: Free-text search string.
        kalshi_rows: List of (ticker, title, end_date).
        top: Maximum results to return.

    Returns:
        Dicts with keys: ticker, title, end_date, score.
        Sorted by score DESC; zero-score rows excluded.
    """
    query_toks = tokenize(query)
    if not query_toks:
        return []

    tokenized = [
        (ticker, title, ed, tokenize(title or ""))
        for ticker, title, ed in kalshi_rows
    ]

    scored: list[dict[str, Any]] = []
    for ticker, title, ed, toks in tokenized:
        s = jaccard(query_toks, toks)
        if s > 0.0:
            scored.append({"ticker": ticker, "title": title, "end_date": ed, "score": s})

    scored.sort(key=lambda x: -x["score"])
    return scored[:top]


def candidates_from_pm(
    pm_question: str,
    pm_event_title: str | None,
    pm_end_date: str | None,
    kalshi_rows: list[tuple[str, str, str | None]],
    top: int = 20,
) -> list[dict[str, Any]]:
    """Find Kalshi candidates for a given PM market.

    Pre-tokenizes all Kalshi rows in a single pass (O(N*T)), then scores each.

    Primary sort:   Jaccard similarity DESC.
    Tiebreaker:     abs(days_diff) ASC, capped at _SENTINEL_DAYS for diffs
                    > _SENTINEL_THRESHOLD days (guards against Kalshi
                    placeholder dates pushing real matches down the list).

    Args:
        pm_question: PM market question text.
        pm_event_title: PM event title (combined with question for tokenization).
        pm_end_date: PM market end date (ISO 8601).
        kalshi_rows: List of (ticker, title, end_date).
        top: Maximum results to return.

    Returns:
        Dicts with keys: ticker, title, end_date, score, days_diff.
        Sorted by (score DESC, days_diff ASC); zero-score rows excluded.
    """
    source_toks = tokenize((pm_question or "") + " " + (pm_event_title or ""))
    if not source_toks:
        return []

    tokenized = [
        (ticker, title, ed, tokenize(title or ""))
        for ticker, title, ed in kalshi_rows
    ]

    scored: list[dict[str, Any]] = []
    for ticker, title, ed, toks in tokenized:
        s = jaccard(source_toks, toks)
        if s > 0.0:
            scored.append(
                {"ticker": ticker, "title": title, "end_date": ed,
                 "score": s, "days_diff": _date_diff_days(pm_end_date, ed)}
            )

    scored.sort(key=lambda x: (-x["score"], x["days_diff"]))
    return scored[:top]


def candidates_from_kalshi(
    kalshi_title: str,
    kalshi_end_date: str | None,
    pm_rows: list[tuple[str, str, str | None, str | None]],
    top: int = 20,
) -> list[dict[str, Any]]:
    """Find PM candidates for a given Kalshi market.

    Pre-tokenizes all PM rows in a single pass (O(N*T)), then scores each.

    Primary sort:   Jaccard similarity DESC.
    Tiebreaker:     abs(days_diff) ASC, capped at _SENTINEL_DAYS for diffs
                    > _SENTINEL_THRESHOLD days.

    Args:
        kalshi_title: Kalshi market title.
        kalshi_end_date: Kalshi market end date (ISO 8601).
        pm_rows: List of (condition_id, question, event_title, end_date).
        top: Maximum results to return.

    Returns:
        Dicts with keys: condition_id, question, event_title, end_date,
        score, days_diff. Sorted by (score DESC, days_diff ASC);
        zero-score rows excluded.
    """
    source_toks = tokenize(kalshi_title or "")
    if not source_toks:
        return []

    tokenized = [
        (cid, q, et, ed, tokenize((q or "") + " " + (et or "")))
        for cid, q, et, ed in pm_rows
    ]

    scored: list[dict[str, Any]] = []
    for cid, q, et, ed, toks in tokenized:
        s = jaccard(source_toks, toks)
        if s > 0.0:
            scored.append(
                {"condition_id": cid, "question": q, "event_title": et,
                 "end_date": ed, "score": s,
                 "days_diff": _date_diff_days(kalshi_end_date, ed)}
            )

    scored.sort(key=lambda x: (-x["score"], x["days_diff"]))
    return scored[:top]
