"""
Cancellation tests: direct cancel (chat mode) and confirm-cancel (task mode).

Covers:
  - CancellationManager state machine (direct/confirm/approve/deny/expire)
  - Direct cancel stops a chat-mode run at the next iteration
  - Task mode: pending cancel does NOT stop the run until approved
  - Approved cancel stops the run with a cancelled event
  - Denied cancel lets the run continue to completion
  - run_id clearing after run() finishes
"""

import threading
import time

from agent.cancellation import (
    CancellationManager, DIRECT, CONFIRM, PENDING, APPROVED, DENIED,
)
from agent.orchestrator import AgentOrchestrator, AgentConfig
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class SlowTool(Tool):
    """A tool whose execute() blocks until released (to hold the loop)."""

    name = "slow_tool"
    description = "Blocks for a bit"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, **kwargs) -> ToolResult:
        self.entered.set()
        self.release.wait(timeout=10)
        return ToolResult(success=True, output="done")


class MultiTool(Tool):
    """Emits several tool calls so the loop runs multiple iterations."""

    name = "multi_tool"
    description = "Run a few times"

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def __init__(self, n=3):
        self.n = n
        self.count = 0

    def execute(self, **kwargs) -> ToolResult:
        self.count += 1
        return ToolResult(success=True, output=f"count={self.count}")


def make_agent(llm, tool=None, **cfg_overrides):
    registry = ToolRegistry()
    if tool:
        registry.register(tool)
    config = AgentConfig(
        planning_enabled=False,
        security_enabled=False,
        memory_enabled=False,
        max_iterations=50,
        max_tool_calls=50,
        **cfg_overrides,
    )
    return AgentOrchestrator(llm=llm, tools=registry, config=config)


# ---------------------------------------------------------------------------
# CancellationManager unit tests
# ---------------------------------------------------------------------------

def test_direct_request_stops_immediately():
    m = CancellationManager()
    run_id = "r1"
    m.request(run_id, mode=DIRECT)
    assert m.should_stop(run_id) is True  # pending direct = stop


def test_confirm_request_does_not_stop_until_approved():
    m = CancellationManager()
    run_id = "r2"
    m.request(run_id, mode=CONFIRM)
    assert m.should_stop(run_id) is False  # pending confirm = keep going

    assert m.approve(run_id) is True
    assert m.should_stop(run_id) is True   # approved = stop


def test_confirm_request_denied_keeps_running():
    m = CancellationManager()
    run_id = "r3"
    m.request(run_id, mode=CONFIRM)
    assert m.deny(run_id) is True
    assert m.should_stop(run_id) is False
    # A later approve attempt on a decided request must not flip it
    assert m.approve(run_id) is False
    assert m.should_stop(run_id) is False


def test_pending_expires_as_denied():
    m = CancellationManager(poll_interval=0.05)
    run_id = "r4"
    m.request(run_id, mode=CONFIRM, expiry_seconds=0.1)
    time.sleep(0.25)
    assert m.should_stop(run_id) is False  # expired -> treated as denied
    req = m.get(run_id)
    assert req.status == DENIED


def test_clear_removes_request():
    m = CancellationManager()
    run_id = "r5"
    m.request(run_id, mode=DIRECT)
    assert m.should_stop(run_id) is True
    m.clear(run_id)
    assert m.should_stop(run_id) is False


def test_unknown_run_id_never_stops():
    m = CancellationManager()
    assert m.should_stop("nope") is False
    assert m.should_stop(None) is False


# ---------------------------------------------------------------------------
# Orchestrator integration tests
# ---------------------------------------------------------------------------

def test_direct_cancel_stops_chat_run():
    """A direct cancel requested before the run starts must abort the loop."""
    from tests.conftest import ScriptedLLM
    llm = ScriptedLLM("first", "second", "third")
    agent = make_agent(llm)
    agent.cancellation_manager = CancellationManager()
    run_id = "chat-run-1"
    agent.cancellation_manager.request(run_id, mode=DIRECT)

    result = agent.run("你好", run_id=run_id)
    assert result.get("cancelled") is True
    assert "取消" in result.get("content", "")
    assert llm.calls == []  # never even called the LLM


def test_confirm_cancel_requires_approval():
    """Task mode: pending confirm does not stop; approval stops at next iter."""
    from tests.conftest import ScriptedLLM
    llm = ScriptedLLM(
        ScriptedLLM().tool_use("slow_tool"),
        ScriptedLLM().text("unused"),
    )
    tool = SlowTool()
    agent = make_agent(llm, tool=tool)
    agent.cancellation_manager = CancellationManager()
    run_id = "task-run-1"
    agent.cancellation_manager.request(run_id, mode=CONFIRM)

    # Run in a thread; the loop blocks inside slow_tool.execute().
    result_holder = {}
    def _run():
        result_holder["result"] = agent.run("写一个报告", run_id=run_id)
    t = threading.Thread(target=_run)
    t.start()
    # Wait until the tool is actually executing (loop is stuck inside it)
    assert tool.entered.wait(timeout=5), "tool never started"
    # Pending confirm must NOT stop the run
    assert agent.cancellation_manager.should_stop(run_id) is False
    # Approve the cancellation while the tool still blocks -> after the tool
    # returns, the loop hits the checkpoint and aborts.
    agent.cancellation_manager.approve(run_id)
    tool.release.set()
    t.join(timeout=10)
    assert not t.is_alive()
    result = result_holder.get("result", {})
    assert result.get("cancelled") is True
    assert "取消" in result.get("content", "")


def test_denied_cancel_continues():
    """Task mode: denied cancel -> run completes normally."""
    from tests.conftest import ScriptedLLM
    llm = ScriptedLLM(
        ScriptedLLM().tool_use("multi_tool"),
        ScriptedLLM().text("任务完成"),
    )
    tool = MultiTool(n=3)
    agent = make_agent(llm, tool=tool)
    agent.cancellation_manager = CancellationManager()
    run_id = "task-run-2"
    agent.cancellation_manager.request(run_id, mode=CONFIRM)

    result = agent.run("写一个报告并总结", run_id=run_id)
    assert result.get("cancelled") is None or result.get("cancelled") is False
    assert "任务完成" in result.get("content", "")
    assert tool.count == 1  # only one tool call (script had one tool_use)


def test_run_id_cleared_after_run():
    """After run() finishes, the cancellation request is cleared."""
    from tests.conftest import ScriptedLLM
    llm = ScriptedLLM("ok")
    agent = make_agent(llm)
    agent.cancellation_manager = CancellationManager()
    run_id = "run-clear-1"
    agent.cancellation_manager.request(run_id, mode=DIRECT)
    result = agent.run("hi", run_id=run_id)
    assert result.get("cancelled") is True
    assert agent.current_run_id is None
    assert agent.cancellation_manager.get(run_id) is None
