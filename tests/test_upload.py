"""
Upload tests: file/image upload API, serving, and chat integration.

Covers:
  - POST /api/upload with a text file -> returns metadata, file saved
  - POST /api/upload with an image -> kind == "image", url served
  - Empty / missing file rejected
  - Oversized file rejected (413)
  - Uploaded file is served back via /api/uploads/<id>
  - /api/chat with attachments includes the file content in the prompt
"""

import io
import os
import sys
from pathlib import Path

import pytest

# Mirror test_app.py: redirect the DB before importing app
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "data"))

# Redirect the app's DB to a temp file BEFORE importing app (mirrors
# test_app.py) so the suite never touches the production database.
import tempfile
import database as _db_module
_TEST_DB = Path(tempfile.mktemp(prefix="ai-chat-upload-test-", suffix=".db"))
_db_module.DB_FILE = _TEST_DB
_db_module._db_instance = None

import app as app_module

app_module.app.config["TESTING"] = True
client = app_module.app.test_client()
CSRF = "pytest-csrf-token"


@pytest.fixture(autouse=True)
def _logged_in():
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["_csrf_token"] = CSRF
    yield


def _headers():
    return {"X-CSRF-Token": CSRF}


def test_upload_text_file():
    data = {
        "file": (io.BytesIO("hello world\nline2\n".encode("utf-8")), "notes.txt"),
    }
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    assert rv.status_code == 201, rv.get_json()
    j = rv.get_json()
    assert j["ok"] is True
    assert j["name"] == "notes.txt"
    assert j["kind"] == "text"
    assert j["size"] == 18
    assert j["file_id"]
    # File actually saved
    fp = app_module.UPLOAD_DIR / f"{j['file_id']}.txt"
    assert fp.exists()
    assert fp.read_text() == "hello world\nline2\n"
    # Served back
    rv2 = client.get(j["url"])
    assert rv2.status_code == 200
    assert rv2.data == b"hello world\nline2\n"
    # cleanup
    fp.unlink(missing_ok=True)


