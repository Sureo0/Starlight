"""
Tests for the human-in-the-loop approval mechanism.

Covers:
  - ApprovalStore: create / decide / pending / list / expire
  - ApprovalManager: request / wait_for_decision / approve / reject / isolation
  - Orchestrator pause-and-wait: approved -> tool runs; rejected -> result
    with approval metadata (never retried); timeout -> expired
  - Fail-safe: side-effectful tool with NO manager -> blocked
  - App API wiring: /api/approvals endpoints
"""

from __future__ import annotations

import json
import threading
import time

from agent.approval import (
    ApprovalStore,
    ApprovalManager,
    APPROVED,
    REJECTED,
    EXPIRED,
    PENDING,
)
from agent.orchestrator import AgentConfig, AgentOrchestrator
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool, ToolResult
from agent.llm_client import LLMResponse
from tests.conftest import ScriptedLLM
from agent.presets import create_agent


def _tool_use(name, args):
    return LLMResponse(
        type="tool_use",
        content="",
        tool_calls=[{
            "id": "c1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
        mode="native",
    )


# ---------------------------------------------------------------------------
# ApprovalStore
# ---------------------------------------------------------------------------

def test_store_create_and_get(tmp_db):
    store = ApprovalStore(tmp_db)
    req_id = store.create(1, "write_file", {"path": "a.txt", "content": "x"}, "2099-01-01T00:00:00+00:00")
    row = store.get(req_id)
    assert row["tool"] == "write_file"
    assert row["args"] == {"path": "a.txt", "content": "x"}
    assert row["status"] == PENDING


def test_store_decide_and_idempotent(tmp_db):
    store = ApprovalStore(tmp_db)
    req_id = store.create(1, "write_file", {}, "2099-01-01T00:00:00+00:00")
    assert store.decide(req_id, APPROVED) is True
    # Second decide on the same request is a no-op
    assert store.decide(req_id, REJECTED) is False
    row = store.get(req_id)
    assert row["status"] == APPROVED


def test_store_pending_and_list(tmp_db):
    store = ApprovalStore(tmp_db)
    tmp_db.create_user("alice", "pw")  # user_id 2 for isolation checks
    a = store.create(1, "write_file", {}, "2099-01-01T00:00:00+00:00")
    b = store.create(1, "execute_code", {}, "2099-01-01T00:00:00+00:00")
    store.create(2, "write_file", {}, "2099-01-01T00:00:00+00:00")
    store.decide(a, APPROVED)

    assert len(store.get_pending(user_id=1)) == 1
    assert store.get_pending(user_id=1)[0]["id"] == b
    assert len(store.get_pending(user_id=2)) == 1
    assert len(store.get_pending()) == 2


def test_store_mark_expired(tmp_db):
    store = ApprovalStore(tmp_db)
    store.create(1, "write_file", {}, "2000-01-01T00:00:00+00:00")
    store.create(1, "write_file", {}, "2099-01-01T00:00:00+00:00")
    n = store.mark_expired()
    assert n == 1
    rows = store.list(user_id=1)
    assert {r["status"] for r in rows} == {PENDING, EXPIRED}


# ---------------------------------------------------------------------------
# ApprovalManager
# ---------------------------------------------------------------------------

def test_manager_wait_returns_approved_after_decide(tmp_db):
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    req = mgr.request(1, "write_file", {"path": "a"})

    def decide_later():
        time.sleep(0.3)
        mgr.approve(req["id"])

    t = threading.Thread(target=decide_later)
    t.start()
    status = mgr.wait_for_decision(req["id"], timeout=5)
    t.join()
    assert status == APPROVED


def test_manager_wait_expires_on_timeout(tmp_db):
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    req = mgr.request(1, "write_file", {})
    status = mgr.wait_for_decision(req["id"], timeout=0.3)
    assert status == EXPIRED


def test_manager_approve_reject_and_isolation(tmp_db):
    mgr = ApprovalManager(ApprovalStore(tmp_db))
    req = mgr.request(1, "write_file", {})
    # Another user cannot decide it
    ok, _ = mgr.approve(req["id"], user_id=2)
    assert ok is False
    assert mgr.store.get(req["id"])["status"] == PENDING
    # Owner can
    ok, _ = mgr.approve(req["id"], user_id=1)
    assert ok is True
    # Already decided -> cannot reject
    ok, _ = mgr.reject(req["id"], user_id=1)
    assert ok is False


def test_manager_pending_expires_stale(tmp_db):
    mgr = ApprovalManager(ApprovalStore(tmp_db))
    req = mgr.request(1, "write_file", {})
    # Manually backdate the expiry
    mgr.store.decide(req["id"], PENDING)  # no-op, still pending
    conn = tmp_db._get_conn()
    conn.execute("UPDATE approval_requests SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (req["id"],))
    conn.commit()
    pending = mgr.pending(user_id=1)
    assert all(r["id"] != req["id"] for r in pending)


# ---------------------------------------------------------------------------
# Orchestrator pause-and-wait
# ---------------------------------------------------------------------------

class TrackTool(Tool):
    """Tool that records executions."""

    retryable = True

    def __init__(self, name="write_file"):
        self._name = name
        self.executions = 0

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "track"

    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    def execute(self, **kwargs):
        self.executions += 1
        return ToolResult(success=True, output="done")


def _approval_agent(tmp_db, tools=None, config=None, manager=None):
    if config is None:
        # Default test config opts INTO approval
        cfg = AgentConfig(
            permission_enabled=False, rate_limit_enabled=False,
            input_validation_enabled=False,
            approval_enabled=True,
        )
    else:
        cfg = config  # custom config is authoritative
    agent = AgentOrchestrator(
        llm=ScriptedLLM(), tools=tools or ToolRegistry(), config=cfg, db=tmp_db,
    )
    agent.approval_manager = manager
    return agent


def test_approval_paused_approved_runs_tool(tmp_db):
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")
    agent = _approval_agent(tmp_db, tools=_reg(tool), manager=mgr)

    def approve_later():
        # _execute_tool creates the request internally — wait for it to
        # appear in the pending list, then approve.
        for _ in range(200):
            pending = mgr.pending(user_id=None)
            if pending:
                mgr.approve(pending[0]["id"])
                return
            time.sleep(0.05)

    t = threading.Thread(target=approve_later)
    t.start()
    result = agent._execute_tool("write_file", {"path": "a.txt"})
    t.join()

    assert result.success
    assert tool.executions == 1
    # The approval metadata is NOT on the result (tool ran normally)


def _reg(tool):
    r = ToolRegistry()
    r.register(tool)
    return r


def test_approval_rejected_returns_rejection(tmp_db):
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")
    agent = _approval_agent(tmp_db, tools=_reg(tool), manager=mgr)

    def reject_later():
        for _ in range(200):
            pending = mgr.pending(user_id=None)
            if pending:
                mgr.reject(pending[0]["id"], reason="不需要")
                return
            time.sleep(0.05)

    t = threading.Thread(target=reject_later)
    t.start()
    result = agent._execute_tool("write_file", {"path": "a.txt"})
    t.join()

    assert result.success is False
    assert result.metadata.get("approval") == REJECTED
    assert "不需要" in (result.error or "")
    assert tool.executions == 0  # tool never ran


def test_approval_timeout_expires(tmp_db):
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")
    cfg = AgentConfig(
        permission_enabled=False, rate_limit_enabled=False,
        input_validation_enabled=False,
        approval_enabled=True,  # opt in
        approval_expiry=1,  # 1s wait
    )
    agent = _approval_agent(tmp_db, tools=_reg(tool), config=cfg, manager=mgr)

    start = time.time()
    result = agent._execute_tool("write_file", {"path": "a.txt"})
    elapsed = time.time() - start

    assert result.success is False
    assert result.metadata.get("approval") == EXPIRED
    assert tool.executions == 0
    assert elapsed >= 0.9  # actually waited


def test_approval_no_manager_blocks(tmp_db):
    tool = TrackTool("write_file")
    agent = _approval_agent(tmp_db, tools=_reg(tool), manager=None)
    result = agent._execute_tool("write_file", {"path": "a.txt"})
    assert result.success is False
    assert "人工确认" in (result.error or "")
    assert tool.executions == 0


def test_approval_disabled_skips_check(tmp_db):
    tool = TrackTool("write_file")
    cfg = AgentConfig(
        permission_enabled=False, rate_limit_enabled=False,
        input_validation_enabled=False,
        approval_enabled=False,
    )
    agent = _approval_agent(tmp_db, tools=_reg(tool), config=cfg, manager=ApprovalManager(ApprovalStore(tmp_db)))
    result = agent._execute_tool("write_file", {"path": "a.txt"})
    assert result.success
    assert tool.executions == 1


def test_approval_read_tool_not_blocked(tmp_db):
    """Read-only tools never trigger approval."""
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("read_file")
    agent = _approval_agent(tmp_db, tools=_reg(tool), manager=mgr)
    result = agent._execute_tool("read_file", {"path": "a.txt"})
    assert result.success
    assert tool.executions == 1


def test_approval_not_retried_after_rejection(tmp_db):
    """A rejected approval must not be auto-retried (human decision)."""
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")

    class FlakyRetryTool(TrackTool):
        retryable = True  # would be retried if the result looked transient

    tool = FlakyRetryTool("write_file")
    cfg = AgentConfig(
        permission_enabled=False, rate_limit_enabled=False,
        input_validation_enabled=False,
        approval_enabled=True,  # opt in
        tool_retry_enabled=True, tool_retry_max=3,
    )
    agent = _approval_agent(tmp_db, tools=_reg(tool), config=cfg, manager=mgr)

    def reject_later():
        # _execute_tool creates a NEW request internally — auto-reject it.
        for _ in range(200):
            pending = mgr.pending(user_id=None)
            if pending:
                mgr.reject(pending[0]["id"], reason="不要")
                return
            time.sleep(0.05)

    t = threading.Thread(target=reject_later)
    t.start()
    result = agent._execute_tool("write_file", {"path": "a.txt"})
    t.join()

    # Even with retries enabled, the rejection must surface immediately
    assert result.success is False
    assert result.metadata.get("approval") == REJECTED
    # No retry happened -> tool never ran
    assert tool.executions == 0
    # And no retry history attached
    assert not result.metadata.get("retries")


def test_full_loop_rejection_adapts(tmp_db, admin_user):
    """End-to-end: agent tries write_file, user rejects, agent produces a
    final answer explaining the rejection instead of crashing."""
    llm = ScriptedLLM(
        _tool_use("write_file", {"path": "x.txt", "content": "hi"}),
        "明白了，用户拒绝了写入操作，我不会执行。",
    )
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)

    agent = create_agent(
        llm_client=llm, db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    agent.approval_manager = mgr
    agent.config.approval_enabled = True  # opt in for this test
    # Speed up tests
    agent.config.approval_expiry = 5

    def reject_later():
        for _ in range(200):
            pending = mgr.pending(user_id=admin_user["id"])
            if pending:
                mgr.reject(pending[0]["id"], reason="测试拒绝")
                return
            time.sleep(0.05)

    t = threading.Thread(target=reject_later)
    t.start()
    result = agent.run("帮我写个文件")
    t.join()

    assert "拒绝" in result["content"]
    # Rejected tool calls don't count toward tool_calls_made (only successful
    # executions do), but the loop DID run the delegate/approval flow.
    assert result["iterations"] >= 1


def test_presets_wire_approval_manager(tmp_db, admin_user):
    """create_agent wires an ApprovalManager when a db is present."""
    agent = create_agent(
        llm_client=ScriptedLLM(), db=tmp_db, workspace_dir=".",
        username="admin", user_id=admin_user["id"],
    )
    assert agent.approval_manager is not None
    # Approval is OPT-IN: presets wire the manager but keep it disabled by
    # default so library/test usage is unchanged.
    assert agent.config.approval_enabled is False
    # The default approval tools include side-effectful ones
    assert "write_file" in agent.config.approval_tools
    assert "execute_code" in agent.config.approval_tools


def test_app_approval_api():
    """The app's /api/approvals endpoints exist and work (via test client)."""
    import sys
    import tempfile
    import os
    import data.database as dbm

    # Use a SEPARATE temp db, NEVER the production one. NOTE: app.py inserts
    # data/ into sys.path and does `from database import db`, which would load
    # data/database.py as a SECOND module (`database`) with its own DB_FILE.
    # Alias the module so both import paths share the same object.
    sys.modules["database"] = dbm

    app_db_path = tempfile.mktemp(prefix="ai-chat-app-", suffix=".db")
    dbm.DB_FILE = app_db_path
    dbm._db_instance = None

    try:
        import app as app_module
        client = app_module.app.test_client()

        app_db = app_module.db
        app_db.create_user("admin", "pw")

        with client.session_transaction() as sess:
            sess["user"] = "admin"
            sess["_csrf_token"] = "test-csrf-token"

        def _csrf():
            return {"X-CSRF-Token": "test-csrf-token"}

        # Create a pending request directly
        mgr = app_module.approval_manager
        uid = app_db.get_user("admin")["id"]
        req = mgr.request(uid, "write_file", {"path": "a.txt"})

        # List pending
        r = client.get("/api/approvals")
        assert r.status_code == 200
        data = r.get_json()
        assert any(p["id"] == req["id"] for p in data["pending"])

        # Approve
        r = client.post(f"/api/approvals/{req['id']}/approve", headers=_csrf())
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

        # Rejecting an already-approved request fails
        r = client.post(
            f"/api/approvals/{req['id']}/reject",
            headers=_csrf(),
            json={"reason": "test"},
        )
        assert r.status_code == 400
    finally:
        dbm._db_instance = None
        try:
            os.remove(app_db_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Per-run approval memory (approval_remember)
# ---------------------------------------------------------------------------

def _approve_pending_thread(mgr):
    """Approve the first pending request (helper for threaded tests)."""

    def _run():
        for _ in range(200):
            pending = mgr.pending(user_id=None)
            if pending:
                mgr.approve(pending[0]["id"])
                return
            time.sleep(0.05)

    t = threading.Thread(target=_run)
    t.start()
    return t


def test_approval_remember_second_call_skips(tmp_db):
    """After one approval, further calls of the same tool in the SAME run
    proceed WITHOUT asking again."""
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")
    agent = _approval_agent(
        tmp_db, tools=_reg(tool), manager=mgr,
        config=AgentConfig(
            permission_enabled=False, rate_limit_enabled=False,
            input_validation_enabled=False,
            approval_enabled=True, approval_remember=True,
        ),
    )

    # First call: needs approval (thread approves it)
    t = _approve_pending_thread(mgr)
    r1 = agent._execute_tool("write_file", {"path": "a.txt"})
    t.join()
    assert r1.success
    assert tool.executions == 1

    # Second call: remembered — NO new request created, executes immediately
    pending_before = len(mgr.pending(user_id=None))
    r2 = agent._execute_tool("write_file", {"path": "b.txt"})
    assert r2.success
    assert tool.executions == 2
    pending_after = len(mgr.pending(user_id=None))
    assert pending_after == pending_before  # no new approval request created


def test_approval_remember_rejection_stays_rejected(tmp_db):
    """A rejected tool stays rejected for the rest of the run — no re-asking."""
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")
    agent = _approval_agent(
        tmp_db, tools=_reg(tool), manager=mgr,
        config=AgentConfig(
            permission_enabled=False, rate_limit_enabled=False,
            input_validation_enabled=False,
            approval_enabled=True, approval_remember=True,
        ),
    )

    def reject_later():
        for _ in range(200):
            pending = mgr.pending(user_id=None)
            if pending:
                mgr.reject(pending[0]["id"], reason="不要写文件")
                return
            time.sleep(0.05)

    t = threading.Thread(target=reject_later)
    t.start()
    r1 = agent._execute_tool("write_file", {"path": "a.txt"})
    t.join()
    assert r1.success is False
    assert tool.executions == 0

    # Second call: remembered rejection, immediate failure, no new request
    pending_before = len(mgr.pending(user_id=None))
    r2 = agent._execute_tool("write_file", {"path": "b.txt"})
    assert r2.success is False
    assert tool.executions == 0
    assert r2.metadata.get("approval") == "rejected_remembered"
    assert "不再询问" in (r2.error or "")
    assert len(mgr.pending(user_id=None)) == pending_before


def test_approval_remember_resets_between_runs(tmp_db):
    """The remembered decision is per-RUN: a new run() asks again."""
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")
    agent = _approval_agent(
        tmp_db, tools=_reg(tool), manager=mgr,
        config=AgentConfig(
            permission_enabled=False, rate_limit_enabled=False,
            input_validation_enabled=False,
            approval_enabled=True, approval_remember=True,
        ),
    )

    # Run 1: approve once, then a second call is remembered
    t = _approve_pending_thread(mgr)
    r1 = agent._execute_tool("write_file", {"path": "a.txt"})
    t.join()
    assert r1.success
    r2 = agent._execute_tool("write_file", {"path": "b.txt"})
    assert r2.success
    assert tool.executions == 2

    # Simulate a new run: memory is cleared
    agent._reset_approval_memory()
    t = _approve_pending_thread(mgr)
    r3 = agent._execute_tool("write_file", {"path": "c.txt"})
    t.join()
    assert r3.success
    assert tool.executions == 3


def test_approval_remember_disabled_asks_every_time(tmp_db):
    """With approval_remember=False, every call creates a new request."""
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    tool = TrackTool("write_file")
    agent = _approval_agent(
        tmp_db, tools=_reg(tool), manager=mgr,
        config=AgentConfig(
            permission_enabled=False, rate_limit_enabled=False,
            input_validation_enabled=False,
            approval_enabled=True, approval_remember=False,
        ),
    )

    for i in range(3):
        t = _approve_pending_thread(mgr)
        r = agent._execute_tool("write_file", {"path": f"{i}.txt"})
        t.join()
        assert r.success
    assert tool.executions == 3
    # 3 approval requests were created (one per call)
    assert len(mgr.history(user_id=None, limit=10)) == 3


def test_approval_remember_different_tools_independent(tmp_db):
    """Remembering write_file does NOT auto-approve other tools."""
    mgr = ApprovalManager(ApprovalStore(tmp_db), poll_interval=0.05)
    wf = TrackTool("write_file")
    ec = TrackTool("execute_code")
    agent = _approval_agent(
        tmp_db, tools=_reg_multi(wf, ec), manager=mgr,
        config=AgentConfig(
            permission_enabled=False, rate_limit_enabled=False,
            input_validation_enabled=False,
            approval_enabled=True, approval_remember=True,
        ),
    )

    # Approve write_file once
    t = _approve_pending_thread(mgr)
    assert agent._execute_tool("write_file", {"path": "a.txt"}).success
    t.join()

    # execute_code still asks (independent memory entry)
    t = _approve_pending_thread(mgr)
    r = agent._execute_tool("execute_code", {"code": "x"})
    t.join()
    assert r.success
    assert ec.executions == 1


def _reg_multi(*tools):
    r = ToolRegistry()
    for tool in tools:
        r.register(tool)
    return r
