"""
AI Chat - SQLite Database Module

Provides ACID-compliant storage for users, conversations, and messages.
Replaces the previous JSON file-based storage.

Usage:
    from database import db

    # Users
    user = db.get_user("admin")
    db.create_user("alice", "hashed_pw")
    db.update_user("alice", password_hash="new_hash")

    # Conversations
    convs = db.list_conversations(user_id=1)
    conv = db.get_conversation("abc123")
    db.save_conversation("abc123", title="New Title", user_id=1)

    # Messages
    msgs = db.get_messages("abc123")
    db.add_message("abc123", "user", "Hello")
"""

import json
import sqlite3
import threading
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("ai-chat.db")

# Scheduled-task tables (appended to the main schema; kept here so the
# scheduler module can stay independent of this file's structure).
_SCHEDULER_SCHEMA = """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        prompt          TEXT    NOT NULL,
        schedule        TEXT    NOT NULL,
        enabled         INTEGER NOT NULL DEFAULT 1,
        conversation_id TEXT,
        last_run_at     TEXT,
        next_run_at     TEXT,
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL
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

# ============================================================
# Database path
# ============================================================
DATA_DIR = Path(__file__).parent  # data/
DB_FILE = DATA_DIR / "chat.db"


# ============================================================
# Database class
# ============================================================
class Database:
    """Thread-safe SQLite database wrapper."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_FILE
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self):
        """Get a thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT    UNIQUE NOT NULL,
                password    TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id          TEXT    PRIMARY KEY,
                title       TEXT    NOT NULL DEFAULT 'New Chat',
                user_id     INTEGER,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT    NOT NULL,
                role            TEXT    NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content         TEXT    NOT NULL,
                timestamp       TEXT    NOT NULL,
                attachments     TEXT    DEFAULT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);

            -- ============================================================
            -- Long-term memory
            -- ============================================================
            CREATE TABLE IF NOT EXISTS memories (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER,
                conversation_id TEXT,
                memory_type     TEXT    NOT NULL DEFAULT 'fact',
                content         TEXT    NOT NULL,
                importance      INTEGER NOT NULL DEFAULT 3,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL,
                last_accessed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
            CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);

            -- ============================================================
            -- Human-in-the-loop approval requests
            -- ============================================================
            CREATE TABLE IF NOT EXISTS approval_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                tool        TEXT    NOT NULL,
                args        TEXT    NOT NULL DEFAULT '{}',
                status      TEXT    NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','approved','rejected','expired','canceled')),
                reason      TEXT,
                created_at  TEXT    NOT NULL,
                decided_at  TEXT,
                expires_at  TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_approvals_user_status
                ON approval_requests(user_id, status);

            -- ============================================================
            -- Conversation summaries (context compression, 跨回合复用)
            -- ============================================================
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                summary         TEXT    NOT NULL,
                message_count   INTEGER NOT NULL DEFAULT 0,
                updated_at      TEXT    NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
        """ + _SCHEDULER_SCHEMA)
        conn.commit()

        # Lightweight column migrations for pre-existing databases:
        # add the `attachments` column to messages if missing.
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
            if cols and "attachments" not in cols:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT NULL"
                )
                conn.commit()
                logger.info("Migrated: messages.attachments column added")
        except Exception:
            logger.warning("messages.attachments migration failed (non-fatal)", exc_info=True)

        # FTS index for memories (separate try: FTS5 may not exist on some builds)
        self._init_memories_fts()

        logger.info("Database initialized: %s", self.db_path)

    def _init_memories_fts(self):
        """Create the FTS5 index over memories.content if it doesn't exist.

        The FTS index is kept in sync manually by the memory methods in this
        class (index rowid == memories.id). Bigram tokens are produced by the
        segmenter in agent/memory/segmenter.py so Chinese text is searchable.
        """
        conn = self._get_conn()
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    tokens,
                    tokenize='unicode61'
                )
                """
            )
            conn.commit()
        except Exception as e:
            logger.error("FTS5 unavailable, memories will use LIKE fallback: %s", e)

    # ============================================================
    # User operations
    # ============================================================

    def get_user(self, username):
        """Get a user by username. Returns dict or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, username, password, created_at, updated_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row:
            return dict(row)
        return None

    def get_user_by_id(self, user_id):
        """Get a user by ID."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, username, password, created_at, updated_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return dict(row)
        return None

    def create_user(self, username, password_hash):
        """Create a new user. Returns user ID or None if exists."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, password, created_at) VALUES (?, ?, ?)",
                (username, password_hash, now),
            )
            conn.commit()
            logger.info("User created: %s (id=%d)", username, cursor.lastrowid)
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    def update_user(self, username, password_hash=None):
        """Update user fields."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        if password_hash is not None:
            conn.execute(
                "UPDATE users SET password = ?, updated_at = ? WHERE username = ?",
                (password_hash, now, username),
            )
        else:
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE username = ?",
                (now, username),
            )
        conn.commit()

    def delete_user(self, username):
        """Delete a user. Returns True if deleted."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return cursor.rowcount > 0

    def list_users(self):
        """Return list of all usernames."""
        conn = self._get_conn()
        rows = conn.execute("SELECT username FROM users ORDER BY id").fetchall()
        return [r["username"] for r in rows]

    def count_users(self):
        """Return total user count."""
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]

    # ============================================================
    # Conversation operations
    # ============================================================

    def list_conversations(self, user_id=None, limit=100, offset=0):
        """List conversations, most recently updated first."""
        conn = self._get_conn()
        if user_id is not None:
            rows = conn.execute(
                """SELECT c.id, c.title, c.created_at, c.updated_at,
                          COUNT(m.id) as message_count
                   FROM conversations c
                   LEFT JOIN messages m ON m.conversation_id = c.id
                   WHERE c.user_id = ?
                   GROUP BY c.id
                   ORDER BY c.updated_at DESC
                   LIMIT ? OFFSET ?""",
                (user_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT c.id, c.title, c.created_at, c.updated_at,
                          COUNT(m.id) as message_count
                   FROM conversations c
                   LEFT JOIN messages m ON m.conversation_id = c.id
                   GROUP BY c.id
                   ORDER BY c.updated_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conv_id):
        """Get a conversation with all its messages."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, title, user_id, created_at, updated_at FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        if not row:
            return None

        conv = dict(row)
        messages = self.get_messages(conv_id)
        conv["messages"] = messages
        return conv

    def create_conversation(self, conv_id, title="New Chat", user_id=None):
        """Create a new conversation."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO conversations (id, title, user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conv_id, title, user_id, now, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_conversation(self, conv_id, title=None):
        """Update conversation title and set updated_at."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        if title is not None:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conv_id),
            )
        else:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conv_id),
            )
        conn.commit()

    def delete_conversation(self, conv_id):
        """Delete a conversation and all its messages (CASCADE)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()

    def count_conversations(self):
        """Return total conversation count."""
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) as cnt FROM conversations").fetchone()["cnt"]

    # ============================================================
    # Message operations
    # ============================================================

    def get_messages(self, conv_id, limit=None):
        """Get all messages for a conversation, ordered by timestamp."""
        conn = self._get_conn()
        if limit:
            rows = conn.execute(
                """SELECT id, role, content, timestamp, attachments
                   FROM messages
                   WHERE conversation_id = ?
                   ORDER BY id ASC
                   LIMIT ?""",
                (conv_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, role, content, timestamp, attachments
                   FROM messages
                   WHERE conversation_id = ?
                   ORDER BY id ASC""",
                (conv_id,),
            ).fetchall()
        out = []
        for r in rows:
            m = dict(r)
            att = m.pop("attachments", None)
            if att:
                try:
                    m["attachments"] = json.loads(att)
                except (json.JSONDecodeError, TypeError):
                    m["attachments"] = None
            else:
                m["attachments"] = None
            out.append(m)
        return out

    def add_message(self, conv_id, role, content, timestamp=None, attachments=None):
        """Add a message to a conversation.

        attachments: optional list of dicts stored as JSON in the
        `attachments` column (e.g. [{"file_id": "...", "name": "a.png",
        "kind": "image"}]).
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        att_json = None
        if attachments:
            att_json = json.dumps(attachments, ensure_ascii=False)
        cursor = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, timestamp, attachments) VALUES (?, ?, ?, ?, ?)",
            (conv_id, role, content, timestamp, att_json),
        )
        conn.commit()

        return cursor.lastrowid

    def save_summary(self, conv_id, summary, message_count=0):
        """Upsert a conversation summary (context compression)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO conversation_summaries (conversation_id, summary, message_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                summary = excluded.summary,
                message_count = excluded.message_count,
                updated_at = excluded.updated_at
            """,
            (conv_id, summary, int(message_count), now),
        )
        conn.commit()
        return True

    def get_summary(self, conv_id):
        """Get the stored summary for a conversation ('' if none)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT summary, message_count FROM conversation_summaries WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()
        return row["summary"] if row else ""

    def get_summary_info(self, conv_id):
        """Get the stored summary plus message count (None if none)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT summary, message_count, updated_at FROM conversation_summaries WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_messages_for_llm(self, conv_id, limit=20):
        """Get messages formatted for LLM API calls."""
        messages = self.get_messages(conv_id, limit=limit)
        return [{"role": m["role"], "content": m["content"]} for m in messages]

    def count_messages(self):
        """Return total message count."""
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) as cnt FROM messages").fetchone()["cnt"]

    # ============================================================
    # Long-term memory operations
    # ============================================================

    def _fts_enabled(self) -> bool:
        """Return True if the FTS5 memory index exists."""
        conn = self._get_conn()
        try:
            conn.execute("SELECT rowid FROM memories_fts LIMIT 1").fetchone()
            return True
        except Exception:
            return False

    @staticmethod
    def _segment_text(text: str) -> str:
        """Tokenize text for FTS indexing (lazy import to avoid cycles)."""
        from agent.memory.segmenter import segment_text
        return segment_text(text)

    def store_memory(
        self,
        user_id: int | None,
        content: str,
        memory_type: str = "fact",
        importance: int = 3,
        conversation_id: str | None = None,
        dedup_window_days: int = 7,
        conflict_window_days: int = 180,
    ) -> dict:
        """Store a memory. Skips near-duplicates stored within the dedup window.

        Conflict handling: if an existing memory of the same type was stored
        within `conflict_window_days` and the new one is MORE important, the
        old one is replaced (the facts changed — e.g. city moved). Equal or
        lower importance keeps the old one (repeated claims just reinforce).

        Returns dict with 'stored'/'duplicate_of'/'replaced' flags.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()

        # Near-duplicate check: same user + same type + same content within window
        if dedup_window_days and content.strip():
            row = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE user_id IS ? AND memory_type = ? AND updated_at >= ?
                ORDER BY updated_at DESC LIMIT 10
                """,
                (
                    user_id,
                    memory_type,
                    (datetime.fromisoformat(now) - timedelta(days=dedup_window_days)).isoformat(),
                ),
            ).fetchall()
            for r in row:
                if r["content"].strip() == content.strip():
                    # Refresh timestamp on re-mention, bump importance slightly
                    conn.execute(
                        "UPDATE memories SET updated_at = ?, importance = MIN(5, importance + 1) WHERE id = ?",
                        (now, r["id"]),
                    )
                    conn.commit()
                    return {
                        "stored": False,
                        "duplicate_of": r["id"],
                        "boosted": True,
                        "memory": self.get_memory(r["id"]),
                    }

        # Conflict check: same type, different content, stored recently,
        # and the NEW claim is more important -> the user's facts changed.
        if conflict_window_days and content.strip():
            conflict = conn.execute(
                """
                SELECT id, content, importance FROM memories
                WHERE user_id IS ? AND memory_type = ? AND content != ?
                  AND updated_at >= ? AND importance < ?
                ORDER BY updated_at DESC LIMIT 5
                """,
                (
                    user_id,
                    memory_type,
                    content.strip(),
                    (datetime.fromisoformat(now) - timedelta(days=conflict_window_days)).isoformat(),
                    importance,
                ),
            ).fetchall()
            if conflict:
                old = conflict[0]
                # Replace: the newer, more important claim wins.
                conn.execute(
                    "DELETE FROM memories WHERE id = ?", (old["id"],)
                )
                if self._fts_enabled():
                    conn.execute(
                        "DELETE FROM memories_fts WHERE rowid = ?", (old["id"],)
                    )
                conn.commit()
                logger.info(
                    "Memory conflict resolved: replaced #%d (%r) with %r (importance %d > %d)",
                    old["id"], old["content"][:40], content.strip()[:40],
                    importance, old["importance"],
                )
                # Fall through to insert the new memory below.

        cursor = conn.execute(
            """
            INSERT INTO memories
                (user_id, conversation_id, memory_type, content, importance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, conversation_id, memory_type, content, importance, now, now),
        )
        mem_id = cursor.lastrowid

        # Keep FTS index in sync
        if self._fts_enabled():
            conn.execute(
                "INSERT INTO memories_fts(rowid, tokens) VALUES (?, ?)",
                (mem_id, self._segment_text(content)),
            )
        conn.commit()

        memory = self.get_memory(mem_id)
        result = {"stored": True, "memory": memory}
        if conflict:
            result["replaced"] = old["id"]
        return result

    def get_memory(self, mem_id: int) -> dict | None:
        """Get a single memory by id."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        return dict(row) if row else None

    def search_memories(
        self,
        query: str,
        user_id: int | None = None,
        limit: int = 10,
        memory_type: str | None = None,
        min_importance: int = 1,
    ) -> list[dict]:
        """Semantic-ish keyword search over memories (FTS5 bigram, BM25 rank).

        Falls back to LIKE search if FTS5 is unavailable.
        """
        conn = self._get_conn()
        limit = min(max(limit, 1), 50)

        if query and self._fts_enabled():
            from agent.memory.segmenter import build_fts_query
            fts_q = build_fts_query(query)
            if fts_q:
                try:
                    sql = """
                        SELECT m.*, bm25(memories_fts) AS rank
                        FROM memories_fts
                        JOIN memories m ON m.id = memories_fts.rowid
                        WHERE memories_fts MATCH ?
                    """
                    params: list = [fts_q]
                    if user_id is not None:
                        sql += " AND m.user_id = ?"
                        params.append(user_id)
                    if memory_type:
                        sql += " AND m.memory_type = ?"
                        params.append(memory_type)
                    sql += " AND m.importance >= {0} ORDER BY rank LIMIT {1}".format("?", "?")
                    params += [min_importance, limit]

                    rows = conn.execute(sql, params).fetchall()
                    results = [dict(r) for r in rows]
                    if results:
                        return results
                except Exception as e:
                    logger.warning("FTS search failed (%s), falling back to LIKE", e)

        # LIKE fallback
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        if query:
            sql += " AND content LIKE ?"
            params.append(f"%{query}%")
        sql += " AND importance >= ? ORDER BY updated_at DESC LIMIT ?"
        params += [min_importance, limit]
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def list_memories(
        self,
        user_id: int | None = None,
        limit: int = 50,
        memory_type: str | None = None,
    ) -> list[dict]:
        """List recent memories (newest first)."""
        conn = self._get_conn()
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, mem_id: int) -> bool:
        """Delete a memory by id. Returns True if deleted."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        if cursor.rowcount > 0 and self._fts_enabled():
            conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (mem_id,))
        conn.commit()
        return cursor.rowcount > 0

    def touch_memory(self, mem_id: int) -> None:
        """Mark a memory as accessed (updates last_accessed_at)."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE memories SET last_accessed_at = ? WHERE id = ?", (now, mem_id)
        )
        conn.commit()

    def update_memory(self, mem_id: int, content: str | None = None,
                      importance: int | None = None) -> bool:
        """Update a memory's content and/or importance. Returns True on success.

        Content changes re-sync the FTS index.
        """
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        current = self.get_memory(mem_id)
        if current is None:
            return False
        new_content = content if content is not None else current["content"]
        new_importance = importance if importance is not None else current["importance"]
        conn.execute(
            "UPDATE memories SET content = ?, importance = ?, updated_at = ? WHERE id = ?",
            (new_content, new_importance, now, mem_id),
        )
        if self._fts_enabled():
            conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (mem_id,))
            conn.execute(
                "INSERT INTO memories_fts(rowid, tokens) VALUES (?, ?)",
                (mem_id, self._segment_text(new_content)),
            )
        conn.commit()
        return True

    def demote_stale_memories(self, days: int = 30) -> int:
        """Decay stale memories: drop importance by 1 (floor 1) for memories
        not accessed in `days` days. Repeated demotion eventually makes them
        cheap to clean up or pushes them below the injection threshold.

        Returns the number of memories demoted.
        """
        conn = self._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """
            SELECT id FROM memories
            WHERE importance > 1 AND updated_at < ?
            AND (last_accessed_at IS NULL OR last_accessed_at < ?)
            """,
            (cutoff, cutoff),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE memories SET importance = MAX(1, importance - 1) WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        logger.info("Demoted %d stale memories", len(ids))
        return len(ids)

    def cleanup_stale_memories(self, days: int = 90) -> int:
        """Delete memories not accessed for `days` days (importance < 4 only).

        Returns the number of memories removed. High-importance memories
        (>= 4) are never auto-removed.
        """
        conn = self._get_conn()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()
        rows = conn.execute(
            """
            SELECT id FROM memories
            WHERE importance < 4 AND updated_at < ?
            AND (last_accessed_at IS NULL OR last_accessed_at < ?)
            """,
            (cutoff, cutoff),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        if self._fts_enabled():
            conn.execute(f"DELETE FROM memories_fts WHERE rowid IN ({placeholders})", ids)
        conn.commit()
        logger.info("Cleaned up %d stale memories", len(ids))
        return len(ids)

    def count_memories(self) -> int:
        """Return total memory count."""
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]

    # ============================================================
    # Maintenance
    # ============================================================

    def vacuum(self):
        """Reclaim space and optimize database."""
        conn = self._get_conn()
        conn.execute("VACUUM")
        logger.info("Database vacuumed")

    def get_stats(self):
        """Return database statistics."""
        conn = self._get_conn()
        db_path = Path(self.db_path) if not isinstance(self.db_path, Path) else self.db_path
        return {
            "users": self.count_users(),
            "conversations": self.count_conversations(),
            "messages": self.count_messages(),
            "db_size_kb": round(db_path.stat().st_size / 1024, 1) if db_path.exists() else 0,
        }

    def close(self):
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# ============================================================
# Singleton instance (lazy initialization)
# ============================================================
_db_instance = None


def get_db():
    """Get or create the database singleton."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance


class _DbProxy:
    """Proxy that delegates to the real database instance."""
    def __getattr__(self, name):
        return getattr(get_db(), name)


db = _DbProxy()
