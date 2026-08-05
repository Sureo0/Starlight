"""
Tests for context compression (LLM-based conversation summarization).

Covers:
  - Cost judgement: should_compress / compute_gain (never compress when it
    would not save tokens, never compress tiny histories)
  - Summarization: incremental merge with previous summary, boundary selection
    (tool round-trips stay intact), summary block formatting
  - Persistence: summary saved/loaded per conversation (跨回合复用)
  - Orchestrator integration: stored summary injected at turn start;
    in-loop compression fires on long histories; fail-soft when the
    summarizer LLM errors
"""

from __future__ import annotations

from conftest import ScriptedLLM, SimpleTool

from agent.compression.manager import CompressionManager
from agent.compression.summarizer import ContextSummarizer
from agent.orchestrator import AgentConfig


# ============================================================
# Helpers
# ============================================================

def _make_history(n=30, role_cycle=("user", "assistant")):
    """Build a large conversation history (~1.2k tokens per message)."""
    line = "这是一条用于模拟长对话的历史消息，包含大量具体细节、中间推理过程和工具执行结果描述。" * 20
    return [
        {"role": role_cycle[i % len(role_cycle)], "content": f"{line} 第 {i} 条"}
        for i in range(n)
    ]


class SummaryLLM:
    """LLM double whose summary content can be scripted."""

    def __init__(self, summary=None, fail=False):
        default = "- 用户想要实现上下文压缩\n- 已完成核心模块设计"
        self.summary = default if summary is None else summary
        self.fail = fail
        self.calls = 0
        self.prompts = []

    def chat(self, messages=None, tools=None, **kw):
        self.calls += 1
        self.prompts.append(messages or [])
        if self.fail:
            raise RuntimeError("simulated summarizer failure")
        return ScriptedLLM.text(self, self.summary)

    def text(self, content="ok", **kw):
        from agent.llm_client import LLMResponse
        return LLMResponse(type="text", content=content, mode="native", **kw)


def _make_manager(llm=None, db=None, budget=32000, trigger=0.75, **kw):
    return CompressionManager(
        llm=llm or SummaryLLM(),
        budget=budget,
        trigger_ratio=trigger,
        min_messages=kw.pop("min_messages", 10),
        keep_recent=kw.pop("keep_recent", 6),
        min_gain_tokens=kw.pop("min_gain_tokens", 800),
        persist_store=db,
        **kw,
    )


# ============================================================
# Cost judgement
# ============================================================

class TestCostJudgement:
    def test_should_compress_false_for_small_history(self):
        m = _make_manager()
        assert m.should_compress(_make_history(n=5)) is False

    def test_should_compress_false_below_trigger(self):
        m = _make_manager()
        history = [{"role": "user", "content": "短消息"}] * 20
        assert m.should_compress(history) is False

    def test_should_compress_true_when_over_trigger(self):
        m = _make_manager()
        assert m.should_compress(_make_history(n=40)) is True

    def test_compress_returns_unchanged_when_not_needed(self):
        llm = SummaryLLM()
        m = _make_manager(llm=llm)
        small = [{"role": "user", "content": "短消息"}] * 20
        result, _, stats = m.compress(small)
        assert result == small
        assert stats is None
        assert llm.calls == 0  # no LLM call wasted

    def test_gain_negative_for_tiny_history(self):
        m = _make_manager()
        assert m.compute_gain([{"role": "user", "content": "x"}] * 3) < 0


# ============================================================
# Summarization & boundaries
# ============================================================