def test_upload_image():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    data = {"file": (io.BytesIO(png), "pic.png")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    assert rv.status_code == 201, rv.get_json()
    j = rv.get_json()
    assert j["ok"] is True
    assert j["kind"] == "image"
    assert j["ext"] == ".png"
    fp = app_module.UPLOAD_DIR / f"{j['file_id']}.png"
    assert fp.exists()
    # Cleanup
    fp.unlink(missing_ok=True)


def test_upload_missing_file():
    rv = client.post("/api/upload", data={}, content_type="multipart/form-data", headers=_headers())
    assert rv.status_code == 400


def test_upload_oversized():
    big = io.BytesIO(b"x" * (app_module.MAX_UPLOAD_SIZE + 1))
    data = {"file": (big, "big.bin")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    assert rv.status_code == 413


def test_upload_path_traversal_blocked():
    rv = client.get("/api/uploads/..%2f..%2fetc%2fpasswd")
    # Flask may decode %2f; either way it must not return file content
    assert rv.status_code in (400, 404, 403)


def _patch_create_agent():
    """Return a context manager that swaps create_agent with a fake whose
    run() captures the user message (avoids real LLM calls)."""
    import contextlib

    @contextlib.contextmanager
    def _cm(captured):
        orig = app_module.create_agent
        class FakeAgent:
            def __init__(self, *a, **kw):
                self.config = type("C", (), {
                    "approval_enabled": False, "approval_remember": False,
                    "cancellation_enabled": True, "cancel_confirm_required": True,
                    "cancel_expiry": 120,
                })()
                self.approval_manager = None
                self.cancellation_manager = app_module.cancellation_manager
                self.trace_sink = None
            def run(self, message, conversation_id=None, run_id=None, user_attachments=None, persist_message=None):
                captured["message"] = message
                captured["run_id"] = run_id
                captured["attachments"] = user_attachments
                captured["persist_message"] = persist_message
                return {"content": "ok", "tool_calls_made": 0, "iterations": 1, "events": [], "cancelled": False}
        app_module.create_agent = lambda *a, **kw: FakeAgent(*a, **kw)
        try:
            yield
        finally:
            app_module.create_agent = orig
    return _cm


import contextlib as _ctx

@_ctx.contextmanager
def _vision_support(enabled):
    """Temporarily stub the global agent_llm's vision capability."""
    orig = app_module.agent_llm
    class _Stub:
        supports_vision = enabled
        backend_name = "stub"
        model_name = "stub-model"
    app_module.agent_llm = _Stub()
    try:
        yield
    finally:
        app_module.agent_llm = orig


def test_attachment_text_in_chat_prompt():
    """Upload a text file then send a chat with the attachment — the file
    content must appear in the message the agent receives."""
    data = {"file": (io.BytesIO("配置项: timeout=30".encode("utf-8")), "cfg.txt")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    captured = {}
    with _patch_create_agent()(captured):
        rv2 = client.post(
            "/api/chat",
            json={
                "conversation_id": "upload-test-conv",
                "message": "请查看这个配置",
                "attachments": [{"file_id": fid, "name": "cfg.txt"}],
            },
            headers=_headers(),
        )
        assert rv2.status_code == 200, rv2.get_json()

    msg = captured.get("message", "")
    assert "cfg.txt" in msg
    assert "timeout=30" in msg  # file content injected
    assert "请查看这个配置" in msg

    # cleanup
    fp = app_module.UPLOAD_DIR / f"{fid}.txt"
    fp.unlink(missing_ok=True)


def test_chat_without_message_but_attachment_allowed():
    """Sending only an attachment (no text) is allowed."""
    data = {"file": (io.BytesIO(b"data1"), "d.txt")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    captured = {}
    with _patch_create_agent()(captured):
        rv2 = client.post(
            "/api/chat",
            json={"conversation_id": "u2", "message": "", "attachments": [{"file_id": fid, "name": "d.txt"}]},
            headers=_headers(),
        )
        assert rv2.status_code == 200

    assert "d.txt" in captured.get("message", "")

    fp = app_module.UPLOAD_DIR / f"{fid}.txt"
    fp.unlink(missing_ok=True)


def test_chat_empty_everything_rejected():
    rv = client.post("/api/chat", json={"conversation_id": "u3", "message": ""}, headers=_headers())
    assert rv.status_code == 400


def test_image_attachment_passed_as_multimodal():
    """Upload an image, send chat with it — the agent must receive the
    image_url data (multimodal) plus the text."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    data = {"file": (io.BytesIO(png), "chart.png")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    captured = {}
    with _vision_support(True), _patch_create_agent()(captured):
        rv2 = client.post(
            "/api/chat",
            json={
                "conversation_id": "img-conv",
                "message": "这张图里有什么?",
                "attachments": [{"file_id": fid, "name": "chart.png"}],
            },
            headers=_headers(),
        )
        assert rv2.status_code == 200, rv2.get_json()

    atts = captured.get("attachments") or []
    assert len(atts) == 1
    assert atts[0]["image_url"].startswith("data:image/png;base64,")
    msg = captured.get("message", "")
    assert "chart.png" in msg
    assert "这张图里有什么" in msg

    fp = app_module.UPLOAD_DIR / f"{fid}.png"
    fp.unlink(missing_ok=True)


def test_attachment_persisted_to_db():
    """The user message (with attachment) must be persisted with the
    attachment metadata in the messages table."""
    import io as _io

    # Upload a text file
    data = {"file": (_io.BytesIO(b"report data"), "report.txt")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    # Use a REAL agent run path with a scripted LLM so messages persist.
    # Simpler: call db.add_message directly to verify round-trip, then
    # verify api_chat passes metadata through (already covered above).
    from data.database import Database
    import tempfile, os as _os
    tmp = tempfile.mktemp(suffix=".db")
    db = Database(tmp)
    db.create_conversation("c1", title="t")
    db.add_message("c1", "user", "看看这个报告", attachments=[{
        "file_id": fid, "name": "report.txt", "kind": "text", "ext": ".txt",
    }])
    msgs = db.get_messages("c1")
    assert msgs[0]["attachments"][0]["name"] == "report.txt"
    assert msgs[0]["attachments"][0]["kind"] == "text"
    db.close()
    _os.remove(tmp)

    fp = app_module.UPLOAD_DIR / f"{fid}.txt"
    fp.unlink(missing_ok=True)


def test_stream_endpoint_accepts_attachments():
    """The SSE stream endpoint must accept attachments and pass image
    parts through to run_stream."""
    import io as _io

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    data = {"file": (_io.BytesIO(png), "chart.png")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    captured = {}
    orig_stream = app_module.agent.run_stream
    def fake_stream(message, conversation_id=None, run_id=None, user_attachments=None, persist_message=None):
        captured["message"] = message
        captured["atts"] = user_attachments
        yield {"type": "text", "content": "done"}
    app_module.agent.run_stream = fake_stream
    try:
        pass
        # Consume the SSE generator (vision ON so image data is attached)
        with _vision_support(True):
            rv2 = client.post(
                "/api/agent/stream",
                json={
                    "conversation_id": "stream-conv",
                    "message": "看图说话",
                    "attachments": [{"file_id": fid, "name": "chart.png"}],
                },
                headers=_headers(),
            )
        assert rv2.status_code == 200
        assert b"data:" in rv2.data
    finally:
        app_module.agent.run_stream = orig_stream

    assert "chart.png" in captured.get("message", "")
    atts = captured.get("atts") or []
    assert atts and atts[0]["image_url"].startswith("data:image/png;base64,")

    fp = app_module.UPLOAD_DIR / f"{fid}.png"
    fp.unlink(missing_ok=True)


def test_orchestrator_persists_attachment_metadata():
    """A real orchestrator run with attachments must persist the attachment
    metadata with the user message."""
    from agent.presets import create_agent as _create_agent
    from tests.conftest import ScriptedLLM
    import tempfile as _tf
    import os as _os

    tmp = _tf.mktemp(suffix=".db")
    from data.database import Database
    db = Database(tmp)
    db.create_conversation("conv-persist", title="t")

    llm = ScriptedLLM("收到")
    agent = _create_agent(llm_client=llm, db=db, username="admin")
    agent.config.approval_enabled = False

    atts = [{"file_id": "f1", "name": "pic.png", "kind": "image", "ext": ".png"}]
    result = agent.run("看看这张图", conversation_id="conv-persist", user_attachments=atts)
    assert result["content"] == "收到"

    msgs = db.get_messages("conv-persist")
    user_msg = [m for m in msgs if m["role"] == "user"][0]
    assert user_msg["attachments"] == atts

    db.close()
    _os.remove(tmp)


def test_attachment_history_roundtrip():
    """History API returns attachments for a conversation."""
    import io as _io

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    data = {"file": (_io.BytesIO(png), "hist.png")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    # Persist directly via the (redirected) app DB singleton
    conv_id = "abcdef0123456789"  # valid hex id (8-16 chars)
    app_module.db.create_conversation(conv_id, title="t")
    app_module.db.add_message(conv_id, "user", "历史图", attachments=[{
        "file_id": fid, "name": "hist.png", "kind": "image", "ext": ".png",
    }])
    app_module.db.add_message(conv_id, "assistant", "看到了")

    rv2 = client.get(f"/api/conversations/{conv_id}")
    assert rv2.status_code == 200
    conv = rv2.get_json()
    user_msgs = [m for m in conv["messages"] if m["role"] == "user"]
    assert user_msgs and user_msgs[0]["attachments"][0]["name"] == "hist.png"
    assert user_msgs[0]["attachments"][0]["kind"] == "image"

    fp = app_module.UPLOAD_DIR / f"{fid}.png"
    fp.unlink(missing_ok=True)


def test_no_vision_model_short_circuits():
    """When the active model has no vision support, sending an image must
    NOT call the LLM at all — return a clear '不支持多模态' notice."""
    import io as _io

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    data = {"file": (_io.BytesIO(png), "nopic.png")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    called = {"run": False}
    orig = app_module.create_agent
    def _boom(*a, **kw):
        called["run"] = True
        raise AssertionError("LLM should NOT be called for vision-blocked images")
    app_module.create_agent = _boom
    try:
        with _vision_support(False):
            rv2 = client.post(
                "/api/chat",
                json={
                    "conversation_id": "nvision-conv",
                    "message": "这是什么",
                    "attachments": [{"file_id": fid, "name": "nopic.png"}],
                },
                headers=_headers(),
            )
    finally:
        app_module.create_agent = orig

    assert rv2.status_code == 200, rv2.get_json()
    body = rv2.get_json()
    assert body.get("vision_blocked") is True, body
    assert "不支持多模态" in body.get("content", "")
    assert called["run"] is False, "agent.run must not be called"

    fp = app_module.UPLOAD_DIR / f"{fid}.png"
    fp.unlink(missing_ok=True)


def test_no_vision_model_short_circuits_stream():
    """The SSE stream endpoint must also refuse image requests without
    calling the LLM when the model has no vision support."""
    import io as _io

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    data = {"file": (_io.BytesIO(png), "nopic2.png")}
    rv = client.post("/api/upload", data=data, content_type="multipart/form-data", headers=_headers())
    j = rv.get_json()
    fid = j["file_id"]

    called = {"run": False}
    orig = app_module.agent.run_stream
    def _boom(*a, **kw):
        called["run"] = True
        raise AssertionError("run_stream should NOT be called for vision-blocked images")
    app_module.agent.run_stream = _boom
    try:
        with _vision_support(False):
            rv2 = client.post(
                "/api/agent/stream",
                json={
                    "conversation_id": "nvision-conv2",
                    "message": "这是什么",
                    "attachments": [{"file_id": fid, "name": "nopic2.png"}],
                },
                headers=_headers(),
            )
    finally:
        app_module.agent.run_stream = orig

    assert rv2.status_code == 200
    body = rv2.data.decode("utf-8")
    assert "不支持多模态" in body
    assert called["run"] is False

    fp = app_module.UPLOAD_DIR / f"{fid}.png"
    fp.unlink(missing_ok=True)


def test_mimo_model_detected_as_vision():
    """mimo-v2.5 (Xiaomi MIMO) must be detected as a vision-capable model,
    so image requests are NOT short-circuited."""
    from agent.llm_client import AgentLLMClient
    c = AgentLLMClient({
        "llms": {"backends": [{"name": "M", "model": "mimo-v2.5"}]},
        "active_backend": "M",
    })
    assert c.supports_vision is True


def test_llm_client_vision_error_message_friendly():
    """A 404/400 'image input not supported' API error must raise a friendly
    Chinese message instead of the raw error."""
    import agent.llm_client as llm_mod

    class FakeResp:
        status_code = 404
        text = '{"error":{"code":"404","message":"No endpoints found that support image input"}}'
    class FakeSession:
        def post(self, *a, **kw):
            return FakeResp()

    c = llm_mod.AgentLLMClient({
        "llms": {"backends": [{"name": "D", "model": "deepseek-v4-flash",
                               "api_key": "sk-x", "api_base": "https://x"}]},
        "active_backend": "D",
    })
    orig = c._session
    c._session = FakeSession()
    try:
        import pytest as _p
        with _p.raises(RuntimeError) as ei:
            c.chat(messages=[{"role": "user", "content": "hi"}])
        assert "不支持多模态" in str(ei.value)
    finally:
        c._session = orig
