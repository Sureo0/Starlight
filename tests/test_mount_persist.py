"""
Integration test: persist_message keeps generated mount/attachment
descriptions out of the conversation history.

When a mounted folder (or file attachment) is attached, app.py builds an
"effective message" that prepends the full manifest (e.g. "[挂载文件夹] ... 共 173
个文件: - .env ...") to the user's raw input. The LLM must receive that
manifest, but the persisted user message must stay the user's raw input —
otherwise reopening the conversation shows the whole file list.
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "data"))

import database as db_module
import tempfile

_TEST_DB = Path(tempfile.mktemp(prefix="ai-chat-mount-test-", suffix=".db"))
db_module.DB_FILE = _TEST_DB
db_module._db_instance = None

import pytest

from tests.conftest import ScriptedLLM
from agent.orchestrator import AgentOrchestrator, AgentConfig
from agent.tools.registry import ToolRegistry


MANIFEST = (
    "[挂载文件夹] AI-Chat (路径: E:\\ai\\AI-Chat, 共 173 个文件):\n"
    "  - .env (206 bytes)\n"
    "  - .gitignore (2907 bytes)\n"
    "  - README.md (5421 bytes)"
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    """Point the module-level DB at a fresh temp file per test."""
    db_module.DB_FILE = tmp_path / "t.db"
    db_module._db_instance = None
    yield
    db_module.DB_FILE = _TEST_DB
    db_module._db_instance = None


def _make_agent(db):
    llm = ScriptedLLM("明白")
    agent = AgentOrchestrator(
        llm=llm,
        tools=ToolRegistry(),
        config=AgentConfig(
            permission_enabled=False,
            rate_limit_enabled=False,
            input_validation_enabled=False,
            planning_enabled=False,
            memory_enabled=False,
        ),
        db=db,
    )
    return agent, llm


def test_run_persists_raw_input_not_manifest():
    db = db_module.get_db()
    conv_id = "conv-mount-1"
    db.create_conversation(conv_id, title="t")
    agent, llm = _make_agent(db)

    # user_message carries the manifest; persist_message is the raw input
    result = agent.run(
        f"{MANIFEST}\n\n分析这个项目",
        conversation_id=conv_id,
        persist_message="分析这个项目",
    )
    assert result["content"] == "明白"

    # LLM received the manifest
    assert MANIFEST in llm.calls[0][-1]["content"]

    # Persisted user message is the RAW input, no manifest
    msgs = db.get_messages(conv_id)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "分析这个项目"
    assert MANIFEST not in user_msgs[0]["content"]


def test_run_default_persists_user_message():
    """Without persist_message, behaviour is unchanged."""
    db = db_module.get_db()
    conv_id = "conv-mount-2"
    db.create_conversation(conv_id, title="t")
    agent, llm = _make_agent(db)

    agent.run("你好", conversation_id=conv_id)

    msgs = db.get_messages(conv_id)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs[0]["content"] == "你好"


def test_run_stream_persists_raw_input_not_manifest():
    db = db_module.get_db()
    conv_id = "conv-mount-3"
    db.create_conversation(conv_id, title="t")
    agent, llm = _make_agent(db)

    events = list(agent.run_stream(
        f"{MANIFEST}\n\n看看目录",
        conversation_id=conv_id,
        persist_message="看看目录",
    ))
    assert any(e.get("type") == "text" for e in events)

    # LLM received the manifest
    assert MANIFEST in llm.calls[0][-1]["content"]

    # Persisted message is raw
    msgs = db.get_messages(conv_id)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs[0]["content"] == "看看目录"
    assert MANIFEST not in user_msgs[0]["content"]


def test_run_stream_default_persists_user_message():
    db = db_module.get_db()
    conv_id = "conv-mount-4"
    db.create_conversation(conv_id, title="t")
    agent, llm = _make_agent(db)

    list(agent.run_stream("测试流", conversation_id=conv_id))

    msgs = db.get_messages(conv_id)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs[0]["content"] == "测试流"