class TestSummarization:
    def test_compress_replaces_old_messages_with_summary_block(self):
        llm = SummaryLLM()
        m = _make_manager(llm=llm)
        history = _make_history(n=40)
        result, summary, stats = m.compress(history)
        assert stats is not None and stats["triggered"] is True
        assert result[0]["role"] == "system"
        assert result[0]["content"].startswith("[对话摘要]")
        assert summary.startswith("-")
        # Recent messages kept verbatim
        assert len(result) == stats["kept_messages"] + 1
        assert result[1:] == history[-stats["kept_messages"]:]

    def test_keeps_recent_messages_verbatim(self):
        m = _make_manager(keep_recent=4)
        result, _, stats = m.compress(_make_history(n=40))
        assert stats["kept_messages"] == 4
        assert result[-4:] == _make_history(n=40)[-4:]

    def test_boundary_keeps_tool_round_trip_together(self):
        """A tool result must never be separated from its assistant tool_calls."""
        m = _make_manager(keep_recent=2)
        big = "很长" * 800  # ~1.2k tokens per message so the history triggers
        history = [
            {"role": "user", "content": "开始" + big},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "结果" + big},
        ] * 12  # 36 messages
        result, _, stats = m.compress(history)
        assert stats is not None
        for i, msg in enumerate(result):
            if msg.get("tool_calls"):
                assert i + 1 < len(result)
                assert result[i + 1]["role"] == "tool"
        # The very last round-trip is always kept intact
        assert result[-1]["role"] == "tool"
        assert result[-2].get("tool_calls")

    def test_incremental_merge_with_previous_summary(self):
        llm = SummaryLLM(summary="- 新要点：完成了压缩\n- 旧要点仍然成立")
        m = _make_manager(llm=llm)
        prev = "- 旧要点一\n- 旧要点二"
        result, _, stats = m.compress(_make_history(n=40), prev_summary=prev)
        assert stats is not None
        block = result[0]["content"]
        assert "旧要点一" in block
        assert "旧要点二" in block
        assert "新要点" in block
        # The prompt included the previous summary (incremental merge)
        assert prev in llm.prompts[-1][0]["content"]

    def test_summarizer_clean_strips_fences_and_prefix(self):
        llm = SummaryLLM(summary="```markdown\n对话摘要：\n- 要点甲\n- 要点乙\n```")
        s = ContextSummarizer(llm=llm)
        out = s.summarize([{"role": "user", "content": "你好" * 100}])
        assert not out.startswith("```")
        assert not out.startswith("对话摘要")
        assert "- 要点甲" in out

    def test_summarizer_too_short_falls_back_to_recall(self):
        """A stub summary (too short) is kept instead of losing everything."""
        llm = SummaryLLM(summary="- 短")  # too short but structured
        s = ContextSummarizer(llm=llm)
        chunk = [{"role": "user", "content": "关键内容ABC" * 100}]
        out = s.summarize(chunk)
        assert "- 短" in out  # model output preserved

    def test_summarizer_empty_output_falls_back_to_recall(self):
        """An EMPTY model output falls back to verbatim recall (nothing lost)."""
        llm = SummaryLLM(summary="")
        s = ContextSummarizer(llm=llm)
        chunk = [{"role": "user", "content": "关键内容ABC" * 100}]
        out = s.summarize(chunk)
        assert "关键内容ABC" in out  # verbatim recall, nothing lost

    def test_fail_soft_on_llm_error(self):
        llm = SummaryLLM(fail=True)
        m = _make_manager(llm=llm)
        result, _, stats = m.compress(_make_history(n=40), prev_summary="- 旧摘要")
        assert stats is not None
        assert "旧摘要" in result[0]["content"]  # previous summary preserved


# ============================================================
# Persistence (跨回合复用)
# ============================================================

class TestPersistence:
    def test_save_and_load_summary(self, tmp_db, tmp_conv):
        m = _make_manager(db=tmp_db)
        assert m.load_summary(tmp_conv) == ""
        assert m.save_summary(tmp_conv, "- 摘要内容") is True
        assert m.load_summary(tmp_conv) == "- 摘要内容"

    def test_compress_persists_summary(self, tmp_db, tmp_conv):
        llm = SummaryLLM()
        m = _make_manager(llm=llm, db=tmp_db)
        m.set_conversation(tmp_conv)
        _, summary, stats = m.compress(_make_history(n=40))
        assert stats["persisted"] is True
        assert m.load_summary(tmp_conv) == summary

    def test_no_persistence_without_conversation(self, tmp_db):
        llm = SummaryLLM()
        m = _make_manager(llm=llm, db=tmp_db)
        m.set_conversation(None)
        _, _, stats = m.compress(_make_history(n=40))
        assert stats["persisted"] is False

