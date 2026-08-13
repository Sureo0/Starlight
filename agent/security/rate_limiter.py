"""
RateLimiter - Tool call rate limiting.

Provides per-user and global rate limiting for tool calls to prevent:
  - Abuse / excessive API usage
  - Cost runaway (LLM token consumption)
  - Denial of service
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger("agent.security.rate_limiter")


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""

    # Per-user limits
    user_max_calls_per_minute: int = 30  # Max tool calls per user per minute
    user_max_calls_per_hour: int = 200  # Max tool calls per user per hour
    user_max_iterations: int = 10  # Max agent loop iterations per request

    # Global limits
    global_max_calls_per_minute: int = 100  # Max total tool calls per minute
    global_max_concurrent: int = 5  # Max concurrent agent executions

    # Cost limits
    user_max_tokens_per_hour: int = 500_000  # Max LLM tokens per user per hour


class RateLimiter:
    """
    Thread-safe rate limiter using sliding window counters.

    Tracks tool calls per user and globally, enforcing limits
    to prevent abuse.
    """

    def __init__(self, config: RateLimitConfig | None = None):
        self.config = config or RateLimitConfig()
        self._lock = threading.Lock()

        # Per-user counters: {username: [(timestamp, count), ...]}
        self._user_calls_minute: dict[str, list[float]] = defaultdict(list)
        self._user_calls_hour: dict[str, list[float]] = defaultdict(list)
        self._user_tokens_hour: dict[str, list[float]] = defaultdict(list)

        # Global counters
        self._global_calls_minute: list[float] = []

        # Concurrent executions
        self._concurrent_count = 0

    def check_tool_call(self, username: str) -> tuple[bool, str]:
        """
        Check if a user is allowed to make a tool call.

        Returns:
            (allowed, reason)
        """
        now = time.time()

        with self._lock:
            # Clean old entries
            self._cleanup(now)

            # Check global limit
            if len(self._global_calls_minute) >= self.config.global_max_calls_per_minute:
                return False, "Global rate limit exceeded (too many calls per minute)"

            # Check user per-minute limit
            user_minute = self._user_calls_minute.get(username, [])
            if len(user_minute) >= self.config.user_max_calls_per_minute:
                return False, f"Rate limit exceeded: {self.config.user_max_calls_per_minute} calls/minute"

            # Check user per-hour limit
            user_hour = self._user_calls_hour.get(username, [])
            if len(user_hour) >= self.config.user_max_calls_per_hour:
                return False, f"Rate limit exceeded: {self.config.user_max_calls_per_hour} calls/hour"

            return True, "OK"

    def record_tool_call(self, username: str, tokens_used: int = 0):
        """Record a tool call for rate limiting."""
        now = time.time()

        with self._lock:
            self._user_calls_minute[username].append(now)
            self._user_calls_hour[username].append(now)
            self._global_calls_minute.append(now)

            if tokens_used > 0:
                self._user_tokens_hour[username].append(now)
                # Track token count in a parallel list
                # For simplicity, we count calls, not exact tokens
                # A more precise implementation would store (timestamp, tokens)


    def acquire_concurrent(self) -> tuple[bool, str]:
        """Try to acquire a concurrent execution slot."""
        with self._lock:
            if self._concurrent_count >= self.config.global_max_concurrent:
                return False, f"Too many concurrent executions (max: {self.config.global_max_concurrent})"
            self._concurrent_count += 1
            return True, "OK"

    def release_concurrent(self):
        """Release a concurrent execution slot."""
        with self._lock:
            self._concurrent_count = max(0, self._concurrent_count - 1)

    def get_stats(self, username: str | None = None) -> dict:
        """Get rate limiting statistics."""
        now = time.time()
        with self._lock:
            self._cleanup(now)

            stats = {
                "concurrent": self._concurrent_count,
                "global_calls_minute": len(self._global_calls_minute),
            }

            if username:
                stats["user_calls_minute"] = len(self._user_calls_minute.get(username, []))
                stats["user_calls_hour"] = len(self._user_calls_hour.get(username, []))

            return stats

    def _cleanup(self, now: float):
        """Remove expired entries from counters."""
        minute_ago = now - 60
        hour_ago = now - 3600

        # Clean per-user counters
        for username in list(self._user_calls_minute.keys()):
            self._user_calls_minute[username] = [
                t for t in self._user_calls_minute[username] if t > minute_ago
            ]
            if not self._user_calls_minute[username]:
                del self._user_calls_minute[username]

        for username in list(self._user_calls_hour.keys()):
            self._user_calls_hour[username] = [
                t for t in self._user_calls_hour[username] if t > hour_ago
            ]
            if not self._user_calls_hour[username]:
                del self._user_calls_hour[username]

        for username in list(self._user_tokens_hour.keys()):
            self._user_tokens_hour[username] = [
                t for t in self._user_tokens_hour[username] if t > hour_ago
            ]
            if not self._user_tokens_hour[username]:
                del self._user_tokens_hour[username]

        # Clean global counter
        self._global_calls_minute = [
            t for t in self._global_calls_minute if t > minute_ago
        ]
