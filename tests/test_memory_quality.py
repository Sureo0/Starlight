"""
Tests for memory quality controls: conflict resolution, reinforcement,
quality gate, consolidation (dedup merge), and decay.

These exercise database.store_memory (conflict/boost), MemoryService
(consolidate/decay/update), and MemoryExtractor (quality gate).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "data"))

from agent.memory.service import MemoryService
from agent.memory.extractor import MemoryExtractor


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture()
def service(tmp_db, admin_user):
    return MemoryService(tmp_db, user_id=admin_user["id"])


class ScriptedExtractLLM:
    """Fake LLM that returns a fixed memories JSON."""

    def __init__(self, payload: str = '{"memories": []}'):
        self.payload = payload
        self.calls = 0

    def chat(self, messages=None, **kw):
        from agent.llm_client import LLMResponse
        self.calls += 1
        return LLMResponse(type="text", content=self.payload, mode="native")


# ============================================================
# Conflict resolution (database layer)
# ============================================================

def test_conflict_newer_higher_importance_replaces(service):
    # Old, low-importance fact
    r1 = service.store("用户住在上海", memory_type="fact", importance=3)
    assert r1["stored"] is True
    old_id = r1["memory"]["id"]

    # Newer, more important claim -> replaces the old one
    r2 = service.store("用户搬到了北京", memory_type="fact", importance=4)
    assert r2["stored"] is True
    assert r2.get("replaced") == old_id
    assert service.get(old_id) is None  # old memory gone
    memories = service.list()
    assert len(memories) == 1
    assert memories[0]["content"] == "用户搬到了北京"


def test_conflict_lower_importance_keeps_old(service):
    r1 = service.store("用户住在上海", memory_type="fact", importance=4)
    old_id = r1["memory"]["id"]

    # New claim with LOWER importance -> old stays (repeated claim only boosts)
    r2 = service.store("用户住在北京", memory_type="fact", importance=3)
    assert r2["stored"] is True  # inserted as a separate memory
    assert r2.get("replaced") is None
    # Both exist (the low-importance one doesn't win)
    assert service.get(old_id) is not None


def test_no_conflict_across_types(service):
    r1 = service.store("用户住在上海", memory_type="fact", importance=3)
    r2 = service.store("用户喜欢上海菜", memory_type="preference", importance=4)
    assert r2["stored"] is True
    assert r2.get("replaced") is None
    assert len(service.list()) == 2


# ============================================================
# Reinforcement (dedup boost)
# ============================================================

def test_repeated_claim_boosts_importance(service):
    r1 = service.store("用户喜欢简洁的回答", memory_type="preference", importance=3)
    mem_id = r1["memory"]["id"]

    # Same content again -> duplicate, importance bumps to 4
    r2 = service.store("用户喜欢简洁的回答", memory_type="preference", importance=3)
    assert r2["stored"] is False
    assert r2["duplicate_of"] == mem_id
    assert r2["boosted"] is True
    mem = service.get(mem_id)
    assert mem["importance"] == 4


# ============================================================
# Quality gate (extractor)
# ============================================================

def test_quality_gate_drops_ai_self_memories(tmp_db, admin_user):
    llm = ScriptedExtractLLM('{"memories": [{"content": "我是AI助手，可以帮你解答问题", "memory_type": "fact", "importance": 3}]}')
    extractor = MemoryExtractor(llm, MemoryService(tmp_db, user_id=admin_user["id"]), quality_gate=True)
    result = extractor.maybe_extract("你好，你是谁？", "我是一个AI助手", force=True)
    assert result["extracted"] == 0
    assert result["stored"] == 0


def test_quality_gate_keeps_real_user_facts(tmp_db, admin_user):
    llm = ScriptedExtractLLM('{"memories": [{"content": "用户是程序员，住在上海", "memory_type": "fact", "importance": 4}]}')
    extractor = MemoryExtractor(llm, MemoryService(tmp_db, user_id=admin_user["id"]), quality_gate=True)
    result = extractor.maybe_extract("我是程序员，住在上海", "好的，记住了", force=True)
    assert result["stored"] == 1


def test_quality_gate_can_be_disabled(tmp_db, admin_user):
    llm = ScriptedExtractLLM('{"memories": [{"content": "我是AI助手", "memory_type": "fact", "importance": 3}]}')
    extractor = MemoryExtractor(llm, MemoryService(tmp_db, user_id=admin_user["id"]), quality_gate=False)
    result = extractor.maybe_extract("你是谁", "我是AI助手", force=True)
    assert result["stored"] == 1  # gate off -> stored


# ============================================================
# Consolidation (merge near-duplicates)
# ============================================================

def test_consolidate_merges_near_duplicates(service):
    r1 = service.store("用户喜欢喝咖啡，每天一杯", memory_type="preference", importance=3)
    r2 = service.store("用户爱喝咖啡，每天早上喝一杯", memory_type="preference", importance=3)
    assert len(service.list()) == 2

    merged = service.consolidate(min_importance=1)
    assert merged >= 1
    remaining = service.list()
    assert len(remaining) == 1
    # Importance merged (3+3 capped at 5)
    assert remaining[0]["importance"] == 5


def test_consolidate_keeps_distinct_memories(service):
    service.store("用户住在上海", memory_type="fact", importance=3)
    service.store("用户喜欢跑步", memory_type="preference", importance=3)
    merged = service.consolidate(min_importance=1)
    assert merged == 0
    assert len(service.list()) == 2


# ============================================================
# Decay (demote stale memories)
# ============================================================

def test_decay_demotes_stale_memories(service):
    r = service.store("用户喜欢阅读", memory_type="preference", importance=4)
    mem_id = r["memory"]["id"]
    # Simulate an old memory: rewrite updated_at into the past
    from datetime import datetime, timedelta, timezone
    service._db._get_conn().execute(
        "UPDATE memories SET updated_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), mem_id),
    )
    service._db._get_conn().commit()

    demoted = service.decay(days=30)
    assert demoted == 1
    mem = service.get(mem_id)
    assert mem["importance"] == 3  # 4 -> 3


def test_decay_floor_is_one(service):
    r = service.store("用户喜欢阅读", memory_type="preference", importance=2)
    mem_id = r["memory"]["id"]
    from datetime import datetime, timedelta, timezone
    service._db._get_conn().execute(
        "UPDATE memories SET updated_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), mem_id),
    )
    service._db._get_conn().commit()

    service.decay(days=30)
    mem = service.get(mem_id)
    assert mem["importance"] == 1  # floor at 1


# ============================================================
# Update
# ============================================================

def test_update_memory_content_and_importance(service):
    r = service.store("用户住在上海", memory_type="fact", importance=3)
    mem_id = r["memory"]["id"]
    assert service.update(mem_id, content="用户住在杭州", importance=5) is True
    mem = service.get(mem_id)
    assert mem["content"] == "用户住在杭州"
    assert mem["importance"] == 5
    # Search still finds the updated content (FTS re-synced)
    hits = service.search("杭州")
    assert any(m["id"] == mem_id for m in hits)
