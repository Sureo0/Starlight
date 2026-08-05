"""
FileGuard - File system isolation and access control.

Security measures:
  - Path traversal prevention (../ attacks)
  - Symlink resolution and validation
  - Allowed directory whitelist
  - File size limits
  - File type restrictions
  - Read-only / read-write mode control
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("agent.security.file_guard")


@dataclass
class FileGuardConfig:
    """Configuration for file system access control."""

    # Root directories the agent can access (empty = workspace only)
    allowed_dirs: list[str] = field(default_factory=list)

    # Maximum file size for reads (bytes)
    max_read_size: int = 1_048_576  # 1MB

    # Maximum file size for writes (bytes)
    max_write_size: int = 1_048_576  # 1MB

    # Blocked file extensions (can't read or write)
    blocked_extensions: frozenset = frozenset({
        ".exe", ".dll", ".so", ".dylib", ".bin",
        ".sh", ".bat", ".cmd", ".ps1",
        ".sqlite", ".db", ".sqlite3",
    })

    # Sensitive paths that are always blocked
    blocked_paths: frozenset = frozenset({
        "/etc/passwd",
        "/etc/shadow",
        "/etc/sudoers",
        "/root",
        "/proc",
        "/sys",
        "C:\\Windows\\System32",
        "C:\\Windows\\SysWOW64",
    })


class FileGuard:
    """
    File system access guard.

    Validates and sanitizes all file operations before they reach the OS.
    """

    def __init__(self, workspace_dir: str | Path, config: FileGuardConfig | None = None):
        self.workspace = Path(workspace_dir).resolve()
        self.config = config or FileGuardConfig()
        # Build allowed directories list
        self._allowed_dirs: list[Path] = [self.workspace]
        for d in self.config.allowed_dirs:
            resolved = Path(d).resolve()
            if resolved.exists():
                self._allowed_dirs.append(resolved)

    def validate_path(self, path: str, operation: str = "read") -> tuple[bool, str | Path, str | None]:
        """
        Validate a file path for the given operation.

        Args:
            path: Relative or absolute path to validate
            operation: "read", "write", or "list"

        Returns:
            (is_valid, resolved_path, error_message)
            If is_valid is False, error_message explains why.
        """
        if not path or not path.strip():
            return False, Path(), "Empty path"

        # Resolve the path
        try:
            # Handle relative paths
            if not os.path.isabs(path):
                resolved = (self.workspace / path).resolve()
            else:
                resolved = Path(path).resolve()
        except (OSError, ValueError) as e:
            return False, Path(), f"Invalid path: {e}"

        # --- Check 1: Path traversal ---
        # The resolved path must be under an allowed directory
        allowed = False
        for allowed_dir in self._allowed_dirs:
            try:
                resolved.relative_to(allowed_dir)
                allowed = True
                break
            except ValueError:
                continue

        if not allowed:
            return False, resolved, (
                f"Path '{path}' is outside allowed directories. "
                f"Access denied for security."
            )

        # --- Check 2: Sensitive system paths ---
        resolved_str = str(resolved).lower().replace("\\", "/")
        for blocked in self.config.blocked_paths:
            if resolved_str.startswith(blocked.lower().replace("\\", "/")):
                return False, resolved, f"Access to '{blocked}' is blocked"

        # --- Check 3: File extension ---
        if resolved.suffix.lower() in self.config.blocked_extensions:
            return False, resolved, (
                f"File type '{resolved.suffix}' is blocked"
            )

        # --- Check 4: Symlink check ---
        if resolved.exists() and resolved.is_symlink():
            # Resolve symlinks and check the target
            real_target = resolved.resolve()
            target_allowed = False
            for allowed_dir in self._allowed_dirs:
                try:
                    real_target.relative_to(allowed_dir)
                    target_allowed = True
                    break
                except ValueError:
                    continue
            if not target_allowed:
                return False, resolved, (
                    f"Symlink '{path}' points outside allowed directories"
                )

        # --- Check 5: Size check (for existing files) ---
        if resolved.exists() and resolved.is_file():
            size = resolved.stat().st_size
            if operation == "read" and size > self.config.max_read_size:
                return False, resolved, (
                    f"File too large: {size / 1024:.0f}KB "
                    f"(max {self.config.max_read_size / 1024:.0f}KB)"
                )
            if operation == "write" and size > self.config.max_write_size:
                return False, resolved, (
                    f"File too large for write: {size / 1024:.0f}KB"
                )

        return True, resolved, None

    def validate_content_size(self, content: str | bytes, operation: str = "write") -> tuple[bool, str | None]:
        """Validate content size before writing."""
        if isinstance(content, str):
            size = len(content.encode("utf-8"))
        else:
            size = len(content)

        limit = self.config.max_write_size if operation == "write" else self.config.max_read_size
        if size > limit:
            return False, (
                f"Content too large: {size / 1024:.0f}KB "
                f"(max {limit / 1024:.0f}KB)"
            )
        return True, None

    def sanitize_filename(self, name: str) -> str:
        """
        Sanitize a filename to prevent injection attacks.
        Removes or replaces dangerous characters.
        """
        # Remove path separators
        name = name.replace("/", "").replace("\\", "")
        # Remove null bytes
        name = name.replace("\x00", "")
        # Remove leading dots (hidden files)
        name = name.lstrip(".")
        # Replace spaces with underscores
        name = name.replace(" ", "_")
        # Remove other dangerous characters
        name = "".join(c for c in name if c.isalnum() or c in "._-")
        # Limit length
        if len(name) > 255:
            name = name[:255]
        return name

    def is_within_workspace(self, path: str | Path) -> bool:
        """Check if a path is within the workspace directory."""
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(self.workspace)
            return True
        except (ValueError, OSError):
            return False

    def get_workspace(self) -> Path:
        """Return the workspace directory path."""
        return self.workspace
