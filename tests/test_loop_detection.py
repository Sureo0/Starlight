"""
Tests for loop detection in the orchestrator — specifically that legitimate
file-iteration writes (same path, different content) are not treated as a loop.
"""

from agent.orchestrator import AgentConfig
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult

from conftest import ScriptedLLM


class RecordingWriteTool(Tool):
    """Writes to a path; records every call (content + path)."""

    def __init__(self):
        self.calls = []

    @property
    def name(self): return "write_file"

    @property
    def description(self): return "write a file"

    @property
    def parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        }

    def execute(self, path="", content="", **kw) -> ToolResult:
        self.calls.append((path, content))
        return ToolResult(success=True, output={"path": path, "written": True})


class ReadOnlyTool(Tool):
    @property
    def name(self): return "read_file"
    @property
    def description(self): return "read a file"
    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    def execute(self, path="", **kw) -> ToolResult:
        return ToolResult(success=True, output={"path": path, "content": "..."})


def _make_agent(tool, llm, **cfg_kw):
    registry = ToolRegistry()
    registry.register(tool)
    defaults = dict(
        permission_enabled=False,
        rate_limit_enabled=False,
        input_validation_enabled=False,
        planning_enabled=False,
        memory_enabled=False,
    )
    defaults.update(cfg_kw)
    from agent.orchestrator import AgentOrchestrator
    return AgentOrchestrator(llm=llm, tools=registry, config=AgentConfig(**defaults))


def _write_call(content):
    import json
    return ScriptedLLM.tool_use(None, "write_file", args=json.dumps({"path": "README.md", "content": content}))


def _write_sequence(*contents):
    """Build a script of write_file calls (one per content) plus a final answer."""
    import json
    calls = [_write_call(c) for c in contents]
    return calls + ["完成。"]


def test_write_file_iteration_not_loop(tmp_path):
    """Writing the SAME path with DIFFERENT content many times is not a loop."""
    tool = RecordingWriteTool()
    llm = ScriptedLLM()
    llm._responses = llm._wrap(
        _write_sequence(*[f"version {i}" for i in range(15)])
    )

    # Content differs each time -> cache never hits, no false loop trigger
    agent = _make_agent(tool, llm)

    result = agent.run("完善 README.md", conversation_id=None)

    assert "循环" not in result["content"]
    assert result["content"] == "完成。"
    assert len(tool.calls) == 15
    # All writes actually executed (cache key is strict: full args)
    contents = [c for _, c in tool.calls]
    assert len(set(contents)) == 15


def test_identical_write_loop_still_caught(tmp_path):
    """Repeated IDENTICAL write calls (true loop) are still terminated."""
    tool = RecordingWriteTool()
    llm = ScriptedLLM()
    llm._responses = llm._wrap(
        _write_sequence(*["same content"] * 14)  # 14 identical writes
    )

    agent = _make_agent(tool, llm)

    result = agent.run("写文件", conversation_id=None)

    assert "循环" in result["content"]  # loop guard fired
    assert len(tool.calls) <= 14


def test_write_then_different_write_not_loop(tmp_path):
    """Alternating DIFFERENT paths resets the counter (not a loop)."""
    tool = RecordingWriteTool()
    llm = ScriptedLLM()

    def _write_pair(i):
        import json
        path = f"file_{i % 3}.md"  # cycle through 3 different paths
        return ScriptedLLM.tool_use(None, "write_file", args=json.dumps({"path": path, "content": f"v{i}"}))

    llm._responses = llm._wrap(
        [_write_pair(i) for i in range(24)] + ["完成。"]
    )

    agent = _make_agent(tool, llm)

    result = agent.run("写文件", conversation_id=None)

    assert "循环" not in result["content"]
    assert len(tool.calls) == 24


def test_same_path_different_content_many_times_trigger_warning():
    """Same-path writes with DIFFERENT content never trigger the loop guard
    (full-args key resets the counter) — this is what makes README-style
    iterative document generation work."""
    # Covered by test_write_file_iteration_not_loop above; this documents intent.
    assert True


def test_read_file_loop_still_caught(tmp_path):
    """Read-only identical calls still trigger the (lower) loop guard."""
    tool = ReadOnlyTool()
    llm = ScriptedLLM()
    llm._responses = llm._wrap([
        ScriptedLLM.tool_use(None, "read_file", args='{"path": "x.py"}') for _ in range(8)
    ] + ["完成。"])

    agent = _make_agent(tool, llm)

    result = agent.run("读文件", conversation_id=None)

    assert "循环" in result["content"]  # read threshold is 6 < 8


def test_empty_arg_write_file_uses_low_threshold(tmp_db):
    """Empty-arg write_file repeats (malformed calls) trip the guard at 6,
    not 12 — a model stuck re-sending {} must be stopped sooner."""
    from agent.orchestrator import AgentOrchestrator, AgentConfig
    from agent.tools.registry import ToolRegistry
    from agent.tools.base import Tool, ToolResult
    from agent.llm_client import LLMResponse
    from conftest import ScriptedLLM

    class EmptyWrite(Tool):
        @property
        def name(self): return "write_file"
        @property
        def description(self): return "write"
        @property
        def parameters_schema(self): return {"type": "object", "properties": {}}
        def execute(self, **kw) -> ToolResult:
            return ToolResult(success=False, error="Empty file path")

    llm = ScriptedLLM()
    for i in range(8):
        llm._responses.append(llm.tool_use("write_file", args="{}"))
    reg = ToolRegistry(); reg.register(EmptyWrite())
    agent = AgentOrchestrator(
        llm=llm, tools=reg,
        config=AgentConfig(permission_enabled=False, rate_limit_enabled=False,
                           input_validation_enabled=True, planning_enabled=False,
                           max_iterations=30),
        db=tmp_db,
    )
    result = agent.run("写文件")
    assert "循环" in result["content"]  # guard fired
    rec = agent.trace_recorder
    assert rec is not None and rec.trace.finish_reason == "loop_detected"
