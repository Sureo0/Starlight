"""
Chinese Bigram Segmenter - zero-dependency tokenizer for FTS5 indexing.

Splits mixed Chinese/English text into searchable tokens:
  - CJK runs are split into overlapping bigrams (e.g. "我喜欢" -> "我喜 喜欢 欢吃" style)
    plus the full run itself as a fallback token.
  - Latin words / numbers are kept whole.

The same tokenizer is used for both indexing (store) and query building,
so indexed terms and query terms always agree.

Usage:
    from agent.memory.segmenter import segment_text, build_fts_query

    tokens = segment_text("我喜欢吃苹果")       # -> "我喜 喜欢 欢吃 吃苹 苹果 我喜欢吃苹果"
    fts_query = build_fts_query("喜欢苹果")     # -> '"喜欢" OR "苹果"'
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger("agent.memory.segmenter")

# CJK Unified Ideographs (basic + extension A)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
# A run of consecutive CJK characters
_CJK_RUN_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]+")
# Latin words / numbers / common separators like email-ish tokens
_LATIN_RE = re.compile(r"[A-Za-z0-9]+")


def is_cjk_char(ch: str) -> bool:
    """Return True if the character is a CJK ideograph."""
    return bool(_CJK_RE.match(ch))


def segment_run(cjk_run: str) -> list[str]:
    """
    Segment a single CJK run into bigram tokens plus the full run.

    Args:
        cjk_run: A consecutive string of CJK characters, e.g. "我喜欢".

    Returns:
        List of tokens, e.g. ["我喜", "喜欢", "我喜欢"].
    """
    if not cjk_run:
        return []
    if len(cjk_run) == 1:
        return [cjk_run]
    tokens = [cjk_run[i : i + 2] for i in range(len(cjk_run) - 1)]
    # Keep the full run as a fallback so exact-phrase queries still match.
    tokens.append(cjk_run)
    return tokens


def segment_text(text: str) -> str:
    """
    Tokenize mixed text into a space-joined token string for FTS5 indexing.

    CJK runs become bigrams; Latin words/numbers stay whole. Non-token
    characters (punctuation, whitespace, emoji) are dropped.

    Examples:
        "我喜欢吃苹果"  -> "我喜 喜欢 欢吃 吃苹 苹果 我喜欢吃苹果"
        "Python 3.10"  -> "Python 3 10"
        "I live in 上海" -> "I live in 在上 上海" (上海 -> "在上 上海 上海")
    """
    if not text:
        return ""

    tokens: list[str] = []
    for seg in _CJK_RUN_RE.split(text):
        if seg:
            # Non-CJK segment: keep Latin words / numbers
            tokens.extend(_LATIN_RE.findall(seg))
        else:
            pass  # split delimiter itself is a CJK run (handled below)

    # Re-scan for CJK runs (split() above consumed the runs)
    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(segment_run(run))

    return " ".join(tokens)


def build_fts_query(text: str) -> str:
    """
    Build an FTS5 MATCH query string from free text.

    The query is a set of OR-ed quoted bigram terms, so any overlapping
    bigram match will hit. This keeps recall high for short queries like
    "上海" while still being precise enough for longer phrases.

    Args:
        text: User/agent free-text search query.

    Returns:
        FTS5 query string, e.g. '"喜欢" OR "苹果"'. Empty string if nothing
        to search on.
    """
    if not text:
        return ""

    terms: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        for t in segment_run(run):
            if t not in terms:
                terms.append(t)
    for word in _LATIN_RE.findall(text):
        if word not in terms:
            terms.append(word)

    if not terms:
        return ""

    quoted = '"' + '" OR "'.join(terms) + '"'
    return quoted
