"""
Agent Orchestrator - The ReAct loop engine.

Supports dual function calling modes:
  - Native: tool results use the 'tool' role
  - Prompt: tool results use special XML tags within 'user' role messages

The orchestrator auto-adapts message formatting based on the response mode.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Generator

from agent.llm_client import AgentLLMClient, LLMResponse, TOOL_RESULT_OPEN, TOOL_RESULT_CLOSE
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry
from agent.memory.context_manager import ContextManager, sanitize_tool_roundtrips
from agent.security.permissions import ToolPermission, PermissionLevel
from agent.security.rate_limiter import RateLimiter, RateLimitConfig
from agent.security.validator import InputValidator, ValidatorConfig

try:
    from agent.memory.service import MemoryService
    from agent.memory.extractor import MemoryExtractor
    _MEMORY_AVAILABLE = True
except ImportError:  # pragma: no cover
    MemoryService = None
    MemoryExtractor = None
    _MEMORY_AVAILABLE = False

try:
    from agent.planning.planner import Plan, PlanTracker, PlanGenerator, StepReviewer
    _PLANNING_AVAILABLE = True
except ImportError:  # pragma: no cover
    Plan = None
    PlanTracker = None
    PlanGenerator = None
    StepReviewer = None
    _PLANNING_AVAILABLE = False

try:
    from agent.retry import RetryConfig, should_retry, compute_delay
    _RETRY_AVAILABLE = True
except ImportError:  # pragma: no cover
    RetryConfig = None
    should_retry = None
    compute_delay = None
    _RETRY_AVAILABLE = False

try:
    from agent.observability.trace_recorder import TraceRecorder
    _OBSERVABILITY_AVAILABLE = True
except ImportError:  # pragma: no cover
    TraceRecorder = None
    _OBSERVABILITY_AVAILABLE = False

# Context compression (LLM summarization of old messages)
try:
    from agent.compression.manager import CompressionManager
    from agent.compression.summarizer import ContextSummarizer
    _COMPRESSION_AVAILABLE = True
except ImportError:  # pragma: no cover
    CompressionManager = None
    ContextSummarizer = None
    _COMPRESSION_AVAILABLE = False

# Sub-agent (delegate) support: quota charged to the parent per mode.
# Lazy import keeps agent.orchestrator importable without the tool module.
try:
    from agent.tools.delegate import MODE_QUOTA
except ImportError:  # pragma: no cover
    MODE_QUOTA = {"research": 1, "code": 2, "full": 3}

try:
    from agent.approval import ApprovalManager, PENDING, APPROVED, REJECTED, EXPIRED
    _APPROVAL_AVAILABLE = True
except ImportError:  # pragma: no cover
    ApprovalManager = None
    PENDING = APPROVED = REJECTED = EXPIRED = None
    _APPROVAL_AVAILABLE = False

try:
    from agent.cancellation import (
        CancellationManager, CancellationRequest,
        DIRECT, CONFIRM,
        PENDING as C_PENDING, APPROVED as C_APPROVED, DENIED as C_DENIED,
        manager as _default_cancel_manager,
    )
    _CANCELLATION_AVAILABLE = True
except ImportError:  # pragma: no cover
    CancellationManager = None  # type: ignore
    CancellationRequest = None  # type: ignore
    DIRECT = CONFIRM = C_PENDING = C_APPROVED = C_DENIED = None  # type: ignore
    _default_cancel_manager = None  # type: ignore
    _CANCELLATION_AVAILABLE = False

logger = logging.getLogger("agent.orchestrator")


# ============================================================
# Agent Configuration
# ============================================================

@dataclass
class AgentConfig:
    """Configuration for the agent's behavior."""

    system_prompt: str = "You are a helpful AI assistant."
    max_iterations: int = 999  # Effectively unlimited (controlled by max_tool_calls instead)
    max_tool_calls: int = 300  # Max total tool calls per request (cost safety)
    max_tokens: int = 4096  # Max tokens in LLM response
    temperature: float = 0.7  # Sampling temperature
    context_window: int = 32000  # Total token budget for context
    tools_enabled: bool = True  # Whether to enable tool calling
    tool_choice: str | dict = "auto"  # "auto", "none", "required"
    use_prompt_tool_calls: bool = False  # Force prompt-based mode

    # Security settings
    security_enabled: bool = True  # Enable security checks
    rate_limit_enabled: bool = True  # Enable rate limiting
    permission_enabled: bool = True  # Enable tool permissions
    input_validation_enabled: bool = True  # Enable input validation

    # Long-term memory settings
    memory_enabled: bool = True  # Enable automatic memory injection
    memory_auto_extract: bool = True  # Extract memories after each turn
    memory_inject_count: int = 4  # Max memories injected into system prompt
    memory_inject_min_importance: int = 2  # Only inject memories >= this importance
    memory_quality_gate: bool = True  # Filter AI-self/generic extraction errors
    memory_consolidate: bool = True  # Merge near-duplicate memories periodically

    # Planning (Plan-then-Execute) settings
    planning_enabled: bool = True  # Enable plan generation for complex tasks
    plan_min_user_chars: int = 30  # Min user message length to trigger planning

    # Plan progress review (LLM re-check of step completion)
    plan_review_enabled: bool = True  # Enable LLM review of plan progress
    plan_review_every: int = 3  # Review after every N tool calls
    plan_review_max_calls: int = 4  # Cap on review calls per run

    # Tool retry settings
    tool_retry_enabled: bool = True  # Retry transient tool failures
    tool_retry_max: int = 2  # Max retries per tool call (total attempts = max + 1)
    tool_retry_base_delay: float = 0.5  # Exponential backoff base (seconds)

    # Execution settings
    execution_timeout: int = 600  # Overall agent loop timeout in seconds (was 120)

    # Sub-agent (delegate) settings
    subagent_max_tool_calls: int = 25  # Child's own tool-call budget per run
    subagent_max_duration: int = 300  # Child's own execution timeout (seconds)

    # Human-in-the-loop approval (side-effectful tools)
    # NOTE: opt-in — the app enables it explicitly. Default OFF keeps
    # library/test usage unchanged (no approval pauses, no blocks).
    approval_enabled: bool = False  # Ask the human before side-effectful tools
    approval_tools: tuple = ("write_file", "execute_code", "memory_forget", "delegate")
    approval_expiry: int = 300  # Seconds a pending request waits before expiring
    approval_auto_approve: bool = False  # Skip confirmation entirely
    approval_remember: bool = True  # Remember decisions within ONE run: after
    # the user approves a tool once, further calls of the same tool in this
    # run proceed without asking again (rejected tools stay rejected).
    # Memory is reset at the start of every run()/run_stream().

    # MCP (Model Context Protocol) integration
    mcp_enabled: bool = True  # Enable external MCP servers
    mcp_servers: dict = field(default_factory=dict)  # Parsed mcp_servers config

    # Context compression (LLM summarization of older messages)
    compression_enabled: bool = True  # Enable compression (no-op if summarizer absent)
    compression_trigger_ratio: float = 0.75  # Compress when history exceeds budget * ratio
    compression_min_messages: int = 10  # Never compress tiny histories
    compression_keep_recent: int = 6  # Recent messages always kept verbatim
    compression_min_gain_tokens: int = 800  # Only compress if it actually saves tokens

    # Cancellation (human stop / confirm-cancel for task mode)
    cancellation_enabled: bool = True  # Enable user cancellation of running agent
    cancel_confirm_required: bool = True  # Task mode: ask before cancelling
    cancel_expiry: int = 120  # Seconds a confirm-cancel request waits


# ============================================================
# Agent Events (for streaming / UI)
# ============================================================

@dataclass
class AgentEvent:
    """An event produced during agent execution."""

    type: str  # "thinking", "tool_call", "tool_result", "text", "done", "error"
    content: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    tool_result: dict = field(default_factory=dict)
    iteration: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"type": self.type}
        if self.content:
            d["content"] = self.content
        if self.tool_name:
            d["tool"] = self.tool_name
        if self.tool_args:
            d["args"] = self.tool_args
        if self.tool_result:
            d["result"] = self.tool_result
        if self.iteration:
            d["iteration"] = self.iteration
        if self.metadata:
            d["metadata"] = self.metadata
        return d


# ============================================================
# Orchestrator
# ============================================================

