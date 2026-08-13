"""
Eval runner - executes the eval tasks against a real agent instance.

Each task gets a fresh agent (new trace) and a scratch workspace:
  eval/workspace_tasks/<task_id>/

Runs in-process using the app's own AgentLLMClient and create_agent, so
the evaluated behavior matches production (tools, memory, planning, retry).
Every run writes a trace via the orchestrator's recorder, and the trace_id
is attached to the task result so it can be replayed on the /traces page.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.llm_client import AgentLLMClient
from agent.presets import create_agent
from agent.security.permissions import PermissionLevel

try:
    from agent.observability.storage import TraceStore
    _HAS_TRACE_STORE = True
except ImportError:  # pragma: no cover
    TraceStore = None
    _HAS_TRACE_STORE = False

from eval.tasks import EvalTask, TaskResult, make_llm_judge

logger = logging.getLogger("eval.runner")


@dataclass
class TaskOutcome:
    """Full result of running one task."""

    task_id: str
    description: str
    prompt: str
    passed: bool
    difficulty: str = "easy"
    reason: str = ""
    score: float | None = None
    duration: float = 0.0
    iterations: int = 0
    tool_calls: int = 0
    tokens: int = 0
    trace_id: str = ""
    events: list[dict] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    run_index: int = 1  # which repetition this outcome is (1-based)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "passed": self.passed,
            "difficulty": self.difficulty,
            "reason": self.reason,
            "score": self.score,
            "duration": round(self.duration, 2),
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "trace_id": self.trace_id,
            "summary": self.summary,
            "error": self.error,
            "run_index": self.run_index,
        }


def build_workspace(project_root: Path, task: EvalTask) -> Path:
    """Prepare the scratch workspace for one task (fresh, wiped).

    The workspace root IS the agent's working directory — files written by
    the agent land here, and checkers look here.
    """
    ws = project_root / "eval" / "workspace_tasks" / task.task_id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True, exist_ok=True)
    # Pre-place seed files (e.g. buggy.py, sales.csv)
    for rel, content in (task.seed_files or {}).items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # Numbers file for code_stats
    if task.task_id == "code_stats":
        (ws / "numbers.txt").write_text(
            "5\n10\n15\n20\n25\n", encoding="utf-8"
        )
    return ws


def run_task(
    llm: AgentLLMClient,
    task: EvalTask,
    project_root: Path,
    judge=None,
    agent_kwargs: dict | None = None,
) -> TaskOutcome:
    """Run a single task against a fresh agent; returns the outcome."""
    ws = build_workspace(project_root, task)
    # Analysis tasks get the project root as their workspace so they can
    # read the repo; everything else is confined to the scratch dir.
    agent_ws = project_root if task.workspace == "project" else ws
    agent = create_agent(
        llm_client=llm,
        db=None,  # eval runs are isolated: no chat DB, no cross-run memory
        # Default: the agent's workspace is the task scratch dir (file tools
        # confined there — tasks can't read/clobber project files). Analysis
        # tasks opt into the project root via task.workspace="project".
        workspace_dir=str(agent_ws),
        username="eval",
        **(agent_kwargs or {}),
    )
    # Eval agent gets the same permissions as a regular user
    agent._permissions.register_user("eval", PermissionLevel.USER)

    # Persist eval traces to the app's trace store so reports can link to
    # replay pages (data/traces; fail-soft if the dir is unwritable).
    if _HAS_TRACE_STORE:
        try:
            _store = TraceStore(project_root / "data" / "traces")
            agent.trace_sink = _store.save
        except Exception:
            _store = None

    outcome = TaskOutcome(
        task_id=task.task_id,
        description=task.description,
        prompt=task.prompt,
        passed=False,
        difficulty=task.difficulty,
    )
    start = time.time()
    try:
        result = agent.run(task.prompt)
        outcome.duration = time.time() - start
        outcome.iterations = result.get("iterations", 0)
        outcome.tool_calls = result.get("tool_calls_made", 0)
        outcome.summary = (result.get("content") or "")[:2000]
        final_answer = result.get("content") or ""

        # Trace bookkeeping
        rec = agent.trace_recorder
        if rec is not None:
            outcome.trace_id = rec.trace.trace_id
            outcome.tokens = getattr(rec.trace, "total_tokens", 0)
            outcome.events = [e.to_dict() for e in rec.trace.events]

        # Verify
        verdict = task.verify(final_answer, ws, llm_judge=judge)
        outcome.passed = verdict.passed
        outcome.reason = verdict.detail
        outcome.score = verdict.score
        logger.info(
            "[%s] %s (%s) %.1fs", task.task_id, "PASS" if verdict.passed else "FAIL",
            verdict.detail[:80], outcome.duration,
        )
    except Exception as e:
        outcome.duration = time.time() - start
        outcome.error = str(e)
        outcome.reason = f"runner exception: {e}"
        logger.exception("[%s] runner exception", task.task_id)
    return outcome


def run_eval(
    llm: AgentLLMClient,
    tasks: list[EvalTask],
    project_root: Path,
    agent_kwargs: dict | None = None,
    verbose: bool = True,
    repeat: int = 1,
) -> dict:
    """Run all tasks (each `repeat` times); returns outcomes + summary.

    Every outcome is one (task, run_index) pair. Aggregation (pass rate per
    task, stability) is computed from these raw outcomes.
    """
    judge = make_llm_judge(llm)
    outcomes: list[TaskOutcome] = []
    for i, task in enumerate(tasks, 1):
        for run_idx in range(1, repeat + 1):
            if verbose:
                if repeat > 1:
                    logger.info(
                        "=== [%d/%d] %s (run %d/%d): %s ===",
                        i, len(tasks), task.task_id, run_idx, repeat, task.description,
                    )
                else:
                    logger.info("=== [%d/%d] %s: %s ===", i, len(tasks), task.task_id, task.description)
            outcome = run_task(llm, task, project_root, judge=judge, agent_kwargs=agent_kwargs)
            outcome.run_index = run_idx
            outcomes.append(outcome)

    passed = sum(1 for o in outcomes if o.passed)
    total_duration = sum(o.duration for o in outcomes)
    total_tokens = sum(o.tokens for o in outcomes)
    total_tool_calls = sum(o.tool_calls for o in outcomes)

    # Per-task aggregation (repeat > 1: pass rate across runs)
    per_task = {}
    by_task: dict[str, list[TaskOutcome]] = {}
    for o in outcomes:
        by_task.setdefault(o.task_id, []).append(o)
    for tid, runs in by_task.items():
        n_pass = sum(1 for r in runs if r.passed)
        per_task[tid] = {
            "runs": len(runs),
            "passed": n_pass,
            "pass_rate": round(n_pass / len(runs) * 100, 1),
            "avg_duration": round(sum(r.duration for r in runs) / len(runs), 2),
            "avg_tokens": int(sum(r.tokens for r in runs) / len(runs)),
            "stable": all(r.passed for r in runs) or not any(r.passed for r in runs),
            "first_trace_id": runs[0].trace_id,
        }

    return {
        "outcomes": outcomes,
        "per_task": per_task,
        "summary": {
            "total": len(outcomes),
            "passed": passed,
            "failed": len(outcomes) - passed,
            "pass_rate": round(passed / len(outcomes) * 100, 1) if outcomes else 0.0,
            "total_duration": round(total_duration, 1),
            "total_tokens": total_tokens,
            "total_tool_calls": total_tool_calls,
            "repeat": repeat,
            "task_count": len(by_task),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
