"""
ToolRegistry - Central registry for all agent tools.

The registry holds tool instances by name and provides methods to:
  - Register/unregister tools
  - Look up tools by name
  - Generate the full tool schema list for LLM prompts
  - List available tool names
"""

from __future__ import annotations

import logging
from typing import Sequence

from agent.tools.base import Tool

logger = logging.getLogger("agent.tools.registry")


class ToolRegistry:
    """
    Central registry for agent tools.

    Usage:
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(WebSearchTool())

        # Get all schemas for LLM
        tools_schema = registry.all_schemas()

        # Execute a tool by name
        result = registry.execute("web_search", query="python asyncio")
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Overwrites if name already exists."""
        if tool.name in self._tools:
            logger.warning("Overwriting existing tool: %s", tool.name)
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it was found and removed."""
        if name in self._tools:
            del self._tools[name]
            logger.info("Unregistered tool: %s", name)
            return True
        return False

    def get(self, name: str) -> Tool | None:
        """Get a tool by name, or None if not found."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def list_names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    def all_schemas(self) -> list[dict]:
        """
        Return all tool schemas in OpenAI function-calling format.

        This list is passed as the `tools` parameter in LLM API calls.
        """
        return [tool.to_prompt_schema() for tool in self._tools.values()]

    def all_tools(self) -> list[Tool]:
        """Return all registered tool instances."""
        return list(self._tools.values())


    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __repr__(self) -> str:
        names = ", ".join(self._tools.keys())
        return f"<ToolRegistry[{len(self._tools)}]: {names}>"
