"""
Tests for the eval suite: task definitions, deterministic checkers,
LLM judge, runner wiring, and report generation.

Run with: python3 -m pytest tests/test_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval.tasks import (
    EvalTask,
    TaskResult,
    TASKS,
    contains,
    file_exists,
    not_contains,
    code_output,
    code_runs,
    fib_stdout,
    make_llm_judge,
)
from eval.runner import build_workspace, run_task, TaskOutcome
from eval.report import build_markdown, build_json


# ============================================================
# Task suite sanity
# ============================================================

def test_suite_has_tasks_and_verification():
    assert len(TASKS) >= 20
    ids = [t.task_id for t in TASKS]
    assert len(ids) == len(set(ids)), "duplicate task ids"

    for t in TASKS:
        # Every task must have some verification: deterministic check,
        # required facts, or an LLM judge prompt.
        assert t.check_fn or t.required_facts or t.check_prompt, (
            f"task {t.task_id} has no verification"
        )
        assert isinstance(t.prompt, str) and t.prompt.strip()


def test_suite_covers_tags():
    tags = {tag for t in TASKS for tag in t.tags}
    for expected in ("file", "code", "qa", "planning", "web", "analysis", "safety"):
        assert expected in tags


def test_suite_has_difficulties():
    diffs = {t.difficulty for t in TASKS}
    assert diffs == {"easy", "medium", "hard"}
    assert sum(1 for t in TASKS if t.difficulty == "hard") >= 3


def test_suite_seed_files_are_wellformed():
    for t in TASKS:
        for rel, content in (t.seed_files or {}).items():
            assert rel.strip() and content.strip(), f"bad seed in {t.task_id}"


# ============================================================
# Deterministic checkers
# ============================================================

def _make_task(scratch="t"):
    return EvalTask(task_id="t", prompt="p", scratch_dir=scratch)


def test_contains_checker(tmp_path):
    root = tmp_path
    task = _make_task()
    (root / "a.txt").write_text("hello world", encoding="utf-8")

    assert contains("a.txt", "hello")(root, task).passed
    assert not contains("a.txt", "nope")(root, task).passed
    assert not contains("missing.txt", "x")(root, task).passed


def test_not_contains_checker(tmp_path):
    root = tmp_path
    task = _make_task()
    (root / "a.txt").write_text("no secrets here", encoding="utf-8")

    # "password" is absent → passes
    assert not_contains("a.txt", "password")(root, task).passed
    # "secrets" IS present → must fail
    assert not not_contains("a.txt", "secrets")(root, task).passed


def test_file_exists_min_chars(tmp_path):
    root = tmp_path
    task = _make_task()
    (root / "a.txt").write_text("x", encoding="utf-8")

    assert file_exists("a.txt")(root, task).passed
    assert not file_exists("a.txt", min_chars=5)(root, task).passed
    assert not file_exists("nope.txt")(root, task).passed


def test_code_output_checker(tmp_path):
    root = tmp_path
    task = _make_task()
    (root / "s.py").write_text(
        "print('hello 42')\n", encoding="utf-8"
    )
    assert code_output("s.py", "hello")(root, task).passed
    assert not code_output("s.py", "nope")(root, task).passed


def test_code_runs_catches_errors(tmp_path):
    root = tmp_path
    task = _make_task()
    (root / "bad.py").write_text(
        "raise ValueError('boom')\n", encoding="utf-8"
    )
    res = code_runs("bad.py")(root, task)
    assert not res.passed
    assert "boom" in res.detail


def test_fib_stdout(tmp_path):
    root = tmp_path
    task = _make_task()
    (root / "fib.py").write_text(
        "a, b = 0, 1\n"
        "for _ in range(10):\n"
        "    a, b = b, a + b\n"
        "print(a)\n",
        encoding="utf-8",
    )
    assert fib_stdout("fib.py", 10)(root, task).passed
    (root / "fib.py").write_text(
        "print('wrong')\n", encoding="utf-8"
    )
    assert not fib_stdout("fib.py", 10)(root, task).passed


def test_llm_judge_parses_json():
    class FakeJudgeLLM:
        def chat(self, messages=None, **kw):
            from agent.llm_client import LLMResponse
            return LLMResponse(
                type="text",
                content='{"passed": true, "reason": "回答正确"}',
                mode="native",
            )

    judge = make_llm_judge(FakeJudgeLLM())
    task = EvalTask(
        task_id="x", prompt="p", check_prompt="回答日本首都是哪里",
        required_facts=[],
    )
    res = judge(task, "东京")
    assert res.passed is True
    assert "回答正确" in res.detail


def test_llm_judge_tolerates_wrapped_json():
    class FakeJudgeLLM:
        def chat(self, messages=None, **kw):
            from agent.llm_client import LLMResponse
            return LLMResponse(
                type="text",
                content="```json\n{\"passed\": false, \"reason\": \"不对\"}\n```",
                mode="native",
            )

    judge = make_llm_judge(FakeJudgeLLM())
    task = EvalTask(task_id="x", prompt="p", check_prompt="q")
    res = judge(task, "answer")
    assert res.passed is False
    assert "不对" in res.detail


# ============================================================
# Runner wiring
# ============================================================

def test_build_workspace_creates_dirs(tmp_path, monkeypatch):
    root = tmp_path
    task = _make_task("sub")
    ws = build_workspace(root, task)
    assert ws.is_dir()
    # Numbers file for code_stats
    stats = EvalTask(task_id="code_stats", prompt="p", scratch_dir="code_stats")
    ws2 = build_workspace(root, stats)
    assert (ws2 / "numbers.txt").exists()
    # Seed files are placed in the workspace
    seeded = EvalTask(task_id="s", prompt="p", seed_files={"a.txt": "hello", "sub/b.txt": "x"})
    ws3 = build_workspace(root, seeded)
    assert (ws3 / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (ws3 / "sub" / "b.txt").read_text(encoding="utf-8") == "x"


def test_run_task_passes_on_deterministic_check(tmp_path, monkeypatch):
    """A task that writes hello.txt must pass end-to-end with a fake LLM."""
    import agent.orchestrator as orch_mod
    from agent.llm_client import LLMResponse

    class Scripted:
        backend_name = "Fake"
        model_name = "fake"

        def __init__(self):
            self.calls = 0

        def chat(self, messages=None, tools=None, **kw):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    type="tool_use",
                    tool_calls=[{
                        "id": "c1", "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": str(tmp_path / "eval" / "workspace_tasks" / "t" / "hello.txt"),
                                "content": "Hello, Agent!",
                            }),
                        },
                    }],
                    mode="native",
                )
            return LLMResponse(type="text", content="完成", mode="native")

    task = EvalTask(
        task_id="t", prompt="写 hello.txt", scratch_dir="t",
        check_fn=contains("hello.txt", "Hello, Agent!"),
    )
    # Short prompt (<30 chars) so planning doesn't consume the first response
    outcome = run_task(
        Scripted(), task, tmp_path,
        agent_kwargs={"planning_enabled": False},
    )
    assert outcome.passed is True
    assert "check passed" in outcome.reason or "exists" in outcome.reason
    assert outcome.trace_id  # trace recorder ran


def test_run_task_fails_when_missing_file(tmp_path):
    from agent.llm_client import LLMResponse

    class TextOnly:
        backend_name = "Fake"
        model_name = "fake"

        def chat(self, messages=None, tools=None, **kw):
            return LLMResponse(type="text", content="我不会用工具", mode="native")

    task = EvalTask(
        task_id="t", prompt="写 hello.txt", scratch_dir="t",
        check_fn=contains("hello.txt", "Hello, Agent!"),
    )
    outcome = run_task(TextOnly(), task, tmp_path)
    assert outcome.passed is False
    assert "missing file" in outcome.reason


# ============================================================
# Report generation
# ============================================================

def test_report_markdown_and_json(tmp_path):
    o1 = TaskOutcome(
        task_id="a", description="A", prompt="p", passed=True, reason="ok",
        duration=1.2, trace_id="tr1",
    )
    o2 = TaskOutcome(
        task_id="b", description="B", prompt="p", passed=False,
        reason="missing file", duration=0.5,
    )
    results = {
        "outcomes": [o1, o2],
        "summary": {
            "total": 2, "passed": 1, "failed": 1, "pass_rate": 50.0,
            "total_duration": 1.7, "total_tokens": 100,
            "total_tool_calls": 3, "timestamp": "2026-08-02 12:00:00",
        },
    }
    md = build_markdown(results, trace_base="http://x:1")
    assert "50.0%" in md
    assert "tr1" in md  # trace link present
    assert "失败详情" in md
    assert "missing file" in md

    j = json.loads(build_json(results))
    assert j["summary"]["pass_rate"] == 50.0
    assert len(j["outcomes"]) == 2
    assert j["outcomes"][0]["trace_id"] == "tr1"
