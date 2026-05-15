"""Shared pytest fixtures."""
from __future__ import annotations

import sqlite3

import pytest

from app.db import schema


@pytest.fixture
def mem_db() -> sqlite3.Connection:
    """In-memory SQLite connection with schema applied."""
    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    return conn


@pytest.fixture(scope="session")
def vcr_config() -> dict[str, object]:
    """Configure vcrpy: use cassettes dir, record new if missing."""
    return {
        "cassette_library_dir": "tests/cassettes",
        "record_mode": "none",          # fail if cassette is missing
        "decode_compressed_response": True,
        "filter_headers": ["Authorization"],  # strip auth from recordings
    }
