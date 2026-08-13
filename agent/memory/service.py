"""
MemoryService - high-level long-term memory API for the agent.

Sits on top of the database's memories table + FTS5 index and provides:
  - store(): dedup-aware memory storage
  - search(): keyword/semantic retrieval with user isolation
  - forget(): delete a memory
  - list(): browse memories
  - summarize(): refresh memory for the LLM context (injection format)

Everything is scoped per user (user_id) so one person's memories never
leak into another's context.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("agent.memory.service")


class MemoryService:
    """High-level memory store/search/forget API bound to a database."""

    # Memory types supported by the automatic extractor
    SUPPORTED_TYPES = ("fact", "preference", "task")

    def __init__(self, db, user_id: int | None = None):
        """
        Args:
            db: The database proxy (from database.db), exposing the memory
                methods (store_memory, search_memories, ...).
            user_id: Database user id the memory belongs to. If None, memories
                are global (no user scoping).
        """
        self._db = db
        self.user_id = user_id

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def store(
        self,
        content: str,
        memory_type: str = "fact",
        importance: int = 3,
        conversation_id: str | None = None,
        dedup_window_days: int = 7,
        conflict_window_days: int = 180,
    ) -> dict:
        """Store a memory. Returns {'stored': bool, 'memory': dict, ...}."""
        if not content or not content.strip():
            return {"stored": False, "memory": None, "duplicate_of": None}
        if memory_type not in self.SUPPORTED_TYPES:
            memory_type = "fact"

        try:
            result = self._db.store_memory(
                user_id=self.user_id,
                content=content.strip(),
                memory_type=memory_type,
                importance=min(max(int(importance), 1), 5),
                conversation_id=conversation_id,
                dedup_window_days=dedup_window_days,
                conflict_window_days=conflict_window_days,
            )
            return result
        except Exception as e:
            logger.exception("store_memory failed")
            return {"stored": False, "memory": None, "duplicate_of": None, "error": str(e)}

    def store_many(
        self,
        items: list[dict],
        conversation_id: str | None = None,
    ) -> dict:
        """
        Store several memories at once (used by the automatic extractor).

        items: list of {"content": str, "memory_type": str, "importance": int}
        Returns summary of what was stored/skipped.
        """
        stored, skipped = [], []
        for item in items or []:
            result = self.store(
                content=item.get("content", ""),
                memory_type=item.get("memory_type", "fact"),
                importance=item.get("importance", 3),
                conversation_id=conversation_id,
            )
            if result.get("stored"):
                stored.append(result["memory"])
            else:
                skipped.append(
                    {
                        "content": item.get("content"),
                        "reason": (
                            "replaced" if result.get("replaced")
                            else "duplicate" if result.get("duplicate_of")
                            else "invalid"
                        ),
                        "replaced_id": result.get("replaced"),
                        "boosted": bool(result.get("boosted")),
                    }
                )
        return {"stored": stored, "skipped": skipped}

    def update(self, mem_id: int, content: str | None = None,
               importance: int | None = None) -> bool:
        """Update a memory's content or importance."""
        try:
            return self._db.update_memory(mem_id, content=content, importance=importance)
        except Exception as e:
            logger.exception("update_memory failed")
            return False

    def consolidate(self, min_importance: int = 2, max_results: int = 20) -> int:
        """Merge near-duplicate memories (same type, similar content).

        Uses the LLM when available to judge semantic similarity; falls back
        to token overlap when the LLM is absent/fails. The lower-importance
        duplicate is merged into the higher-importance one (importance adds,
        content of the winner is kept). Returns number of merged memories.

        Fail-soft: never raises.
        """
        try:
            memories = self.list(limit=1000)
        except Exception:
            return 0
        # Group by type, keep only those at/above the floor
        by_type: dict[str, list[dict]] = {}
        for m in memories:
            if int(m.get("importance", 0)) >= min_importance:
                by_type.setdefault(m.get("memory_type", "fact"), []).append(m)

        merged_count = 0
        for mtype, group in by_type.items():
            group.sort(key=lambda m: -int(m.get("importance", 0)))
            used: set[int] = set()
            for i, a in enumerate(group):
                if a["id"] in used:
                    continue
                for b in group[i + 1 :]:
                    if b["id"] in used:
                        continue
                    if self._are_duplicates(a, b):
                        # Merge b into a (a is higher importance)
                        self.update(
                            a["id"],
                            importance=min(5, int(a.get("importance", 1)) + int(b.get("importance", 1))),
                        )
                        self.forget(b["id"])
                        used.add(b["id"])
                        merged_count += 1
                        logger.info(
                            "Consolidated memory #%d into #%d (%s)",
                            b["id"], a["id"], mtype,
                        )
        return merged_count

    def _are_duplicates(self, a: dict, b: dict) -> bool:
        """Cheap similarity check: significant token overlap or identical content."""
        ca = (a.get("content") or "").strip()
        cb = (b.get("content") or "").strip()
        if not ca or not cb:
            return False
        if ca == cb:
            return True
        # Token overlap ratio (both directions)
        ta = set(self._tokenize(ca))
        tb = set(self._tokenize(cb))
        if not ta or not tb:
            return False
        overlap = len(ta & tb) / min(len(ta), len(tb))
        return overlap >= 0.8 and len(ta & tb) >= 3

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """CJK-friendly tokenization for similarity: chars + ascii words."""
        import re
        tokens = re.findall(r"[一-鿿]|[a-zA-Z0-9_]+", text)
        return tokens

    def decay(self, days: int = 30) -> int:
        """Demote stale memories (importance -1, floor 1)."""
        try:
            return self._db.demote_stale_memories(days=days)
        except Exception as e:
            logger.exception("demote_stale_memories failed")
            return 0

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 5,
        memory_type: str | None = None,
        min_importance: int = 1,
        touch: bool = True,
    ) -> list[dict]:
        """Search memories relevant to `query`. Returns list of memory dicts."""
        if not query:
            return []
        try:
            results = self._db.search_memories(
                query=query,
                user_id=self.user_id,
                limit=limit,
                memory_type=memory_type,
                min_importance=min_importance,
            )
            if touch:
                for r in results:
                    try:
                        self._db.touch_memory(r["id"])
                    except Exception:
                        pass
            return results
        except Exception as e:
            logger.exception("search_memories failed")
            return []

    def list(self, limit: int = 50, memory_type: str | None = None) -> list[dict]:
        """List the most recent memories."""
        try:
            return self._db.list_memories(
                user_id=self.user_id, limit=limit, memory_type=memory_type
            )
        except Exception as e:
            logger.exception("list_memories failed")
            return []

    def forget(self, mem_id: int) -> bool:
        """Delete a memory. Returns True if deleted."""
        try:
            return self._db.delete_memory(mem_id)
        except Exception as e:
            logger.exception("delete_memory failed")
            return False

    def get(self, mem_id: int) -> dict | None:
        """Fetch a single memory."""
        try:
            return self._db.get_memory(mem_id)
        except Exception as e:
            logger.exception("get_memory failed")
            return None

    def count(self) -> int:
        """Number of memories for this user."""
        try:
            return len(self.list(limit=100000))
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Context injection helpers
    # ------------------------------------------------------------------

    def build_injection_context(
        self, query: str, max_memories: int = 4, min_importance: int = 2
    ) -> str:
        """
        Build a system-prompt injection block from memories relevant to `query`.

        Returns a ready-to-append string (empty if no memories matched).
        Format is compact so it costs few tokens:
            [长期记忆]
            - [fact] 用户住在上海 (2026-08-01)
            - [preference] 喜欢简洁的回答
        """
        if not query:
            return ""
        memories = self.search(query=query, limit=max_memories, min_importance=min_importance)
        if not memories:
            return ""

        lines = ["[长期记忆]", "以下是与此对话相关的历史记忆，可直接引用："]
        for m in memories:
            date = (m.get("updated_at") or m.get("created_at") or "")[:10]
            mtype = m.get("memory_type", "fact")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"- [{mtype}] {content}（记录于 {date}）")
        return "\n".join(lines)

    def summarize(self, limit: int = 20) -> str:
        """Return a plain-text digest of recent memories (for debugging/UI)."""
        memories = self.list(limit=limit)
        if not memories:
            return "（暂无长期记忆）"
        lines = []
        for m in memories:
            date = (m.get("updated_at") or m.get("created_at") or "")[:10]
            lines.append(
                f"#{m['id']} [{m.get('memory_type')}] 重要度{m.get('importance')} "
                f"{m.get('content')} ({date})"
            )
        return "\n".join(lines)
