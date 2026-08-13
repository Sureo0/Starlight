"""
InputValidator - Input sanitization and validation.

Provides validation for:
  - Tool arguments (per-tool schema validation)
  - User messages (length, content)
  - File paths (injection prevention)
  - Code content (dangerous pattern detection)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("agent.security.validator")


@dataclass
class ValidatorConfig:
    """Validation configuration."""

    max_message_length: int = 10_000
    max_tool_args_size: int = 50_000  # Max JSON size for tool arguments
    max_code_length: int = 50_000
    blocked_patterns: list[str] = None  # Regex patterns to block

    def __post_init__(self):
        if self.blocked_patterns is None:
            self.blocked_patterns = [
                # Prompt injection patterns
                r"ignore\s+(all\s+)?previous\s+instructions",
                r"disregard\s+(all\s+)?prior",
                r"you\s+are\s+now\s+",
                r"system\s*:\s*you\s+are",
                # Dangerous code patterns
                r"os\.system\s*\(",
                r"os\.popen\s*\(",
                r"subprocess\.(call|run|Popen)\s*\(",
                r"__import__\s*\(",
                r"eval\s*\(",
                r"exec\s*\(",
            ]


class InputValidator:
    """
    Validates and sanitizes all inputs to the agent system.
    """

    def __init__(self, config: ValidatorConfig | None = None):
        self.config = config or ValidatorConfig()
        self._compiled_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.config.blocked_patterns
        ]

    def validate_message(self, message: str) -> tuple[bool, str]:
        """
        Validate a user message.

        Returns:
            (is_valid, error_message)
        """
        if not message or not message.strip():
            return False, "Empty message"

        if len(message) > self.config.max_message_length:
            return False, (
                f"Message too long: {len(message)} chars "
                f"(max {self.config.max_message_length})"
            )

        # Check for prompt injection
        for pattern in self._compiled_patterns:
            if pattern.search(message):
                logger.warning("Blocked prompt injection attempt: %s", message[:100])
                return False, "Message contains blocked content"

        return True, "OK"

    def validate_tool_args(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """
        Validate tool arguments.

        Returns:
            (is_valid, error_message)
        """
        # Check size
        try:
            args_json = json.dumps(args)
        except (TypeError, ValueError) as e:
            return False, f"Invalid arguments format: {e}"

        if len(args_json) > self.config.max_tool_args_size:
            return False, (
                f"Tool arguments too large: {len(args_json)} chars "
                f"(max {self.config.max_tool_args_size})"
            )

        # Per-tool validation
        if tool_name == "execute_code":
            return self._validate_code_args(args)
        elif tool_name in ("read_file", "write_file", "list_files"):
            return self._validate_file_args(args)
        elif tool_name == "web_search":
            return self._validate_search_args(args)
        elif tool_name == "chat_completion":
            return self._validate_chat_args(args)

        return True, "OK"

    def _validate_code_args(self, args: dict) -> tuple[bool, str]:
        """Validate code execution arguments."""
        code = args.get("code", "")
        if not code:
            return False, "Empty code"

        if len(code) > self.config.max_code_length:
            return False, (
                f"Code too long: {len(code)} chars "
                f"(max {self.config.max_code_length})"
            )

        # Check for dangerous patterns in code
        for pattern in self._compiled_patterns:
            if pattern.search(code):
                return False, f"Code contains blocked pattern: {pattern.pattern}"

        return True, "OK"

    def _validate_file_args(self, args: dict) -> tuple[bool, str]:
        """Validate file operation arguments."""
        path = args.get("path", "")
        if not path:
            # Surface what WAS provided so the LLM can correct itself.
            # write_file expects {"path": "相对路径", "content": "内容"};
            # read_files is the batch variant with {"paths": [...]}.
            keys = ", ".join(sorted(k for k in args.keys() if k != "path")) or "(无其他参数)"
            return False, (
                f"Empty file path。收到参数: {keys}。"
                f"write_file 需要格式 {{\"path\": \"相对路径\", \"content\": \"内容\"}}，"
                f"path 必填。不要使用 paths 或 files 字段。"
            )

        # Check for null bytes
        if "\x00" in path:
            return False, "Path contains null bytes"

        # Check for extremely long paths
        if len(path) > 1000:
            return False, f"Path too long: {len(path)} chars"

        # Check content size for write operations
        content = args.get("content")
        if content is not None:
            if len(content) > 1_048_576:  # 1MB
                return False, f"Content too large: {len(content)} chars (max 1MB)"

        return True, "OK"

    def _validate_search_args(self, args: dict) -> tuple[bool, str]:
        """Validate web search arguments."""
        query = args.get("query", "")
        if not query:
            return False, "Empty search query"

        if len(query) > 500:
            return False, f"Search query too long: {len(query)} chars"

        # Check for prompt injection in search query
        for pattern in self._compiled_patterns:
            if pattern.search(query):
                return False, "Search query contains blocked content"

        return True, "OK"

    def _validate_chat_args(self, args: dict) -> tuple[bool, str]:
        """Validate chat completion arguments."""
        messages = args.get("messages", [])
        if not messages:
            return False, "Empty messages list"

        if len(messages) > 50:
            return False, f"Too many messages: {len(messages)} (max 50)"

        for i, msg in enumerate(messages):
            if "role" not in msg or "content" not in msg:
                return False, f"Message {i} missing 'role' or 'content'"
            if msg["role"] not in ("system", "user", "assistant"):
                return False, f"Message {i} has invalid role: {msg['role']}"

        return True, "OK"

