"""
Tests for LLM plan-progress review (StepReviewer + PlanTracker integration).

Covers: reviewer parsing (plain JSON, fenced, broken), verdict application
(mark + unmark), fail-soft behavior (LLM error keeps heuristics), review
call capping, and orchestrator wiring (reviewer attached when configured).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.planning.planner import Plan, PlanTracker, StepReviewer
from agent.llm_client import LLMResponse


class ScriptedReviewLLM:
    """Fake LLM that returns a scripted review JSON."""

    def __init__(self, *responses, backend="Fake", model="fake"):
        self._responses = list(responses)
        self.calls = 0
        self.last_messages = None
        self.backend_name = backend
        self.model_name = model

    def chat(self, messages=None, **kw):
        self.calls += 1
        self.last_messages = messages
        if self._responses:
            r = self._responses.pop(0)
        else:
            r = '{"completed": [], "incomplete": [], "reason": "no change"}'
        return LLMResponse(type="text", content=r, mode="native")


def _snapshot(goal="g", steps=None, completed=None, tools=None):
    return {
        "goal": goal,
        "steps": steps
        or [
            {"index": 0, "description": "搜索资料", "detail": "", "completed": True},
            {"index": 1, "description": "写入文件", "detail": "", "completed": False},
            {"index": 2, "description": "执行代码", "detail": "", "completed": False},
        ],
        "completed": sorted(completed or [0]),
        "tool_history": tools or [{"tool": "web_search", "args": {}}],
    }


# ============================================================
# StepReviewer parsing
# ============================================================

def test_reviewer_parses_plain_json():
    r = StepReviewer(ScriptedReviewLLM('{"completed": [0, 1], "incomplete": [2], "reason": "ok"}'))
    v = r.review(_snapshot())
    assert v == {"completed": [0, 1], "incomplete": [2], "reason": "ok"}


def test_reviewer_parses_fenced_json():
    r = StepReviewer(ScriptedReviewLLM('```json\n{"completed": [1], "incomplete": [], "reason": "x"}\n```'))
    v = r.review(_snapshot())
    assert v["completed"] == [1]


def test_reviewer_tolerates_wrapped_json():
    r = StepReviewer(ScriptedReviewLLM('回复: {"completed": [0], "incomplete": [], "reason": "y"} 完毕'))
    v = r.review(_snapshot())
    assert v["completed"] == [0]


def test_reviewer_returns_none_on_garbage():
    r = StepReviewer(ScriptedReviewLLM("这完全不是 JSON"))
    assert r.review(_snapshot()) is None


def test_reviewer_returns_none_on_llm_exception():
    class Boom:
        def chat(self, **kw):
            raise RuntimeError("api down")

    r = StepReviewer(Boom())
    assert r.review(_snapshot()) is None


def test_reviewer_caps_calls():
    r = StepReviewer(ScriptedReviewLLM('{"completed": [], "incomplete": [], "reason": ""}'), max_review_calls=2)
    assert r.review(_snapshot()) is not None
    assert r.review(_snapshot()) is not None
    assert r.review(_snapshot()) is None  # capped


# ============================================================
# PlanTracker + reviewer integration
# ============================================================

def _tracker(steps_desc, completed=None):
    t = PlanTracker(Plan("g", [{"description": d, "detail": ""} for d in steps_desc]))
    for i in completed or []:
        t.mark_step(i)
    return t


def test_review_marks_unmarked_steps():
    r = StepReviewer(ScriptedReviewLLM('{"completed": [0, 1], "incomplete": [], "reason": "both done"}'))
    t = _tracker(["搜索资料", "写入文件", "执行代码"], completed=[0])
    t.tool_history = [("web_search", {}), ("write_file", {})]
    t.attach_reviewer(r)

    res = t.review_progress()
    assert res["marked"] == [1]
    assert t.completed == {0, 1}
    assert res["detail"] == "both done"


def test_review_unmarks_false_positive():
    """Heuristic wrongly marked step 2 done; reviewer should unmark it."""
    r = StepReviewer(ScriptedReviewLLM('{"completed": [0], "incomplete": [2], "reason": "代码没执行"}'))
    t = _tracker(["搜索资料", "写入文件", "执行代码"], completed=[0, 2])
    t.tool_history = [("web_search", {})]
    t.attach_reviewer(r)

    res = t.review_progress()
    assert res["unmarked"] == [2]
    assert t.completed == {0}


def test_review_fail_soft_keeps_heuristics():
    class Boom:
        def chat(self, **kw):
            raise RuntimeError("api down")

    r = StepReviewer(Boom())
    t = _tracker(["搜索资料", "写入文件"], completed=[0])
    t.attach_reviewer(r)

    res = t.review_progress()
    assert res is None
    assert t.completed == {0}  # unchanged


def test_review_noop_when_no_reviewer():
    t = _tracker(["搜索资料"])
    assert t.review_progress() is None


def test_review_without_plan_returns_none():
    t = PlanTracker(None)
    assert t.review_progress() is None


def test_tool_history_recorded_by_note_tool():
    t = _tracker(["搜索资料"])
    t.note_tool("web_search", {"query": "x"})
    assert t.tool_history == [("web_search", {"query": "x"})]


def test_review_snapshot_includes_tool_history():
    """The prompt sent to the reviewer must contain the evidence."""
    llm = ScriptedReviewLLM('{"completed": [], "incomplete": [], "reason": ""}')
    r = StepReviewer(llm)
    t = _tracker(["搜索资料", "写入文件"], completed=[0])
    t.tool_history = [("web_search", {"query": "ai"}), ("write_file", {"path": "a.md"})]
    t.attach_reviewer(r)
    t.review_progress()

    user_msg = llm.last_messages[-1]["content"]
    assert "web_search" in user_msg
    assert "write_file" in user_msg
    assert "写入文件" in user_msg


# ============================================================
# Orchestrator wiring
# ============================================================

def test_presets_wire_reviewer(tmp_db):
    """create_agent wires a StepReviewer when planning is enabled."""
    from agent.presets import create_agent

    agent = create_agent(
        llm_client=ScriptedReviewLLM(),
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        planning_enabled=True,
    )
    assert agent.plan_reviewer is not None
    assert isinstance(agent.plan_reviewer, StepReviewer)


def test_presets_disable_reviewer(tmp_db):
    from agent.presets import create_agent

    agent = create_agent(
        llm_client=ScriptedReviewLLM(),
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        planning_enabled=True,
        plan_review_enabled=False,
    )
    assert agent.plan_reviewer is None
