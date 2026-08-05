"""
Observability: agent run tracing (execution replay).

Records every LLM call, tool invocation, plan, memory injection and
termination reason for an agent run into a local trace store so runs can be
reviewed and replayed from the /traces page.
"""

from agent.observability.trace_recorder import (
    TraceRecorder,
    TraceEvent,
    AgentTrace,
    FINISH_REASONS,
)
from agent.observability.storage import TraceStore

__all__ = [
    "TraceRecorder",
    "TraceEvent",
    "AgentTrace",
    "TraceStore",
    "FINISH_REASONS",
]
