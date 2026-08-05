"""
Context Manager - Manages the LLM context window.

Handles token budgeting, message truncation, and intelligent summarization
to keep the conversation within the model's context window limits.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("agent.memory.context")


def estimate_tokens(text: str) -> int:
    """
    Rough token estimation for Chinese/English mixed text.

    Uses a simple heuristic:
    - CJK characters: ~1.5 tokens each
    - Latin/punctuation: ~0.25 tokens per character (roughly 4 chars per token)
    This is a rough approximation; for production, use tiktoken.
    """
    if not text:
        return 0

    cjk_count = len(re.findall(r"[一-鿿㐀-䶿豈-﫿]", text))
    other_count = len(text) - cjk_count
    return int(cjk_count * 1.5 + other_count * 0.25)


def estimate_message_tokens(message: dict) -> int:
    """Estimate tokens for a single message dict."""
    content = message.get("content", "")
    # Multimodal content arrays (OpenAI format): count text parts + a fixed
    # per-image cost (base64 URL tokens are charged as image tokens by APIs).
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += estimate_tokens(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    total += 850  # ~typical per-image token cost
            else:
                total += estimate_tokens(str(part))
        return total + 4
    # Add overhead for role, formatting, etc.
    return estimate_tokens(content) + 4


def sanitize_tool_roundtrips(messages: list[dict]) -> list[dict]:
    """Enforce native function-calling protocol integrity on a message list.

    API rule: every 'tool' message must be a response to a preceding
    assistant message that declared the matching 'tool_calls', and every
    assistant tool_calls message must be followed by responses to ALL of its
    tool_call_ids before any non-tool message. Truncation/compression can
    cut between an assistant message and its tool results, producing lists
    the API rejects with a 400. This repairs such lists in place:

      - drops 'tool' messages with no preceding tool_calls (orphans)
      - drops incomplete round-trips (assistant tool_calls whose results
        were cut off, or that a later non-tool message interrupted)

    A no-op for prompt-mode histories (they contain no 'tool' role).
    """
    if not messages:
        return messages
    cleaned: list[dict] = []
    pending = 0  # outstanding tool_calls awaiting a result
    roundtrip_start: int | None = None  # index where the current round-trip begins
    for m in messages:
        role = m.get("role", "")
        if role == "assistant" and m.get("tool_calls"):
            if pending > 0:
                # Previous round-trip was never fully answered — drop it
                del cleaned[roundtrip_start:]
                logger.debug("Dropping incomplete tool round-trip (interrupted)")
            pending = len(m["tool_calls"])
            roundtrip_start = len(cleaned)
            cleaned.append(m)
        elif role == "tool":
            if pending > 0:
                pending -= 1
                cleaned.append(m)
            else:
                logger.debug("Dropping orphaned 'tool' message (no preceding tool_calls)")
        else:  # user / system / plain assistant
            if pending > 0:
                # Non-tool message interrupted an unanswered round-trip
                del cleaned[roundtrip_start:]
                pending = 0
                logger.debug("Dropping incomplete tool round-trip (unanswered calls)")
            cleaned.append(m)
    if pending > 0:
        # Trailing round-trip whose results were cut off
        del cleaned[roundtrip_start:]
        logger.debug("Dropping trailing incomplete tool round-trip")
    return cleaned


class ContextManager:
    """
    Manages the conversation context window.

    Ensures messages fit within the token budget by:
    1. Always keeping the system prompt
    2. Always keeping the most recent N messages
    3. Truncating older messages with a summary placeholder
    """

    def __init__(self, max_tokens: int = 8000, reserve_for_response: int = 1000):
        """
        Args:
            max_tokens: Total token budget for the context window.
            reserve_for_response: Tokens to reserve for the LLM's response.
        """
        self.max_tokens = max_tokens
        self.reserve = reserve_for_response

    @property
    def available_tokens(self) -> int:
        """Tokens available for messages (excluding response reserve)."""
        return self.max_tokens - self.reserve

    def fit_messages(
        self,
        system_prompt: str,
        messages: list[dict],
        min_recent: int = 4,
    ) -> list[dict]:
        """
        Fit messages within the token budget.

        Strategy:
        - System prompt always goes first
        - Most recent `min_recent` messages are always kept
        - Older messages are included until budget is exhausted
        - If over budget, older messages are replaced with a summary marker

        Args:
            system_prompt: The system prompt (always included).
            messages: List of message dicts with 'role' and 'content'.
            min_recent: Minimum recent messages to always keep.

        Returns:
            Fitted list of messages that fit within the token budget.
        """
        system_tokens = estimate_tokens(system_prompt)
        budget = self.available_tokens - system_tokens

        if budget <= 0:
            logger.warning("System prompt alone exceeds token budget")
            return [{"role": "system", "content": system_prompt}]

        result = [{"role": "system", "content": system_prompt}]

        if not messages:
            return result

        # Calculate total tokens for all messages
        total_msg_tokens = sum(estimate_message_tokens(m) for m in messages)

        if total_msg_tokens <= budget:
            # Everything fits
            result.extend(messages)
            return result

        # Need to truncate — keep the most recent messages
        # Start from the end and work backwards. Native-mode tool protocol:
        # a 'tool' result is only valid when the assistant message before it
        # carried the matching 'tool_calls'. We never cut between an assistant
        # tool_calls message and its tool results — a whole round-trip is
        # dropped or kept together, so no orphaned 'tool' messages survive.
        kept_tokens = 0
        kept_messages = []
        for msg in reversed(messages):
            msg_tokens = estimate_message_tokens(msg)
            if kept_tokens + msg_tokens > budget and len(kept_messages) >= min_recent:
                break
            kept_messages.insert(0, msg)
            kept_tokens += msg_tokens

        # Repair the truncation seam: if the first kept message is a 'tool'
        # result whose paired assistant message was cut off, drop the orphaned
        # tool messages from the kept window (their pairing is unrecoverable).
        sanitized = sanitize_tool_roundtrips(kept_messages)
        sanitized_dropped = len(kept_messages) - len(sanitized)
        kept_messages = sanitized
        kept_tokens = sum(estimate_message_tokens(m) for m in kept_messages)

        # If we dropped older messages, add a marker
        dropped_count = len(messages) - len(kept_messages) + sanitized_dropped
        if dropped_count > 0:
            total_dropped_tokens = sum(
                estimate_message_tokens(m)
                for m in messages[:dropped_count]
            )
            result.append({
                "role": "system",
                "content": (
                    f"[{dropped_count} earlier messages omitted for context length. "
                    f"Approximately {total_dropped_tokens} tokens of history were compressed.]"
                ),
            })
            logger.info(
                "Context truncated: dropped %d messages (%d tokens), kept %d messages (%d tokens)",
                dropped_count, total_dropped_tokens,
                len(kept_messages), kept_tokens,
            )

        result.extend(kept_messages)
        return result

    def get_stats(self, system_prompt: str, messages: list[dict]) -> dict:
        """Get context window usage statistics."""
        system_tokens = estimate_tokens(system_prompt)
        msg_tokens = sum(estimate_message_tokens(m) for m in messages)
        total = system_tokens + msg_tokens
        return {
            "system_tokens": system_tokens,
            "message_tokens": msg_tokens,
            "total_tokens": total,
            "budget": self.max_tokens,
            "available": self.available_tokens,
            "usage_percent": round(total / self.max_tokens * 100, 1),
            "message_count": len(messages),
        }
