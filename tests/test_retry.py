"""
Tests for the tool retry policy (agent/retry.py + orchestrator integration).
"""

import random

from agent.retry import RetryConfig, should_retry, compute_delay, is_transient_error
from agent.tools.registry import ToolRegistry
from agent.tools.base import ToolResult

from conftest import FlakyTool, SimpleTool


# ============================================================
# Unit tests: retry policy
# ============================================================

def test_transient_detection():
    transient = [
        "Connection refused by remote host",
        "request timed out after 30s",
        "HTTP 429 Too Many Requests",
        "rate limit exceeded",
        "500 Internal Server Error",
        "upstream timeout",
        "Service Unavailable",
        "connection reset by peer",
        "temporary failure in name resolution",
    ]
    for msg in transient:
        assert is_transient_error(msg), f"expected transient: {msg}"

    permanent = [
        "File not found: /tmp/x",
        "Permission denied",
        "Invalid JSON argument",
        "",
        None,
        "SyntaxError: invalid syntax",
    ]
    for msg in permanent:
        assert not is_transient_error(msg), f"expected permanent: {msg!r}"


def test_should_retry_rules():
    cfg = RetryConfig(max_retries=2)

    # Retryable tool + transient error + budget left -> retry
    assert should_retry(True, "connection refused", 0, cfg)
    # Non-transient error -> never retry
    assert not should_retry(True, "File not found", 0, cfg)
    # Non-retryable (write) tool -> never retry even on transient
    assert not should_retry(False, "connection refused", 0, cfg)
    # Budget exhausted -> no retry
    assert not should_retry(True, "connection refused", 2, cfg)
    # Disabled -> no retry
    assert not should_retry(True, "connection refused", 0, RetryConfig(enabled=False))
    # No error text -> no retry
    assert not should_retry(True, None, 0, cfg)


def test_backoff_grows_and_caps():
    random.seed(42)
    cfg = RetryConfig(base_delay=1.0, max_delay=4.0)

    delays = [compute_delay(i, cfg) for i in range(3)]
    assert delays[0] < delays[1] < delays[2]  # exponential growth
    assert delays[0] >= 0.5 and delays[2] <= 6.0  # within jitter bounds
    assert compute_delay(9, cfg) <= 4.8  # capped at max_delay + jitter


# ============================================================
# Integration tests: orchestrator retry loop
# ============================================================

def _agent_with(agent_factory, tool, **kw):
    """Build an agent whose registry contains only `tool`."""
    registry = ToolRegistry()
    registry.register(tool)
    defaults = dict(tool_retry_max=2, tool_retry_base_delay=0.01)
    defaults.update(kw)
    return agent_factory(tools=registry, **defaults)


def test_retry_recovers(agent_factory):
    tool = FlakyTool(fail_count=1)
    agent = _agent_with(agent_factory, tool)

    result = agent._execute_tool("flaky", {})

    assert result.success
    assert tool.calls == 2  # 1 fail + 1 retry
    assert len(result.metadata["retries"]) == 1
    assert result.metadata["retries"][0]["attempt"] == 1


def test_retry_exhausted(agent_factory):
    tool = FlakyTool(fail_count=99)
    agent = _agent_with(agent_factory, tool)

    result = agent._execute_tool("flaky", {})

    assert not result.success
    assert tool.calls == 3  # max_retries=2 -> 3 attempts
    assert len(result.metadata["retries"]) == 2


def test_no_retry_on_permanent_error(agent_factory):
    tool = SimpleTool(name="permanent", error="File not found: /no/such/file")
    agent = _agent_with(agent_factory, tool)

    result = agent._execute_tool("permanent", {})

    assert not result.success
    assert tool.calls == 1
    assert "retries" not in result.metadata


def test_no_retry_on_write_tool(agent_factory):
    tool = FlakyTool(fail_count=99, name="writer", retryable=False)
    agent = _agent_with(agent_factory, tool)

    result = agent._execute_tool("writer", {})

    assert not result.success
    assert tool.calls == 1  # write tools never retry


def test_retry_disabled(agent_factory):
    tool = FlakyTool(fail_count=1)
    agent = _agent_with(agent_factory, tool, tool_retry_enabled=False)

    result = agent._execute_tool("flaky", {})

    assert not result.success
    assert tool.calls == 1


class ExplodingTool(SimpleTool):
    """Raises a transient-looking exception on first call, then succeeds."""

    def __init__(self, name="explode"):
        super().__init__(name=name)
        self._armed = True

    def execute(self, **kwargs):
        self.calls += 1
        if self._armed:
            self._armed = False
            raise TimeoutError("request timed out")
        return ToolResult(success=True, output="recovered")


def test_exception_retry(agent_factory):
    tool = ExplodingTool()
    agent = _agent_with(agent_factory, tool)

    result = agent._execute_tool("explode", {})

    assert result.success
    assert tool.calls == 2
    assert "retries" in result.metadata
