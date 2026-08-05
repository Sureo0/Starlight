"""
Scheduled task scheduler for the agent.

Lets the agent run AUTOMATICALLY at configured times — the "主动" capability:
a task (prompt) is executed by a fresh agent instance in a background thread,
and the result is posted into a conversation so the user can read it like a
normal chat.

Components:
  - TaskStore: SQLite persistence for tasks + run history (scheduled_tasks /
    scheduled_task_runs tables).
  - AgentScheduler: wraps APScheduler BackgroundScheduler; owns the execution
    loop. Execution is gated by the shared RateLimiter's concurrent slot so a
    scheduled run never competes with the user's live chat (or vice versa) —
    if the slot is busy, the run is SKIPPED (missed) and recorded.
  - One agent instance per run, built via the create_agent factory with its
    own workspace — thread-safe, no shared mutable agent state.

Schedule expressions are plain text (APScheduler cron syntax), e.g.
"0 9 * * *" (every day 09:00). The app converts stored expressions to
CronTrigger jobs.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("agent.scheduler")

# ============================================================
# Run statuses
# ============================================================

RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_FAILED = "failed"
RUN_SKIPPED = "skipped"  # concurrent slot busy — run was skipped

RUN_TERMINAL = {RUN_SUCCESS, RUN_FAILED, RUN_SKIPPED}


# ============================================================
# Storage
# ============================================================

class TaskStore:
    """SQLite persistence for scheduled tasks and their run history."""

    def __init__(self, db):
        self.db = db

    # --------------------------------------------------------
    # Schema (appended to database._init_schema)
    # --------------------------------------------------------

    @staticmethod
    def schema_sql() -> str:
        return """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                prompt      TEXT    NOT NULL,
                schedule    TEXT    NOT NULL,            -- cron expression
                enabled     INTEGER NOT NULL DEFAULT 1,
                conversation_id TEXT,
                last_run_at TEXT,
                next_run_at TEXT,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER NOT NULL,
                status      TEXT    NOT NULL,
                started_at  TEXT    NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                content     TEXT,
                error       TEXT,
                trace_id    TEXT,
                FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_task_runs_task ON scheduled_task_runs(task_id, id DESC);
        """

    # --------------------------------------------------------
    # Tasks CRUD
    # --------------------------------------------------------

    def create_task(self, name, prompt, schedule, enabled=True, conversation_id=None):
        """Create a task; returns the new id."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self.db._get_conn()
        cur = conn.execute(
            """INSERT INTO scheduled_tasks
               (name, prompt, schedule, enabled, conversation_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, prompt, schedule, 1 if enabled else 0, conversation_id, now, now),
        )
        conn.commit()
        return cur.lastrowid

    def update_task(self, task_id, **fields):
        """Update a task (name/prompt/schedule/enabled/conversation_id)."""
        allowed = {"name", "prompt", "schedule", "enabled", "conversation_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn = self.db._get_conn()
        conn.execute(
            f"UPDATE scheduled_tasks SET {sets} WHERE id = ?",
            (*updates.values(), task_id),
        )
        conn.commit()
        return True

    def delete_task(self, task_id):
        conn = self.db._get_conn()
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        conn.commit()
        return True

    def get_task(self, task_id):
        conn = self.db._get_conn()
        row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_tasks(self):
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_task_times(self, task_id, last_run_at=None, next_run_at=None):
        conn = self.db._get_conn()
        conn.execute(
            "UPDATE scheduled_tasks SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
            (last_run_at, next_run_at, datetime.now(timezone.utc).isoformat(), task_id),
        )
        conn.commit()

    # --------------------------------------------------------
    # Run history
    # --------------------------------------------------------

    def record_run_start(self, task_id):
        """Insert a run row; returns its id."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self.db._get_conn()
        cur = conn.execute(
            """INSERT INTO scheduled_task_runs (task_id, status, started_at)
               VALUES (?, ?, ?)""",
            (task_id, RUN_RUNNING, now),
        )
        conn.commit()
        return cur.lastrowid

    def record_run_finish(self, run_id, status, content=None, error=None,
                          duration_ms=None, trace_id=None):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.db._get_conn()
        conn.execute(
            """UPDATE scheduled_task_runs
               SET status = ?, finished_at = ?, duration_ms = ?, content = ?, error = ?, trace_id = ?
               WHERE id = ?""",
            (status, now, duration_ms, content, error, trace_id, run_id),
        )
        conn.commit()

    def list_runs(self, task_id=None, limit=50):
        conn = self.db._get_conn()
        if task_id:
            rows = conn.execute(
                """SELECT * FROM scheduled_task_runs
                   WHERE task_id = ? ORDER BY id DESC LIMIT ?""",
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM scheduled_task_runs ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_runs(self, limit=20):
        """Latest runs joined with their task names (for the history page)."""
        conn = self.db._get_conn()
        rows = conn.execute(
            """SELECT r.id, r.task_id, r.status, r.started_at, r.finished_at,
                      r.duration_ms, r.content, r.error, r.trace_id,
                      t.name AS task_name
               FROM scheduled_task_runs r
               LEFT JOIN scheduled_tasks t ON t.id = r.task_id
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_old_runs(self, keep=200):
        """Prune run history (keep most recent N rows)."""
        conn = self.db._get_conn()
        conn.execute(
            """DELETE FROM scheduled_task_runs WHERE id NOT IN (
                   SELECT id FROM scheduled_task_runs ORDER BY id DESC LIMIT ?
               )""",
            (keep,),
        )
        conn.commit()
        return True


# ============================================================
# Scheduler
# ============================================================

class AgentScheduler:
    """Runs agent tasks on a schedule (APScheduler background thread)."""

    def __init__(
        self,
        store: TaskStore,
        agent_factory,
        rate_limiter=None,
        enabled: bool = True,
        max_instances: int = 1,
    ):
        """
        Args:
            store: TaskStore instance.
            agent_factory: callable() -> new agent instance per run.
            rate_limiter: shared RateLimiter (concurrency gate with live chat).
            enabled: master switch (config scheduled.enabled).
            max_instances: APScheduler max_instances per job (default 1 —
                overlapping runs are skipped).
        """
        self.store = store
        self.agent_factory = agent_factory
        self.rate_limiter = rate_limiter
        self.enabled = enabled
        self.max_instances = max_instances
        self._scheduler = None
        self._jobs: dict[int, object] = {}  # task_id -> APScheduler job
        self._lock = threading.Lock()

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        """Start the background scheduler and schedule all enabled tasks.

        Fail-soft: if APScheduler is not installed, scheduled tasks are
        disabled with a clear log message (the rest of the app is unaffected).
        """
        if not self.enabled:
            logger.info("Scheduled tasks disabled (scheduled.enabled=false)")
            return
        if self.running:
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            logger.error(
                "APScheduler is not installed — scheduled tasks are DISABLED. "
                "Install it with: pip install apscheduler (or pip install -r requirements.txt)"
            )
            self._scheduler = None
            return

        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._scheduler.start()
        self._reschedule_all()
        logger.info("AgentScheduler started")

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            self._jobs.clear()

    def _reschedule_all(self) -> None:
        if self._scheduler is None:
            return
        for task in self.store.list_tasks():
            if task.get("enabled"):
                self._schedule_task(task)

    # --------------------------------------------------------
    # Per-task scheduling
    # --------------------------------------------------------

    def _schedule_task(self, task: dict) -> None:
        """Add/refresh the APScheduler job for one task."""
        if self._scheduler is None:
            return
        task_id = task["id"]
        job_id = f"scheduled_task_{task_id}"
        try:
            trigger = self._cron_trigger(task["schedule"])
        except Exception as e:
            logger.warning("Task %d invalid schedule %r: %s", task_id, task["schedule"], e)
            return
        with self._lock:
            existing = self._jobs.get(task_id)
            if existing is not None:
                existing.remove()
        job = self._scheduler.add_job(
            self._run_task_job,
            trigger=trigger,
            args=[task_id],
            id=job_id,
            replace_existing=True,
            max_instances=self.max_instances,
            coalesce=True,
        )
        with self._lock:
            self._jobs[task_id] = job
        self.store.set_task_times(task_id, next_run_at=_iso(job.next_run_time))
        logger.info("Scheduled task %d (%s): %s", task_id, task["name"], task["schedule"])

    def unschedule_task(self, task_id: int) -> None:
        with self._lock:
            job = self._jobs.pop(task_id, None)
        if job is not None:
            job.remove()

    def refresh_task(self, task_id: int) -> None:
        """Re-schedule a task after edit (or unschedule if disabled/deleted)."""
        task = self.store.get_task(task_id)
        if task is None:
            self.unschedule_task(task_id)
            return
        if task.get("enabled"):
            self._schedule_task(task)
        else:
            self.unschedule_task(task_id)

    def refresh_all(self) -> None:
        self.unschedule_all()
        self._reschedule_all()

    def unschedule_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.remove()

    # --------------------------------------------------------
    # Execution
    # --------------------------------------------------------

    def _run_task_job(self, task_id: int) -> None:
        """APScheduler job entry point (background thread)."""
        if self._scheduler is None or not self.running:
            return
        task = self.store.get_task(task_id)
        if task is None or not task.get("enabled"):
            return

        # Update next_run_at immediately so the UI shows the fresh schedule
        job = self._jobs.get(task_id)
        self.store.set_task_times(
            task_id, next_run_at=_iso(job.next_run_time) if job else None
        )

        # Concurrency gate: share the live-chat slot so a scheduled run never
        # competes with the user's chat (and vice versa). Busy => skip.
        acquired = False
        if self.rate_limiter is not None:
            acquired, reason = self.rate_limiter.acquire_concurrent()
            if not acquired:
                self._record_run(task_id, RUN_SKIPPED, error=f"系统繁忙（并发槽位被占用）: {reason}")
                logger.info("Scheduled task %d skipped (concurrent slot busy)", task_id)
                return

        try:
            self._record_run(task_id, run_fn=lambda: self._execute(task))
        finally:
            if acquired and self.rate_limiter is not None:
                self.rate_limiter.release_concurrent()

    def run_task_now(self, task_id: int) -> dict:
        """Run a task immediately (manual trigger from the UI).

        Respects the same concurrency gate: if the live chat holds the slot,
        the manual run is skipped (fail-soft, recorded).
        """
        task = self.store.get_task(task_id)
        if task is None:
            return {"ok": False, "message": "任务不存在"}
        acquired = False
        if self.rate_limiter is not None:
            acquired, reason = self.rate_limiter.acquire_concurrent()
            if not acquired:
                run_id = self.store.record_run_start(task_id)
                self.store.record_run_finish(
                    run_id, RUN_SKIPPED, error=f"系统繁忙（并发槽位被占用）: {reason}",
                    duration_ms=0,
                )
                return {"ok": False, "message": f"系统繁忙: {reason}"}
        try:
            result = self._record_run(task_id, run_fn=lambda: self._execute(task))
            return {"ok": result["status"] == RUN_SUCCESS, "run": result}
        finally:
            if acquired and self.rate_limiter is not None:
                self.rate_limiter.release_concurrent()

    def _record_run(self, task_id: int, status: str = None, error: str = None,
                    run_fn=None) -> dict:
        """Execute run_fn (if given) recording the run; or record a bare run.

        Returns the run row dict.
        """
        run_id = self.store.record_run_start(task_id)
        started = time.time()
        if run_fn is None:
            self.store.record_run_finish(run_id, status, error=error, duration_ms=0)
            self.store.set_task_times(task_id, last_run_at=_now())
            return {"id": run_id, "status": status, "error": error}
        try:
            outcome = run_fn()
        except Exception as e:
            logger.exception("Scheduled task %d crashed", task_id)
            self.store.record_run_finish(
                run_id, RUN_FAILED, error=f"执行异常: {e}",
                duration_ms=int((time.time() - started) * 1000),
            )
            self.store.set_task_times(task_id, last_run_at=_now())
            return {"id": run_id, "status": RUN_FAILED, "error": str(e)}
        self.store.record_run_finish(
            run_id, outcome["status"], content=outcome.get("content"),
            error=outcome.get("error"), duration_ms=int((time.time() - started) * 1000),
            trace_id=outcome.get("trace_id"),
        )
        self.store.set_task_times(task_id, last_run_at=_now())
        return {
            "id": run_id, "status": outcome["status"],
            "content": outcome.get("content"), "error": outcome.get("error"),
            "trace_id": outcome.get("trace_id"),
        }

    def _execute(self, task: dict) -> dict:
        """Run one task with a fresh agent; post the result into its conversation."""
        prompt = task["prompt"]
        conv_id = task.get("conversation_id")
        agent = self.agent_factory()
        try:
            result = agent.run(prompt, conversation_id=conv_id)
        finally:
            # Let the agent's trace sink flush; give it a beat in background
            time.sleep(0.1)

        content = result.get("content") or ""
        trace_id = None
        try:
            trace_id = getattr(
                getattr(agent, "trace_recorder", None), "trace", None
            ).trace_id
        except Exception:
            trace_id = None

        # Success = the run finished with a real answer. run() returns
        # {"content": ...} on both success and guarded failures; the trace's
        # finish_reason disambiguates (text_response = success).
        success = False
        error = "任务未正常完成"
        try:
            finish_reason = getattr(
                getattr(agent, "trace_recorder", None), "trace", None
            ).finish_reason
        except Exception:
            finish_reason = None
        if finish_reason == "text_response" or (
            finish_reason is None and content
        ):
            success = True
            error = None
        elif result.get("events"):
            errs = [e for e in result["events"] if e.get("type") == "error"]
            if errs:
                error = errs[-1].get("content") or errs[-1].get("error") or error

        return {
            "status": RUN_SUCCESS if success else RUN_FAILED,
            "content": content,
            "error": error,
            "trace_id": trace_id,
        }

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    @staticmethod
    def _cron_trigger(expr: str):
        """Parse a cron expression into an APScheduler CronTrigger."""
        from apscheduler.triggers.cron import CronTrigger
        parts = [p.strip() for p in expr.split()]
        if len(parts) != 5:
            raise ValueError(f"cron expression must have 5 fields, got {len(parts)}")
        return CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4],
        )

    def validate_schedule(self, expr: str) -> tuple[bool, str]:
        """Validate a cron expression; returns (ok, message)."""
        try:
            self._cron_trigger(expr)
            return True, "ok"
        except Exception as e:
            return False, str(e)


# ============================================================
# Time helpers
# ============================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None