# ============================================================
# Orchestrator integration
# ============================================================

class TestOrchestratorIntegration:
    def test_stored_summary_injected_at_turn_start(self, tmp_db, tmp_conv, agent_factory):
        """A persisted summary is injected as a system message on the next turn."""
        tmp_db.save_summary(tmp_conv, "- 用户之前讨论过数据库设计")
        llm = ScriptedLLM()
        agent = agent_factory(
            llm=llm,
            config=AgentConfig(permission_enabled=False, compression_enabled=True),
        )
        agent.compression = _make_manager(llm=llm, db=tmp_db)
        result = agent.run("继续之前的话题", tmp_conv)
        assert result["content"] == "ok"
        sent = llm.calls[-1]
        texts = [m.get("content", "") for m in sent if m.get("role") == "system"]
        assert any("[对话摘要]" in t and "数据库设计" in t for t in texts)

    def test_in_loop_compression_fires_on_long_history(self, tmp_db, tmp_conv, agent_factory):
        """Multiple tool round-trips with long results inflate the history past
        the trigger; the next iteration's pre-call compression replaces older
        messages with a summary (hook receives stats)."""
        from agent.tools.registry import ToolRegistry

        llm = ScriptedLLM()
        agent = agent_factory(
            llm=llm,
            config=AgentConfig(
                permission_enabled=False,
                compression_enabled=True,
                compression_min_messages=6,
                compression_min_gain_tokens=100,
                compression_keep_recent=4,
                max_iterations=20,
            ),
        )
        events = []
        agent.compression = _make_manager(
            llm=llm, db=tmp_db, min_messages=6, keep_recent=4,
            min_gain_tokens=100, hook=events.append,
        )

        # A tool whose result is huge (~13k tokens) inflates the history fast
        from agent.tools.base import ToolResult

        class LongTool(SimpleTool):
            def __init__(self):
                super().__init__(name="longtool")
                self._result = ToolResult(success=True, output="结果" * 3000)

        registry = ToolRegistry()
        registry.register(LongTool())
        agent.tools = registry

        # Several tool round-trips, then a final text answer
        llm._responses = [
            llm.tool_use("longtool", {}),
            llm.tool_use("longtool", {}),
            llm.tool_use("longtool", {}),
            llm.tool_use("longtool", {}),
            llm.text("完成"),
        ]
        result = agent.run("执行", tmp_conv)
        assert result["content"] == "完成"
        # Compression fired inside the loop (hook received stats)
        assert events, "compression hook was never called"
        assert events[0]["triggered"] is True

    def test_compression_disabled_skips_everything(self, agent_factory):
        llm = ScriptedLLM()
        agent = agent_factory(
            llm=llm,
            config=AgentConfig(permission_enabled=False, compression_enabled=False),
        )
        assert agent.compression is None  # not built when disabled

    def test_fail_soft_when_summarizer_errors_in_loop(self, tmp_db, tmp_conv, agent_factory):
        """A failing summarizer must not break the agent loop."""
        llm = ScriptedLLM()
        agent = agent_factory(
            llm=llm,
            config=AgentConfig(permission_enabled=False, compression_enabled=True),
        )
        agent.compression = _make_manager(
            llm=SummaryLLM(fail=True), db=tmp_db,
            min_messages=6, keep_recent=4, min_gain_tokens=100,
        )
        result = agent.run("测试", tmp_conv)
        assert result["content"] == "ok"
        assert result["tool_calls_made"] == 0
