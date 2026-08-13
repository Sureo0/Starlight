"""
CompressionManager — orchestrates context compression.

When the running conversation exceeds the trigger threshold, older messages
are replaced by an LLM-generated summary:

  [系统]           system prompt (+ 记忆/计划 blocks, handled by orchestrator)
  [系统] [对话摘要] previous summary MERGED with newly summarized older messages
  [user/assistant] recent messages kept verbatim (boundary: last tool round-trip)
  ...              live messages that triggered the compression

Persistence: the merged summary is stored per-conversation in SQLite
(conversation_summaries table) so FUTURE turns load it as their starting
summary instead of re-summarizing the whole history.

Cost judgement: compression only fires when it actually SAVES context —
an LLM call producing a ~0.3k-token summary must displace enough messages
to be worth it. compute_gain()/should_compress() implement this.

Recursion guard: after compression the message list is ~`budget` tokens;
if the live history is still over budget (rare pathological cases), the
messages are returned TRUNCATED rather than compressed repeatedly.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from agent.compression.summarizer import ContextSummarizer
from agent.memory.context_manager import estimate_message_tokens, estimate_tokens

logger = logging.getLogger("agent.compression.manager")

CompressionHook = Callable[[dict], None]


def _sanitize_summary(summary: str) -> str:
    """Trim very long summaries (safety cap) and collapse whitespace."""
    summary = (summary or "").strip()
    if len(summary) > 2000:
        summary = summary[:2000].rstrip() + "…"
    return summary


class CompressionManager:
    """Summarize old conversation messages to keep the context bounded."""

    def __init__(
        self,
        llm,
        summarizer: ContextSummarizer | None = None,
        budget: int = 32000,
        trigger_ratio: float = 0.75,
        min_messages: int = 10,
        keep_recent: int = 6,
        reserve_for_response: int = 1000,
        min_gain_tokens: int = 800,
        max_summary_chars: int = 2000,
        persist_store=None,
        hook: CompressionHook | None = None,
    ):
        self.llm = llm
        self.summarizer = summarizer or ContextSummarizer(llm)
        self.budget = budget
        self.trigger_ratio = trigger_ratio
        self.min_messages = min_messages
        self.keep_recent = keep_recent
        self.reserve = reserve_for_response
        self.min_gain_tokens = min_gain_tokens
        self.max_summary_chars = max_summary_chars
        self.persist_store = persist_store  # Database proxy (optional)
        self.hook = hook  # observability hook (trace recording)

    # ============================================================
    # Public API
    # ============================================================

    @property
    def trigger_tokens(self) -> int:
        """Token level at which compression is triggered."""
        return int(self.budget * self.trigger_ratio)


    def should_compress(
        self, history: list[dict], prev_summary: str | None = None
    ) -> bool:
        """Whether history should be compressed.

        Compress when the message list (excluding any existing summary block)
        is over the trigger AND compression would actually save tokens.
        """
        clean = self._strip_summary(history)
        if len(clean) < self.min_messages:
            return False
        total = sum(estimate_message_tokens(m) for m in clean)
        if total < self.trigger_tokens:
            return False
        gain = self.compute_gain(clean, prev_summary or "")
        return gain >= self.min_gain_tokens

    def compute_gain(self, history: list[dict], prev_summary: str = "") -> int:
        """Tokens saved by compressing `history` (negative = compression loses).

        The summary cost is estimated from the summarizer's TARGET size
        (not its hard cap) — the model is instructed to stay near the target,
        and compress_or_truncate() enforces the budget afterward.
        """
        if len(history) < self.min_messages:
            return -1
        total = sum(estimate_message_tokens(m) for m in history)
        if total <= self.trigger_tokens:
            return -1
        boundary = self._find_boundary(history)
        kept = sum(
            estimate_message_tokens(m) for m in history[boundary:]
        )
        merged_prev = _sanitize_summary(prev_summary) + "\n" if prev_summary else ""
        target = getattr(self.summarizer, "target_chars", 320)
        # ~1.5 tokens per CJK char + fixed overhead
        summary_tokens = estimate_tokens(merged_prev) + int(target * 1.5) + 100
        return total - kept - summary_tokens

    def compress(
        self, history: list[dict], prev_summary: str | None = None
    ) -> tuple[list[dict], str, dict | None]:
        """Compress old messages of `history`; returns (messages, new_summary, info).

        - `history` is the current live message list (system prompts are NOT
          included; the caller keeps them separate).
        - Returns a new message list where the summarized portion is replaced
          by a single "[对话摘要]" system message, plus the summary text and
          a stats dict (None if compression didn't run).
        """
        stats: dict | None = None
        clean = self._strip_summary(history)
        if len(clean) < self.min_messages:
            return history, (prev_summary or ""), None

        total = sum(estimate_message_tokens(m) for m in clean)
        if total < self.trigger_tokens:
            return history, (prev_summary or ""), None

        gain = self.compute_gain(clean, prev_summary or "")
        if gain < self.min_gain_tokens:
            logger.info(
                "Compression skipped (gain %d < %d tokens)", gain, self.min_gain_tokens
            )
            return history, (prev_summary or ""), None

        boundary = self._find_boundary(clean)
        old_part, recent_part = clean[:boundary], clean[boundary:]

        try:
            new_summary = self.summarizer.summarize(old_part, prev_summary or "")
        except Exception as e:
            logger.warning("Summarization failed: %s — falling back to truncation", e)
            new_summary = (prev_summary or "") or "[对话摘要] (摘要生成失败)"

        new_summary = _sanitize_summary(new_summary)
        merged = prev_summary or ""
        summary_block = self._summary_block(merged, new_summary, boundary)

        result = [summary_block] + recent_part
        old_tokens = sum(estimate_message_tokens(m) for m in old_part)
        new_tokens = estimate_tokens(summary_block.get("content", ""))
        stats = {
            "triggered": True,
            "old_messages": len(old_part),
            "kept_messages": len(recent_part),
            "old_tokens": old_tokens,
            "new_tokens": new_tokens,
            "saved_tokens": max(old_tokens - new_tokens, 0),
            "summary_chars": len(new_summary),
            "persisted": False,
        }

        if (
            self.persist_store is not None
            and self.persist_store is not False
            and self._conv_id
        ):
            try:
                ok = self.persist_store.save_summary(
                    self._conv_id, _sanitize_summary(new_summary), len(old_part)
                )
                stats["persisted"] = bool(ok)
            except Exception as e:
                logger.warning("Summary persistence failed: %s", e)
                stats["persisted"] = False

        if self.hook:
            try:
                self.hook(stats)
            except Exception as e:  # pragma: no cover
                logger.warning("Compression hook failed: %s", e)

        logger.info(
            "Compressed %d messages (%d tokens) -> summary (%d tokens); saved %d",
            stats["old_messages"], stats["old_tokens"], stats["new_tokens"],
            stats["saved_tokens"],
        )
        return result, new_summary, stats

    def compress_or_truncate(
        self, history: list[dict], prev_summary: str | None = None
    ) -> tuple[list[dict], str, dict | None]:
        """Compress; if the result is still over budget, truncate (safety)."""
        result, summary, stats = self.compress(history, prev_summary)
        total = sum(estimate_message_tokens(m) for m in result)
        if total > self.budget and len(result) > self.keep_recent:
            kept = self._truncate_to_budget(result)
            if stats is not None:
                stats["truncated_fallback"] = True
                stats["kept_messages"] = len(kept)
            logger.warning("Post-compression still over budget; truncated to %d msgs", len(kept))
            return kept, summary, stats
        return result, summary, stats

    # ============================================================
    # Building the summary block
    # ============================================================

    @staticmethod
    def _summary_block(prev_summary: str, new_summary: str, boundary: int) -> dict:
        """Merge previous + new summaries into the injection block."""
        lines: list[str] = []
        if prev_summary:
            for line in prev_summary.splitlines():
                s = line.strip()
                if s:
                    lines.append(s if s.startswith("- ") else f"- {s}")
        if new_summary:
            for line in new_summary.splitlines():
                s = line.strip()
                if s and s not in lines:
                    lines.append(s if s.startswith("- ") else f"- {s}")
        deduped: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line not in seen:
                seen.add(line)
                deduped.append(line)
        body = "\n".join(deduped) if deduped else "- (对话未产生重要内容)"
        return {
            "role": "system",
            "content": (
                f"[对话摘要] 以下是你与此用户早期对话的摘要，"
                f"共包含 {boundary} 条早期消息的要点，"
                "新对话请直接基于摘要中的事实继续，不要当作未知信息：\n" + body
            ),
        }

    # ============================================================
    # Boundary / truncation helpers
    # ============================================================

    def _find_boundary(self, messages: list[dict]) -> int:
        """Index where old messages end and recent messages begin.

        Prefers a tool round-trip boundary (assistant+tool messages stay
        together) so we never split an assistant turn from its tool results.
        Falls back to keeping the last `keep_recent` messages.
        """
        n = len(messages)
        if n <= self.keep_recent:
            return 0
        # Walk backwards to find the start of the most recent tool round-trip
        for i in range(n - 1, -1, -1):
            role = messages[i].get("role", "")
            if role == "tool" or (
                role == "assistant" and messages[i].get("tool_calls")
            ):
                # start of this round-trip: the assistant message with tool_calls
                while i > 0 and messages[i - 1].get("role") == "assistant":
                    i -= 1
                if n - i <= self.keep_recent:
                    return i
                return i
        return max(n - self.keep_recent, 0)

    def _truncate_to_budget(self, messages: list[dict]) -> list[dict]:
        """Truncate the tail of a message list until it fits the budget.

        Native-mode tool protocol: a 'tool' result message is only valid if
        the assistant message BEFORE it carried the matching 'tool_calls'.
        Blindly popping from the front could delete that assistant message
        while its tool results survive (or vice versa), which makes the API
        reject the whole request with a 400. So we only ever drop whole
        "round-trips" (assistant-with-tool_calls + its tool results), plus
        any leading user/assistant messages that carry no tool pairing.
        """
        total = sum(estimate_message_tokens(m) for m in messages)
        while total > self.budget and len(messages) > self.keep_recent:
            removed = messages.pop(0)
            total -= estimate_message_tokens(removed)
            # Drop trailing orphaned tool results of the removed round-trip
            # (pop them all so no 'tool' message outlives its tool_calls).
            while (
                messages
                and messages[0].get("role") == "tool"
                and not self._has_pending_tool_calls(messages)
            ):
                removed = messages.pop(0)
                total -= estimate_message_tokens(removed)
        return messages

    @staticmethod
    def _has_pending_tool_calls(messages: list[dict]) -> bool:
        """Whether an assistant message with tool_calls still precedes the
        message at the front of the list (a 'tool' result at the front is
        therefore still paired)."""
        for m in reversed(messages):
            role = m.get("role", "")
            if role == "assistant" and m.get("tool_calls"):
                return True
            if role in ("user", "system"):
                break
        return False

    # ============================================================
    # Summary persistence (跨回合复用)
    # ============================================================

    def _strip_summary(self, history: list[dict]) -> list[dict]:
        """Remove any existing [对话摘要] system message from the history."""
        return [
            m for m in history
            if not (
                m.get("role") == "system"
                and str(m.get("content", "")).startswith("[对话摘要]")
            )
        ]

    # --- conversation-scoped persistence ------------------------

    @property
    def _conv_id(self):
        return getattr(self, "conversation_id", None)

    def set_conversation(self, conversation_id: str | None) -> None:
        """Scope persistence to a conversation."""
        self.conversation_id = conversation_id

    def load_summary(self, conversation_id: str | None) -> str:
        """Load the stored summary for a conversation ('' if none)."""
        if not conversation_id or self.persist_store is None or self.persist_store is False:
            return ""
        try:
            stored = self.persist_store.get_summary(conversation_id)
            return _sanitize_summary(stored or "")
        except Exception as e:
            logger.warning("Summary load failed: %s", e)
            return ""

    def save_summary(self, conversation_id: str | None, summary: str) -> bool:
        """Persist the summary for a conversation (fail-soft)."""
        if not conversation_id or self.persist_store is None or self.persist_store is False:
            return False
        try:
            return bool(
                self.persist_store.save_summary(
                    conversation_id, _sanitize_summary(summary), 0
                )
            )
        except Exception as e:
            logger.warning("Summary save failed: %s", e)
            return False

    # ------------------------------------------------------------
    # Static helpers for the orchestrator
    # ------------------------------------------------------------

    @staticmethod
    def build_summary_message(summary: str, message_count: int = 0) -> dict:
        """Build a "[对话摘要]" system message for injecting a stored summary."""
        count_line = (
            f"（含早期 {message_count} 条消息的要点）" if message_count else ""
        )
        return {
            "role": "system",
            "content": (
                f"[对话摘要] 以下是你与此用户早期对话的摘要{count_line}，"
                "新对话请直接基于摘要中的事实继续，不要当作未知信息：\n"
                + _sanitize_summary(summary)
            ),
        }
