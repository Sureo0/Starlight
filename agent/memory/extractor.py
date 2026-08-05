"""
Memory Extractor - automatic long-term memory extraction.

After each conversation turn, this module asks the LLM to decide whether the
exchange contains information worth remembering long-term (user facts,
preferences, task progress). The LLM returns a small JSON list which is stored
via MemoryService.

Design decisions:
  - Fail-soft: any error disables extraction for that turn, never blocks the chat.
  - Cheap: a single LLM call with a tiny prompt, low max_tokens, only after
    sufficiently long turns (>= min_turn_messages).
  - Structured: the LLM outputs strict JSON so we can validate before storing.
  - Dedup: MemoryService.store() already skips near-duplicates.
  - Concurrency-safe: a per-user lock prevents two turns from extracting
    simultaneously.
"""

from __future__ import annotations

import json
import logging
import re
import threading

from agent.memory.service import MemoryService

logger = logging.getLogger("agent.memory.extractor")

EXTRACT_SYSTEM_PROMPT = """你是一个记忆管理助手。判断下面这段对话中是否有值得长期记住的信息。

值得记忆的信息包括：
- preference：用户的偏好、习惯、口味（如"喜欢简洁的回答"、"不爱吃辣"）
- fact：用户的个人事实、背景信息（如"住在上海"、"是程序员"、"养了一只猫"）
- task：进行中的任务或项目（如"正在开发 AI 聊天项目"、"下周要提交论文"）

不值得记忆的包括：闲聊、一次性问答、寒暄、与用户个人无关的通用知识。

注意：
- 只记录**关于用户本人**的信息。AI 的自我介绍、通用建议、聊天内容本身都不算。
- 重要度只给 4-5 如果它会影响未来的对话（如关键偏好、长期项目）；普通事实给 3。

只输出 JSON，不要输出任何其他文字。格式：
{"memories": [{"content": "一句话描述", "memory_type": "preference|fact|task", "importance": 1-5}]}

如果该对话没有值得记忆的信息，输出 {"memories": []}。"""

# Patterns that indicate the "memory" is actually about the AI itself or
# generic content — classic extraction errors we filter out hard.
_AI_SELF_PATTERNS = (
    "我是", "我是一个", "我是你", "我是您的", "我是一个AI", "作为AI", "作为人工智能",
    "我是人工智能", "我是语言模型", "我是助手", "本AI", "本助手", "我的知识",
    "我是DeepSeek", "我是Claude", "我是GPT", "作为语言模型", "由OpenAI",
)
_GENERIC_PATTERNS = (
    "你好", "再见", "谢谢", "不客气", "很高兴", "请问", "有什么可以帮你",
    "很高兴认识", "祝您", "欢迎", "有什么问题", "有什么我可以帮你",
)


