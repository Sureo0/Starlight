"""
Agent Tools - Base classes and registry.

All agent tools inherit from Tool and follow a unified protocol,
enabling the orchestrator to call any tool interchangeably.
"""

from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

__all__ = ["Tool", "ToolResult", "ToolRegistry"]
