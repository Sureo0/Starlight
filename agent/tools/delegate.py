"""
SubagentTool - delegate a focused sub-task to a nested agent.

The parent agent calls `delegate(task=..., mode=...)` and blocks while a
child agent runs with a RESTRICTED tool subset and its own budget. The
child's final answer (plus a summary of its internal activity) is returned
as the tool result, so the parent can reason about it without seeing the
child's full transcript.

Design decisions:
  - Synchronous: the parent blocks until the child finishes. This keeps the
    existing single-loop architecture intact and avoids shared-state races.
  - Mode-based capability control:
        research -> read-only tools (files, web search, memory, ...)
        code     -> + write_file / execute_code (sandboxed)
        full     -> everything the parent has (minus delegate itself)
    The child NEVER receives the delegate tool, so sub-agents cannot nest
    unboundedly through this tool.
  - Budget: the child gets its own max_tool_calls / execution_timeout, and
    additionally reports how many tool calls it made (subagent_calls) so the
    parent can deduct a proportional share from its own budget.
  - Trace: the child run is recorded as a separate AgentTrace (trace_sink);
    its summary is embedded in the delegate tool result, and the parent's
    trace recorder marks delegate calls so the traces UI can render a
    child-agent card.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from agent.orchestrator import AgentConfig, AgentOrchestrator
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

logger = logging.getLogger("agent.tools.delegate")

# ---------------------------------------------------------------------------
# Mode -> tool subset mapping (capability control)
# ---------------------------------------------------------------------------

# Tools a research-only child may use (everything read-only / safe).
RESEARCH_TOOLS = {
    "memory_query",
    "memory_list",
    "list_files",
    "read_file",
    "read_files",
    "web_search",
    "get_weather",
    "chat_completion",
}

# Tools a code-mode child may use: research + local file writes + sandboxed
# code execution.
CODE_TOOLS = RESEARCH_TOOLS | {"write_file", "execute_code"}

MODE_TOOLS: dict[str, set[str]] = {
    "research": RESEARCH_TOOLS,
    "code": CODE_TOOLS,
    # "full" is resolved at build time against the parent's registry.
}

# How many parent-budget tool calls a delegate call is worth (per mode).
# Weighed by how much work a child in that mode can burn through.
MODE_QUOTA: dict[str, int] = {
    "research": 1,
    "code": 2,
    "full": 3,
}

VALID_MODES = tuple(MODE_TOOLS)


def resolve_subagent_tools(
    parent_registry: ToolRegistry, mode: str
) -> ToolRegistry:
    """Build the child's tool registry from the parent's (no delegate tool)."""
    names = set(MODE_TOOLS.get(mode, RESEARCH_TOOLS))
    if mode == "full":
        names = set(parent_registry.list_names())
    names.discard("delegate")  # children never spawn their own children
    names.discard("spawn_agent")  # legacy placeholder, never registered anyway
    subset = ToolRegistry()
    for name in names:
        tool = parent_registry.get(name)
        if tool is not None:
            subset.register(tool)
    return subset


def build_child_config(
    parent_config: AgentConfig, mode: str, remaining_timeout: float
) -> AgentConfig:
    """Child config: planning off, own budgets, timeout capped by the parent's.

    Security (permissions/rate-limit/validation) is inherited by the child
    through the shared permission registry + rate limiter passed at runtime;
    the child runs the same loop machinery as the parent.
    """
    timeout = max(10.0, min(float(parent_config.execution_timeout), remaining_timeout))
    return AgentConfig(
        system_prompt=_SUBAGENT_SYSTEM_PROMPT,
        max_tool_calls=max(5, min(parent_config.max_tool_calls, 25)),
        execution_timeout=int(timeout),
        planning_enabled=False,  # children stay focused; no plan overhead
        plan_review_enabled=False,
        compression_enabled=False,  # children are short-lived; no summarization
        memory_enabled=parent_config.memory_enabled,
        memory_auto_extract=False,  # no long-term extraction from sub-tasks
        memory_inject_count=parent_config.memory_inject_count,
        memory_inject_min_importance=parent_config.memory_inject_min_importance,
        security_enabled=False,  # runtime components (permissions/limiter) are
        # attached explicitly by the executor instead
        tools_enabled=True,
        max_tokens=parent_config.max_tokens,
        temperature=parent_config.temperature,
    )


_SUBAGENT_SYSTEM_PROMPT = (
    "你是一个被上级代理委派的子代理，专注完成被分配的子任务。\n"
    "规则：\n"
    "1. 只处理任务描述中要求的内容，不要擅自扩大任务范围。\n"
    "2. 优先使用工具收集信息或执行操作，必要时才直接回答。\n"
    "3. 完成任务后，用简洁的中文给出最终答案，直接面向父代理，不要提及"
    "'我是子代理'等元信息。\n"
    "4. 如果任务无法完成，明确说明原因和已尝试的做法。"
)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class SubagentExecutor:
    """Runs one child agent synchronously and returns its result summary."""

    def __init__(
        self,
        llm,
        parent: AgentOrchestrator,
        workspace_dir: str,
        username: str | None,
        trace_sink: Callable | None = None,
    ):
        self._llm = llm
        self._parent = parent
        self._workspace_dir = workspace_dir
        self._username = username
        self._trace_sink = trace_sink

    def run(self, task: str, mode: str, remaining_timeout: float) -> dict:
        """Run a child agent. Returns a summary dict (never raises)."""
        started = time.time()
        subagent_id = uuid.uuid4().hex[:8]

        child_registry = resolve_subagent_tools(self._parent.tools, mode)
        child_config = build_child_config(self._parent.config, mode, remaining_timeout)

        child = AgentOrchestrator(
            llm=self._llm,
            tools=child_registry,
            config=child_config,
            db=self._parent.db,
            username=self._username,
        )

        # Inherit the parent's security components so children are subject to
        # the same permission rules and rate limits as the parent agent.
        child._permissions = self._parent._permissions
        child._rate_limiter = self._parent._rate_limiter
        child._validator = self._parent._validator

        # Children run with their own recorder (created by run()); the executor
        # inspects finish_reason to judge success, and the sink persists the
        # child's trace so the delegate result's trace_id resolves in the UI.
        child.trace_recorder = None  # run() attaches a fresh one
        sink = self._trace_sink or getattr(self._parent, "trace_sink", None)
        child.trace_sink = sink

        # Don't let a child's own planning loop touch the parent's tracker.
        child.plan_generator = None
        child.plan_reviewer = None

        # Children inherit memory injection (context), but no extractor.
        if self._parent.memory_service is not None:
            child.memory_service = self._parent.memory_service

        logger.info(
            "subagent[%s] mode=%s tools=%d starting: %.60s",
            subagent_id, mode, len(child_registry), task.replace("\n", " "),
        )

        try:
            result = child.run(task)
            # run() does NOT return finish_reason; read it from the child's
            # trace recorder (finish_reason lives on the AgentTrace).
            finish_reason = _child_finish_reason(child) or "text_response"
            content = str(result.get("content", ""))
            tool_calls_made = int(result.get("tool_calls_made", 0))
            iterations = int(result.get("iterations", 0))
            summary = {
                "subagent_id": subagent_id,
                "mode": mode,
                "content": content,
                "tool_calls_made": tool_calls_made,
                "iterations": iterations,
                "plan": result.get("plan"),
                "duration": round(time.time() - started, 2),
                "trace_id": _child_trace_id(child),
                "finish_reason": finish_reason,
            }
            if finish_reason not in ("text_response", "no_valid_tool_calls"):
                # The child failed (timeout / loop / budget / error ...). The
                # parent still sees the content, but the tool marks the call
                # as failed so the parent knows to treat it as a soft failure.
                summary["error"] = f"子代理未正常完成 ({finish_reason})"
            if tool_calls_made >= child_config.max_tool_calls:
                summary["truncated"] = True
            return summary
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception("subagent[%s] crashed", subagent_id)
            return {
                "subagent_id": subagent_id,
                "mode": mode,
                "content": "",
                "error": f"子代理内部错误: {exc}",
                "tool_calls_made": 0,
                "iterations": 0,
                "duration": round(time.time() - started, 2),
            }


def _child_trace_id(child: AgentOrchestrator) -> str | None:
    """Best-effort: return the child trace id if one was recorded."""
    try:
        if child.trace_recorder is not None:
            return child.trace_recorder.trace.trace_id
    except Exception:
        pass
    return None


def _child_finish_reason(child: AgentOrchestrator) -> str | None:
    """Best-effort: return the child's finish_reason from its trace."""
    try:
        if child.trace_recorder is not None:
            return child.trace_recorder.trace.finish_reason
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class SubagentTool(Tool):
    """delegate — run a focused sub-task in a nested agent (sync)."""

    retryable = False  # child runs may have side effects; never auto-retry

    name = "delegate"

    description = (
        "把任务分解出一个可以独立完成的子任务，交给一个子代理去执行。"
        "子代理拥有受限的工具集（research=只读研究；code=可读写文件并执行沙箱代码；"
        "full=全部工具）。适用于：独立的调研/查证、需要写代码或文件的重活、"
        "可以并行思考的独立子问题。返回子代理的最终回答和工具调用统计。"
        "注意：子代理无法再派生子代理；不要用 delegate 做简单任务。"
    )

    parameters_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要交给子代理的完整任务描述，必须自包含（子代理看不到本对话的上下文）。",
            },
            "mode": {
                "type": "string",
                "enum": list(VALID_MODES),
                "description": (
                    "子代理的能力范围：research=只读（搜索/读文件/查记忆）；"
                    "code=额外可写文件和执行沙箱代码；full=父代理全部工具。"
                ),
            },
        },
        "required": ["task"],
    }

    def __init__(
        self,
        llm,
        parent: AgentOrchestrator,
        workspace_dir: str,
        username: str | None = None,
        trace_sink: Callable | None = None,
        max_timeout: float = 300.0,
    ):
        self._executor = SubagentExecutor(
            llm, parent, workspace_dir, username, trace_sink
        )
        self._max_timeout = max_timeout

    def execute(self, task: str | None = None, mode: str = "research", **kwargs) -> ToolResult:
        if not task or not str(task).strip():
            return ToolResult(
                success=False,
                error="delegate 需要 task 参数（要交给子代理的任务描述）",
            )
        if mode not in VALID_MODES:
            return ToolResult(
                success=False,
                error=(
                    f"未知模式 '{mode}'，可选: {', '.join(VALID_MODES)}"
                ),
            )

        remaining = self._max_timeout
        summary = self._executor.run(str(task), mode, remaining)

        if summary.get("error"):
            # Child failed (crash, timeout, loop, ...). The parent still gets
            # the child's partial content in metadata so it can decide whether
            # to retry differently.
            return ToolResult(
                success=False,
                error=summary["error"],
                output=summary.get("content") or "",
                metadata={
                    "subagent_calls": summary.get("tool_calls_made", 0),
                    "subagent_mode": mode,
                    "subagent_id": summary.get("subagent_id"),
                    "trace_id": summary.get("trace_id"),
                    "subagent_finish_reason": summary.get("finish_reason"),
                },
            )

        return ToolResult(
            success=True,
            output={
                "subagent_answer": summary.get("content", ""),
                "subagent_id": summary.get("subagent_id"),
                "mode": mode,
                "tool_calls_made": summary.get("tool_calls_made", 0),
                "iterations": summary.get("iterations", 0),
                "truncated": summary.get("truncated", False),
                "duration": summary.get("duration"),
            },
            metadata={
                "subagent_calls": summary.get("tool_calls_made", 0),
                "subagent_mode": mode,
                "subagent_id": summary.get("subagent_id"),
                "trace_id": summary.get("trace_id"),
            },
        )