class MemoryExtractor:
    """Extracts long-term memories from conversation turns via the LLM."""

    def __init__(
        self,
        llm,
        memory_service: MemoryService,
        min_turn_messages: int = 2,
        min_user_chars: int = 8,
        max_memories_per_turn: int = 5,
        timeout: int = 30,
        quality_gate: bool = True,
    ):
        self._llm = llm
        self._memory = memory_service
        self.min_turn_messages = min_turn_messages
        self.min_user_chars = min_user_chars
        self.max_memories_per_turn = max_memories_per_turn
        self.timeout = timeout
        self.quality_gate = quality_gate
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def maybe_extract(
        self,
        user_message: str,
        assistant_response: str,
        conversation_id: str | None = None,
        force: bool = False,
    ) -> dict:
        """
        Attempt to extract and store memories from one turn.

        Returns {"extracted": int, "stored": int, "skipped": int, "error": str?}
        Never raises.
        """
        if not force and not self._should_extract(user_message, assistant_response):
            return {"extracted": 0, "stored": 0, "skipped": 0}

        if not self._lock.acquire(blocking=False):
            logger.info("Memory extraction already running, skipping")
            return {"extracted": 0, "stored": 0, "skipped": 0, "error": "busy"}

        try:
            items = self._ask_llm(user_message, assistant_response)
            if not items:
                return {"extracted": 0, "stored": 0, "skipped": 0}

            result = self._memory.store_many(items, conversation_id=conversation_id)
            return {
                "extracted": len(items),
                "stored": len(result.get("stored", [])),
                "skipped": len(result.get("skipped", [])),
            }
        except Exception as e:
            logger.exception("Memory extraction failed (turn will proceed normally)")
            return {"extracted": 0, "stored": 0, "skipped": 0, "error": str(e)}
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _should_extract(self, user_message: str, assistant_response: str) -> bool:
        """Cheap pre-filter: only bother the LLM for substantial turns."""
        # A turn must have a real user question and a real assistant answer
        if not user_message or not assistant_response:
            return False
        if len(user_message.strip()) < self.min_user_chars:
            return False
        if len(assistant_response.strip()) < 8:
            return False
        return True

    def _ask_llm(self, user_message: str, assistant_response: str) -> list[dict]:
        """Call the LLM and parse its JSON memory list. Returns [] on any failure."""
        turn_text = (
            f"【用户】{user_message[:2000]}\n\n【助手】{assistant_response[:3000]}"
        )

        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": f"对话内容：\n{turn_text}"},
        ]

        try:
            response = self._llm.chat(
                messages=messages,
                tools=None,  # plain completion, no tool schemas
                tool_choice="none",
                temperature=0.1,
                max_tokens=400,
                timeout=self.timeout,
            )
        except Exception as e:
            logger.warning("Extraction LLM call failed: %s", e)
            return []

        content = (response.content or "").strip()
        items = self._parse_response(content)
        if self.quality_gate:
            items = [it for it in items if self._passes_quality_gate(it)]
        return items[: self.max_memories_per_turn]

    # ------------------------------------------------------------------
    # Quality gate: filter out classic extraction errors
    # ------------------------------------------------------------------

    def _passes_quality_gate(self, item: dict) -> bool:
        """Reject memories that are about the AI itself or generic chit-chat."""
        content = (item.get("content") or "").strip()
        if not content:
            return False
        # AI self-descriptions (the extractor sometimes stores what the
        # assistant said about itself as if it were a user fact).
        for pat in _AI_SELF_PATTERNS:
            if pat in content:
                logger.info("Quality gate: dropped AI-self memory: %r", content[:60])
                return False
        # Generic chit-chat that slipped through
        lowered = content.lower()
        for pat in _GENERIC_PATTERNS:
            if pat in lowered and len(content) < 20:
                logger.info("Quality gate: dropped generic memory: %r", content[:60])
                return False
        return True

    @staticmethod
    def _parse_response(content: str) -> list[dict]:
        """Parse the LLM's JSON response into validated memory items."""
        if not content:
            return []

        # Strip markdown fences if the model wrapped the JSON
        text = content.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        # Fall back to first {...} block
        if not text.startswith("{"):
            brace = text.find("{")
            if brace >= 0:
                text = text[brace:]

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find the JSON object inside surrounding prose
            try:
                start, end = text.index("{"), text.rindex("}")
                data = json.loads(text[start : end + 1])
            except (ValueError, json.JSONDecodeError):
                logger.warning("Could not parse extractor output: %.120s", content)
                return []

        raw_items = data.get("memories", []) if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            return []

        valid_types = MemoryService.SUPPORTED_TYPES
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            content_text = str(raw.get("content", "")).strip()
            if len(content_text) < 4 or len(content_text) > 500:
                continue
            mtype = raw.get("memory_type", "fact")
            if mtype not in valid_types:
                mtype = "fact"
            try:
                importance = int(raw.get("importance", 3))
            except (TypeError, ValueError):
                importance = 3
            items.append(
                {
                    "content": content_text,
                    "memory_type": mtype,
                    "importance": min(max(importance, 1), 5),
                }
            )
        return items
