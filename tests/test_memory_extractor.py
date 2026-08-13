"""
Tests for the automatic memory extractor (agent/memory/extractor.py).
"""

from agent.memory.extractor import MemoryExtractor
from agent.memory.service import MemoryService


def _extractor(tmp_db, llm=None, **kw):
    svc = MemoryService(tmp_db, user_id=1)
    defaults = dict(min_user_chars=8)
    defaults.update(kw)
    return MemoryExtractor(llm, svc, **defaults), svc


class FakeExtractLLM:
    """Returns a fixed JSON memory list on every call."""

    def __init__(self, content):
        self._content = content

    def chat(self, **kw):
        from agent.llm_client import LLMResponse
        return LLMResponse(type="text", content=self._content, mode="native")


# ============================================================
# Parsing
# ============================================================

def test_parse_plain_json():
    out = MemoryExtractor._parse_response(
        '{"memories": [{"content": "用户喜欢猫", "memory_type": "preference", "importance": 4}]}'
    )
    assert len(out) == 1
    assert out[0]["content"] == "用户喜欢猫"
    assert out[0]["memory_type"] == "preference"
    assert out[0]["importance"] == 4


def test_parse_fenced_json():
    fenced = "```json\n{\"memories\": [{\"content\": \"正在开发AI聊天项目\", \"memory_type\": \"task\", \"importance\": 2}]}\n```"
    out = MemoryExtractor._parse_response(fenced)
    assert len(out) == 1
    assert out[0]["memory_type"] == "task"


def test_parse_prose_wrapped():
    prose = '以下是记忆：{"memories": [{"content": "用户住北京", "memory_type": "fact", "importance": 3}]} 完'
    out = MemoryExtractor._parse_response(prose)
    assert len(out) == 1
    assert out[0]["content"] == "用户住北京"


def test_parse_garbage_returns_empty():
    assert MemoryExtractor._parse_response("抱歉，我无法处理。") == []
    assert MemoryExtractor._parse_response("") == []


def test_parse_validates_fields():
    bad = '{"memories": [{"content": "用户养狗", "memory_type": "animal", "importance": 9}]}'
    out = MemoryExtractor._parse_response(bad)
    # invalid type falls back to 'fact', importance clamped to 5
    assert out[0]["memory_type"] == "fact"
    assert out[0]["importance"] == 5
    # too-short content filtered
    short = '{"memories": [{"content": "短", "memory_type": "fact"}]}'
    assert MemoryExtractor._parse_response(short) == []


# ============================================================
# Extraction flow
# ============================================================

def test_extract_stores_memory(tmp_db):
    llm = FakeExtractLLM(
        '{"memories": [{"content": "用户正在开发AI聊天项目", "memory_type": "task", "importance": 4}]}'
    )
    extractor, svc = _extractor(tmp_db, llm)

    result = extractor.maybe_extract(
        "我在开发一个AI聊天项目", "好的，你正在开发AI聊天项目。", "conv1", force=True
    )

    assert result["extracted"] == 1
    assert result["stored"] == 1
    mems = tmp_db.list_memories(1)
    assert mems[0]["content"] == "用户正在开发AI聊天项目"


def test_extract_dedup(tmp_db):
    llm = FakeExtractLLM(
        '{"memories": [{"content": "用户喜欢喝绿茶", "memory_type": "preference", "importance": 3}]}'
    )
    extractor, svc = _extractor(tmp_db, llm)

    extractor.maybe_extract("我喜欢喝绿茶", "好的，记住了。", "c1", force=True)
    second = extractor.maybe_extract("我还是喜欢喝绿茶", "好的，记住了。", "c2", force=True)

    assert second["stored"] == 0  # duplicate skipped
    assert len(tmp_db.list_memories(1)) == 1


def test_extract_skips_trivial_turns(tmp_db):
    llm = FakeExtractLLM('{"memories": []}')
    extractor, svc = _extractor(tmp_db, llm)

    # Short user message -> no extraction call at all
    result = extractor.maybe_extract("hi", "好的", "c1")
    assert result["extracted"] == 0
    assert llm._content  # still constructed


def test_extract_fail_soft(tmp_db):
    class BrokenLLM:
        def chat(self, **kw):
            raise RuntimeError("boom")

    extractor, svc = _extractor(tmp_db, BrokenLLM())
    result = extractor.maybe_extract(
        "我在开发一个AI聊天项目", "好的，正在开发。", "c1", force=True
    )
    assert result["extracted"] == 0  # never raises
    assert result["stored"] == 0


def test_extract_concurrency_lock(tmp_db):
    """Concurrent extraction calls are serialized; second is skipped as busy."""
    llm = FakeExtractLLM('{"memories": [{"content": "用户喜欢跑步", "memory_type": "preference", "importance": 3}]}')
    extractor, svc = _extractor(tmp_db, llm)

    # Hold the lock manually to simulate a concurrent run
    assert extractor._lock.acquire(blocking=False)
    try:
        result = extractor.maybe_extract(
            "我喜欢跑步", "好的，记住了。", "c1", force=True
        )
        assert result["extracted"] == 0
        assert result.get("error") == "busy"
    finally:
        extractor._lock.release()


# ============================================================
# Gate conditions
# ============================================================

def test_should_extract_gates():
    ext = object.__new__(MemoryExtractor)
    ext.min_user_chars = 8

    # 10-char user message passes the length gate
    assert ext._should_extract(
        "我真的非常喜欢喝绿茶", "好的，记住了你喜欢喝绿茶！以后我会记得。"
    )
    assert not ext._should_extract("hi", "好的")  # too short
    assert not ext._should_extract("我喜欢喝绿茶", "")  # empty response
    assert not ext._should_extract("", "好的")  # empty user
