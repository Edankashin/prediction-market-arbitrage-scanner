"""Shared utilities: structured JSON logging, HTTP retry, rate limiting.

Nothing in here imports from other app modules.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import ParamSpec, TypeVar

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

P = ParamSpec("P")
R = TypeVar("R")


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, datefmt=None),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Any extra keys attached via logging.getLogger().info("msg", extra={...})
        for key, val in record.__dict__.items():
            if key not in (
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "id", "levelname", "levelno",
                "lineno", "message", "module", "msecs", "msg", "name",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName", "taskName",
            ):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure root logger for structured JSON output to stdout and file."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    formatter = _JsonFormatter()

    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(formatter)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "bot.log"),
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(stdout_handler)
    root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# HTTP session factory with transport-level retry
# ---------------------------------------------------------------------------

def make_session(
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> requests.Session:
    """Return a requests.Session with urllib3-level retry on transient errors."""
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=list(status_forcelist),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# Application-level retry decorator
# ---------------------------------------------------------------------------

def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable: tuple[type[Exception], ...] = (requests.RequestException, OSError),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: retry with exponential backoff on retryable exceptions.

    Distinct from urllib3 retry: this handles application-level failures
    (e.g., unexpected HTTP 4xx that the transport layer won't retry).
    """
    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        log = logging.getLogger(fn.__module__)

        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        log.warning(
                            "retry_attempt",
                            extra={
                                "fn": fn.__qualname__,
                                "attempt": attempt + 1,
                                "delay_s": delay,
                                "error": str(exc),
                            },
                        )
                        time.sleep(delay)
            # last_exc is always set if we reach here (max_attempts >= 1)
            raise last_exc  # type: ignore[misc]

        # Preserve the original function's signature for mypy
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Simple token-bucket rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Minimum inter-call gap enforcer. Not thread-safe (single-thread loop)."""

    def __init__(self, calls_per_sec: float) -> None:
        self._interval = 1.0 / calls_per_sec
        self._last_call: float = 0.0

    def wait(self) -> None:
        """Sleep until at least interval seconds have elapsed since last call."""
        now = time.monotonic()
        gap = self._interval - (now - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.monotonic()
