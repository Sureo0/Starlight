"""
Shared pytest fixtures for the AI Chat test suite.

- tmp_db: in-memory-ish Database on a temp file, with an 'admin' user
- make_agent: build an AgentOrchestrator with test-friendly config
- FakeLLM / ScriptedLLM: scriptable LLM doubles for agent-loop tests
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid

import pytest

from data.database import Database
from agent.orchestrator import AgentOrchestrator, AgentConfig
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult
from agent.llm_client import LLMResponse
from agent.presets import create_agent


# ============================================================
# Database fixtures
# ============================================================

@pytest.fixture()
def tmp_db():
    """Database on a unique temp file, with an 'admin' user created."""
    db_path = tempfile.mktemp(prefix="ai-chat-test-", suffix=".db")
    db = Database(db_path)
    db.create_user("admin", hashlib.sha256(b"pw").hexdigest())
    yield db
    db.close()
    import os
    try:
        os.remove(db_path)
    except OSError:
        pass


@pytest.fixture()
def admin_user(tmp_db):
    """The admin user dict from tmp_db."""
    return tmp_db.get_user("admin")


@pytest.fixture()
def tmp_conv(tmp_db, admin_user):
    """A fresh conversation owned by admin."""
    conv_id = uuid.uuid4().hex[:12]
    tmp_db.create_conversation(conv_id, title="test", user_id=admin_user["id"])
    return conv_id


# ============================================================
# Agent factory
# ============================================================

@pytest.fixture()
def agent_factory(tmp_db):
    """Build a configured AgentOrchestrator for tests.

    Permission checks are disabled by default so ad-hoc test tools work;
    override via kwargs.
    """
    def _make(tools=None, config=None, llm=None, **config_kwargs):
        defaults = dict(
            permission_enabled=False,
            rate_limit_enabled=False,
            input_validation_enabled=False,
        )
        defaults.update(config_kwargs)
        cfg = config or AgentConfig(**defaults)
        registry = tools or ToolRegistry()
        return AgentOrchestrator(llm=llm, tools=registry, config=cfg, db=tmp_db)

    return _make


@pytest.fixture()
def full_agent(tmp_db, admin_user):
    """Full agent built via presets.create_agent (all tools, memory, planning)."""
    llm = ScriptedLLM()
    agent = create_agent(
        llm_client=llm,
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
    )
    agent._llm = llm  # convenient handle back to the fake
    return agent


# ============================================================
# LLM doubles
# ============================================================

class ScriptedLLM:
    """Fake LLM whose responses follow a script.

    Each call pops the next response from the script list; the last entry is
    repeated. Records every messages payload it receives.
    """

    def __init__(self, *responses, default=None):
        # Accept plain strings (auto-wrapped as text responses) or LLMResponse
        self._responses = self._wrap(responses)
        if default is not None and not isinstance(default, LLMResponse):
            default = LLMResponse(type="text", content=default, mode="native")
        self._default = default or LLMResponse(type="text", content="ok", mode="native")
        self.calls: list[list[dict]] = []
        self.injected: dict[str, str] = {}  # role+prefix -> content

    @staticmethod
    def _wrap(responses) -> list[LLMResponse]:
        return [
            r if isinstance(r, LLMResponse) else LLMResponse(type="text", content=r, mode="native")
            for r in responses
        ]

    def chat(self, messages=None, tools=None, tool_choice=None, temperature=None,
             max_tokens=None, timeout=None, force_mode=None, **kw):
        self.calls.append(messages or [])
        # Record system-message injections for assertions
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "system":
                c = m.get("content", "")
                for key in ("[执行计划]", "[计划进度]", "[长期记忆]"):
                    if c.startswith(key):
                        self.injected[key] = c
        if self._responses:
            return self._responses.pop(0)
        return self._default

    def text(self, content="ok", **kw):
        return LLMResponse(type="text", content=content, mode="native", **kw)

    def tool_use(self, name, args=None, call_id="c1", **kw):
        return LLMResponse(
            type="tool_use",
            content="",
            tool_calls=[{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args or "{}"},
            }],
            mode="native",
            **kw,
        )


# ============================================================
# Test tool helpers
# ============================================================

class SimpleTool(Tool):
    """Configurable tool for testing orchestrator behavior."""

    def __init__(self, name="simple", result=None, error=None, retryable=True):
        self._name = name
        self._result = result or ToolResult(success=True, output="ok")
        self._error = error
        self.calls = 0
        self.retryable = retryable

    @property
    def name(self): return self._name

    @property
    def description(self): return f"test tool {self._name}"

    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        if self._error:
            return ToolResult(success=False, error=self._error)
        return self._result


class FlakyTool(SimpleTool):
    """Fails with a transient error the first N times, then succeeds."""

    def __init__(self, fail_count=1, error="connection refused", name="flaky", **kw):
        super().__init__(name=name, error=error, **kw)
        self._fail = fail_count
        self._base_error = error

    def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        if self.calls <= self._fail:
            return ToolResult(success=False, error=self._base_error)
        return ToolResult(success=True, output=f"ok after {self.calls} calls")


# ============================================================
# Session-wide cleanup for tests that redirect the app's DB
# ============================================================

def pytest_sessionfinish(session, exitstatus):
    """Remove temp DB files left by test_app.py after the session ends.

    SAFETY: only ever deletes a database whose path contains the
    "ai-chat-app-test-" marker that test_app.py uses for its redirected
    temp DB. If test_app.py wasn't collected this run (e.g. running a
    single test file), ``database.DB_FILE`` still points at the
    PRODUCTION db (data/chat.db) — deleting its -wal/-shm there would
    destroy real data. Never touch it.
    """
    import os
    import sys
    from pathlib import Path

    _root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_root / "data"))

    import database as db_module
    db_path = getattr(db_module, "DB_FILE", None)
    if db_path is None:
        return
    if "ai-chat-app-test-" not in str(db_path):
        return  # production db — never clean
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(db_path) + suffix)
        except OSError:
            pass
