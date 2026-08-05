"""
Integration tests: orchestrator + long-term memory injection and auto-extraction.
"""

from conftest import ScriptedLLM


def test_full_agent_wires_memory(full_agent):
    assert full_agent.memory_service is not None
    assert full_agent.memory_extractor is not None
    assert full_agent.plan_generator is not None
    names = sorted(t.name for t in full_agent.tools.all_tools())
    for expected in ("memory_query", "memory_store", "memory_forget", "memory_list"):
        assert expected in names


def test_memory_injection_on_relevant_query(tmp_db, admin_user, tmp_conv):
    """A stored memory is injected as a system message when the query is relevant."""
    tmp_db.store_memory(admin_user["id"], "用户喜欢喝绿茶", "preference", 4)

    llm = ScriptedLLM()
    from agent.presets import create_agent
    agent = create_agent(
        llm_client=llm,
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
    )

    result = agent.run("你记得我喜欢喝什么吗？", conversation_id=tmp_conv)

    assert result["content"]
    assert "[长期记忆]" in llm.injected.get("[长期记忆]", "")
    assert "绿茶" in llm.injected.get("[长期记忆]", "")


def test_auto_extraction_stores_task_memory(tmp_db, admin_user, tmp_conv):
    """After a turn, the extractor LLM call stores a task memory."""
    llm = ScriptedLLM(
        # planner call (call 1) -> empty plan
        '{"goal": "", "steps": []}',
        # main loop (call 2) -> text answer
        "好的，我记住了你的偏好。",
        # extractor (call 3) -> JSON memory
        '{"memories": [{"content": "用户正在开发AI聊天项目", "memory_type": "task", "importance": 4}]}',
    )
    from agent.presets import create_agent
    agent = create_agent(
        llm_client=llm,
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
    )

    agent.run("我在开发一个AI聊天项目，最近刚加了自动备份功能", conversation_id=tmp_conv)

    mems = tmp_db.list_memories(admin_user["id"])
    assert len(mems) == 1
    assert mems[0]["content"] == "用户正在开发AI聊天项目"
    assert mems[0]["memory_type"] == "task"


def test_memory_recalled_in_later_conversation(tmp_db, admin_user):
    """Memory stored in one conversation is injected into a later one."""
    from agent.presets import create_agent

    # Seed memory
    tmp_db.store_memory(admin_user["id"], "用户喜欢喝绿茶", "preference", 4)

    # Fresh conversation, fresh agent
    import uuid
    conv2 = uuid.uuid4().hex[:12]
    tmp_db.create_conversation(conv2, title="later", user_id=admin_user["id"])

    llm = ScriptedLLM()
    agent = create_agent(
        llm_client=llm,
        db=tmp_db,
        workspace_dir=".",
        username="admin",
        user_id=admin_user["id"],
    )

    agent.run("你还记得我喜欢喝什么吗？", conversation_id=conv2)

    injected = llm.injected.get("[长期记忆]", "")
    assert "绿茶" in injected
    assert "preference" in injected


def test_memory_user_isolation(tmp_db):
    """User A's memories are invisible to user B."""
    from agent.memory.service import MemoryService

    tmp_db.create_user("bob", "x")
    bob = tmp_db.get_user("bob")
    tmp_db.store_memory(1, "用户A的秘密", "fact", 4)

    svc_a = MemoryService(tmp_db, user_id=1)
    svc_b = MemoryService(tmp_db, user_id=bob["id"])

    assert len(svc_a.search("秘密")) == 1
    assert len(svc_b.search("秘密")) == 0
