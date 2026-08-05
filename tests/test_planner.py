"""
Tests for the planning module (Plan, PlanTracker, PlanGenerator) and its
orchestrator integration.
"""

import pytest

from agent.planning.planner import Plan, PlanTracker, PlanGenerator

from conftest import ScriptedLLM


# ============================================================
# Parsing
# ============================================================

def _gen():
    g = object.__new__(PlanGenerator)
    g.max_steps = 8
    return g


def test_parse_valid_json():
    g = _gen()
    p = g._parse('{"goal": "写报告", "steps": [{"description": "搜索资料", "detail": "用web_search"}, {"description": "写文件"}]}')
    assert not p.is_empty
    assert p.goal == "写报告"
    assert len(p.steps) == 2
    assert p.steps[0]["detail"] == "用web_search"


def test_parse_fenced_json():
    g = _gen()
    fenced = "```json\n{\"goal\": \"g\", \"steps\": [{\"description\": \"步骤一\"}]}\n```"
    p = g._parse(fenced)
    assert len(p.steps) == 1


def test_parse_prose_wrapped():
    g = _gen()
    prose = '好的，计划如下：{"goal": "g", "steps": [{"description": "步骤一"}]} 完成'
    p = g._parse(prose)
    assert len(p.steps) == 1


def test_parse_truncated_json_repair():
    """DeepSeek truncates planner JSON mid-string; the repair must recover it."""
    g = _gen()
    truncated = '{"goal": "g", "steps": [{"description": "步骤一", "detail": "确保'
    p = g._parse(truncated)
    assert len(p.steps) == 1
    assert p.steps[0]["description"] == "步骤一"
    assert p.goal == "g"


def test_parse_garbage_returns_empty():
    g = _gen()
    assert g._parse("抱歉我无法生成计划").is_empty
    assert g._parse("").is_empty
    assert g._parse('{"goal": "", "steps": []}').is_empty


def test_parse_caps_steps():
    import json as _json
    g = object.__new__(PlanGenerator)
    g.max_steps = 3
    steps = [{"description": f"步骤{i}"} for i in range(6)]
    data = _json.dumps({"goal": "g", "steps": steps})
    p = g._parse(data)
    assert len(p.steps) == 3


# ============================================================
# PlanTracker
# ============================================================

def test_tracker_progress():
    plan = Plan("g", [
        {"description": "搜索资料"},
        {"description": "写代码"},
        {"description": "保存文件"},
    ])
    t = PlanTracker(plan)
    assert t.active
    assert t._current_index() == 0

    t.note_tool("web_search", {"query": "x"})  # matches 搜索
    assert t._current_index() == 1
    t.note_tool("execute_code", {})  # matches 代码
    assert t._current_index() == 2
    t.mark_step(2)
    assert t._current_index() is None  # all done


def test_tracker_progress_injection():
    plan = Plan("g", [{"description": "步骤一"}, {"description": "步骤二"}])
    t = PlanTracker(plan)
    block = t.build_progress_injection()
    assert "[计划进度]" in block
    assert "0/2" in block
    assert "步骤一 <- 当前" in block

    t.mark_step(0)
    block2 = t.build_progress_injection()
    assert "1/2" in block2
    assert "步骤二 <- 当前" in block2
    assert "[x] 步骤一" in block2


def test_plan_build_injection():
    plan = Plan("整理项目", [
        {"description": "读取文件列表", "detail": "用list_files"},
        {"description": "写入总结"},
    ])
    block = plan.build_injection()
    assert block.startswith("[执行计划]")
    assert "目标：整理项目" in block
    assert "1. 读取文件列表（用list_files）" in block
    assert "2. 写入总结" in block


# ============================================================
# PlanGenerator
# ============================================================

def test_should_plan():
    g = object.__new__(PlanGenerator)
    g.min_user_chars = 30

    assert g.should_plan("请帮我写一份关于人工智能发展趋势的研究报告，包含技术对比和未来展望，最后保存为文件")
    assert g.should_plan("写代码")  # short but has cue
    assert not g.should_plan("你好")  # short, no cue
    assert not g.should_plan("")


def test_generate_returns_plan():
    llm = ScriptedLLM('{"goal": "整理项目", "steps": [{"description": "读取文件列表"}, {"description": "写入总结文件"}]}')
    gen = PlanGenerator(llm, min_user_chars=1)
    plan = gen.generate("请帮我整理一下项目目录，生成一个总结文件")

    assert not plan.is_empty
    assert len(plan.steps) == 2
    assert plan.goal == "整理项目"


def test_generate_fail_soft():
    class Boom:
        def chat(self, **kw):
            raise RuntimeError("llm down")

    gen = PlanGenerator(Boom(), min_user_chars=1)
    plan = gen.generate("很长的请求内容用来触发规划" * 5)
    assert plan.is_empty  # never raises


def test_generate_skips_trivial():
    llm = ScriptedLLM('{"goal": "", "steps": []}')
    gen = PlanGenerator(llm, min_user_chars=100)  # high threshold
    plan = gen.generate("你好")
    assert plan.is_empty
    assert len(llm.calls) == 0  # planner never called


# ============================================================
# Orchestrator integration
# ============================================================

def test_plan_injected_and_executed(tmp_db, admin_user, tmp_conv):
    """Full flow: plan generated -> injected -> progress updated -> in result."""
    llm = ScriptedLLM()
    llm._responses = llm._wrap([
        # planner (call 1)
        '{"goal": "整理项目文件", "steps": [{"description": "读取文件列表"}, {"description": "写入总结文件"}]}',
        # iteration 1: list_files (matches 读取)
        llm.tool_use("list_files"),
        # iteration 2: final answer
        "项目整理完成。",
    ])
    from agent.presets import create_agent
    agent = create_agent(
        llm_client=llm,
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
    )

    result = agent.run(
        "请帮我整理这个项目的文件结构，写一份总结报告保存下来",
        conversation_id=tmp_conv,
    )

    # Plan injected in iteration 1
    assert "[执行计划]" in llm.injected.get("[执行计划]", "")
    # Progress injected in later iteration
    assert "[计划进度]" in llm.injected.get("[计划进度]", "")
    # Result carries plan info
    assert result.get("plan")
    assert result["plan"]["goal"] == "整理项目文件"
    done = [s for s in result["plan"]["steps"] if s["completed"]]
    assert len(done) == 1  # list_files completed step 1
    # Plan event emitted
    assert any(e.get("type") == "plan" for e in result["events"])


def test_no_plan_for_trivial_query(tmp_db, admin_user, tmp_conv):
    llm = ScriptedLLM("你好！")
    from agent.presets import create_agent
    agent = create_agent(
        llm_client=llm,
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
    )

    result = agent.run("你好", conversation_id=tmp_conv)

    assert "plan" not in result
    assert "[执行计划]" not in llm.injected


def test_planning_disabled(tmp_db, admin_user, tmp_conv):
    llm = ScriptedLLM("好的")
    from agent.presets import create_agent
    agent = create_agent(
        llm_client=llm,
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
        planning_enabled=False,
    )

    result = agent.run("请帮我写一个很复杂的报告", conversation_id=tmp_conv)

    assert "plan" not in result
    assert len(llm.calls) == 1  # no planner call
