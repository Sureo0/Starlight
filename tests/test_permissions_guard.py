"""
Tests for permission coverage of all registered tools and the failure-loop
guard (N consecutive failed tool calls terminate the loop).
"""

from agent.security.permissions import ToolPermission, PermissionLevel
from agent.orchestrator import AgentConfig
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult

from conftest import ScriptedLLM


# ============================================================
# Permission coverage
# ============================================================

def test_all_registered_tools_permitted():
    """Every tool registered by the full agent is permitted for a USER-level user."""
    import tempfile, hashlib
    from data.database import Database
    from agent.presets import create_agent

    db = Database(tempfile.mktemp(suffix=".db"))
    db.create_user("admin", hashlib.sha256(b"pw").hexdigest())
    user = db.get_user("admin")

    agent = create_agent(
        llm_client=ScriptedLLM(),
        db=db,
        workspace_dir=".",
        username="admin",
        user_id=user["id"],
    )

    tp = ToolPermission()
    tp.register_user("admin", PermissionLevel.USER)
    all_tools = agent.tools.list_names()
    denied = []
    for name in all_tools:
        allowed, _ = tp.can_use_tool("admin", name)
        if not allowed:
            denied.append(name)

    assert not denied, f"tools denied for USER level: {denied}"
    db.close()


def test_guest_level_read_only():
    """Guest users keep read-only access — the safety boundary still holds."""
    tp = ToolPermission()
    tp.register_user("guest", PermissionLevel.GUEST)

    assert tp.can_use_tool("guest", "read_file")[0]
    assert tp.can_use_tool("guest", "read_files")[0]
    assert not tp.can_use_tool("guest", "write_file")[0]
    assert not tp.can_use_tool("guest", "web_search")[0]
    assert not tp.can_use_tool("guest", "execute_code")[0]
    assert not tp.can_use_tool("guest", "chat_completion")[0]


# ============================================================
# Failure-loop guard
# ============================================================

class AlwaysFailTool(Tool):
    """Fails every call with a parameter error."""

    def __init__(self, error="Empty file path"):
        self._error = error
        self.calls = 0

    @property
    def name(self): return "write_file"
    @property
    def description(self): return "write"
    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, **kw) -> ToolResult:
        self.calls += 1
        return ToolResult(success=False, error=self._error)


def test_failure_loop_guard_stops_loop():
    """8 consecutive tool failures terminate the loop with a clear message."""
    tool = AlwaysFailTool()
    llm = ScriptedLLM()
    # 10 failing write_file calls with DIFFERENT args (bypasses same-call guard)
    import json as _json
    llm._responses = llm._wrap([
        ScriptedLLM.tool_use(None, "write_file", args=_json.dumps({"path": "", "content": f"v{i}"}))
        for i in range(10)
    ] + ["完成。"])

    from agent.orchestrator import AgentOrchestrator
    registry = ToolRegistry()
    registry.register(tool)
    agent = AgentOrchestrator(
        llm=llm,
        tools=registry,
        config=AgentConfig(
            permission_enabled=False,
            rate_limit_enabled=False,
            input_validation_enabled=False,
            planning_enabled=False,
            memory_enabled=False,
        ),
    )

    result = agent.run("写文件", conversation_id=None)

    assert "连续失败" in result["content"]
    assert "Empty file path" in result["content"]  # last error surfaced
    assert tool.calls == 8  # stopped at the 8th failure, not 10


def test_success_resets_failure_counter():
    """A success between failures resets the guard counter."""
    class SometimesFail(Tool):
        def __init__(self):
            self.calls = 0

        @property
        def name(self): return "flaky_tool"
        @property
        def description(self): return "flaky"
        @property
        def parameters_schema(self):
            return {"type": "object", "properties": {}, "required": []}

        def execute(self, **kw) -> ToolResult:
            self.calls += 1
            # fail, fail, SUCCESS, then fail x7 (never 8 in a row)
            return ToolResult(success=True, output="ok") if self.calls % 3 == 0 \
                else ToolResult(success=False, error="boom")

    tool = SometimesFail()
    llm = ScriptedLLM()
    import json as _json
    llm._responses = llm._wrap([
        ScriptedLLM.tool_use(None, "flaky_tool", args=_json.dumps({"n": i}))
        for i in range(12)
    ] + ["完成。"])

    from agent.orchestrator import AgentOrchestrator
    registry = ToolRegistry()
    registry.register(tool)
    agent = AgentOrchestrator(
        llm=llm,
        tools=registry,
        config=AgentConfig(
            permission_enabled=False,
            rate_limit_enabled=False,
            input_validation_enabled=False,
            planning_enabled=False,
            memory_enabled=False,
        ),
    )

    result = agent.run("重试", conversation_id=None)

    assert "连续失败" not in result["content"]  # guard never fired
    assert tool.calls == 12  # all calls executed


# ============================================================
# Rate-limit failures must NOT trip the failure-loop guard
# ============================================================

class RateLimitedTool(Tool):
    """Fails with a rate-limit style error (metadata.rate_limited=True)."""

    def __init__(self, name="ratelimited"):
        self._name = name
        self._rl = True
        self.calls = 0

    @property
    def name(self): return self._name

    @property
    def description(self): return "rate limited test tool"

    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        if self._rl:
            result = ToolResult(
                success=False,
                error="工具调用频率限制: Rate limit exceeded: 30 calls/minute",
            )
            result.metadata["rate_limited"] = True
            return result
        return ToolResult(success=True, output="ok")


def test_rate_limited_failures_do_not_trip_guard(tmp_db):
    """A loop of rate-limited failures must NOT trigger failure_loop."""
    from agent.orchestrator import AgentOrchestrator, AgentConfig
    from agent.tools.registry import ToolRegistry
    from agent.llm_client import LLMResponse

    llm = ScriptedLLM()
    tool = RateLimitedTool()
    reg = ToolRegistry()
    reg.register(tool)
    agent = AgentOrchestrator(
        llm=llm,
        tools=reg,
        config=AgentConfig(
            permission_enabled=False,
            rate_limit_enabled=False,
            input_validation_enabled=False,
            max_iterations=30,
        ),
        db=tmp_db,
    )
    # 12 consecutive rate-limited tool calls (over the same-call threshold,
    # but rate-limit retries must not trip either guard)
    for _ in range(12):
        llm._responses.append(
            LLMResponse(
                type="tool_use",
                tool_calls=[{
                    "id": f"c{_}", "type": "function",
                    "function": {"name": "ratelimited", "arguments": "{}"},
                }],
                mode="native",
            )
        )
    result = agent.run("测试限流")
    # The guard must NOT fire (rate-limited failures are excluded)
    rec = agent.trace_recorder
    assert rec is not None and rec.trace.finish_reason != "failure_loop"
    assert result["iterations"] > 1
