"""
App-level smoke tests: module imports, route registration, memory API.

IMPORTANT: these tests must NOT touch the production database (data/chat.db).
The app's global db singleton is pointed at a temp file before `app` is
imported, so running the suite while the real app is live is safe.
"""

import sys
import tempfile
from pathlib import Path

# The app imports `database` from its data/ dir — mirror that so we can
# redirect the DB before importing app.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "data"))

import database as db_module

# Point the app's DB at an isolated temp file BEFORE importing app
_TEST_DB = Path(tempfile.mktemp(prefix="ai-chat-app-test-", suffix=".db"))
db_module.DB_FILE = _TEST_DB
db_module._db_instance = None  # reset singleton

import pytest

import app as app_module

# Trigger schema init on the isolated temp DB (lazy singleton wouldn't run it)
app_module.db.get_stats()
if app_module.db.get_user("admin") is None:
    import hashlib
    app_module.db.create_user("admin", hashlib.sha256(b"pw").hexdigest())


@pytest.fixture(autouse=True)
def _keep_db_alive():
    """No-op per-test fixture: the temp DB must survive the whole module.

    Deleting the DB file between tests breaks the app's already-open
    connection (WAL/SHM re-creation fails). Cleanup happens once at session
    end via conftest.pytest_sessionfinish.
    """
    yield


def test_app_imports_and_agent_wired():
    """app.py imports cleanly and the global agent has all capabilities."""
    agent = app_module.agent

    assert agent is not None
    assert agent.memory_service is not None
    assert agent.memory_extractor is not None
    assert agent.plan_generator is not None
    assert agent.config.tool_retry_enabled
    # All 13 tools registered (incl. read_files batch tool + delegate sub-agent)
    assert len(agent.tools.list_names()) == 13
    assert "read_files" in agent.tools.list_names()
    assert "delegate" in agent.tools.list_names()


def test_memory_routes_registered():
    rules = [r.rule for r in app_module.app.url_map.iter_rules()]
    for expected in (
        "/api/agent/memories",
        "/api/agent/memories/search",
    ):
        assert expected in rules, f"missing route {expected}"
    assert any(r.endswith("<int:mem_id>") for r in rules if "memories" in r)


def test_memory_api_crud():
    """Memory CRUD via the Flask test client with a real session + CSRF."""
    client = app_module.app.test_client()
    token = "pytest-csrf-token"

    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["_csrf_token"] = token

    headers = {"X-CSRF-Token": token}

    # Create
    resp = client.post(
        "/api/agent/memories",
        json={"content": "pytest 测试记忆", "memory_type": "fact", "importance": 3},
        headers=headers,
    )
    assert resp.status_code == 201
    mem_id = resp.get_json()["memory"]["id"]

    try:
        # List
        resp = client.get("/api/agent/memories")
        assert resp.status_code == 200
        assert resp.get_json()["count"] >= 1

        # Search
        resp = client.get("/api/agent/memories/search?q=测试")
        assert resp.status_code == 200
        assert any(m["content"] == "pytest 测试记忆" for m in resp.get_json()["results"])
    finally:
        # Delete
        resp = client.delete(f"/api/agent/memories/{mem_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


def test_memory_api_requires_auth():
    """Unauthenticated requests to the memory API are rejected."""
    client = app_module.app.test_client()
    resp = client.get("/api/agent/memories")
    assert resp.status_code in (302, 401)


def test_trace_routes_registered():
    rules = [r.rule for r in app_module.app.url_map.iter_rules()]
    for expected in (
        "/traces",
        "/api/agent/traces",
        "/api/agent/traces/<trace_id>",
    ):
        assert expected in rules, f"missing route {expected}"


def test_trace_api_list_and_detail():
    """Trace list/detail/delete via the Flask test client."""
    client = app_module.app.test_client()
    token = "pytest-csrf-token"
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["_csrf_token"] = token
    headers = {"X-CSRF-Token": token}

    # List (may be empty in a fresh environment)
    resp = client.get("/api/agent/traces")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "traces" in data and "total" in data

    # Detail of a missing trace -> 404
    resp = client.get("/api/agent/traces/doesnotexist")
    assert resp.status_code == 404

    # Delete of a missing trace -> 404
    resp = client.delete("/api/agent/traces/doesnotexist", headers=headers)
    assert resp.status_code == 404


def test_trace_api_requires_auth():
    client = app_module.app.test_client()
    resp = client.get("/api/agent/traces")
    assert resp.status_code in (302, 401)


def test_csrf_rejects_missing_token():
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"

    resp = client.post(
        "/api/agent/memories",
        json={"content": "x", "memory_type": "fact", "importance": 3},
    )
    assert resp.status_code == 403


# ============================================================
# active_backend selection
# ============================================================

def test_get_active_backend_prefers_active_backend():
    """_get_active_backend returns the backend named by active_backend."""
    import app as app_module
    cfg = {
        "llms": {"backends": [
            {"name": "A", "enabled": True},
            {"name": "B", "enabled": True},
        ]},
        "active_backend": "B",
    }
    llm = app_module.LLMClient(cfg)
    assert llm._get_active_backend()["name"] == "B"


def test_get_active_backend_falls_back_to_first_enabled():
    """Without active_backend, the first enabled backend wins."""
    import app as app_module
    cfg = {"llms": {"backends": [
        {"name": "A", "enabled": False},
        {"name": "B", "enabled": True},
        {"name": "C", "enabled": True},
    ]}}
    llm = app_module.LLMClient(cfg)
    assert llm._get_active_backend()["name"] == "B"


def test_get_active_backend_unknown_name_falls_back():
    """An active_backend name that doesn't exist falls back gracefully."""
    import app as app_module
    cfg = {"llms": {"backends": [
        {"name": "A", "enabled": True},
    ]}, "active_backend": "Nope"}
    llm = app_module.LLMClient(cfg)
    assert llm._get_active_backend()["name"] == "A"


def test_get_active_backend_empty_returns_none():
    import app as app_module
    llm = app_module.LLMClient({"llms": {"backends": []}})
    assert llm._get_active_backend() is None
