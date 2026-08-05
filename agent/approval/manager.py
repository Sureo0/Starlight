"""
Human-in-the-loop approval system.

Before executing a side-effectful tool (write_file, execute_code, delegate,
memory_forget, ...) the orchestrator asks the human for confirmation. The
request is persisted in SQLite, and the orchestrator's tool-execution loop
PAUSES while it polls the request status. The human can approve or reject
from the chat UI (or via the REST API); the loop resumes accordingly.

Design:
  - ApprovalStore: thin SQLite layer (one table: approval_requests)
  - ApprovalManager: create/query/decide/expire logic
  - Orchestrator integration: a tool that needs approval -> create a pending
    request -> poll until approved/rejected/expired -> continue or abort.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("agent.approval")

# Tool statuses
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
CANCELED = "canceled"

# How long a pending request waits for human input before expiring.
DEFAULT_EXPIRY_SECONDS = 300

# How often the orchestrator polls the store while waiting.
POLL_INTERVAL = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: str | None) -> float:
    """Parse an ISO timestamp back to epoch seconds (0 if unparseable)."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


@dataclass
class ApprovalRequest:
    """A single pending/decided approval request."""

    id: int
    user_id: int | None
    tool: str
    args: dict = field(default_factory=dict)
    status: str = PENDING
    reason: str | None = None
    created_at: str = ""
    decided_at: str | None = None
    expires_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "expires_at": self.expires_at,
        }


class ApprovalStore:
    """SQLite persistence for approval requests (thin, DB-agnostic)."""

    def __init__(self, db):
        self.db = db

    # ---- write ----

    def create(
        self,
        user_id: int | None,
        tool: str,
        args: dict,
        expires_at: str,
    ) -> int:
        """Insert a pending request; returns its id."""
        conn = self.db._get_conn()
        cur = conn.execute(
            """
            INSERT INTO approval_requests
                (user_id, tool, args, status, created_at, expires_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (user_id, tool, json.dumps(args, ensure_ascii=False), _now_iso(), expires_at),
        )
        conn.commit()
        return cur.lastrowid

    def decide(self, req_id: int, status: str, reason: str | None = None) -> bool:
        """Set a request's status (approved/rejected/...). Returns False if
        the request was already decided or doesn't exist."""
        conn = self.db._get_conn()
        cur = conn.execute(
            """
            UPDATE approval_requests
            SET status = ?, reason = ?, decided_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, reason, _now_iso(), req_id),
        )
        conn.commit()
        return cur.rowcount > 0

    # ---- read ----

    def get(self, req_id: int) -> dict | None:
        conn = self.db._get_conn()
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (req_id,)
        ).fetchone()
        return self._row_to_dict(row)

    def get_pending(self, user_id: int | None = None, limit: int = 100) -> list[dict]:
        conn = self.db._get_conn()
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE user_id = ? AND status = 'pending'
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE status = 'pending'
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list(
        self,
        user_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        conn = self.db._get_conn()
        sql = "SELECT * FROM approval_requests WHERE 1=1"
        params: list = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ---- housekeeping ----

    def mark_expired(self, now_iso: str | None = None) -> int:
        """Mark pending requests whose expiry has passed as 'expired'."""
        now = now_iso or _now_iso()
        conn = self.db._get_conn()
        cur = conn.execute(
            """
            UPDATE approval_requests
            SET status = 'expired', decided_at = ?, reason = '超时未处理'
            WHERE status = 'pending' AND expires_at < ?
            """,
            (now, now),
        )
        conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_dict(row) -> dict | None:
        if row is None:
            return None
        try:
            args = json.loads(row["args"]) if row["args"] else {}
        except Exception:
            args = {}
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "tool": row["tool"],
            "args": args,
            "status": row["status"],
            "reason": row["reason"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "expires_at": row["expires_at"],
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ApprovalManager:
    """High-level approval workflow (create / wait / decide)."""

    def __init__(
        self,
        store: ApprovalStore,
        expiry_seconds: int = DEFAULT_EXPIRY_SECONDS,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.store = store
        self.expiry_seconds = expiry_seconds
        self.poll_interval = poll_interval

    # ---- create ----

    def request(
        self,
        user_id: int | None,
        tool: str,
        args: dict,
    ) -> dict:
        """Create a pending approval request. Returns the request dict."""
        expires_at = datetime.fromtimestamp(
            time.time() + self.expiry_seconds, tz=timezone.utc
        ).isoformat()
        req_id = self.store.create(user_id, tool, args or {}, expires_at)
        req = self.store.get(req_id)
        logger.info(
            "Approval requested: id=%s tool=%s user_id=%s (expires %s)",
            req_id, tool, user_id, expires_at,
        )
        return req or {"id": req_id, "tool": tool, "args": args or {}}

    # ---- waiting (used by the orchestrator's paused tool execution) ----

    def wait_for_decision(
        self,
        req_id: int,
        timeout: float | None = None,
    ) -> str:
        """Block (poll) until the request leaves 'pending' or the timeout hits.

        Returns the final status: approved / rejected / expired / canceled.
        """
        deadline = time.time() + (timeout if timeout is not None else self.expiry_seconds)
        while True:
            row = self.store.get(req_id)
            if row is not None:
                status = row["status"]
                if status != PENDING:
                    return status
            # Not decided yet: expire it if past deadline
            if time.time() >= deadline:
                self.store.decide(req_id, EXPIRED, "等待确认超时")
                return EXPIRED
            time.sleep(min(self.poll_interval, max(0.1, deadline - time.time())))

    # ---- decide ----

    def approve(self, req_id: int, user_id: int | None = None, reason: str | None = None) -> tuple[bool, str]:
        """Approve a request. Returns (ok, message)."""
        row = self.store.get(req_id)
        if row is None:
            return False, "请求不存在"
        if user_id is not None and row["user_id"] not in (None, user_id):
            return False, "无权操作他人的审批请求"
        if row["status"] != PENDING:
            return False, f"该请求已处理（{row['status']}）"
        self.store.decide(req_id, APPROVED, reason)
        return True, "已批准"

    def reject(self, req_id: int, user_id: int | None = None, reason: str | None = None) -> tuple[bool, str]:
        """Reject a request. Returns (ok, message)."""
        row = self.store.get(req_id)
        if row is None:
            return False, "请求不存在"
        if user_id is not None and row["user_id"] not in (None, user_id):
            return False, "无权操作他人的审批请求"
        if row["status"] != PENDING:
            return False, f"该请求已处理（{row['status']}）"
        self.store.decide(req_id, REJECTED, reason or "用户拒绝")
        return True, "已拒绝"

    # ---- queries ----

    def pending(self, user_id: int | None = None, limit: int = 100) -> list[dict]:
        """Pending requests (optionally for one user), with expiry applied."""
        self.store.mark_expired()
        return self.store.get_pending(user_id=user_id, limit=limit)

    def history(self, user_id: int | None = None, limit: int = 50) -> list[dict]:
        return self.store.list(user_id=user_id, limit=limit)