class AgentOrchestrator:
    """
    The core agent loop engine.

    Implements the ReAct (Reason + Act) pattern:
        User message -> LLM reasoning -> Tool calls -> Results -> LLM reasoning -> ... -> Final answer

    Automatically handles native vs prompt-based function calling.
    """

    def __init__(
        self,
        llm: AgentLLMClient,
        tools: ToolRegistry,
        config: AgentConfig | None = None,
        db=None,
        username: str | None = None,
    ):
        """
        Args:
            llm: The LLM client for making API calls.
            tools: Registry of available tools.
            config: Agent configuration. Uses defaults if None.
            db: Database proxy for message persistence (optional).
            username: Current user for permission checks (optional).
        """
        self.llm = llm
        self.tools = tools
        self.config = config or AgentConfig()
        self.db = db
        self.username = username or "anonymous"
        self.context = ContextManager(max_tokens=self.config.context_window)
        # Context compression: summarizes older messages via the LLM when the
        # history exceeds the trigger threshold. Fail-soft: if the summarizer
        # is unavailable or errors, compression silently does nothing.
        self.compression = None
        if _COMPRESSION_AVAILABLE and self.config.compression_enabled:
            try:
                self.compression = CompressionManager(
                    llm=llm,
                    budget=self.config.context_window,
                    trigger_ratio=self.config.compression_trigger_ratio,
                    min_messages=self.config.compression_min_messages,
                    keep_recent=self.config.compression_keep_recent,
                    min_gain_tokens=self.config.compression_min_gain_tokens,
                    persist_store=db,
                    hook=self._compression_hook if _OBSERVABILITY_AVAILABLE else None,
                )
            except Exception:
                logger.exception("CompressionManager init failed; continuing without it")
                self.compression = None

        # Security components
        self._permissions = ToolPermission()
        self._rate_limiter = RateLimiter()
        self._validator = InputValidator()

        # Long-term memory components (optional)
        self.memory_service: MemoryService | None = None
        self.memory_extractor: MemoryExtractor | None = None

        # Planning components (optional)
        self.plan_generator: PlanGenerator | None = None
        self.plan_reviewer: StepReviewer | None = None
        self.plan_tracker: PlanTracker | None = None
        self._last_plan_tracker: PlanTracker | None = None

        # Observability (optional)
        self.trace_recorder: TraceRecorder | None = None
        # Callable(trace: AgentTrace) -> None called when a run finishes
        # (used by the app to persist traces to the store; fail-soft)
        self.trace_sink = None

        # Human-in-the-loop approval (optional, wired by the app)
        self.approval_manager: ApprovalManager | None = None
        # Cancellation manager (optional; the app wires the shared singleton)
        self.cancellation_manager = (
            _default_cancel_manager if _CANCELLATION_AVAILABLE else None
        )
        # run_id for the CURRENT run; cancellation checkpoints poll this
        self.current_run_id: str | None = None
        # Attachment metadata for the current run's user message (persisted
        # with the message); set by _build_initial_messages.
        self._current_attachments: list | None = None
        # Per-run approval decision cache: tool name -> True (approved) /
        # False (rejected). When approval_remember is on, the user's decision
        # for a tool applies to ALL subsequent calls of that tool within the
        # SAME run — long tasks stop nagging after the first confirmation.
        # Reset at the start of every run()/run_stream().
        self._approval_memory: dict[str, bool] = {}

    def _reset_approval_memory(self) -> None:
        """Clear per-run approval decisions (called at run start)."""
        self._approval_memory = {}

    # ----------------------------------------------------------
    # Cancellation support
    # ----------------------------------------------------------

    def _new_run_id(self) -> str:
        """Generate a unique run id for cancellation tracking."""
        return uuid.uuid4().hex[:16]

    def _cancel_requested(self) -> bool:
        """Checkpoint: should the current run abort now (direct or approved)?"""
        if not self.config.cancellation_enabled:
            return False
        mgr = self.cancellation_manager
        if mgr is None or self.current_run_id is None:
            return False
        try:
            return mgr.should_stop(self.current_run_id)
        except Exception:
            return False

    def _cancel_status(self) -> str | None:
        """Current cancellation status for this run (for events/traces)."""
        if not self.config.cancellation_enabled:
            return None
        mgr = self.cancellation_manager
        if mgr is None or self.current_run_id is None:
            return None
        try:
            req = mgr.get(self.current_run_id)
            return req.status if req else None
        except Exception:
            return None

    def attach_recorder(self, recorder) -> None:
        """Attach a TraceRecorder to capture this agent's runs (fail-soft)."""
        self.trace_recorder = recorder
        if recorder is not None:
            recorder.attach()

    def _plan_result_dict(self, plan_tracker) -> dict:
        """Build the 'plan' field for run() results (empty dict when inactive)."""
        if plan_tracker is None or not plan_tracker.active:
            return {}
        try:
            return plan_tracker.to_dict()
        except Exception:
            return {}

    def _build_result(self, plan_tracker, **kwargs) -> dict:
        """Standard result dict with optional plan info."""
        result = dict(kwargs)
        plan = self._plan_result_dict(plan_tracker)
        if plan:
            result["plan"] = plan
        return result

    def _trace_finish(self, reason: str, detail: str = "", success: bool = False,
                      content: str = "", tool_calls_made: int = 0,
                      iterations: int = 0) -> None:
        """Finish the trace for this run and hand it to the sink (fail-soft)."""
        try:
            if self.trace_recorder is not None:
                self.trace_recorder.finish(
                    reason, detail, success, content, tool_calls_made, iterations
                )
                trace = self.trace_recorder.trace
                if self.trace_sink is not None:
                    self.trace_sink(trace)
        except Exception:
            logger.debug("trace finish failed", exc_info=True)

    def _run_stream_trace(self, user_message: str, conversation_id: str | None) -> None:
        """Create and start a trace recorder for streaming runs."""
        if not _OBSERVABILITY_AVAILABLE:
            return
        self.trace_recorder = TraceRecorder().attach()
        self.trace_recorder.report_start(
            user_message, self.username, conversation_id,
            backend=getattr(self.llm, "backend_name", "") or "",
            model=getattr(self.llm, "model_name", "") or "",
        )

    # ----------------------------------------------------------
    # Sub-agent (delegate) support
    # ----------------------------------------------------------

    def _charge_subagent_quota(self, name: str, tool_args: dict,
                               parent_used: int) -> int:
        """Return the number of additional parent-budget tool calls a delegate
        call should be charged, based on the mode it ran in.

        The child burns its own max_tool_calls, so the parent should not count
        the full amount (that would starve the parent). Instead the parent
        pays a small proportional toll so nested delegation stays bounded.
        """
        try:
            if name != "delegate":
                return 0
            mode = (tool_args or {}).get("mode", "research")
            return MODE_QUOTA.get(mode, 1)
        except Exception:  # pragma: no cover - safety net
            return 0

    def _trace_delegate_result(self, result_dict: dict) -> None:
        """Emit a trace event summarizing a delegate (sub-agent) result."""
        try:
            if self.trace_recorder is None:
                return
            meta = result_dict.get("metadata") or {}
            from agent.observability.trace_recorder import TraceEvent
            self.trace_recorder.trace.add(TraceEvent(
                type="subagent",
                tool="delegate",
                args=meta,
                result={
                    "success": result_dict.get("success"),
                    "content": (result_dict.get("output") or {}).get(
                        "subagent_answer", ""
                    )[:2000],
                    "error": result_dict.get("error"),
                    "subagent_id": meta.get("subagent_id"),
                    "mode": meta.get("subagent_mode"),
                    "tool_calls_made": meta.get("subagent_calls"),
                    "trace_id": meta.get("trace_id"),
                },
                iteration=None,
            ))
        except Exception:  # pragma: no cover - observability must never break the loop
            pass

    # ----------------------------------------------------------
    # Human-in-the-loop approval (pause-and-wait)
    # ----------------------------------------------------------

    def _check_approval(self, name: str, args: dict) -> ToolResult | None:
        """If `name` needs approval and no manager is configured, return a
        ToolResult explaining that the tool was skipped. If a manager is
        configured, create a pending request and WAIT for the user's decision.

        With approval_remember enabled, the user's decision for a tool is
        cached for the REST of this run: approved tools proceed without
        asking again; rejected tools fail immediately. This keeps long tasks
        (many write_file / execute_code calls) from nagging repeatedly.

        Returns None when the tool may proceed.
        """
        if not _APPROVAL_AVAILABLE or not self.config.approval_enabled:
            return None
        if self.config.approval_auto_approve:
            return None
        if name not in self.config.approval_tools:
            return None

        manager = self.approval_manager
        if manager is None:
            # No approval backend wired: fail-safe — refuse the side-effectful
            # tool rather than running it silently.
            return ToolResult(
                success=False,
                error=(
                    f"工具 {name} 需要人工确认，但当前未配置审批系统，已阻止执行。"
                    "请联系管理员启用审批。"
                ),
                metadata={"approval": "blocked_no_manager"},
            )

        # Per-run remembered decision: the user already decided this tool
        # earlier in this run — don't ask again.
        if self.config.approval_remember and name in self._approval_memory:
            remembered = self._approval_memory[name]
            if self.trace_recorder:
                from agent.observability.trace_recorder import TraceEvent
                self.trace_recorder.trace.add(TraceEvent(
                    type="approval",
                    tool=name,
                    detail=(
                        f"审批记忆命中: {name} 本任务内已"
                        + ("批准，直接放行" if remembered else "拒绝，不再询问")
                    ),
                ))
            if remembered:
                logger.info("Approval remembered (approved): %s", name)
                return None  # proceed silently
            return ToolResult(
                success=False,
                error=f"用户之前已拒绝工具 {name}（本次任务内不再询问）",
                metadata={"approval": "rejected_remembered", "tool": name},
            )

        try:
            user_id = self._approval_user_id()
            req = manager.request(user_id, name, args)
            req_id = req.get("id")
            if self.trace_recorder:
                from agent.observability.trace_recorder import TraceEvent
                self.trace_recorder.trace.add(TraceEvent(
                    type="approval",
                    tool=name,
                    args={"request_id": req_id, "status": "pending"},
                    detail=f"请求人工确认 #{req_id}（{name}）",
                ))
            status = manager.wait_for_decision(req_id, timeout=self.config.approval_expiry)

            if self.trace_recorder:
                from agent.observability.trace_recorder import TraceEvent
                self.trace_recorder.trace.add(TraceEvent(
                    type="approval",
                    tool=name,
                    args={"request_id": req_id, "status": status},
                    detail=f"审批结果 #{req_id}: {status}",
                ))

            if status == APPROVED:
                logger.info("Approval granted: #%s %s", req_id, name)
                if self.config.approval_remember:
                    self._approval_memory[name] = True
                    logger.info(
                        "Approval remembered for this run: %s (further calls won't ask)",
                        name,
                    )
                return None  # proceed to execute
            if status == REJECTED:
                reason = (manager.store.get(req_id) or {}).get("reason") or "用户拒绝"
                if self.config.approval_remember:
                    self._approval_memory[name] = False
                return ToolResult(
                    success=False,
                    error=f"用户拒绝了工具调用 {name}: {reason}",
                    metadata={"approval": "rejected", "request_id": req_id},
                )
            return ToolResult(
                success=False,
                error=f"工具调用 {name} 未获批准（{status}），已跳过。",
                metadata={"approval": status, "request_id": req_id},
            )
        except Exception as exc:  # pragma: no cover - approval must never crash the loop
            logger.exception("Approval check failed for %s", name)
            return ToolResult(
                success=False,
                error=f"审批系统异常: {exc}，已阻止执行 {name}",
                metadata={"approval": "error"},
            )

    def _approval_user_id(self) -> int | None:
        """Best-effort: resolve the current user's db id for approval scoping."""
        try:
            if self.db is not None:
                user = self.db.get_user(self.username)
                if user:
                    return user["id"]
        except Exception:
            pass
        return None

    def run(
        self,
        user_message: str,
        conversation_id: str | None = None,
        run_id: str | None = None,
        user_attachments: list | None = None,
    ) -> dict:
        """
        Execute the agent loop for a user message.

        Args:
            user_message: The user's message.
            conversation_id: Optional conversation to persist messages to.
            run_id: Optional client-supplied id for cancellation tracking.
                    The front-end generates one so it can cancel the run
                    while the synchronous request is still in flight.

        Returns:
            dict with keys: "content", "tool_calls_made", "iterations", "events", "mode"
        """
        # Per-run approval memory: fresh decisions for every new task
        self._reset_approval_memory()

        # Cancellation: unique id for this run (front-end can cancel it)
        self.current_run_id = run_id or self._new_run_id()
        self._current_attachments = None

        events: list[AgentEvent] = []
        tool_calls_made = 0
        last_mode = "native"

        # --- Observability: create recorder for this run ---
        # (Sub-agent runs attach their own recorder; only top-level runs
        # create one here.)
        if _OBSERVABILITY_AVAILABLE and self.trace_recorder is None:
            self.trace_recorder = TraceRecorder().attach()
        if self.trace_recorder:
            self.trace_recorder.report_start(
                user_message, self.username, conversation_id,
                backend=getattr(self.llm, "backend_name", "") or "",
                model=getattr(self.llm, "model_name", "") or "",
            )

        # --- Security: Validate user message ---
        if self.config.security_enabled and self.config.input_validation_enabled:
            is_valid, error = self._validator.validate_message(user_message)
            if not is_valid:
                if self.trace_recorder:
                    self.trace_recorder.report_security(
                        f"message validation failed: {error}"
                    )
                    self.trace_recorder.finish(
                        "validation_error", f"消息验证失败: {error}",
                        success=False,
                    )
                return {
                    "content": f"消息验证失败: {error}",
                    "tool_calls_made": 0,
                    "iterations": 0,
                    "events": [{"type": "error", "content": error}],
                    "mode": "native",
                }

        # --- Security: Rate limit check ---
        if self.config.security_enabled and self.config.rate_limit_enabled:
            allowed, reason = self._rate_limiter.check_tool_call(self.username)
            if not allowed:
                if self.trace_recorder:
                    self.trace_recorder.report_security(f"rate limited: {reason}")
                    self.trace_recorder.finish(
                        "rate_limited", f"请求过于频繁: {reason}", success=False,
                    )
                return {
                    "content": f"请求过于频繁: {reason}",
                    "tool_calls_made": 0,
                    "iterations": 0,
                    "events": [{"type": "error", "content": reason}],
                    "mode": "native",
                }

        # --- Security: Acquire concurrent slot ---
        if self.config.security_enabled and self.config.rate_limit_enabled:
            acquired, reason = self._rate_limiter.acquire_concurrent()
            if not acquired:
                if self.trace_recorder:
                    self.trace_recorder.report_security(f"busy: {reason}")
                    self.trace_recorder.finish(
                        "busy", f"系统繁忙: {reason}", success=False,
                    )
                return {
                    "content": f"系统繁忙: {reason}",
                    "tool_calls_made": 0,
                    "iterations": 0,
                    "events": [{"type": "error", "content": reason}],
                    "mode": "native",
                }

        try:
            self._last_plan_tracker = None
            result = self._run_loop(
                user_message, conversation_id, events, tool_calls_made, last_mode,
                user_attachments=user_attachments,
            )
            # Attach plan info to the result if a plan was generated
            plan = self._plan_result_dict(self._last_plan_tracker)
            if plan:
                result["plan"] = plan
            return result
        finally:
            # --- Security: Release concurrent slot ---
            if self.config.security_enabled and self.config.rate_limit_enabled:
                self._rate_limiter.release_concurrent()
            # Cancellation: drop the request so a finished run can't be cancelled
            if self.cancellation_manager is not None and self.current_run_id:
                try:
                    self.cancellation_manager.clear(self.current_run_id)
                except Exception:
                    pass
            self.current_run_id = None

    def _run_loop(self, user_message, conversation_id, events, tool_calls_made, last_mode,
                  user_attachments=None):
        """The inner agent loop (separated for try/finally)."""
        import time as _time
        _start_time = _time.time()
        _timeout = self.config.execution_timeout  # overall loop timeout (seconds)

        # Internal finish helper: wraps every return with the plan tracker so
        # run() can attach plan info to the result.
        plan_tracker: PlanTracker | None = None

        def _finish(**kwargs) -> tuple[dict, PlanTracker | None]:
            return dict(kwargs), plan_tracker

        # 1. Build initial messages
        messages = self._build_initial_messages(
            user_message, conversation_id, user_attachments=user_attachments
        )

        # 1b. Plan-then-Execute: generate an explicit plan for complex tasks
        plan_tracker: PlanTracker | None = None
        self._last_plan_tracker = None
        if self.config.planning_enabled and self.plan_generator is not None:
            plan = self.plan_generator.generate(user_message)
            if plan is not None and not plan.is_empty:
                plan_tracker = PlanTracker(plan)
                self._last_plan_tracker = plan_tracker
                # Attach the LLM step reviewer (fail-soft: heuristics stay
                # authoritative if the reviewer is unavailable or fails).
                if (
                    self.config.plan_review_enabled
                    and self.plan_reviewer is not None
                ):
                    plan_tracker.attach_reviewer(self.plan_reviewer)
                plan_block = plan.build_injection()
                if plan_block:
                    # Inject the plan right after the system prompts
                    messages.insert(1, {"role": "system", "content": plan_block})
                    events.append(AgentEvent(
                        type="plan",
                        content=plan_block,
                        metadata={"goal": plan.goal, "step_count": len(plan.steps)},
                    ))
                    if self.trace_recorder:
                        self.trace_recorder.report_plan(
                            plan.goal, plan.steps, plan_block,
                        )
                    logger.info(
                        "[conv=%s] Plan generated: %d steps for goal=%r",
                        conversation_id or "new", len(plan.steps), plan.goal,
                    )

        # Deduplication: cache results of identical tool calls
        _tool_call_cache: dict[str, dict] = {}  # call_key -> result_dict
        _consecutive_same_tool = 0
        _last_tool_call_key = ""
        _consecutive_failures = 0  # failure-loop guard counter
        _last_result_dict: dict = {}  # result of the previous tool call (for loop guards)

        # 2. ReAct loop
        for iteration in range(self.config.max_iterations):
            # Cancellation checkpoint: user asked to stop (direct) or the
            # confirm-cancel was approved — abort gracefully.
            if self._cancel_requested():
                reason = "用户取消了任务"
                logger.info("[conv=%s] Cancelled by user at iteration %d", conversation_id or "new", iteration + 1)
                events.append(AgentEvent(
                    type="cancelled",
                    content=reason,
                    iteration=iteration + 1,
                ))
                if self.trace_recorder:
                    self.trace_recorder.report_loop_guard(
                        "user cancelled the run", iteration=iteration + 1,
                    )
                self._trace_finish(
                    "cancelled", reason, success=False, content=reason,
                    tool_calls_made=tool_calls_made, iterations=iteration + 1,
                )
                return {
                    "content": reason,
                    "tool_calls_made": tool_calls_made,
                    "iterations": iteration + 1,
                    "events": [e.to_dict() for e in events],
                    "mode": last_mode,
                    "cancelled": True,
                }

            logger.info(
                "[conv=%s] Iteration %d/%d (tool_calls=%d)",
                conversation_id or "new",
                iteration + 1,
                self.config.max_iterations,
                tool_calls_made,
            )

            # Check total tool call budget
            if tool_calls_made >= self.config.max_tool_calls:
                logger.warning("Total tool call limit reached: %d", self.config.max_tool_calls)
                if self.trace_recorder:
                    self.trace_recorder.report_loop_guard(
                        f"tool call budget exhausted ({self.config.max_tool_calls})",
                        iteration=iteration + 1,
                    )
                self._trace_finish(
                    "tool_limit",
                    f"已达到工具调用上限 ({self.config.max_tool_calls} 次)",
                    success=False,
                    content=f"已达到工具调用上限 ({self.config.max_tool_calls} 次)，请简化请求后重试。",
                    tool_calls_made=tool_calls_made, iterations=iteration + 1,
                )
                return {
                    "content": f"已达到工具调用上限 ({self.config.max_tool_calls} 次)，请简化请求后重试。",
                    "tool_calls_made": tool_calls_made,
                    "iterations": iteration + 1,
                    "events": [e.to_dict() for e in events],
                    "mode": last_mode,
                }

            # Check overall timeout
            elapsed = _time.time() - _start_time
            if elapsed > _timeout:
                logger.warning("Agent timeout after %.0fs", elapsed)
                if self.trace_recorder:
                    self.trace_recorder.report_loop_guard(
                        f"execution timeout after {int(elapsed)}s", iteration=iteration + 1,
                    )
                self._trace_finish(
                    "timeout", f"任务执行超时（已用 {int(elapsed)} 秒）",
                    success=False,
                    content=f"任务执行超时（已用 {int(elapsed)} 秒），请简化请求后重试。",
                    tool_calls_made=tool_calls_made, iterations=iteration + 1,
                )
                return {
                    "content": f"任务执行超时（已用 {int(elapsed)} 秒），请简化请求后重试。",
                    "tool_calls_made": tool_calls_made,
                    "iterations": iteration + 1,
                    "events": [e.to_dict() for e in events],
                    "mode": last_mode,
                }

            # Build tool schemas
            tool_schemas = None
            if self.config.tools_enabled and self.tools:
                tool_schemas = self.tools.all_schemas()

            # Inject current plan progress (after first iteration, when tools
            # have been executed and progress may have changed)
            if plan_tracker is not None and plan_tracker.active and iteration > 0:
                progress = plan_tracker.build_progress_injection()
                if progress:
                    # Replace any previous progress block to avoid accumulation
                    messages = [
                        m for m in messages
                        if not (m.get("role") == "system" and m.get("content", "").startswith("[计划进度]"))
                    ]
                    messages.insert(1, {"role": "system", "content": progress})
                    if self.trace_recorder:
                        self.trace_recorder.report_plan_progress(plan_tracker)

            # Context compression: summarize older messages if the history has
            # grown past the trigger threshold (LLM summarization, fail-soft).
            if iteration > 0:
                messages = self._maybe_compress_messages(messages, conversation_id)

            # Determine force_mode
            force_mode = "prompt" if self.config.use_prompt_tool_calls else None

            # Call LLM
            try:
                _llm_start = time.time()
                response = self.llm.chat(
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice=self.config.tool_choice if tool_schemas else "none",
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    force_mode=force_mode,
                )
                last_mode = response.mode
                if self.trace_recorder:
                    self.trace_recorder.report_llm_call(
                        messages, response,
                        duration=time.time() - _llm_start,
                        iteration=iteration + 1,
                    )
            except Exception as e:
                logger.exception("LLM call failed at iteration %d", iteration + 1)
                if self.trace_recorder:
                    self.trace_recorder.report_error(f"LLM call failed: {e}", iteration=iteration + 1)
                events.append(AgentEvent(type="error", content=str(e)))
                self._trace_finish(
                    "llm_error", f"LLM 调用失败: {e}", success=False,
                    content=f"LLM 调用失败: {e}",
                    tool_calls_made=tool_calls_made, iterations=iteration + 1,
                )
                return {
                    "content": f"LLM 调用失败: {e}",
                    "tool_calls_made": tool_calls_made,
                    "iterations": iteration + 1,
                    "events": [e.to_dict() for e in events],
                    "mode": last_mode,
                }

            # 3. Check response type
            if response.type == "text":
                # LLM decided to respond directly — loop complete
                events.append(AgentEvent(
                    type="text",
                    content=response.content,
                    iteration=iteration + 1,
                ))

                # Persist to database
                if conversation_id and self.db:
                    self.db.add_message(
                        conversation_id, "user", user_message,
                        attachments=self._current_attachments,
                    )
                    self.db.add_message(conversation_id, "assistant", response.content)

                # Post-turn: automatic long-term memory extraction
                self._maybe_extract_memories(
                    user_message, response.content, conversation_id
                )

                self._trace_finish(
                    "text_response", "", success=True,
                    content=response.content,
                    tool_calls_made=tool_calls_made, iterations=iteration + 1,
                )
                return {
                    "content": response.content,
                    "tool_calls_made": tool_calls_made,
                    "iterations": iteration + 1,
                    "events": [e.to_dict() for e in events],
                    "mode": last_mode,
                }

            if response.type == "tool_use" and response.has_tool_calls:
                # Filter out empty/invalid tool calls
                valid_calls = [
                    tc for tc in response.tool_calls
                    if tc.get("function", {}).get("name")
                ]
                if not valid_calls:
                    # No valid tool calls — treat as text response
                    content = response.content or "我没有找到需要调用的工具。"
                    events.append(AgentEvent(type="text", content=content, iteration=iteration + 1))
                    if conversation_id and self.db:
                        self.db.add_message(
                            conversation_id, "user", user_message,
                            attachments=self._current_attachments,
                        )
                        self.db.add_message(conversation_id, "assistant", content)

                    # Post-turn: automatic long-term memory extraction
                    self._maybe_extract_memories(
                        user_message, content, conversation_id
                    )

                    self._trace_finish(
                        "no_valid_tool_calls", "", success=True,
                        content=content,
                        tool_calls_made=tool_calls_made, iterations=iteration + 1,
                    )
                    return {
                        "content": content,
                        "tool_calls_made": tool_calls_made,
                        "iterations": iteration + 1,
                        "events": [e.to_dict() for e in events],
                        "mode": last_mode,
                    }

                # 4. Execute tool calls
                # Format the assistant message based on mode
                if last_mode == "native":
                    # Native mode: use standard tool_calls format
                    assistant_msg: dict = {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": response.tool_calls,
                    }
                    messages.append(assistant_msg)
                else:
                    # Prompt mode: the assistant's text already contains the XML tool calls
                    # We just add the text content as the assistant message
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                    })

                # Execute each tool and add results
                for tool_call in response.tool_calls:
                    func = tool_call.get("function", {})
                    tool_name = func.get("name", "")
                    tool_args_str = func.get("arguments", "{}")

                    # Parse arguments
                    try:
                        tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                    except json.JSONDecodeError:
                        tool_args = {}

                    # Model-side recovery: some models (e.g. DeepSeek under
                    # load) emit a tool_call with EMPTY arguments while putting
                    # the real content in the response text. For write_file,
                    # recover the JSON payload from the text.
                    if (
                        tool_name == "write_file"
                        and not tool_args
                        and response.content
                    ):
                        recovered = self._extract_write_args_from_text(response.content)
                        if recovered:
                            tool_args = recovered
                            logger.info(
                                "write_file: recovered path/content from response text"
                            )

                    # Loop detection: track repeated identical tool calls.
                    # The key is the FULL arguments — rewriting a file with new
                    # content is legitimate iteration and resets the counter.
                    # Only genuinely identical calls accumulate.
                    call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"

                    if call_key == _last_tool_call_key:
                        _consecutive_same_tool += 1
                    else:
                        _consecutive_same_tool = 0
                    _last_tool_call_key = call_key

                    # Break if same tool called N times in a row with same args.
                    # Write operations get a higher threshold (12) since iterating
                    # on a file's content is normal for document-generation tasks.
                    # Retries after a RATE-LIMITED result are legitimate (the
                    # model waits and retries), so they don't accumulate here.
                    write_ops = {"write_file", "memory_store"}
                    threshold = 12 if tool_name in write_ops else 6
                    # Empty-arg write_file calls are never legitimate iteration:
                    # the model is stuck re-sending a malformed call, so use the
                    # LOW threshold for those even for write tools.
                    if tool_name in write_ops and not tool_args:
                        threshold = 6
                    _prev_limited = bool(
                        _last_result_dict.get("metadata", {}).get("rate_limited")
                    ) if _last_result_dict else False
                    if _prev_limited:
                        _consecutive_same_tool = 0  # rate-limit retries are fine
                    if _consecutive_same_tool >= threshold:
                        logger.warning(
                            "[conv=%s] Same tool call repeated %d times, breaking loop",
                            conversation_id or "new",
                            _consecutive_same_tool + 1,
                        )
                        if self.trace_recorder:
                            self.trace_recorder.report_loop_guard(
                                f"identical tool call '{tool_name}' repeated "
                                f"{_consecutive_same_tool + 1} times",
                                iteration=iteration + 1,
                            )
                        events.append(AgentEvent(
                            type="error",
                            content=f"检测到重复工具调用 ({tool_name})，已终止循环",
                            iteration=iteration + 1,
                        ))
                        self._trace_finish(
                            "loop_detected",
                            f"相同工具调用 '{tool_name}' 重复 {_consecutive_same_tool + 1} 次",
                            success=False,
                            content=(
                                f"检测到重复调用工具 '{tool_name}'，任务可能陷入循环。"
                                "请简化请求或换个方式提问。"
                            ),
                            tool_calls_made=tool_calls_made, iterations=iteration + 1,
                        )
                        return {
                            "content": f"检测到重复调用工具 '{tool_name}'，任务可能陷入循环。请简化请求或换个方式提问。",
                            "tool_calls_made": tool_calls_made,
                            "iterations": iteration + 1,
                            "events": [e.to_dict() for e in events],
                            "mode": last_mode,
                        }

                    events.append(AgentEvent(
                        type="tool_call",
                        tool_name=tool_name,
                        tool_args=tool_args,
                        iteration=iteration + 1,
                    ))
                    if self.trace_recorder:
                        self.trace_recorder.report_tool_call(
                            tool_name, tool_args, iteration=iteration + 1,
                        )

                    # Check cache: skip if same call was already made
                    call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
                    if call_key in _tool_call_cache:
                        # Reuse cached result (no API call, no count)
                        result_dict = _tool_call_cache[call_key]
                        logger.info("Tool call cached, reusing result: %s", tool_name)
                        if self.trace_recorder:
                            self.trace_recorder.report_tool_result(
                                tool_name, result_dict, from_cache=True,
                                iteration=iteration + 1,
                            )
                    else:
                        # Execute the tool
                        result = self._execute_tool(tool_name, tool_args)
                        result_dict = result.to_dict()
                        if self.trace_recorder:
                            self.trace_recorder.report_tool_result(
                                tool_name, result_dict,
                                retries=result.metadata.get("retries"),
                                iteration=iteration + 1,
                            )
                        # Sub-agent (delegate) support:
                        #  - charge the parent a small quota share for the child's work
                        #  - never cache delegate results (each run is unique)
                        #  - record a subagent summary event in the trace
                        _delegate_quota = self._charge_subagent_quota(
                            tool_name, tool_args, parent_used=tool_calls_made
                        )
                        if _delegate_quota:
                            tool_calls_made += _delegate_quota
                            self._trace_delegate_result(result_dict)
                        # Cache successful results for dedup
                        elif result.success:
                            tool_calls_made += 1
                            _tool_call_cache[call_key] = result_dict
                    _last_result_dict = result_dict

                    # Failure-loop guard: N consecutive FAILED tool calls (any
                    # args — parameter errors, permission denials, etc.) mean the
                    # model is stuck; terminate instead of burning the timeout.
                    # Rate-limited failures are NOT counted: they are a transient
                    # system state, and the model may legitimately wait + retry.
                    _is_rate_limited = bool(
                        result_dict.get("metadata", {}).get("rate_limited")
                    )
                    if result_dict.get("success"):
                        _consecutive_failures = 0
                    elif _is_rate_limited:
                        logger.info(
                            "[conv=%s] tool rate-limited (not counted as failure)",
                            conversation_id or "new",
                        )
                        # Back off briefly so the sliding window can recover;
                        # the model sees the rate-limit error and can continue
                        # with read-only work or wait.
                        time.sleep(1.0)
                    else:
                        _consecutive_failures += 1
                        if _consecutive_failures >= 8:
                            logger.warning(
                                "[conv=%s] %d consecutive tool failures, breaking loop",
                                conversation_id or "new",
                                _consecutive_failures,
                            )
                            if self.trace_recorder:
                                self.trace_recorder.report_loop_guard(
                                    f"{_consecutive_failures} consecutive failed tool calls "
                                    f"(last: {tool_name})",
                                    iteration=iteration + 1,
                                )
                            events.append(AgentEvent(
                                type="error",
                                content=f"工具连续失败 {_consecutive_failures} 次，已终止循环",
                                iteration=iteration + 1,
                            ))
                            self._trace_finish(
                                "failure_loop",
                                f"工具连续失败 {_consecutive_failures} 次",
                                success=False,
                                content=(
                                    f"工具连续失败 {_consecutive_failures} 次（最近失败："
                                    f"{tool_name}: {result_dict.get('error', '')[:100]}）。"
                                    "请检查工具参数后重试，或换个方式提问。"
                                ),
                                tool_calls_made=tool_calls_made, iterations=iteration + 1,
                            )
                            return {
                                "content": (
                                    f"工具连续失败 {_consecutive_failures} 次（最近失败："
                                    f"{tool_name}: {result_dict.get('error', '')[:100]}）。"
                                    "请检查工具参数后重试，或换个方式提问。"
                                ),
                                "tool_calls_made": tool_calls_made,
                                "iterations": iteration + 1,
                                "events": [e.to_dict() for e in events],
                                "mode": last_mode,
                            }

                    # Update plan progress based on the tool call
                    if plan_tracker is not None:
                        plan_tracker.note_tool(tool_name, tool_args)
                        # Periodically ask the LLM reviewer to re-check step
                        # completion (corrects heuristic false marks).
                        _call_count = len(plan_tracker.tool_history)
                        if (
                            self.config.plan_review_enabled
                            and plan_tracker.reviewer is not None
                            and _call_count % self.config.plan_review_every == 0
                            and _call_count > 0
                        ):
                            review = plan_tracker.review_progress()
                            if review and self.trace_recorder:
                                self.trace_recorder.report_plan_review(review)

                    events.append(AgentEvent(
                        type="tool_result",
                        tool_name=tool_name,
                        tool_result=result_dict,
                        iteration=iteration + 1,
                    ))

                    # Add tool result to messages — format depends on mode
                    if last_mode == "native":
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.get("id", ""),
                            "content": json.dumps(result_dict, ensure_ascii=False),
                        })
                    else:
                        # Prompt mode: format as XML result block in a user message
                        result_text = self.llm.format_tool_result_for_prompt(tool_name, result_dict)
                        messages.append({
                            "role": "user",
                            "content": result_text,
                        })

                continue

            # Unknown response type (or tool_use with no valid calls)
            logger.warning("Unexpected response type: %s (has_tool_calls=%s)", response.type, response.has_tool_calls)
            content = response.content or "处理过程中遇到未知错误。"
            if self.trace_recorder:
                self.trace_recorder.report_error(
                    f"unexpected response type: {response.type} "
                    f"(has_tool_calls={response.has_tool_calls})",
                    iteration=iteration + 1,
                )
            events.append(AgentEvent(type="error", content=content))
            if conversation_id and self.db:
                self.db.add_message(
                    conversation_id, "user", user_message,
                    attachments=self._current_attachments,
                )
                self.db.add_message(conversation_id, "assistant", content)

            # Post-turn: automatic long-term memory extraction
            self._maybe_extract_memories(user_message, content, conversation_id)

            self._trace_finish(
                "unexpected_type", f"unexpected response type: {response.type}",
                success=False, content=content,
                tool_calls_made=tool_calls_made, iterations=iteration + 1,
            )
            return {
                "content": content,
                "tool_calls_made": tool_calls_made,
                "iterations": iteration + 1,
                "events": [e.to_dict() for e in events],
                "mode": last_mode,
            }

        # Total tool call budget exhausted
        logger.warning("Total tool call limit reached: %d", self.config.max_tool_calls)
        if self.trace_recorder:
            self.trace_recorder.report_loop_guard(
                f"iteration limit reached ({self.config.max_iterations})",
            )
        self._trace_finish(
            "iteration_limit",
            f"达到最大迭代次数 ({self.config.max_iterations})",
            success=False,
            content=f"已达到工具调用上限 ({self.config.max_tool_calls} 次)，请简化请求后重试。",
            tool_calls_made=tool_calls_made, iterations=iteration + 1,
        )
        return {
            "content": f"已达到工具调用上限 ({self.config.max_tool_calls} 次)，请简化请求后重试。",
            "tool_calls_made": tool_calls_made,
            "iterations": iteration + 1,
            "events": [e.to_dict() for e in events],
            "mode": last_mode,
        }

    def run_stream(
        self,
        user_message: str,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> Generator[dict, None, None]:
        """
        Execute the agent loop and yield events as they happen (SSE streaming).

        Args:
            run_id: Optional client-supplied id for cancellation tracking.
        """
        # Per-run approval memory: fresh decisions for every new task
        self._reset_approval_memory()

        # Cancellation: unique id for this run (front-end can cancel it)
        self.current_run_id = run_id or self._new_run_id()
        self._current_attachments = None

        # --- Observability: create recorder for this run ---
        # (Sub-agent runs attach their own recorder; only top-level runs
        # create one here.)
        if _OBSERVABILITY_AVAILABLE and self.trace_recorder is None:
            self._run_stream_trace(user_message, conversation_id)

        # --- Security: Validate user message ---
        if self.config.security_enabled and self.config.input_validation_enabled:
            is_valid, error = self._validator.validate_message(user_message)
            if not is_valid:
                if self.trace_recorder:
                    self.trace_recorder.report_security(
                        f"message validation failed: {error}"
                    )
                    self._trace_finish(
                        "validation_error", f"消息验证失败: {error}", success=False,
                    )
                yield {"type": "error", "content": f"消息验证失败: {error}"}
                return

        # --- Security: Rate limit check ---
        if self.config.security_enabled and self.config.rate_limit_enabled:
            allowed, reason = self._rate_limiter.check_tool_call(self.username)
            if not allowed:
                if self.trace_recorder:
                    self.trace_recorder.report_security(f"rate limited: {reason}")
                    self._trace_finish(
                        "rate_limited", f"请求过于频繁: {reason}", success=False,
                    )
                yield {"type": "error", "content": f"请求过于频繁: {reason}"}
                return

        # --- Security: Acquire concurrent slot ---
        acquired = False
        if self.config.security_enabled and self.config.rate_limit_enabled:
            acquired, reason = self._rate_limiter.acquire_concurrent()
            if not acquired:
                if self.trace_recorder:
                    self.trace_recorder.report_security(f"busy: {reason}")
                    self._trace_finish(
                        "busy", f"系统繁忙: {reason}", success=False,
                    )
                yield {"type": "error", "content": f"系统繁忙: {reason}"}
                return

        try:
            yield from self._run_stream_loop(user_message, conversation_id)
        finally:
            if acquired and self.config.rate_limit_enabled:
                self._rate_limiter.release_concurrent()
            if self.cancellation_manager is not None and self.current_run_id:
                try:
                    self.cancellation_manager.clear(self.current_run_id)
                except Exception:
                    pass
            self.current_run_id = None

    def _run_stream_loop(
        self, user_message: str, conversation_id: str | None,
        user_attachments: list | None = None,
    ) -> Generator[dict, None, None]:
        """Inner streaming loop (separated for try/finally)."""
        last_mode = "native"
        # Streaming loop budget counter: the stream previously had NO tool-call
        # budget of its own — delegate quota charging needs one.
        stream_tool_calls = 0

        # 1. Build initial messages
        messages = self._build_initial_messages(
            user_message, conversation_id, user_attachments=user_attachments
        )

        # 1b. Plan-then-Execute: generate an explicit plan for complex tasks
        plan_tracker: PlanTracker | None = None
        self._last_plan_tracker = None
        if self.config.planning_enabled and self.plan_generator is not None:
            plan = self.plan_generator.generate(user_message)
            if plan is not None and not plan.is_empty:
                plan_tracker = PlanTracker(plan)
                self._last_plan_tracker = plan_tracker
                # Attach the LLM step reviewer (fail-soft: heuristics stay
                # authoritative if the reviewer is unavailable or fails).
                if (
                    self.config.plan_review_enabled
                    and self.plan_reviewer is not None
                ):
                    plan_tracker.attach_reviewer(self.plan_reviewer)
                plan_block = plan.build_injection()
                if plan_block:
                    messages.insert(1, {"role": "system", "content": plan_block})
                    yield {"type": "plan", "content": plan_block,
                           "metadata": {"goal": plan.goal, "step_count": len(plan.steps)}}
                    if self.trace_recorder:
                        self.trace_recorder.report_plan(
                            plan.goal, plan.steps, plan_block,
                        )
                    logger.info(
                        "[conv=%s] Plan generated: %d steps for goal=%r",
                        conversation_id or "new", len(plan.steps), plan.goal,
                    )

        # 2. ReAct loop
        import time as _time
        _start_time = _time.time()
        _timeout = self.config.execution_timeout  # overall loop timeout (seconds)

        for iteration in range(self.config.max_iterations):
            # Cancellation checkpoint: user asked to stop (direct) or the
            # confirm-cancel was approved — abort gracefully.
            if self._cancel_requested():
                reason = "用户取消了任务"
                logger.info("[conv=%s] Stream cancelled by user at iteration %d", conversation_id or "new", iteration + 1)
                if self.trace_recorder:
                    self.trace_recorder.report_loop_guard(
                        "user cancelled the run", iteration=iteration + 1,
                    )
                    self._trace_finish(
                        "cancelled", reason, success=False, content=reason,
                        iterations=iteration + 1,
                    )
                yield {"type": "cancelled", "content": reason}
                return

            # Check overall timeout (streaming had no guard before)
            elapsed = _time.time() - _start_time
            if elapsed > _timeout:
                logger.warning("Agent stream timeout after %.0fs", elapsed)
                if self.trace_recorder:
                    self.trace_recorder.report_loop_guard(
                        f"execution timeout after {int(elapsed)}s", iteration=iteration + 1,
                    )
                    self._trace_finish(
                        "timeout", f"任务执行超时（已用 {int(elapsed)} 秒）",
                        success=False,
                        content=f"任务执行超时（已用 {int(elapsed)} 秒），请简化请求后重试。",
                        iterations=iteration + 1,
                    )
                yield {
                    "type": "error",
                    "content": f"任务执行超时（已用 {int(elapsed)} 秒），请简化请求后重试。",
                }
                return

            # Streaming tool-call budget check
            if stream_tool_calls >= self.config.max_tool_calls:
                logger.warning(
                    "Stream total tool call limit reached: %d", self.config.max_tool_calls
                )
                if self.trace_recorder:
                    self.trace_recorder.report_loop_guard(
                        f"tool call budget exhausted ({self.config.max_tool_calls})",
                        iteration=iteration + 1,
                    )
                    self._trace_finish(
                        "tool_limit",
                        f"已达到工具调用上限 ({self.config.max_tool_calls} 次)",
                        success=False,
                        content=f"已达到工具调用上限 ({self.config.max_tool_calls} 次)，请简化请求后重试。",
                        iterations=iteration + 1,
                    )
                yield {
                    "type": "error",
                    "content": f"已达到工具调用上限 ({self.config.max_tool_calls} 次)，请简化请求后重试。",
                }
                return

            tool_schemas = None
            if self.config.tools_enabled and self.tools:
                tool_schemas = self.tools.all_schemas()

            # Inject current plan progress (after first iteration)
            if plan_tracker is not None and plan_tracker.active and iteration > 0:
                progress = plan_tracker.build_progress_injection()
                if progress:
                    messages = [
                        m for m in messages
                        if not (m.get("role") == "system" and m.get("content", "").startswith("[计划进度]"))
                    ]
                    messages.insert(1, {"role": "system", "content": progress})
                    if self.trace_recorder:
                        self.trace_recorder.report_plan_progress(plan_tracker)

            # Context compression: summarize older messages if the history has
            # grown past the trigger threshold (LLM summarization, fail-soft).
            if iteration > 0:
                messages = self._maybe_compress_messages(messages, conversation_id)

            force_mode = "prompt" if self.config.use_prompt_tool_calls else None

            try:
                _llm_start = time.time()
                response = self.llm.chat(
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice=self.config.tool_choice if tool_schemas else "none",
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    force_mode=force_mode,
                )
                last_mode = response.mode
                if self.trace_recorder:
                    self.trace_recorder.report_llm_call(
                        messages, response,
                        duration=time.time() - _llm_start,
                        iteration=iteration + 1,
                    )
            except Exception as e:
                if self.trace_recorder:
                    self.trace_recorder.report_error(
                        f"LLM call failed: {e}", iteration=iteration + 1,
                    )
                    self._trace_finish(
                        "llm_error", f"LLM 调用失败: {e}", success=False,
                        content=f"LLM 调用失败: {e}",
                        iterations=iteration + 1,
                    )
                yield {"type": "error", "content": str(e)}
                return

            if response.type == "text":
                yield {"type": "text", "content": response.content}

                if conversation_id and self.db:
                    self.db.add_message(
                        conversation_id, "user", user_message,
                        attachments=self._current_attachments,
                    )
                    self.db.add_message(conversation_id, "assistant", response.content)

                # Post-turn: automatic long-term memory extraction
                self._maybe_extract_memories(
                    user_message, response.content, conversation_id
                )

                if self.trace_recorder:
                    self._trace_finish(
                        "text_response", "", success=True,
                        content=response.content, iterations=iteration + 1,
                    )
                yield {"type": "done", "mode": last_mode}
                return

            if response.type == "tool_use" and response.has_tool_calls:
                # Filter out empty/invalid tool calls
                valid_calls = [
                    tc for tc in response.tool_calls
                    if tc.get("function", {}).get("name")
                ]
                if not valid_calls:
                    content = response.content or "我没有找到需要调用的工具。"
                    yield {"type": "text", "content": content}
                    if conversation_id and self.db:
                        self.db.add_message(
                            conversation_id, "user", user_message,
                            attachments=self._current_attachments,
                        )
                        self.db.add_message(conversation_id, "assistant", content)

                    # Post-turn: automatic long-term memory extraction
                    self._maybe_extract_memories(
                        user_message, content, conversation_id
                    )

                    if self.trace_recorder:
                        self._trace_finish(
                            "no_valid_tool_calls", "", success=True,
                            content=content, iterations=iteration + 1,
                        )
                    yield {"type": "done", "mode": last_mode}
                    return

                # Format assistant message based on mode
                if last_mode == "native":
                    assistant_msg: dict = {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": response.tool_calls,
                    }
                    messages.append(assistant_msg)
                else:
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                    })

                for tool_call in response.tool_calls:
                    func = tool_call.get("function", {})
                    tool_name = func.get("name", "")
                    tool_args_str = func.get("arguments", "{}")

                    try:
                        tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                    except json.JSONDecodeError:
                        tool_args = {}

                    # Same model-side recovery as the non-streaming loop:
                    # empty write_file args but content in the response text.
                    if (
                        tool_name == "write_file"
                        and not tool_args
                        and response.content
                    ):
                        recovered = self._extract_write_args_from_text(response.content)
                        if recovered:
                            tool_args = recovered
                            logger.info(
                                "write_file: recovered path/content from response text (stream)"
                            )

                    yield {
                        "type": "tool_call",
                        "tool": tool_name,
                        "args": tool_args,
                        "iteration": iteration + 1,
                        "mode": last_mode,
                    }
                    if self.trace_recorder:
                        self.trace_recorder.report_tool_call(
                            tool_name, tool_args, iteration=iteration + 1,
                        )

                    result = self._execute_tool(tool_name, tool_args)
                    result_dict = result.to_dict()
                    if self.trace_recorder:
                        self.trace_recorder.report_tool_result(
                            tool_name, result_dict,
                            retries=result.metadata.get("retries"),
                            iteration=iteration + 1,
                        )

                    # Sub-agent (delegate) support: charge parent quota + trace
                    _delegate_quota = self._charge_subagent_quota(
                        tool_name, tool_args, parent_used=stream_tool_calls
                    )
                    if _delegate_quota:
                        stream_tool_calls += _delegate_quota
                        self._trace_delegate_result(result_dict)
                    else:
                        stream_tool_calls += 1

                    # Update plan progress based on the tool call
                    if plan_tracker is not None:
                        plan_tracker.note_tool(tool_name, tool_args)
                        # Periodically ask the LLM reviewer to re-check step
                        # completion (corrects heuristic false marks).
                        _call_count = len(plan_tracker.tool_history)
                        if (
                            self.config.plan_review_enabled
                            and plan_tracker.reviewer is not None
                            and _call_count % self.config.plan_review_every == 0
                            and _call_count > 0
                        ):
                            review = plan_tracker.review_progress()
                            if review and self.trace_recorder:
                                self.trace_recorder.report_plan_review(review)

                    yield {
                        "type": "tool_result",
                        "tool": tool_name,
                        "result": result_dict,
                        "iteration": iteration + 1,
                        "mode": last_mode,
                    }

                    # Add result in mode-appropriate format
                    if last_mode == "native":
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.get("id", ""),
                            "content": json.dumps(result_dict, ensure_ascii=False),
                        })
                    else:
                        result_text = self.llm.format_tool_result_for_prompt(
                            tool_name, result_dict
                        )
                        messages.append({
                            "role": "user",
                            "content": result_text,
                        })

                continue

            if self.trace_recorder:
                self.trace_recorder.report_error(
                    f"unexpected response type: {response.type}",
                    iteration=iteration + 1,
                )
                self._trace_finish(
                    "unexpected_type", f"unexpected response type: {response.type}",
                    success=False, iterations=iteration + 1,
                )
            yield {"type": "error", "content": f"Unexpected response type: {response.type}"}
            return

        if self.trace_recorder:
            self.trace_recorder.report_loop_guard(
                f"iteration limit reached ({self.config.max_iterations})",
            )
            self._trace_finish(
                "iteration_limit",
                f"达到最大迭代次数 ({self.config.max_iterations})",
                success=False, iterations=self.config.max_iterations,
            )
        yield {"type": "error", "content": f"达到最大迭代次数 ({self.config.max_iterations})"}

    # ------------------------------------------------------------
    # Context compression (LLM summarization of older messages)
    # ------------------------------------------------------------

    def _compression_hook(self, stats: dict) -> None:
        """Observability hook: record a compression event in the trace."""
        if not self.trace_recorder:
            return
        try:
            self.trace_recorder.report_compression(stats)
        except Exception:  # pragma: no cover
            pass

    def _load_stored_summary(
        self, conversation_id: str | None
    ) -> tuple[str, dict | None]:
        """Load the persisted conversation summary, if any.

        Returns (summary_text, info_dict_or_None). The summary is injected as
        a system message so long conversations keep their context across turns
        even before the in-loop compression triggers.
        """
        if not conversation_id or self.compression is None or not self.db:
            return "", None
        try:
            info = self.db.get_summary_info(conversation_id)
            if not info or not info.get("summary"):
                return "", None
            return info["summary"], info
        except Exception as e:
            logger.warning("Stored summary load failed: %s", e)
            return "", None

    def _maybe_compress_messages(
        self, messages: list[dict], conversation_id: str | None
    ) -> list[dict]:
        """Compress the live history if it exceeds the trigger threshold.

        Called before each LLM call: replaces the summarized portion of the
        history with a merged "[对话摘要]" system message. System blocks
        (system prompt, memory/plan injections) are never summarized — only
        the user/assistant/tool history is compressed. Fail-soft: any error
        leaves messages untouched.
        """
        if self.compression is None or not self.config.compression_enabled:
            return messages
        if len(messages) < self.config.compression_min_messages:
            return messages
        try:
            system_blocks = [
                m for m in messages if m.get("role") == "system"
            ]
            history = [
                m for m in messages if m.get("role") != "system"
            ]
            if len(history) < self.config.compression_min_messages:
                return messages
            self.compression.set_conversation(conversation_id)
            prev = self.compression.load_summary(conversation_id) or ""
            new_history, new_summary, stats = self.compression.compress_or_truncate(
                history, prev
            )
            if stats is not None:
                logger.info(
                    "[conv=%s] Context compressed: %d msgs -> summary, saved %d tokens",
                    conversation_id or "new",
                    stats["old_messages"], stats["saved_tokens"],
                )
            if new_summary:
                self.compression.save_summary(conversation_id, new_summary)
            # Reassemble: system blocks stay at the front (minus any stale
            # in-memory summary block — compress() replaces it with a fresh
            # merged one), then the compressed history.
            system_blocks = [
                m for m in system_blocks
                if not str(m.get("content", "")).startswith("[对话摘要]")
            ]
            # Tool protocol safety net: compression/truncation may cut between
            # an assistant tool_calls message and its tool results. Drop any
            # orphaned/incomplete round-trips so the API doesn't reject the
            # whole request (400: tool without preceding tool_calls).
            new_history = sanitize_tool_roundtrips(new_history)
            return system_blocks + new_history
        except Exception as e:
            logger.warning("Context compression failed (continuing untruncated): %s", e)
            return messages

    def _build_initial_messages(
        self, user_message: str, conversation_id: str | None,
        user_attachments: list | None = None,
    ) -> list[dict]:
        """Build the initial message list with system prompt and history.

        user_attachments: optional list of {"image_url": data_url} parts —
        images are embedded as OpenAI-style multimodal content so the model
        can actually see them.
        """
        # System prompt
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        system_prompt = self.config.system_prompt.replace("{current_date_time}", now)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # Long-term memory injection: retrieve relevant memories and add them
        # as a second system message so the model can reference them.
        if (
            self.config.memory_enabled
            and self.memory_service is not None
            and user_message
        ):
            try:
                memory_block = self.memory_service.build_injection_context(
                    query=user_message,
                    max_memories=self.config.memory_inject_count,
                    min_importance=self.config.memory_inject_min_importance,
                )
                if memory_block:
                    messages.append({"role": "system", "content": memory_block})
                    if self.trace_recorder:
                        # Count injected memory lines from the block
                        _count = sum(
                            1 for line in memory_block.splitlines()
                            if line.strip().startswith("-")
                        )
                        self.trace_recorder.report_memory_inject(
                            memory_block, _count
                        )
            except Exception as e:
                logger.warning("Memory injection failed: %s", e)

        # Load history from DB if available
        history = []
        if conversation_id and self.db:
            try:
                history = self.db.get_messages_for_llm(conversation_id, limit=20)
            except Exception as e:
                logger.warning("Failed to load history: %s", e)

        # Add user message (multimodal if images were attached)
        if user_attachments:
            parts = []
            if user_message:
                parts.append({"type": "text", "text": user_message})
            for att in user_attachments:
                if att.get("image_url"):
                    parts.append({"type": "image_url", "image_url": {"url": att["image_url"]}})
            history.append({"role": "user", "content": parts})
        else:
            history.append({"role": "user", "content": user_message})

        # Remember attachment metadata for persistence (this run's user msg)
        self._current_attachments = user_attachments or None

        # Persisted conversation summary (from context compression of earlier
        # turns): injected so the model keeps the long-term context even
        # before the in-loop compression triggers.
        stored_summary, _summary_info = self._load_stored_summary(conversation_id)
        if stored_summary:
            messages.append(
                self.compression.build_summary_message(stored_summary)
                if self.compression is not None
                else {"role": "system", "content": f"[对话摘要] {stored_summary}"}
            )

        # Fit within context window
        fitted = self.context.fit_messages(
            system_prompt, history, min_recent=4
        )
        # fitted[0] is the system prompt; keep memory injection as system too
        messages.extend(fitted[1:] if len(fitted) > 1 else [])
        return messages

    def _maybe_extract_memories(
        self, user_message: str, assistant_response: str, conversation_id: str | None
    ) -> None:
        """Post-turn automatic long-term memory extraction (fail-soft)."""
        if (
            not self.config.memory_enabled
            or not self.config.memory_auto_extract
            or self.memory_extractor is None
        ):
            return
        try:
            result = self.memory_extractor.maybe_extract(
                user_message=user_message,
                assistant_response=assistant_response,
                conversation_id=conversation_id,
            )
            if result.get("stored"):
                logger.info(
                    "Stored %d new memories (extracted %d)",
                    result["stored"],
                    result["extracted"],
                )
            if self.trace_recorder:
                self.trace_recorder.report_memory_extract(result)

            # Periodic maintenance: merge near-duplicates and decay stale
            # memories so the store stays high-signal (fail-soft).
            if (
                self.config.memory_consolidate
                and self.memory_service is not None
            ):
                try:
                    merged = self.memory_service.consolidate()
                    if merged:
                        logger.info("Consolidated %d duplicate memories", merged)
                except Exception as e:
                    logger.warning("Memory consolidation skipped: %s", e)
        except Exception as e:
            logger.warning("Memory extraction skipped: %s", e)

    @staticmethod
    def _extract_write_args_from_text(text: str) -> dict | None:
        """Recover write_file {path, content} from model response text.

        Models that emit empty tool arguments sometimes put the real JSON in
        their visible text. We look for:
          1. A JSON object with "path" and "content" keys, or
          2. An XML-ish <write_file path="...">content</write_file> block.

        Returns the recovered args dict, or None.
        """
        if not text:
            return None
        # 1. JSON object containing path (and usually content)
        for m in re.finditer(r"\{[^{}]*\"path\"\s*:\s*\"[^\"]+\"[^{}]*\}", text):
            try:
                data = json.loads(m.group(0))
                if data.get("path") and (data.get("content") is not None or len(data) > 1):
                    return {
                        "path": str(data["path"]),
                        "content": data.get("content", ""),
                    }
            except (json.JSONDecodeError, TypeError):
                continue
        # 2. XML-ish block: <write_file path="...">content</write_file>
        m = re.search(
            r"<write_file\s+path\s*=\s*[\"']([^\"']+)[\"']\s*>([\s\S]*?)</write_file>",
            text,
        )
        if m:
            return {"path": m.group(1).strip(), "content": m.group(2).strip()}
        return None

    def _execute_tool(self, name: str, args: dict) -> ToolResult:
        """Execute a tool by name with the given arguments (with security checks).

        Transient failures (network, timeout, 5xx, rate-limit) on idempotent
        tools are retried with exponential backoff. Write tools (retryable=False)
        are never retried to avoid duplicate side effects. Retry history is
        attached to the result metadata so the LLM can see what happened.
        """
        tool = self.tools.get(name)
        if not tool:
            return ToolResult(
                success=False,
                error=f"Unknown tool: '{name}'. Available tools: {', '.join(self.tools.list_names())}",
            )

        # --- Human-in-the-loop approval: side-effectful tools need the user's
        # OK before they run. The loop PAUSES here (blocking) while the user
        # decides in the UI; on approval the tool executes, on rejection the
        # model gets a rejection result and can adapt. ---
        approval = self._check_approval(name, args)
        if approval is not None:
            return approval

        # Mounted-folder access: file tools consult their approval callback
        # with the current run_id so "allow"-policy mounts can remember an
        # approval for the rest of this run. Injected here (not at tool
        # construction) so every run gets a fresh id.
        if hasattr(tool, "_approval_cb") and tool._approval_cb is not None:
            try:
                tool._current_run_id = self.current_run_id
            except Exception:
                pass

        # --- Security: Permission check ---
        if self.config.security_enabled and self.config.permission_enabled:
            allowed, reason = self._permissions.can_use_tool(self.username, name)
            if not allowed:
                logger.warning("Tool denied: %s for user %s: %s", name, self.username, reason)
                return ToolResult(success=False, error=f"权限不足: {reason}")

        # --- Security: Rate limit per tool call ---
        if self.config.security_enabled and self.config.rate_limit_enabled:
            allowed, reason = self._rate_limiter.check_tool_call(self.username)
            if not allowed:
                # Rate limiting is a transient system state, NOT an agent
                # mistake. Mark it so the failure-loop guard doesn't count it
                # (8 rate-limit hits would otherwise falsely terminate a
                # legitimate long task) and the model can wait and retry.
                result = ToolResult(success=False, error=f"工具调用频率限制: {reason}")
                result.metadata["rate_limited"] = True
                return result

        # --- Arg normalization: LLMs sometimes mix up tool schemas (e.g. pass
        # `paths`/`file` to write_file instead of `path`). Tolerate the most
        # common mixups so the task can proceed instead of failing 8x.
        if name == "write_file" and not args.get("path"):
            # Case 1: the whole arguments dict got double-wrapped, e.g.
            # {"arguments": "{\"path\": ..., \"content\": ...}"} — unwrap
            # repeatedly until no more "arguments" layers remain.
            for _ in range(3):
                wrapped = args.get("arguments")
                if not (isinstance(wrapped, str) and wrapped.strip()):
                    break
                try:
                    inner = json.loads(wrapped)
                except json.JSONDecodeError:
                    break
                if not isinstance(inner, dict):
                    break
                args.update(inner)
            for alt_key in ("paths", "files", "file_path", "filename"):
                alt = args.get(alt_key)
                if isinstance(alt, list) and alt:
                    args["path"] = str(alt[0])
                    break
                elif isinstance(alt, str) and alt.strip():
                    args["path"] = alt
                    break
            if args.get("path"):
                logger.info("write_file: normalized missing path from '%s'", alt_key)

        # --- Security: Input validation ---
        if self.config.security_enabled and self.config.input_validation_enabled:
            is_valid, error = self._validator.validate_tool_args(name, args)
            if not is_valid:
                logger.warning("Tool args validation failed: %s: %s", name, error)
                return ToolResult(success=False, error=f"参数验证失败: {error}")

        # --- Execute with retry ---
        retry_cfg = RetryConfig(
            enabled=bool(self.config.tool_retry_enabled and _RETRY_AVAILABLE),
            max_retries=self.config.tool_retry_max,
            base_delay=self.config.tool_retry_base_delay,
        ) if RetryConfig else None

        attempts = 0
        retry_history: list[dict] = []

        while True:
            try:
                start = time.time()
                result = tool.execute(**args)
                duration = time.time() - start

                # Record the call for rate limiting
                if self.config.rate_limit_enabled:
                    self._rate_limiter.record_tool_call(self.username)

                logger.info(
                    "Tool executed: %s by %s in %.1fms success=%s (attempt %d)",
                    name, self.username, duration * 1000, result.success, attempts + 1,
                )

                if result.success or retry_cfg is None:
                    if retry_history:
                        result.metadata["retries"] = retry_history
                    result.metadata["duration"] = round(duration, 3)
                    return result

                # Failed result — decide whether to retry.
                # NOTE: internal rate-limit failures are NOT retried here —
                # the sliding window (60s) won't recover within the retry
                # budget, so retrying just burns time. The orchestrator loop
                # handles them via its own backoff instead.
                if result.metadata.get("rate_limited"):
                    if retry_history:
                        result.metadata["retries"] = retry_history
                    return result
                # Approval-related failures (rejected/expired/blocked) are a
                # HUMAN decision, never transient — retrying would re-prompt
                # the user or loop forever.
                if result.metadata.get("approval"):
                    if retry_history:
                        result.metadata["retries"] = retry_history
                    return result
                if should_retry(
                    tool_retryable=getattr(tool, "retryable", True),
                    error_text=result.error,
                    attempts_done=attempts,
                    cfg=retry_cfg,
                ):
                    delay = compute_delay(attempts, retry_cfg)
                    retry_history.append({
                        "attempt": attempts + 1,
                        "error": result.error,
                        "retry_in": round(delay, 2),
                    })
                    logger.warning(
                        "Tool '%s' transient failure (attempt %d/%d): %s — retrying in %.1fs",
                        name, attempts + 1, retry_cfg.max_retries + 1, result.error, delay,
                    )
                    time.sleep(delay)
                    attempts += 1
                    continue

                # Not retryable / exhausted — attach history and return
                if retry_history:
                    result.metadata["retries"] = retry_history
                return result

            except Exception as e:
                logger.exception("Tool '%s' raised an exception (attempt %d)", name, attempts + 1)
                result = ToolResult(success=False, error=f"Tool error: {e}")

                if retry_cfg is not None and should_retry(
                    tool_retryable=getattr(tool, "retryable", True),
                    error_text=str(e),
                    attempts_done=attempts,
                    cfg=retry_cfg,
                ):
                    delay = compute_delay(attempts, retry_cfg)
                    retry_history.append({
                        "attempt": attempts + 1,
                        "error": str(e),
                        "retry_in": round(delay, 2),
                    })
                    logger.warning(
                        "Tool '%s' raised transient error (attempt %d/%d): %s — retrying in %.1fs",
                        name, attempts + 1, retry_cfg.max_retries + 1, e, delay,
                    )
                    time.sleep(delay)
                    attempts += 1
                    continue

                if retry_history:
                    result.metadata["retries"] = retry_history
                return result

    def set_config(self, **kwargs):
        """Update agent config dynamically."""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
            else:
                logger.warning("Unknown config key: %s", key)
