"""
ContextSummarizer — LLM-based conversation summarization.

Summarizes a chunk of conversation into a structured, incremental-friendly
summary (中文). The summary format mirrors the existing 长期记忆 injection
block so the model reads it the same way: a short intro line followed by
"- " bullet claims.

Incremental design:
  - If a previous summary exists, it is passed back to the LLM so the new
    summary MERGES old facts with the new chunk instead of replacing them.
  - Guardrails: length caps (target 320 / hard 900 chars), "忽略无关内容"
    instruction, and a 全文-recall fallback when the model emits a summary
    that is too short to be useful.

Fail-soft: any LLM error bubbles up to CompressionManager which falls back
to plain truncation (the previous behavior).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("agent.compression.summarizer")


class ContextSummarizer:
    """Summarize conversation chunks with an LLM."""

    SUMMARY_PREFIX = "[对话摘要]"

    def __init__(
        self,
        llm,
        target_chars: int = 320,
        max_chars: int = 900,
        max_input_chars: int = 16000,
    ):
        self.llm = llm
        self.target_chars = target_chars
        self.max_chars = max_chars
        self.max_input_chars = max_input_chars

    # ============================================================
    # Public API
    # ============================================================

    def summarize(
        self, chunk_messages: list[dict], previous_summary: str | None = None
    ) -> str:
        """Summarize a chunk of conversation.

        Args:
            chunk_messages: Messages to summarize (list of {role, content}).
            previous_summary: Prior summary text (without prefix) to merge with,
                or None for a first summary.

        Returns:
            Summary text without the "[对话摘要]" prefix.
        """
        return self._summarize(chunk_messages, previous_summary or "")

    # ============================================================
    # Implementation
    # ============================================================

    def _summarize(self, messages: list[dict], prev: str) -> str:
        text = self._format_chunk(messages)
        prompt = self._build_prompt(text, prev)
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            temperature=0.3,
            max_tokens=1500,
        )
        content = (getattr(response, "content", "") or "").strip()
        summary = self._clean(content)
        if self._too_short(summary):
            logger.warning(
                "Summary too short (%d chars), falling back to text recall", len(summary)
            )
            # Keep whatever the model produced if it has any structure;
            # otherwise fall back to verbatim recall of the chunk start.
            summary = summary if summary else self._truncate_for_recall(text)
        return summary

    # ------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------

    def _build_prompt(self, text: str, prev: str) -> str:
        parts = [
            "请把下面的对话压缩成一段简洁的中文摘要，用于长期上下文。",
            "",
            "## 要求",
            "1. 只保留对后续对话仍然重要的事实：用户的意图、目标、需求、偏好、",
            "   决策、关键数据/结论、当前进行中的任务及其进度、文件路径、",
            "   已完成和未完成的事项。",
            "2. 忽略寒暄、重复、过时的中间推理和与任务无关的内容。",
            "3. 用要点列表（每行以 '- ' 开头）输出，不要标题、不要编号。",
            f"4. 摘要控制在 {self.target_chars} 字左右，最多不超过 {self.max_chars} 字。",
        ]
        if prev:
            parts += [
                "",
                "## 已有摘要（重要）",
                "以下是之前轮次的摘要。请把它作为基础，将下面的新对话内容**合并**",
                "进摘要：保留仍然成立的旧要点，加入新要点，删除已被新对话推翻的旧要点，",
                "不要重复描述同一件事，不要丢失重要信息。",
                "",
                prev[:8000],
            ]
        parts += [
            "",
            "## 新对话内容",
            "",
            text,
            "",
            "## 输出",
            "直接输出合并后的完整摘要（不要输出摘要二字，不要输出 markdown 代码块）：",
        ]
        return "\n".join(parts)

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    @staticmethod
    def _format_chunk(messages: list[dict]) -> str:
        """Render messages as readable 用户:/助手:/工具: lines."""
        lines: list[str] = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if not isinstance(content, str):
                try:
                    import json

                    content = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content = str(content)
            if role == "assistant":
                label = "助手"
            elif role == "user":
                label = "用户"
            else:
                label = "工具结果"
            lines.append(f"{label}: {content}")
        return "\n".join(lines)

    def _clean(self, text: str) -> str:
        """Strip prefix / code fences / leading label noise from LLM output."""
        text = re.sub(r"^```(?:markdown|text)?\s*", "", text).strip()
        text = re.sub(r"\s*```$", "", text).strip()
        if text.startswith(self.SUMMARY_PREFIX):
            text = text[len(self.SUMMARY_PREFIX):].strip()
        # Strip a leading label line ("摘要：", "对话摘要：", "以下是摘要：")
        text = re.sub(r"^(摘要|对话摘要|以下是摘要)[:：]\s*", "", text)
        # Ensure bullet lines look like bullets even if the model wrote 中文标点
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("•", "·", "*", "1.", "2.")):
                stripped = "- " + stripped.lstrip("•·* ").strip()
            lines.append(stripped)
        text = "\n".join(lines).strip()
        # Hard length cap (safety net)
        if len(text) > self.max_chars:
            text = text[: self.max_chars].rstrip() + "…"
        return text

    def _too_short(self, summary: str) -> bool:
        """A summary shorter than the target is probably a stub (guardrail)."""
        if not summary:
            return True
        if len(summary) < self.target_chars * 0.5:
            return True
        bullet_count = len([1 for line in summary.splitlines() if line.strip().startswith("-")])
        return bullet_count < 1

    def _truncate_for_recall(self, text: str) -> str:
        """Fallback: keep the beginning of the chunk verbatim (recall > loss)."""
        return text[: self.max_chars]
