"""
Tests for MCP integration: manager lifecycle, tool adaptation, permissions
wiring, and presets registration (with a real stdio mock MCP server).

The mock server (tests/mock_mcp_server.py) speaks enough of the MCP stdio
protocol to list tools and answer tool calls.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.mcp.manager import (
    MCPManager,
    MCPServerConfig,
    parse_mcp_config,
    STATE_CONNECTED,
)
from agent.tools.mcp_tool import MCPTool
from agent.tools.registry import ToolRegistry
from agent.security.permissions import ToolPermission, ToolCategory, PermissionLevel

MOCK_SERVER = str(PROJECT_ROOT / "tests" / "mock_mcp_server.py")


def _wait_for(fn, timeout=15, interval=0.3):
    """Poll until fn() is truthy or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def stdio_config():
    return MCPServerConfig(
        name="mock",
        transport="stdio",
        command=sys.executable,
        args=[MOCK_SERVER],
        enabled=True,
        permission="user",
    )


# ============================================================
# Config parsing
# ============================================================

def test_parse_mcp_config_basic():
    cfgs = parse_mcp_config({
        "fs": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "enabled": True,
            "permission": "read-only",
        },
        "http1": {"transport": "http", "url": "http://localhost:8931/mcp"},
    })
    assert len(cfgs) == 2
    fs = next(c for c in cfgs if c.name == "fs")
    assert fs.command == "npx"
    assert fs.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    assert fs.permission == "read-only"
    h = next(c for c in cfgs if c.name == "http1")
    assert h.transport == "http" and h.url == "http://localhost:8931/mcp"


def test_parse_mcp_config_defaults_and_validation():
    cfgs = parse_mcp_config({
        "weird": {"transport": "tcp", "permission": "superadmin"},
    })
    assert len(cfgs) == 1
    assert cfgs[0].transport == "stdio"  # unknown transport falls back
    assert cfgs[0].permission == "user"  # unknown permission falls back


def test_parse_mcp_config_skips_non_dict():
    assert parse_mcp_config({"bad": "not a dict"}) == []


# ============================================================
# Manager lifecycle with the real mock server
# ============================================================

def test_manager_connects_and_lists_tools(stdio_config):
    mgr = MCPManager()
    mgr.configure([stdio_config])
    ok = _wait_for(lambda: any(
        s.status.state == STATE_CONNECTED for s in mgr._servers.values()
    ))
    assert ok, "mock server did not connect"
    tools = mgr.all_tools()
    names = {t["name"] for t in tools}
    assert {"echo", "add"} <= names
    mgr.shutdown()


def test_manager_call_tool(stdio_config):
    mgr = MCPManager()
    mgr.configure([stdio_config])
    assert _wait_for(lambda: mgr.all_tools())
    res = mgr.call_tool("mock", "add", {"a": 2, "b": 3})
    assert res["success"] is True
    assert res["output"] == "5"
    res2 = mgr.call_tool("mock", "echo", {"text": "hello mcp"})
    assert res2["success"] is True
    assert "hello mcp" in res2["output"]
    mgr.shutdown()


def test_manager_call_unknown_tool(stdio_config):
    mgr = MCPManager()
    mgr.configure([stdio_config])
    assert _wait_for(lambda: mgr.all_tools())
    res = mgr.call_tool("mock", "nope", {})
    assert res["success"] is False
    assert "unknown tool" in res.get("output", "") or "unknown tool" in res.get("error", "")
    mgr.shutdown()


def test_manager_call_unconfigured_server():
    mgr = MCPManager()
    res = mgr.call_tool("ghost", "x", {})
    assert res["success"] is False
    assert "not configured" in res["error"]


def test_manager_statuses(stdio_config):
    mgr = MCPManager()
    mgr.configure([stdio_config])
    assert _wait_for(lambda: mgr.all_tools())
    statuses = mgr.statuses()
    assert statuses[0]["name"] == "mock"
    assert statuses[0]["state"] == STATE_CONNECTED
    assert statuses[0]["tools"] >= 2
    mgr.shutdown()


# ============================================================
# MCPTool adapter
# ============================================================

def test_mcp_tool_registers_and_executes(stdio_config):
    mgr = MCPManager()
    mgr.configure([stdio_config])
    assert _wait_for(lambda: mgr.all_tools())

    registry = ToolRegistry()
    tool = MCPTool(
        manager=mgr,
        server_name="mock",
        tool_name="add",
        description="add numbers",
        input_schema={"type": "object", "properties": {}},
    )
    registry.register(tool)
    assert registry.has("mock__add")

    result = tool.execute(a=10, b=32)
    assert result.success is True
    assert result.output == "42"
    assert result.metadata.get("mcp_server") == "mock"

    schema = tool.to_prompt_schema()
    assert schema["function"]["name"] == "mock__add"
    mgr.shutdown()


