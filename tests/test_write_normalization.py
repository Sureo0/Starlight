"""
Tests for write_file argument normalization and detailed validation errors.
"""

from agent.orchestrator import AgentConfig
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult
from agent.security.validator import InputValidator, ValidatorConfig

from conftest import ScriptedLLM


class RecordingWriteTool(Tool):
    """Records the args it received; succeeds when path is present."""

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
        if not path:
            return ToolResult(success=False, error="Empty file path")
        return ToolResult(success=True, output={"path": path, "written": True})


def _make_agent(tool, **cfg_kw):
    registry = ToolRegistry()
    registry.register(tool)
    defaults = dict(
        permission_enabled=False,
        rate_limit_enabled=False,
        input_validation_enabled=True,  # keep validation ON for these tests
        planning_enabled=False,
        memory_enabled=False,
    )
    defaults.update(cfg_kw)
    from agent.orchestrator import AgentOrchestrator
    return AgentOrchestrator(llm=None, tools=registry, config=AgentConfig(**defaults))


def test_write_file_normalizes_paths_list():
    """write_file called with `paths: ["README.md"]` gets path normalized."""
    tool = RecordingWriteTool()
    agent = _make_agent(tool)

    result = agent._execute_tool("write_file", {"paths": ["README.md"], "content": "hi"})

    assert result.success, result
    assert tool.calls[-1][0] == "README.md"  # path extracted from paths[0]


def test_write_file_normalizes_filename_str():
    """write_file called with `filename: "x.md"` gets path normalized."""
    tool = RecordingWriteTool()
    agent = _make_agent(tool)

    result = agent._execute_tool("write_file", {"filename": "x.md", "content": "hi"})

    assert result.success, result
    assert tool.calls[-1][0] == "x.md"


def test_write_file_still_fails_without_path():
    """No path-like key at all -> validation fails with a detailed message."""
    tool = RecordingWriteTool()
    agent = _make_agent(tool)

    result = agent._execute_tool("write_file", {"content": "hi"})

    assert not result.success
    assert "Empty file path" in result.error
    assert "content" in result.error  # error shows what keys were received
    assert "path" in result.error  # and what format is expected


def test_normalized_tool_call_flows_through_loop():
    """End-to-end: LLM sends paths to write_file -> normalized -> succeeds."""
    tool = RecordingWriteTool()
    llm = ScriptedLLM()
    import json as _json
    llm._responses = llm._wrap([
        ScriptedLLM.tool_use(None, "write_file", args=_json.dumps({"paths": ["README.md"], "content": "v1"})),
        "写好了。",
    ])
    agent = _make_agent(tool)
    agent.llm = llm

    result = agent.run("完善 README.md", conversation_id=None)

    assert "失败" not in result["content"]
    assert result["content"] == "写好了。"
    assert tool.calls[-1][0] == "README.md"


def test_validator_error_message_detail():
    """The validator error now names the received keys and expected format."""
    v = InputValidator(ValidatorConfig())
    ok, err = v.validate_tool_args("write_file", {"content": "hi"})
    assert not ok
    assert "Empty file path" in err
    assert "content" in err  # received keys
    assert "path" in err  # expected format mention


def test_nested_arguments_unwrapped():
    """LLM double-wrapped the args: {"arguments": "{\"path\":...}"} -> unwrap."""
    tool = RecordingWriteTool()
    agent = _make_agent(tool)

    result = agent._execute_tool(
        "write_file",
        {"arguments": '{"path": "nested.md", "content": "v2"}'},
    )
    assert result.success
    assert tool.calls[-1][0] == "nested.md"
    assert tool.calls[-1][1] == "v2"


def test_nested_arguments_double_wrapped():
    """Even triple-wrapped args recover: {"arguments": "{...}"}."""
    tool = RecordingWriteTool()
    agent = _make_agent(tool)

    result = agent._execute_tool(
        "write_file",
        {"arguments": '{"arguments": "{\\"path\\": \\"_wf_test.md\\", \\"content\\": \\"x\\"}"}'},
    )
    assert result.success
    assert tool.calls[-1][0] == "_wf_test.md"


def test_recover_write_args_from_text_json():
    """Empty tool args + JSON payload in response text -> recovered."""
    from agent.orchestrator import AgentOrchestrator
    args = AgentOrchestrator._extract_write_args_from_text(
        '好的，写入：{"path": "README.md", "content": "# hi"}'
    )
    assert args == {"path": "README.md", "content": "# hi"}


def test_recover_write_args_from_text_xml():
    from agent.orchestrator import AgentOrchestrator
    args = AgentOrchestrator._extract_write_args_from_text(
        '<write_file path="notes.md">内容一</write_file>'
    )
    assert args == {"path": "notes.md", "content": "内容一"}


def test_recover_write_args_returns_none_without_payload():
    from agent.orchestrator import AgentOrchestrator
    assert AgentOrchestrator._extract_write_args_from_text("普通文本") is None
    assert AgentOrchestrator._extract_write_args_from_text("") is None


def test_empty_args_recovered_in_loop(tmp_db):
    """End-to-end: model emits empty write_file args + JSON in text -> file written."""
    from agent.orchestrator import AgentOrchestrator, AgentConfig
    from agent.tools.registry import ToolRegistry
    from agent.tools.base import Tool, ToolResult
    from agent.llm_client import LLMResponse
    from conftest import ScriptedLLM

    class RecordingWrite(Tool):
        def __init__(self):
            self.calls = []
        @property
        def name(self): return "write_file"
        @property
        def description(self): return "write"
        @property
        def parameters_schema(self): return {"type": "object", "properties": {}}
        def execute(self, path="", content="", **kw) -> ToolResult:
            self.calls.append((path, content))
            if not path:
                return ToolResult(success=False, error="Empty file path")
            return ToolResult(success=True, output={"path": path})

    llm = ScriptedLLM(
        LLMResponse(
            type="tool_use",
            content='请写入: {"path": "out.md", "content": "recovered"}',
            tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "write_file", "arguments": "{}"},
            }],
            mode="native",
        ),
        "写好了。",
    )
    tool = RecordingWrite()
    reg = ToolRegistry(); reg.register(tool)
    agent = AgentOrchestrator(
        llm=llm, tools=reg,
        config=AgentConfig(permission_enabled=False, rate_limit_enabled=False,
                           input_validation_enabled=True, planning_enabled=False,
                           max_iterations=10),
        db=tmp_db,
    )
    result = agent.run("写文件")
    assert "失败" not in result["content"]
    assert tool.calls[-1] == ("out.md", "recovered")
