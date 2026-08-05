"""
Memory Tools - long-term memory tools for the agent.

These tools wrap MemoryService (SQLite + FTS5) to give the agent durable,
cross-conversation memory:
  - memory_query:   search long-term memories by keywords (FTS5 bigram, BM25)
  - memory_store:   store a memory explicitly
  - memory_forget:  delete a memory
  - memory_list:    list recent memories

All tools are user-scoped (user_id) so memory never leaks between users.
"""

from __future__ import annotations

import logging

from agent.tools.base import Tool, ToolResult
from agent.memory.service import MemoryService

logger = logging.getLogger("agent.tools.memory")


class MemoryQueryTool(Tool):
    """
    Search long-term memories across all conversations.

    Uses FTS5 bigram search with BM25 ranking, so Chinese keyword queries
    like "上海" or "喜欢什么" match stored memories well.
    """

    def __init__(self, db=None, memory_service: MemoryService | None = None):
        """
        Args:
            db: Database proxy (legacy path). If given, a MemoryService is
                derived from it.
            memory_service: A MemoryService instance. Prefer this.
        """
        self._db = db
        self._service = memory_service or (MemoryService(db) if db else None)

    @property
    def name(self) -> str:
        return "memory_query"

    @property
    def description(self) -> str:
        return (
            "搜索长期记忆。跨会话检索用户之前提到过的事实、偏好和任务信息。"
            "当用户的问题涉及『我之前说过…』『你记得…』『我上次…』等，或需要"
            "了解用户的背景信息时使用。"
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如 '上海'、'工作'、'喜欢的食物'",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数。默认 5，最大 20",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "preference", "task"],
                    "description": "按记忆类型过滤。省略则全部",
                },
            },
            "required": ["query"],
        }

    def execute(
        self,
        query: str = "",
        limit: int = 5,
        memory_type: str | None = None,
        **kwargs,
    ) -> ToolResult:
        """Search long-term memories."""
        if not self._service:
            return ToolResult(success=False, error="记忆服务不可用")
        if not query or not query.strip():
            return ToolResult(
                success=True,
                output=self._service.list(limit=min(limit, 20)),
                metadata={"count": 0, "query": query},
            )

        limit = min(max(limit, 1), 20)
        try:
            results = self._service.search(
                query=query, limit=limit, memory_type=memory_type
            )
            formatted = [
                {
                    "id": m.get("id"),
                    "memory_type": m.get("memory_type"),
                    "content": m.get("content"),
                    "importance": m.get("importance"),
                    "updated_at": (m.get("updated_at") or "")[:10],
                }
                for m in results
            ]
            return ToolResult(
                success=True,
                output=formatted,
                metadata={"count": len(formatted), "query": query},
            )
        except Exception as e:
            logger.exception("Memory query failed")
            return ToolResult(success=False, error=str(e))


class MemoryStoreTool(Tool):
    """
    Store a long-term memory explicitly.

    Use when the user states a fact/preference/task that will matter in
    future conversations.
    """

    # Storage is not idempotent — never auto-retry.
    retryable: bool = False

    def __init__(self, db=None, memory_service: MemoryService | None = None):
        self._db = db
        self._service = memory_service or (MemoryService(db) if db else None)

    @property
    def name(self) -> str:
        return "memory_store"

    @property
    def description(self) -> str:
        return (
            "存储一条长期记忆。当用户明确说出个人偏好、重要事实或进行中的任务"
            "（如『我喜欢简洁的回答』『我住在上海』『我在开发XX项目』）时使用，"
            "以便未来对话中记住。"
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记忆的内容，一句话，第三人称描述",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "preference", "task"],
                    "description": "类型：事实/偏好/任务。默认 fact",
                },
                "importance": {
                    "type": "integer",
                    "description": "重要度 1-5，默认 3",
                },
            },
            "required": ["content"],
        }

    def execute(
        self,
        content: str = "",
        memory_type: str = "fact",
        importance: int = 3,
        **kwargs,
    ) -> ToolResult:
        """Store a memory."""
        if not self._service:
            return ToolResult(success=False, error="记忆服务不可用")
        if not content or not content.strip():
            return ToolResult(success=False, error="content 不能为空")

        try:
            result = self._service.store(
                content=content,
                memory_type=memory_type,
                importance=importance,
            )
            if result.get("stored"):
                return ToolResult(
                    success=True,
                    output={
                        "memory_id": result["memory"]["id"],
                        "content": result["memory"]["content"],
                        "memory_type": result["memory"]["memory_type"],
                        "importance": result["memory"]["importance"],
                        "stored_at": result["memory"]["created_at"][:19],
                    },
                    metadata={"stored": True},
                )
            # Duplicate or invalid
            dup = result.get("duplicate_of")
            return ToolResult(
                success=True,
                output={
                    "content": content,
                    "note": "该记忆已存在" if dup else "记忆未存储",
                    "duplicate_of": dup,
                },
                metadata={"stored": False},
            )
        except Exception as e:
            logger.exception("Memory store failed")
            return ToolResult(success=False, error=str(e))


class MemoryForgetTool(Tool):
    """Delete a stored long-term memory by id."""

    # Deletion is not idempotent — never auto-retry.
    retryable: bool = False

    def __init__(self, db=None, memory_service: MemoryService | None = None):
        self._db = db
        self._service = memory_service or (MemoryService(db) if db else None)

    @property
    def name(self) -> str:
        return "memory_forget"

    @property
    def description(self) -> str:
        return (
            "删除一条长期记忆。当用户明确要求忘记某条记忆（如『忘掉这条』"
            "『不要再记住X』）时使用。"
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "要删除的记忆 ID（来自 memory_query 的结果）",
                },
            },
            "required": ["memory_id"],
        }

    def execute(self, memory_id: int = 0, **kwargs) -> ToolResult:
        if not self._service:
            return ToolResult(success=False, error="记忆服务不可用")
        try:
            deleted = self._service.forget(int(memory_id))
            return ToolResult(
                success=True,
                output={"deleted": deleted, "memory_id": memory_id},
            )
        except Exception as e:
            logger.exception("Memory forget failed")
            return ToolResult(success=False, error=str(e))


class MemoryListTool(Tool):
    """List recent long-term memories."""

    def __init__(self, db=None, memory_service: MemoryService | None = None):
        self._db = db
        self._service = memory_service or (MemoryService(db) if db else None)

    @property
    def name(self) -> str:
        return "memory_list"

    @property
    def description(self) -> str:
        return "列出最近的长期记忆。用于了解当前已记住的信息。"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回条数。默认 20，最大 50",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "preference", "task"],
                    "description": "按类型过滤",
                },
            },
            "required": [],
        }

    def execute(self, limit: int = 20, memory_type: str | None = None, **kwargs) -> ToolResult:
        if not self._service:
            return ToolResult(success=False, error="记忆服务不可用")
        try:
            memories = self._service.list(limit=min(max(limit, 1), 50), memory_type=memory_type)
            formatted = [
                {
                    "id": m.get("id"),
                    "memory_type": m.get("memory_type"),
                    "content": m.get("content"),
                    "importance": m.get("importance"),
                    "updated_at": (m.get("updated_at") or "")[:10],
                }
                for m in memories
            ]
            return ToolResult(
                success=True,
                output=formatted,
                metadata={"count": len(formatted)},
            )
        except Exception as e:
            logger.exception("Memory list failed")
            return ToolResult(success=False, error=str(e))
