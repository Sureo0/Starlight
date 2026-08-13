"""
Retry policy for tool execution.

Decides whether a failed tool result is worth retrying based on:
  - The tool's retryable flag (write tools are not safe to re-run)
  - The error message, matched against known transient patterns
    (network errors, timeouts, rate limits, 5xx server errors)

Pure functions + small dataclass so the logic is unit-testable.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass

logger = logging.getLogger("agent.retry")

# Patterns that indicate a transient failure worth retrying.
TRANSIENT_PATTERNS: list[str] = [
    # Network / connection
    r"connection (?:refused|reset|timed out|error)",
    r"network (?:error|unreachable|issue)",
    r"failed to fetch",
    r"timeout",
    r"timed? ?out",
    # HTTP status based
    r"\b429\b",
    r"too many requests",
    r"rate limit",
    r"\b5\d\d\b",            # 500, 502, 503, 504 ...
    r"internal server error",
    r"bad gateway",
    r"service unavailable",
    r"server error",
    # API/LLM transient
    r"upstream (?:error|timeout|failure)",
    r"temporarily (?:unavailable|overloaded)",
    r"overloaded",
    r"try again later",
    r"temporary failure",
    # Generic
    r"econnreset",
    r"econnrefused",
    r"eof",
]

_TRANSIENT_RE: re.Pattern | None = None


def _pattern() -> re.Pattern:
    global _TRANSIENT_RE
    if _TRANSIENT_RE is None:
        _TRANSIENT_RE = re.compile("|".join(f"({p})" for p in TRANSIENT_PATTERNS), re.IGNORECASE)
    return _TRANSIENT_RE


def is_transient_error(error_text: str | None) -> bool:
    """Return True if the error looks transient (worth retrying)."""
    if not error_text:
        return False
    return bool(_pattern().search(error_text))


@dataclass
class RetryConfig:
    """Retry policy configuration."""

    enabled: bool = True
    max_retries: int = 2  # total attempts = max_retries + 1
    base_delay: float = 0.5  # seconds; doubles each retry
    max_delay: float = 4.0
    jitter: float = 0.2  # +/- 20% jitter to avoid thundering herd


def compute_delay(attempt: int, cfg: RetryConfig) -> float:
    """Exponential backoff with jitter for retry attempt (0-based)."""
    delay = min(cfg.base_delay * (2 ** attempt), cfg.max_delay)
    jitter_amount = delay * cfg.jitter
    return max(0.0, delay + random.uniform(-jitter_amount, jitter_amount))


def should_retry(
    tool_retryable: bool,
    error_text: str | None,
    attempts_done: int,
    cfg: RetryConfig,
) -> bool:
    """
    Decide whether to retry a failed tool call.

    Args:
        tool_retryable: Tool.retryable flag (False for write tools).
        error_text: The tool's error message (None if success).
        attempts_done: How many attempts have already been made (0 = first).
        cfg: Retry policy.

    Returns:
        True if another attempt should be made.
    """
    if not cfg.enabled:
        return False
    if not tool_retryable:
        return False
    if attempts_done >= cfg.max_retries:
        return False
    if not is_transient_error(error_text):
        return False
    return True
