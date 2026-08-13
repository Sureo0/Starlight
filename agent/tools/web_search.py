"""
Web Search Tool - Search the internet for information.

Supports multiple search backends:
  - Tavily API (primary, requires API key)
  - Bing (fallback, no API key needed)
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape

import requests

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("agent.tools.web_search")


class WebSearchTool(Tool):
    """
    Search the internet for information.

    Tries Tavily first if an API key is configured, otherwise falls back
    to Bing HTML scraping.
    """

    def __init__(self, tavily_api_key: str | None = None):
        self._tavily_key = tavily_api_key
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the internet for current information. Returns a list of "
            "search results with titles, URLs, and snippets. Use this when you "
            "need up-to-date information that isn't in your training data."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default: 5, Max: 10",
                },
            },
            "required": ["query"],
        }

    def execute(self, query: str, max_results: int = 5, **kwargs) -> ToolResult:
        """Execute a web search."""
        if not query.strip():
            return ToolResult(success=False, error="Search query cannot be empty")

        max_results = min(max(max_results, 1), 10)

        # Try Tavily first
        if self._tavily_key:
            try:
                return self._search_tavily(query, max_results)
            except Exception as e:
                logger.warning("Tavily search failed, falling back to Bing: %s", e)

        # Fallback to Bing
        try:
            return self._search_bing(query, max_results)
        except Exception as e:
            logger.exception("Bing search also failed")
            return ToolResult(
                success=False,
                error=f"Web search failed: {e}",
            )

    def _search_tavily(self, query: str, max_results: int) -> ToolResult:
        """Search using Tavily API."""
        resp = self._session.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._tavily_key,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
            },
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Tavily API error: {resp.status_code}")

        data = resp.json()
        results = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", "")[:300],
            })

        answer = data.get("answer", "")
        output = {"results": results}
        if answer:
            output["answer"] = answer

        return ToolResult(
            success=True,
            output=output,
            metadata={"source": "tavily", "count": len(results)},
        )

    def _search_bing(self, query: str, max_results: int) -> ToolResult:
        """Search using Bing HTML (no API key needed)."""
        resp = self._session.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max_results + 5},  # extra for filtering
            timeout=15,
        )

        if resp.status_code != 200:
            raise RuntimeError(f"Bing returned status {resp.status_code}")

        html = resp.text
        results = []

        # Extract titles and URLs from <h2><a> pattern
        h2_pattern = re.compile(
            r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>\s*</h2>',
            re.DOTALL,
        )

        for match in h2_pattern.finditer(html):
            if len(results) >= max_results:
                break

            url = match.group(1).strip()
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            title = unescape(title)

            # Skip Bing internal links
            if "bing.com" in url:
                continue

            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": "",
                })

        return ToolResult(
            success=True,
            output={"results": results},
            metadata={"source": "bing", "count": len(results)},
        )