def test_mcp_tool_not_retryable():
    mgr = MCPManager()
    tool = MCPTool(manager=mgr, server_name="s", tool_name="t")
    assert tool.retryable is False  # side effects unknown -> no auto-retry


# ============================================================
# Permissions wiring
# ============================================================

def test_mcp_tools_require_permission(stdio_config):
    mgr = MCPManager()
    mgr.configure([stdio_config])
    assert _wait_for(lambda: mgr.all_tools())

    perm = ToolPermission()
    perm.register_user("bob", PermissionLevel.USER)

    # Tool without category override -> denied for USER (not in defaults)
    allowed, _ = perm.can_use_tool("bob", "mock__add")
    assert not allowed

    # Read-only servers map to READ -> allowed for USER
    perm.add_category_override("mock__echo", ToolCategory.READ)
    allowed, _ = perm.can_use_tool("bob", "mock__echo")
    assert allowed
    mgr.shutdown()


def test_readonly_server_maps_to_read_category(stdio_config):
    from agent.mcp.manager import parse_mcp_config

    cfgs = parse_mcp_config({
        "mockro": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [MOCK_SERVER],
            "permission": "read-only",
        }
    })
    mgr = MCPManager()
    mgr.configure(cfgs)
    assert _wait_for(lambda: mgr.all_tools())

    perm = ToolPermission()
    perm.register_user("bob", PermissionLevel.USER)
    for t in mgr.all_tools():
        perm.add_category_override(f"{t['server']}__{t['name']}", ToolCategory.READ)
    allowed, _ = perm.can_use_tool("bob", "mockro__add")
    assert allowed
    mgr.shutdown()


# ============================================================
# Presets wiring
# ============================================================

def test_presets_register_mcp_tools(tmp_db, monkeypatch):
    """create_agent with mcp_servers config registers MCP tools + manager."""
    import yaml
    from agent.presets import create_agent

    config = {
        "mcp_enabled": True,
        "mcp_servers": {
            "mock": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [MOCK_SERVER],
                "enabled": True,
            }
        },
    }
    # AgentConfig takes mcp_servers as dict; use create_agent kwargs
    class FakeLLM:
        backend_name = "Fake"
        model_name = "fake"

        def chat(self, **kw):
            from agent.llm_client import LLMResponse
            return LLMResponse(type="text", content="ok", mode="native")

    agent = create_agent(
        llm_client=FakeLLM(),
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        mcp_enabled=True,
        mcp_servers=config["mcp_servers"],
    )
    assert agent.mcp_manager is not None
    # Wait for the mock server to connect and tools to sync
    ok = _wait_for(lambda: any("mock__" in n for n in agent.tools.list_names()))
    assert ok, f"MCP tools not registered; have: {agent.tools.list_names()}"
    assert "mock__add" in agent.tools.list_names()
    assert "mock__echo" in agent.tools.list_names()


def test_presets_no_mcp_when_disabled(tmp_db):
    from agent.presets import create_agent

    class FakeLLM:
        backend_name = "Fake"
        model_name = "fake"

        def chat(self, **kw):
            from agent.llm_client import LLMResponse
            return LLMResponse(type="text", content="ok", mode="native")

    agent = create_agent(
        llm_client=FakeLLM(),
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        mcp_enabled=False,
        mcp_servers={"mock": {"transport": "stdio", "command": "x"}},
    )
    assert not any(n.startswith("mock__") for n in agent.tools.list_names())


def test_presets_tolerate_missing_mcp_lib(tmp_db, monkeypatch):
    """If the mcp package is missing, agent creation still works."""
    import agent.presets as presets_mod
    from agent.presets import create_agent

    monkeypatch.setattr(presets_mod, "_MCP_AVAILABLE", False)

    class FakeLLM:
        backend_name = "Fake"
        model_name = "fake"

        def chat(self, **kw):
            from agent.llm_client import LLMResponse
            return LLMResponse(type="text", content="ok", mode="native")

    agent = create_agent(
        llm_client=FakeLLM(),
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        mcp_enabled=True,
        mcp_servers={"mock": {"transport": "stdio"}},
    )
    assert agent is not None
