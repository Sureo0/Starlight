"""
Tests for the sub-agent (delegate) mechanism.

Covers:
  - Mode-based capability control (research/code/full tool subsets)
  - Presets wiring (delegate registered; children never get it)
  - Sync execution: parent delegates -> child runs -> structured result
  - Parent quota charging (delegate calls cost the parent a mode-weighted toll)
  - Delegate results are never cached
  - Trace integration (subagent event emitted; child trace persisted via sink)
  - Error handling (missing task, bad mode, child crash)
"""

from __future__ import annotations

import json

from agent.orchestrator import AgentConfig, AgentOrchestrator
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult
from agent.tools.delegate import (
    SubagentTool,
    resolve_subagent_tools,
    MODE_QUOTA,
    RESEARCH_TOOLS,
    VALID_MODES,
)
from agent.observability.trace_recorder import TraceRecorder
from agent.llm_client import LLMResponse
from tests.conftest import ScriptedLLM
from agent.presets import create_agent


def _tool_use(name, args):
    """Build an LLMResponse that asks for a tool call."""
    return LLMResponse(
        type="tool_use",
        content="",
        tool_calls=[{
            "id": "c1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
        mode="native",
    )


# ---------------------------------------------------------------------------
# Mode-based capability control
# ---------------------------------------------------------------------------

class FakeTool(Tool):
    """A fake tool that records calls and returns a canned output."""

    retryable = True

    def __init__(self, name="fake", output="ok"):
        self._name = name
        self._output = output
        self.calls = 0

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"fake {self._name}"

    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        self.calls += 1
        return ToolResult(success=True, output=self._output)


def _parent_with_tools():
    """A parent registry with all tool categories present."""
    registry = ToolRegistry()
    for name in ("read_file", "write_file", "execute_code", "web_search", "delegate", "spawn_agent"):
        registry.register(FakeTool(name))
    return registry


def test_resolve_research_mode_excludes_write_and_code():
    child = resolve_subagent_tools(_parent_with_tools(), "research")
    names = set(child.list_names())
    assert "read_file" in names
    assert "web_search" in names
    assert "write_file" not in names
    assert "execute_code" not in names


def test_resolve_code_mode_includes_write_and_code():
    child = resolve_subagent_tools(_parent_with_tools(), "code")
    names = set(child.list_names())
    assert "read_file" in names
    assert "write_file" in names
    assert "execute_code" in names


def test_resolve_full_mode_gets_all_parent_tools():
    child = resolve_subagent_tools(_parent_with_tools(), "full")
    names = set(child.list_names())
    assert "read_file" in names
    assert "write_file" in names
    assert "execute_code" in names


def test_children_never_get_delegate_tool():
    registry = _parent_with_tools()
    for mode in VALID_MODES:
        child = resolve_subagent_tools(registry, mode)
        assert "delegate" not in set(child.list_names()), f"mode {mode} leaked delegate"


def test_legacy_spawn_agent_placeholder_removed():
    child = resolve_subagent_tools(_parent_with_tools(), "full")
    assert "spawn_agent" not in set(child.list_names())


def test_unknown_mode_falls_back_to_research():
    child = resolve_subagent_tools(_parent_with_tools(), "bogus")
    assert set(child.list_names()) == RESEARCH_TOOLS & set(_parent_with_tools().list_names())


def test_mode_quota_values():
    assert MODE_QUOTA["research"] >= 1
    assert MODE_QUOTA["code"] >= MODE_QUOTA["research"]
    assert MODE_QUOTA["full"] >= MODE_QUOTA["code"]


# ---------------------------------------------------------------------------
# Presets wiring
# ---------------------------------------------------------------------------

def test_presets_register_delegate(tmp_db, admin_user):
    llm = ScriptedLLM()
    agent = create_agent(
        llm_client=llm, db=tmp_db, workspace_dir=".", username="admin",
        user_id=admin_user["id"],
    )
    assert agent.tools.has("delegate")
    # Delegate must never appear in the child's own toolset (no nesting)
    child = resolve_subagent_tools(agent.tools, "full")
    assert "delegate" not in set(child.list_names())


def test_presets_disable_tools_skips_delegate(tmp_db, admin_user):
    llm = ScriptedLLM()
    agent = create_agent(
        llm_client=llm, db=tmp_db, workspace_dir=".", username="admin",
        user_id=admin_user["id"], tools_enabled=False,
    )
    assert not agent.tools.has("delegate")


# ---------------------------------------------------------------------------
# Sync execution via SubagentTool
# ---------------------------------------------------------------------------

def test_delegate_runs_child_and_returns_summary(tmp_db, admin_user):
    parent = create_agent(
        llm_client=ScriptedLLM(), db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    # Child script: one successful tool call, then a final answer. The
    # executor builds its own child from the parent's registry (research
    # mode), so the read_file tool is the REAL one — use an existing path.
    child_llm = ScriptedLLM(_tool_use("read_file", {"path": "README.md"}), "child final answer")

    from agent.tools.delegate import SubagentExecutor
    executor = SubagentExecutor(
        llm=child_llm, parent=parent, workspace_dir=".",
        username="admin", trace_sink=None,
    )
    summary = executor.run("do a thing", "research", 60.0)

    assert summary["mode"] == "research"
    assert summary["content"] == "child final answer"
    assert summary["tool_calls_made"] >= 1
    assert summary["iterations"] >= 1
    assert "subagent_id" in summary
    assert summary["duration"] >= 0


def test_delegate_tool_rejects_missing_task(tmp_db, admin_user):
    parent = create_agent(
        llm_client=ScriptedLLM(), db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    tool = SubagentTool(llm=ScriptedLLM(), parent=parent, workspace_dir=".")
    result = tool.execute(mode="research")
    assert not result.success
    assert "task" in result.error


def test_delegate_tool_rejects_bad_mode(tmp_db, admin_user):
    parent = create_agent(
        llm_client=ScriptedLLM(), db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    tool = SubagentTool(llm=ScriptedLLM(), parent=parent, workspace_dir=".")
    result = tool.execute(task="x", mode="banana")
    assert not result.success
    assert "模式" in result.error


def test_delegate_tool_is_not_retryable():
    # Side-effectful child runs must never be auto-retried by the orchestrator.
    assert SubagentTool.retryable is False


# ---------------------------------------------------------------------------
# Parent-loop integration (quota + no-cache + trace event)
# ---------------------------------------------------------------------------

def _make_parent_loop_agent(tmp_db, admin_user):
    """Parent that calls delegate once (research), then finishes. Returns
    (agent, recorder)."""
    parent_llm = ScriptedLLM(
        _tool_use("delegate", {"task": "subtask", "mode": "research"}),
        "parent done",
    )
    # Child reads a real file (succeeds), then answers.
    child_llm = ScriptedLLM(
        _tool_use("read_file", {"path": "README.md"}),
        "child done",
    )

    parent = create_agent(
        llm_client=parent_llm, db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    # Route the delegate tool's executor to the child script.
    parent.tools.get("delegate")._executor._llm = child_llm

    recorder = TraceRecorder().attach()
    parent.attach_recorder(recorder)
    return parent, recorder


def test_parent_loop_delegate_charges_quota(tmp_db, admin_user):
    parent, recorder = _make_parent_loop_agent(tmp_db, admin_user)
    result = parent.run("please delegate")

    # research mode -> quota 1: parent counts the delegate call once.
    assert result["tool_calls_made"] == 1
    assert result["content"] == "parent done"


def test_parent_loop_delegate_emits_subagent_trace(tmp_db, admin_user):
    parent, recorder = _make_parent_loop_agent(tmp_db, admin_user)
    parent.run("please delegate")

    events = [e for e in recorder.trace.events if e.type == "subagent"]
    assert len(events) == 1
    ev = events[0]
    assert ev.tool == "delegate"
    assert ev.result["mode"] == "research"
    assert ev.result["success"] is True
    assert ev.result["tool_calls_made"] >= 1
    assert ev.result["subagent_id"]


def test_parent_loop_delegate_not_cached(tmp_db, admin_user):
    parent, recorder = _make_parent_loop_agent(tmp_db, admin_user)
    result = parent.run("please delegate")

    tool_results = [
        e for e in result["events"]
        if e.get("type") == "tool_result" and e.get("tool") == "delegate"
    ]
    assert tool_results
    # Cached results carry detail="cached" — delegate must never be cached.
    assert all(e.get("detail") != "cached" for e in tool_results)


def test_child_crash_returns_error_summary(tmp_db, admin_user):
    parent_llm = ScriptedLLM(_tool_use("delegate", {"task": "boom", "mode": "research"}))
    parent = create_agent(
        llm_client=parent_llm, db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )

    class BoomLLM:
        backend_name = "boom"
        model_name = "boom"

        def chat(self, *a, **kw):
            raise RuntimeError("boom")

    parent.tools.get("delegate")._executor._llm = BoomLLM()

    # Parent must not crash; the delegate tool result carries the error.
    tool = parent.tools.get("delegate")
    result = tool.execute(task="boom", mode="research")
    assert not result.success
    assert "llm_error" in (result.error or "")
    # The child's partial content is preserved in output
    assert "boom" in (result.output or "")


def test_child_trace_saved_via_sink(tmp_db, admin_user):
    """Child traces are persisted through the parent's trace sink."""
    saved = []

    def sink(trace):
        saved.append(trace)

    parent_llm = ScriptedLLM(
        _tool_use("delegate", {"task": "sub", "mode": "research"}),
        "done",
    )
    child_llm = ScriptedLLM("child answer")

    parent = create_agent(
        llm_client=parent_llm, db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    parent.trace_sink = sink
    parent.tools.get("delegate")._executor._llm = child_llm

    parent.run("delegate")

    child_traces = [t for t in saved if t.user_message == "sub"]
    assert len(child_traces) == 1
    assert child_traces[0].success
    assert child_traces[0].content == "child answer"


def test_stream_delegate_emits_subagent_trace(tmp_db, admin_user):
    """Streaming loop: delegate works and records a subagent trace event."""
    parent_llm = ScriptedLLM(
        _tool_use("delegate", {"task": "stream sub", "mode": "code"}),
        "parent stream done",
    )
    child_llm = ScriptedLLM("child stream answer")

    parent = create_agent(
        llm_client=parent_llm, db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    parent.tools.get("delegate")._executor._llm = child_llm
    recorder = TraceRecorder().attach()
    parent.attach_recorder(recorder)

    events = list(parent.run_stream("delegate stream"))
    texts = [e.get("content") for e in events if e.get("type") == "text"]
    assert texts == ["parent stream done"]

    sub_events = [e for e in recorder.trace.events if e.type == "subagent"]
    assert len(sub_events) == 1
    assert sub_events[0].result["mode"] == "code"


# ---------------------------------------------------------------------------
# Orchestrator quota helper unit tests
# ---------------------------------------------------------------------------

def test_charge_subagent_quota_directly(tmp_db):
    agent = AgentOrchestrator(
        llm=ScriptedLLM(), tools=ToolRegistry(), config=AgentConfig(),
    )
    assert agent._charge_subagent_quota("delegate", {"mode": "research"}) == MODE_QUOTA["research"]
    assert agent._charge_subagent_quota("delegate", {"mode": "code"}) == MODE_QUOTA["code"]
    assert agent._charge_subagent_quota("delegate", {"mode": "full"}) == MODE_QUOTA["full"]
    # non-delegate tools charge nothing extra
    assert agent._charge_subagent_quota("read_file", {}) == 0
    # missing mode defaults to research
    assert agent._charge_subagent_quota("delegate", {}) == MODE_QUOTA["research"]
