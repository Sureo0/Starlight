"""
Context compression — LLM-based conversation summarization.

Keeps long conversations inside the context window WITHOUT losing history:
older messages are distilled into a structured summary (incrementally merged
with any previous summary), the most recent messages are always kept intact,
and the result is persisted per-conversation so future turns can reuse it.
"""

from agent.compression.manager import CompressionManager
from agent.compression.summarizer import ContextSummarizer

__all__ = ["CompressionManager", "ContextSummarizer"]
