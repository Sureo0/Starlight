"""
MCPTool - adapts a remote MCP tool to the agent's Tool interface.

Registered in the ToolRegistry under the name "<server>__<tool>", so the
rest of the agent (permissions, retries, loop guards, tracing) treats MCP
tools exactly like built-in ones. The orchestrator never needs to know a
tool came from MCP.
"""

from __future__ import annotations

import logging
import time

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("agent.tools.mcp")


class MCPTool(Tool):
    """A tool backed by a call on an MCP server."""

    # MCP tools may have side effects we can't know about — don't auto-retry.
    retryable: bool = False

    def __init__(self, manager, server_name: str, tool_name: str,
                 description: str = "", input_schema: dict | None = None,
                 permission: str = "user"):
        self._manager = manager
        self._server = server_name
        self._tool = tool_name
        self._desc = description or f"MCP tool {server_name}/{tool_name}"
        self._schema = input_schema or {"type": "object", "properties": {}}
        self.permission = permission

    @property
    def name(self) -> str:
        return f"{self._server}__{self._tool}"

    @property
    def server_name(self) -> str:
        return self._server

    @property
    def tool_name(self) -> str:
        return self._tool

    @property
    def description(self) -> str:
        return (
            f"{self._desc} "
            f"[MCP server: {self._server}]"
        )

    @property
    def parameters_schema(self) -> dict:
        return self._schema

    def execute(self, **kwargs) -> ToolResult:
        start = time.time()
        result = self._manager.call_tool(self._server, self._tool, kwargs)
        duration = time.time() - start

        if result.get("success"):
            return ToolResult(
                success=True,
                output=result.get("output"),
                metadata={
                    "mcp_server": self._server,
                    "mcp_tool": self._tool,
                    "duration": round(duration, 3),
                },
            )
        return ToolResult(
            success=False,
            output=result.get("output"),
            error=result.get("error") or "MCP tool call failed",
            metadata={
                "mcp_server": self._server,
                "mcp_tool": self._tool,
                "duration": round(duration, 3),
            },
        )
