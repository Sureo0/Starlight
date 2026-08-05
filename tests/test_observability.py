"""
Tests for the agent observability layer: TraceRecorder + TraceStore.

Covers: event capture on a full agent run (LLM calls, tool calls, plan,
memory, finish reasons), redaction of sensitive tool args, cache-hit
marking, store persistence round-trip, listing filters, and pruning.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from agent.observability.trace_recorder import (
    TraceRecorder,
    TraceEvent,
    AgentTrace,
    redact,
)
from agent.observability.storage import TraceStore
from agent.orchestrator import AgentConfig
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry
from agent.llm_client import LLMResponse


# ============================================================
# Tool helpers
# ============================================================

class ScriptedToolLLM:
    """Fake LLM that plays a script of responses; records its calls."""

    def __init__(self, *responses, backend="DeepSeek", model="deepseek-v4-flash"):
        self._responses = list(responses)
        self.backend_name = backend
        self.model_name = model
        self.calls: list[list[dict]] = []

    def chat(self, messages=None, tools=None, tool_choice=None, temperature=None,
             max_tokens=None, timeout=None, force_mode=None, **kw):
        self.calls.append(messages or [])
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(type="text", content="ok", mode="native",
                           usage={"total_tokens": 7})

    def tool_use(self, name, args="{}", call_id="c1", usage=None):
        return LLMResponse(
            type="tool_use",
            content="",
            tool_calls=[{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }],
            mode="native",
            usage=usage or {"total_tokens": 9},
        )


class EchoTool(Tool):
    """Returns its args as output (so redaction can be observed)."""

    def __init__(self, name="echo"):
        self._name = name

    @property
    def name(self): return self._name

    @property
    def description(self): return "echo"

    @property
    def parameters_schema(self): return {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, output=kwargs)


def _agent(llm, tools, **cfg_kwargs):
    cfg = AgentConfig(
        permission_enabled=False,
        rate_limit_enabled=False,
        input_validation_enabled=False,
        **cfg_kwargs,
    )
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    from agent.orchestrator import AgentOrchestrator
    return AgentOrchestrator(llm=llm, tools=registry, config=cfg, username="admin")


# ============================================================
# TraceRecorder
# ============================================================

def test_simple_text_run_records_llm_call_and_finish():
    llm = ScriptedToolLLM(LLMResponse(
        type="text", content="你好", mode="native", usage={"total_tokens": 42},
    ))
    agent = _agent(llm, [], memory_enabled=False, planning_enabled=False)

    result = agent.run("测试消息", conversation_id="conv1")

    assert result["content"] == "你好"
    rec = agent.trace_recorder
    assert rec is not None
    assert rec.trace.finish_reason == "text_response"
    assert rec.trace.success is True
    assert rec.trace.total_tokens == 42
    assert rec.trace.backend == "DeepSeek"
    assert rec.trace.model == "deepseek-v4-flash"
    assert rec.trace.username == "admin"
    assert rec.trace.conversation_id == "conv1"

    types = [e.type for e in rec.trace.events]
    assert types == ["llm_call"]
    ev = rec.trace.events[0]
    assert ev.messages is not None and ev.messages[-1]["role"] == "user"
    assert ev.response == "你好"
    assert ev.usage == {"total_tokens": 42}


def test_tool_run_records_call_result_and_cache_hit():
    llm = ScriptedToolLLM()
    llm._responses = [
        llm.tool_use("echo", '{"x": 1}'),
        llm.tool_use("echo", '{"x": 1}'),
        LLMResponse(type="text", content="done", mode="native",
                    usage={"total_tokens": 5}),
    ]
    agent = _agent(llm, [EchoTool()], memory_enabled=False, planning_enabled=False)

    agent.run("执行")

    types = [(e.type, e.tool, e.detail) for e in agent.trace_recorder.trace.events]
    # llm_call, tool_call, tool_result, llm_call, tool_call, tool_result(cached), llm_call
    assert ("tool_call", "echo", None) in types
    cached = [(t, d) for (ty, t, d) in types if ty == "tool_result" and d == "cached"]
    assert cached, "second identical call should be marked as cached"
    assert agent.trace_recorder.trace.tool_calls_made == 1  # cached call not counted


def test_redaction_hides_sensitive_args():
    llm = ScriptedToolLLM()
    llm._responses = [
        llm.tool_use("echo", json.dumps({"api_key": "sk-123456", "path": "/a", "nested": {"token": "abc"}})),
        LLMResponse(type="text", content="done", mode="native"),
    ]
    agent = _agent(llm, [EchoTool()], memory_enabled=False, planning_enabled=False)

    agent.run("写点东西")

    # The trace store must never see the secret
    blob = json.dumps(agent.trace_recorder.trace.to_dict())
    assert "sk-123456" not in blob
    assert "abc" not in blob
    assert "***" in blob


def test_redact_function_direct():
    assert redact({"api_key": "secret", "ok": 1}) == {"api_key": "***", "ok": 1}
    assert redact("short") == "short"
    assert redact({"x": [{"password": "pw"}]}) == {"x": [{"password": "***"}]}


def test_validation_failure_records_security_and_finish():
    cfg = AgentConfig(
        permission_enabled=False,
        rate_limit_enabled=False,
        input_validation_enabled=True,
        memory_enabled=False,
        planning_enabled=False,
    )
    from agent.orchestrator import AgentOrchestrator
    from agent.security.validator import InputValidator, ValidatorConfig

    # Force a validation failure by blocking a pattern
    vcfg = ValidatorConfig(blocked_patterns=["forbidden"])
    agent = AgentOrchestrator(llm=ScriptedToolLLM(), tools=ToolRegistry(), config=cfg,
                              username="admin")
    agent._validator = InputValidator(vcfg)
    result = agent.run("forbidden content")

    assert not result["content"].startswith("你好")
    rec = agent.trace_recorder
    assert rec.trace.finish_reason == "validation_error"
    assert rec.trace.success is False
    assert any(e.type == "security" for e in rec.trace.events)


def test_loop_guard_records_finish_reason():
    llm = ScriptedToolLLM()
    for _ in range(8):
        llm._responses.append(llm.tool_use("echo", '{"same": true}'))
    agent = _agent(llm, [EchoTool()], memory_enabled=False, planning_enabled=False,
                   max_iterations=20)
    # 8 identical tool calls in a row -> loop guard fires at threshold 6
    result = agent.run("循环测试")
    rec = agent.trace_recorder
    assert rec.trace.finish_reason == "loop_detected"
    assert any(e.type == "loop_guard" for e in rec.trace.events)
    assert rec.trace.success is False


# ============================================================
# TraceStore
# ============================================================

@pytest.fixture()
def store(tmp_path):
    return TraceStore(tmp_path / "traces", max_traces=3)


def _sample_trace(trace_id, msg="hi", started=None, success=True):
    t = AgentTrace(
        trace_id=trace_id,
        user_message=msg,
        username="admin",
        conversation_id="c1",
        backend="DeepSeek",
        model="m",
        started_at=started or time.time(),
    )
    # Mirror recorder behavior: llm_call events accumulate total_tokens
    rec = TraceRecorder(trace=t)
    rec.report_llm_call(
        [{"role": "user", "content": "hi"}],
        LLMResponse(type="text", content="hi", mode="native",
                    usage={"total_tokens": 3}),
    )
    t.finish("text_response", success=success, content="hi", tool_calls_made=1,
             iterations=2)
    return t


def test_store_save_get_roundtrip(store):
    trace = _sample_trace("abc123")
    store.save(trace)

    loaded = store.get("abc123")
    assert loaded is not None
    assert loaded.trace_id == "abc123"
    assert loaded.finish_reason == "text_response"
    assert loaded.success is True
    assert loaded.total_tokens == 3
    assert len(loaded.events) == 1
    assert loaded.events[0].type == "llm_call"
    assert loaded.events[0].response == "hi"

    # File persisted on disk
    assert (store._dir / "abc123.json").exists()


def test_store_list_filters_and_order(store):
    old = _sample_trace("t1", started=time.time() - 100)
    new = _sample_trace("t2", started=time.time() - 50, success=False)
    store.save(old)
    store.save(new)

    all_traces = store.list()
    assert [t["trace_id"] for t in all_traces] == ["t2", "t1"]  # newest first

    ok = store.list(success=True)
    assert [t["trace_id"] for t in ok] == ["t1"]

    failed = store.list(success=False)
    assert [t["trace_id"] for t in failed] == ["t2"]

    # Summaries must not carry events
    assert "events" not in all_traces[0]


def test_store_prunes_oldest(store):
    for i in range(5):
        store.save(_sample_trace(f"pr{i}", started=time.time() - (5 - i)))
    ids = [t["trace_id"] for t in store.list()]
    assert len(ids) == 3
    assert "pr0" not in ids and "pr1" not in ids
    assert store.count() == 3


def test_store_delete(store):
    store.save(_sample_trace("delme"))
    assert store.delete("delme") is True
    assert store.get("delme") is None
    assert store.delete("delme") is False


def test_store_rebuilds_index_from_disk(tmp_path):
    store = TraceStore(tmp_path / "traces")
    trace = _sample_trace("rebuild1")
    store.save(trace)

    # A new store instance over the same dir must see existing traces
    store2 = TraceStore(tmp_path / "traces")
    loaded = store2.get("rebuild1")
    assert loaded is not None
    assert loaded.trace_id == "rebuild1"
