"""
Tests for the execution timeout (agent loop + streaming).
"""

import time

from agent.orchestrator import AgentOrchestrator, AgentConfig
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult

from conftest import ScriptedLLM


class SlowTool(Tool):
    """Sleeps for a while to trigger the loop timeout."""

    def __init__(self, sleep=1.0):
        self._sleep = sleep
        self.calls = 0

    @property
    def name(self): return "slow"
    @property
    def description(self): return "slow tool"
    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        time.sleep(self._sleep)
        return ToolResult(success=True, output="done")


class ToolOnlyLLM:
    """Always requests a tool call with varying args, so the loop never
    terminates on its own and doesn't trip the duplicate-call guard."""

    def __init__(self):
        self.calls = 0

    def chat(self, **kw):
        from agent.llm_client import LLMResponse
        self.calls += 1
        return LLMResponse(
            type="tool_use",
            content="",
            tool_calls=[{"id": f"c{self.calls}", "type": "function",
                         "function": {"name": "slow", "arguments": f'{{"n": {self.calls}}}'}}],
            mode="native",
        )


def _make_agent(tool, llm, timeout):
    registry = ToolRegistry()
    registry.register(tool)
    cfg = AgentConfig(
        execution_timeout=timeout,
        permission_enabled=False,
        rate_limit_enabled=False,
        input_validation_enabled=False,
        planning_enabled=False,
        memory_enabled=False,
    )
    return AgentOrchestrator(llm=llm, tools=registry, config=cfg)


def test_loop_timeout_returns_error():
    """The non-streaming loop stops and reports a timeout."""
    tool = SlowTool(sleep=0.3)
    llm = ToolOnlyLLM()
    agent = _make_agent(tool, llm, timeout=1)

    start = time.time()
    result = agent.run("do it", conversation_id=None)
    elapsed = time.time() - start

    assert "超时" in result["content"]
    assert result["iterations"] >= 1
    # Should not run forever: loop terminated near the timeout
    assert elapsed < 5


def test_stream_timeout_emits_error():
    """The streaming loop also stops and reports a timeout."""
    tool = SlowTool(sleep=0.3)
    llm = ToolOnlyLLM()
    agent = _make_agent(tool, llm, timeout=1)

    events = list(agent.run_stream("do it"))
    error_events = [e for e in events if e.get("type") == "error"]
    assert error_events, "expected an error event on timeout"
    assert "超时" in error_events[0]["content"]


def test_no_timeout_when_fast():
    """Normal short tasks complete without hitting the timeout."""
    tool = SlowTool(sleep=0.01)
    llm = ScriptedLLM("任务完成。")
    agent = _make_agent(tool, llm, timeout=10)

    result = agent.run("hi", conversation_id=None)

    assert "超时" not in result["content"]
    assert result["content"] == "任务完成。"


def test_default_timeout_is_600():
    """The default execution timeout is 600s (was hardcoded 120)."""
    assert AgentConfig().execution_timeout == 600


# ============================================================
# Sandbox: os allowed for read-only, destructive ops blocked
# ============================================================

def test_sandbox_os_readonly_allowed():
    """import os works; read-only usage (path/listdir) is fine."""
    from agent.security.sandbox import CodeSandbox, SandboxConfig
    sb = CodeSandbox(SandboxConfig(timeout=15))
    res = sb.execute('import os\nprint(os.path.join("a", "b"))')
    assert res["returncode"] == 0
    assert "a/b" in res["stdout"]


def test_sandbox_os_destructive_blocked():
    """os.remove/unlink etc. are blocked at runtime with a clear error."""
    from agent.security.sandbox import CodeSandbox, SandboxConfig
    sb = CodeSandbox(SandboxConfig(timeout=15))
    res = sb.execute('import os\nos.remove("/tmp/definitely-not-here")')
    assert res["returncode"] != 0
    assert "blocked" in res["stderr"]


def test_sandbox_subprocess_still_blocked():
    from agent.security.sandbox import CodeSandbox, SandboxConfig
    sb = CodeSandbox(SandboxConfig(timeout=15))
    res = sb.execute('import subprocess\nprint("x")')
    assert res["returncode"] != 0
    assert "blocked" in res["stderr"]
