"""
TraceRecorder - captures agent execution as structured, replayable events.

A trace is a single agent run (one user message -> final answer). It records:
  - LLM calls: prompt (messages sent), response content, mode, token usage
  - Tool calls: name, args, result, duration, retries
  - Plan generation and per-step progress
  - Long-term memory injection / extraction
  - Security events (permission denials, validation failures, rate limits)
  - Loop guards triggered (dedup, failure-loop, budget, timeout)
  - Termination reason and total duration

Sensitive values (api keys, secrets) in tool arguments are redacted so the
trace store never persists credentials.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

# Termination reasons
FINISH_REASONS = {
    "text_response": "LLM returned final text response",
    "no_valid_tool_calls": "LLM emitted no valid tool calls",
    "tool_limit": "Total tool call budget exhausted",
    "iteration_limit": "Max loop iterations reached",
    "timeout": "Execution timeout exceeded",
    "loop_detected": "Loop guard fired (identical tool calls)",
    "failure_loop": "Failure-loop guard fired (consecutive failures)",
    "llm_error": "LLM call failed",
    "unexpected_type": "Unexpected response type",
    "validation_error": "User message validation failed",
    "rate_limited": "Rate limit reached",
    "busy": "Concurrency slot unavailable",
    "error": "Generic error",
    "cancelled": "User cancelled the run",
}

_SENSITIVE_KEYS = {
    "api_key", "apikey", "key", "token", "secret",
    "password", "passwd", "pwd", "authorization", "cookie",
}


def redact(value: Any, key: str = "", depth: int = 0) -> Any:
    """Recursively redact sensitive-looking keys in a nested structure.

    Leaves non-matching values untouched; replaces sensitive values with a
    short length-preserving hint. Depth-limited to avoid pathological nesting.
    """
    if depth > 8:
        return "<truncated>"
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_KEYS else redact(v, k, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, key, depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 20000:
        return value[:20000] + "...<truncated>"
    return value


@dataclass
class TraceEvent:
    """A single event inside a trace."""

    type: str  # llm_call | tool_call | tool_result | plan | plan_progress | memory_inject | memory_extract | security | loop_guard | error | info
    ts: float = field(default_factory=time.time)
    duration: float | None = None  # seconds (llm_call / tool_call)
    content: str | None = None
    messages: list | None = None  # llm_call: prompt sent to the model
    response: str | None = None  # llm_call: model text response (content)
    tool_calls: list | None = None  # llm_call: tool calls in the response
    mode: str | None = None  # "native" | "prompt"
    usage: dict | None = None  # token usage from the provider
    tool: str | None = None
    args: dict | None = None  # redacted tool arguments
    result: dict | None = None  # tool result dict
    retries: list | None = None  # retry history for a tool call
    error: str | None = None
    detail: str | None = None  # generic payload (security / guard messages)
    iteration: int | None = None

    def to_dict(self) -> dict:
        d = {"type": self.type, "ts": round(self.ts, 3)}
        for k in (
            "duration", "content", "messages", "response", "tool_calls", "mode",
            "usage", "tool", "args", "result", "retries", "error", "detail",
            "iteration",
        ):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


@dataclass
class AgentTrace:
    """A complete record of one agent run."""

    trace_id: str
    user_message: str
    username: str
    conversation_id: str | None
    backend: str  # LLM backend name ("" if unknown)
    model: str  # model name ("" if unknown)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    duration: float | None = None
    finish_reason: str = "error"
    finish_detail: str = ""
    success: bool = False
    content: str = ""
    tool_calls_made: int = 0
    iterations: int = 0
    plan_generated: bool = False
    plan_goal: str = ""
    plan_steps: int = 0
    total_tokens: int = 0
    events: list[TraceEvent] = field(default_factory=list)

    def add(self, event: TraceEvent) -> None:
        self.events.append(event)

    def finish(
        self,
        reason: str = "error",
        detail: str = "",
        success: bool = False,
        content: str = "",
        tool_calls_made: int = 0,
        iterations: int = 0,
    ) -> None:
        self.finish_reason = reason
        self.finish_detail = detail
        self.success = success
        self.content = content
        self.tool_calls_made = tool_calls_made
        self.iterations = iterations
        self.finished_at = time.time()
        self.duration = round(self.finished_at - self.started_at, 3)

    def to_dict(self, with_events: bool = True) -> dict:
        d = {
            "trace_id": self.trace_id,
            "user_message": self.user_message[:200],
            "username": self.username,
            "conversation_id": self.conversation_id,
            "backend": self.backend,
            "model": self.model,
            "started_at": self.started_at,
            "started_iso": datetime.fromtimestamp(
                self.started_at, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "finished_at": self.finished_at,
            "duration": self.duration,
            "finish_reason": self.finish_reason,
            "finish_detail": self.finish_detail,
            "success": self.success,
            "content": self.content,
            "tool_calls_made": self.tool_calls_made,
            "iterations": self.iterations,
            "plan_generated": self.plan_generated,
            "plan_goal": self.plan_goal,
            "plan_steps": self.plan_steps,
            "total_tokens": self.total_tokens,
            "event_count": len(self.events),
        }
        if with_events:
            d["events"] = [e.to_dict() for e in self.events]
        return d


class TraceRecorder:
    """
    Records traces. Attach to an orchestrator via attach() — the orchestrator
    then reports all of its activity through recorder.report_*().

    Recording is best-effort: every report method catches exceptions and
    never raises, so observability can never break the agent loop.
    """

    def __init__(self, trace: AgentTrace | None = None):
        self.trace = trace or AgentTrace(
            trace_id=uuid.uuid4().hex[:12],
            user_message="",
            username="",
            conversation_id=None,
            backend="",
            model="",
        )
        self._attached = False

    # ----------------------------------------------------------
    # Attachment helpers (used by the orchestrator)
    # ----------------------------------------------------------

    def attach(self) -> "TraceRecorder":
        """Mark the recorder as attached to an orchestrator."""
        self._attached = True
        return self

    # ----------------------------------------------------------
    # Reporters — all fail-soft
    # ----------------------------------------------------------

    def _safe(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # pragma: no cover - safety net only
            return None

    def report_start(self, user_message: str, username: str, conversation_id: str | None,
                     backend: str = "", model: str = "") -> None:
        self._safe(self._set_meta, user_message, username, conversation_id, backend, model)

    def _set_meta(self, user_message, username, conversation_id, backend, model):
        self.trace.user_message = user_message
        self.trace.username = username
        self.trace.conversation_id = conversation_id
        if backend:
            self.trace.backend = backend
        if model:
            self.trace.model = model

    def report_info(self, detail: str, **extra) -> None:
        self._safe(self.trace.add, TraceEvent(type="info", detail=detail, **extra))

    def report_security(self, detail: str, **extra) -> None:
        self._safe(self.trace.add, TraceEvent(type="security", detail=detail, **extra))

    def report_error(self, error: str, **extra) -> None:
        self._safe(self.trace.add, TraceEvent(type="error", error=str(error)[:2000], **extra))

    def report_loop_guard(self, detail: str, **extra) -> None:
        self._safe(self.trace.add, TraceEvent(type="loop_guard", detail=detail, **extra))

    def report_plan(self, goal: str, steps: list, plan_block: str = "") -> None:
        self._safe(self._set_plan, goal, steps, plan_block)

    def _set_plan(self, goal, steps, plan_block):
        self.trace.plan_generated = True
        self.trace.plan_goal = str(goal)[:200]
        self.trace.plan_steps = len(steps or [])
        self.trace.add(TraceEvent(
            type="plan",
            content=(plan_block or "")[:4000],
            detail=f"goal={goal!r} steps={len(steps or [])}",
        ))

    def report_plan_progress(self, tracker) -> None:
        self._safe(self._plan_progress, tracker)

    def _plan_progress(self, tracker):
        try:
            progress = tracker.build_progress_injection()
        except Exception:
            progress = None
        self.trace.add(TraceEvent(
            type="plan_progress",
            detail=f"remaining={len(tracker.remaining_steps)} completed={len(tracker.completed_steps)}"
                   if getattr(tracker, "remaining_steps", None) is not None else "",
            content=(progress or "")[:4000],
            iteration=getattr(self, "_iteration", None),
        ))

    def report_plan_review(self, review: dict) -> None:
        """Record an LLM plan-progress review (marks/unmarks steps)."""
        self._safe(self.trace.add, TraceEvent(
            type="plan_review",
            detail=(
                f"marked={review.get('marked')} unmarked={review.get('unmarked')} "
                f"reason={review.get('detail', '')[:200]}"
            ),
        ))

    def report_memory_inject(self, memory_block: str, count: int) -> None:
        self._safe(self.trace.add, TraceEvent(
            type="memory_inject",
            detail=f"injected {count} memories",
            content=(memory_block or "")[:4000],
        ))

    def report_memory_extract(self, result: dict) -> None:
        self._safe(self.trace.add, TraceEvent(
            type="memory_extract",
            detail=f"extracted={result.get('extracted', 0)} stored={result.get('stored', 0)}",
        ))

    def report_compression(self, stats: dict) -> None:
        """Record a context-compression event (older messages summarized)."""
        self._safe(self.trace.add, TraceEvent(
            type="compression",
            detail=(
                f"压缩 {stats.get('old_messages', 0)} 条消息 -> 摘要，"
                f"节省 {stats.get('saved_tokens', 0)} tokens"
            ),
            content=(
                f"old_messages={stats.get('old_messages', 0)} "
                f"kept_messages={stats.get('kept_messages', 0)} "
                f"old_tokens={stats.get('old_tokens', 0)} "
                f"new_tokens={stats.get('new_tokens', 0)} "
                f"saved_tokens={stats.get('saved_tokens', 0)} "
                f"summary_chars={stats.get('summary_chars', 0)} "
                f"persisted={stats.get('persisted', False)}"
            ),
        ))

    def report_llm_call(self, messages: list | None, response, duration: float | None = None,
                        iteration: int | None = None) -> None:
        self._safe(self._llm_call, messages, response, duration, iteration)

    def _llm_call(self, messages, response, duration, iteration):
        usage = getattr(response, "usage", None) or {}
        if isinstance(usage, dict):
            tokens = (
                usage.get("total_tokens")
                or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                or 0
            )
            self.trace.total_tokens += int(tokens)
        mode = getattr(response, "mode", None) or ""
        if mode and not self.trace.model:
            self.trace.model = getattr(response, "model", "") or ""
        self.trace.add(TraceEvent(
            type="llm_call",
            duration=round(duration, 3) if duration is not None else None,
            messages=self._summarize_messages(messages),
            response=(getattr(response, "content", "") or "")[:4000] or None,
            tool_calls=[
                {
                    "name": tc.get("function", {}).get("name", ""),
                    "args": redact(
                        _safe_json_loads(tc.get("function", {}).get("arguments", "{}"))
                    ),
                }
                for tc in (getattr(response, "tool_calls", None) or [])
            ] or None,
            mode=mode or None,
            usage=usage or None,
            iteration=iteration,
        ))

    @staticmethod
    def _summarize_messages(messages: list | None) -> list | None:
        """Reduce a prompt to a compact, replayable list of messages."""
        if not messages:
            return None
        out = []
        for m in messages[-40:]:  # cap prompt size in traces
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):  # multimodal
                content = json.dumps(content, ensure_ascii=False)[:2000]
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role == "tool":
                # Tool results may echo sensitive values back — redact the
                # JSON payload before persisting it in the trace.
                try:
                    content = json.dumps(
                        redact(json.loads(content)), ensure_ascii=False
                    )
                except Exception:
                    pass
            entry = {"role": role, "content": content[:4000]}
            if role == "assistant" and m.get("tool_calls"):
                entry["tool_calls"] = [
                    {"name": tc.get("function", {}).get("name", ""),
                     "args": redact(
                         _safe_json_loads(tc.get("function", {}).get("arguments", "{}"))
                     )}
                    for tc in m["tool_calls"]
                ]
            elif role == "tool":
                entry["tool_call_id"] = m.get("tool_call_id", "")
            out.append(entry)
        return out

    def report_tool_call(self, name: str, args: dict, iteration: int | None = None) -> None:
        self._safe(self.trace.add, TraceEvent(
            type="tool_call",
            tool=name,
            args=redact(args),
            iteration=iteration,
        ))

    def report_tool_result(self, name: str, result: dict, duration: float | None = None,
                           retries: list | None = None, iteration: int | None = None,
                           from_cache: bool = False) -> None:
        self._safe(self.trace.add, TraceEvent(
            type="tool_result",
            tool=name,
            result=redact(result),
            duration=round(duration, 3) if duration is not None else None,
            retries=retries or None,
            detail="cached" if from_cache else None,
            iteration=iteration,
        ))

    def finish(self, reason: str, detail: str = "", success: bool = False,
               content: str = "", tool_calls_made: int = 0, iterations: int = 0) -> None:
        self._safe(self.trace.finish, reason, detail, success, content,
                   tool_calls_made, iterations)


def _safe_json_loads(s):
    try:
        return json.loads(s) if isinstance(s, str) else s
    except Exception:
        return {}
