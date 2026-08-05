"""
Tool base class and ToolResult data structure.

Every agent tool must inherit from Tool and implement:
  - name: unique identifier (e.g. "read_file")
  - description: human-readable description (injected into LLM system prompt)
  - parameters_schema: JSON Schema dict describing the tool's parameters
  - execute(**kwargs) -> ToolResult: the actual tool logic
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent.tools")


@dataclass
class ToolResult:
    """Standard result returned by every tool execution."""

    success: bool
    output: Any = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict."""
        d = {"success": self.success}
        if self.output is not None:
            d["output"] = self.output
        if self.error is not None:
            d["error"] = self.error
        if self.metadata:
            d["metadata"] = self.metadata
        return d


class Tool(ABC):
    """
    Abstract base class for all agent tools.

    Subclasses must implement the four abstract members and the execute() method.
    The base class provides to_prompt_schema() for free, which converts the tool
    into the OpenAI function-calling format that LLMs understand.
    """

    # Whether the tool is safe to retry on transient failures.
    #
    # Read-only tools (web_search, get_weather, read_file, ...) are idempotent
    # and safe to retry. Write tools (write_file, memory_store, execute_code,
    # chat_completion) may have side effects; their subclasses override this to
    # False so the orchestrator never blindly re-executes them.
    retryable: bool = True

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name, e.g. 'read_file', 'web_search'."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Description injected into the LLM system prompt. Be precise and concise."""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict:
        """
        JSON Schema dict describing the tool's parameters.

        Example:
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query"
                    }
                },
                "required": ["query"]
            }
        """
        ...

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """
        Execute the tool with the given arguments.

        Arguments come from the LLM's tool_call JSON. The tool should validate
        them and return a ToolResult. Exceptions should be caught internally
        and returned as ToolResult(success=False, error=...).
        """
        ...

    def to_prompt_schema(self) -> dict:
        """
        Convert to OpenAI function-calling schema format.

        This is what gets injected into the LLM's `tools` parameter:
            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": { ... JSON Schema ... }
                }
            }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    def __repr__(self) -> str:
        return f"<Tool:{self.name}>"
