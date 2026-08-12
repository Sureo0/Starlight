"""
File Tools - Read and write files with security isolation.

All operations go through FileGuard which enforces:
  - Path traversal prevention
  - Symlink validation
  - File size limits
  - Blocked file types

Mounted folders: paths of the form "mount:<id>/rel/path" refer to files
inside a mounted folder (registered via the MountManager). Access to
mounted folders requires human approval — the tool calls an optional
approval callback (wired by the app to the approval manager) before
reading; if the callback is absent, access is refused.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.tools.base import Tool, ToolResult
from agent.security.file_guard import FileGuard, FileGuardConfig

logger = logging.getLogger("agent.tools.file")

MOUNT_PREFIX = "mount:"


def _split_mount_ref(path: str) -> tuple[str | None, str]:
    """Split 'mount:<id>/rel/path' into (mount_id, rel_path).

    Returns (None, path) when the path is not a mount reference.
    """
    if path.startswith(MOUNT_PREFIX):
        rest = path[len(MOUNT_PREFIX):]
        # mount:<id> or mount:<id>/rel/path
        if "/" in rest:
            mid, rel = rest.split("/", 1)
        elif "\\" in rest:
            mid, rel = rest.split("\\", 1)
        else:
            mid, rel = rest, ""
        return (mid, rel) if mid else (None, path)
    return None, path


def _resolve_mount_ref(mount_manager, mount_id: str, rel_path: str) -> Path | None:
    """Resolve a mount reference to an absolute path (None if invalid)."""
    if mount_manager is None:
        return None
    try:
        return mount_manager.resolve_mount_path(mount_id, rel_path)
    except Exception:
        return None


def _approval_result(verdict) -> ToolResult | None:
    """Convert an approval-callback verdict into a ToolResult.

    The callback may return None (approved) or a dict shaped like a
    ToolResult (blocked). Returns None for approval, a ToolResult for denial.
    """
    if verdict is None:
        return None
    if isinstance(verdict, ToolResult):
        return verdict
    return ToolResult(
        success=bool(verdict.get("success", False)),
        error=verdict.get("error"),
        metadata=verdict.get("metadata") or {},
    )


class ReadFileTool(Tool):
    """Read the contents of a file within the workspace."""

    def __init__(self, workspace_dir: str | Path, guard_config: FileGuardConfig | None = None,
                 mount_manager=None, approval_cb=None):
        self._guard = FileGuard(workspace_dir, config=guard_config)
        self._mount_manager = mount_manager
        self._approval_cb = approval_cb

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. The file must be within the workspace "
            "or inside a mounted folder (use 'mount:<id>/rel/path' for mounted files). "
            "Returns the file content as a string. For binary files, returns metadata only."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file (relative to workspace root), "
                                   "or 'mount:<id>/rel/path' for files inside a mounted folder.",
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding. Default: 'utf-8'",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Max lines to read. Default: 500. Set 0 for no limit.",
                },
            },
            "required": ["path"],
        }

    def execute(self, path: str, encoding: str = "utf-8", max_lines: int = 500, **kwargs) -> ToolResult:
        """Read a file with security validation."""
        try:
            # Mount reference: resolve against the mounted folder and ask for
            # approval before reading.
            mount_id, rel = _split_mount_ref(path)
            if mount_id is not None:
                resolved = _resolve_mount_ref(self._mount_manager, mount_id, rel)
                if resolved is None:
                    return ToolResult(success=False, error=f"无效的挂载路径: {path}")
                if self._approval_cb is not None:
                    verdict = self._approval_cb("read_file", {
                        "mount_id": mount_id,
                        "path": str(resolved),
                        "operation": "read",
                        "run_id": getattr(self, "_current_run_id", None),
                        "conv_id": getattr(self, "_current_conv_id", None),
                    })
                    blocked = _approval_result(verdict)
                    if blocked is not None:
                        return blocked
                # No approval callback wired: refuse mounted access by default.
                if self._approval_cb is None:
                    return ToolResult(
                        success=False,
                        error="访问挂载文件夹需要人工确认，但当前未配置审批系统，已阻止读取。",
                        metadata={"approval": "blocked_no_manager"},
                    )
            else:
                # Validate path through FileGuard
                is_valid, resolved, error = self._guard.validate_path(path, operation="read")
                if not is_valid:
                    return ToolResult(success=False, error=error)

            if not resolved.exists():
                return ToolResult(success=False, error=f"File not found: {path}")

            if not resolved.is_file():
                return ToolResult(success=False, error=f"Not a file: {path}")

            # Try to read as text
            try:
                content = resolved.read_text(encoding=encoding)
            except UnicodeDecodeError:
                return ToolResult(
                    success=True,
                    output={
                        "path": str(resolved if mount_id is not None
                                     else resolved.relative_to(self._guard.get_workspace())),
                        "type": "binary",
                        "size_bytes": resolved.stat().st_size,
                    },
                )

            # Apply line limit
            lines = content.split("\n")
            total_lines = len(lines)
            if max_lines > 0 and total_lines > max_lines:
                content = "\n".join(lines[:max_lines])
                truncated = True
            else:
                truncated = False

            output = {
                "path": str(resolved if mount_id is not None
                             else resolved.relative_to(self._guard.get_workspace())),
                "content": content,
                "total_lines": total_lines,
                "size_bytes": resolved.stat().st_size,
            }
            if truncated:
                output["truncated"] = True
                output["shown_lines"] = max_lines

            return ToolResult(success=True, output=output)

        except Exception as e:
            logger.exception("Read file failed")
            return ToolResult(success=False, error=str(e))


class ReadFilesTool(Tool):
    """Batch-read multiple files in a single tool call.

    Exists so file-heavy tasks (project surveys, README generation) don't
    need one LLM round-trip per file. Limits: max 15 files per call, 400
    lines each by default — keep payloads inside the context window.
    """

    def __init__(self, workspace_dir: str | Path, guard_config: FileGuardConfig | None = None,
                 mount_manager=None, approval_cb=None):
        self._guard = FileGuard(workspace_dir, config=guard_config)
        self._single = ReadFileTool(workspace_dir, guard_config=guard_config,
                                    mount_manager=mount_manager, approval_cb=approval_cb)

    @property
    def name(self) -> str:
        return "read_files"

    @property
    def description(self) -> str:
        return (
            "批量读取多个文件（推荐用于了解项目结构时）。一次最多读取 15 个文件，"
            "每个文件最多 400 行。返回每个文件的内容。当需要读取多个文件时，"
            "用这个工具替代多次 read_file，可以大幅节省时间。"
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要读取的文件路径列表（相对工作区根目录）。最多 15 个。",
                },
            },
            "required": ["paths"],
        }

    def execute(self, paths=None, **kwargs) -> ToolResult:
        if not paths or not isinstance(paths, list):
            return ToolResult(success=False, error="paths 必须是文件路径数组")
        if len(paths) > 15:
            return ToolResult(
                success=False,
                error=f"一次最多读取 15 个文件，收到 {len(paths)} 个。请分批读取。",
            )

        results = []
        for p in paths[:15]:
            result = self._single.execute(path=p, max_lines=400)
            if result.success:
                out = result.output
                results.append({
                    "path": out.get("path", p),
                    "content": out.get("content", ""),
                    "total_lines": out.get("total_lines", 0),
                    "type": out.get("type", "text"),
                })
            else:
                results.append({"path": p, "error": result.error})

        return ToolResult(
            success=True,
            output={"files": results, "read_count": len(results)},
        )


class WriteFileTool(Tool):
    """Write content to a file within the workspace with security validation."""

    # Writing is not idempotent — never auto-retry.
    retryable: bool = False

    def __init__(self, workspace_dir: str | Path, guard_config: FileGuardConfig | None = None,
                 mount_manager=None, approval_cb=None):
        self._guard = FileGuard(workspace_dir, config=guard_config)
        self._mount_manager = mount_manager
        self._approval_cb = approval_cb

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file within the workspace. "
            "Creates the file if it doesn't exist, overwrites if it does. "
            "Creates parent directories automatically."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path for the file (relative to workspace root).",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file.",
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding. Default: 'utf-8'",
                },
            },
            "required": ["path", "content"],
        }

    def execute(self, path: str, content: str, encoding: str = "utf-8", **kwargs) -> ToolResult:
        """Write content to a file with security validation."""
        try:
            # Mount reference: writing into a mounted folder also requires approval.
            mount_id, rel = _split_mount_ref(path)
            if mount_id is not None:
                resolved = _resolve_mount_ref(self._mount_manager, mount_id, rel)
                if resolved is None:
                    return ToolResult(success=False, error=f"无效的挂载路径: {path}")
                if self._approval_cb is not None:
                    verdict = self._approval_cb("write_file", {
                        "mount_id": mount_id,
                        "path": str(resolved),
                        "operation": "write",
                        "run_id": getattr(self, "_current_run_id", None),
                        "conv_id": getattr(self, "_current_conv_id", None),
                    })
                    blocked = _approval_result(verdict)
                    if blocked is not None:
                        return blocked
                if self._approval_cb is None:
                    return ToolResult(
                        success=False,
                        error="写入挂载文件夹需要人工确认，但当前未配置审批系统，已阻止写入。",
                        metadata={"approval": "blocked_no_manager"},
                    )
            else:
                # Validate path
                is_valid, resolved, error = self._guard.validate_path(path, operation="write")
                if not is_valid:
                    return ToolResult(success=False, error=error)

            # Validate content size
            is_valid, error = self._guard.validate_content_size(content, operation="write")
            if not is_valid:
                return ToolResult(success=False, error=error)

            # Create parent directories
            resolved.parent.mkdir(parents=True, exist_ok=True)

            # Write file
            resolved.write_text(content, encoding=encoding)

            return ToolResult(
                success=True,
                output={
                    "path": str(resolved if mount_id is not None
                                 else resolved.relative_to(self._guard.get_workspace())),
                    "size_bytes": resolved.stat().st_size,
                    "lines": len(content.split("\n")),
                },
            )

        except Exception as e:
            logger.exception("Write file failed")
            return ToolResult(success=False, error=str(e))


class ListFilesTool(Tool):
    """List files and directories in the workspace with security validation."""

    def __init__(self, workspace_dir: str | Path, guard_config: FileGuardConfig | None = None,
                 mount_manager=None, approval_cb=None):
        self._guard = FileGuard(workspace_dir, config=guard_config)
        self._mount_manager = mount_manager
        self._approval_cb = approval_cb

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "List files and directories at a given path within the workspace, "
            "or inside a mounted folder ('mount:<id>/' lists the mount root). "
            "Returns names, types (file/directory), and sizes."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory path. Use '.' for workspace root.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter results (e.g. '*.py', '**/*.txt').",
                },
            },
            "required": [],
        }

    def execute(self, path: str = ".", pattern: str = "", **kwargs) -> ToolResult:
        """List files in directory with security validation."""
        try:
            # Mount reference: list inside a mounted folder (approval needed).
            mount_id, rel = _split_mount_ref(path)
            if mount_id is not None:
                resolved = _resolve_mount_ref(self._mount_manager, mount_id, rel)
                if resolved is None:
                    return ToolResult(success=False, error=f"无效的挂载路径: {path}")
                if self._approval_cb is not None:
                    verdict = self._approval_cb("list_files", {
                        "mount_id": mount_id,
                        "path": str(resolved),
                        "operation": "list",
                        "run_id": getattr(self, "_current_run_id", None),
                        "conv_id": getattr(self, "_current_conv_id", None),
                    })
                    blocked = _approval_result(verdict)
                    if blocked is not None:
                        return blocked
                if self._approval_cb is None:
                    return ToolResult(
                        success=False,
                        error="访问挂载文件夹需要人工确认，但当前未配置审批系统，已阻止访问。",
                        metadata={"approval": "blocked_no_manager"},
                    )
            else:
                # Validate path
                is_valid, resolved, error = self._guard.validate_path(path, operation="list")
                if not is_valid:
                    return ToolResult(success=False, error=error)

            if not resolved.exists():
                return ToolResult(success=False, error=f"Directory not found: {path}")

            if not resolved.is_dir():
                return ToolResult(success=False, error=f"Not a directory: {path}")

            if pattern:
                items = list(resolved.glob(pattern))
            else:
                items = list(resolved.iterdir())

            # Sort: directories first, then by name
            items.sort(key=lambda p: (not p.is_dir(), p.name.lower()))

            result = []
            for item in items[:100]:
                try:
                    # For workspace paths, verify each item stays inside the
                    # allowed dirs; mounted paths are pre-validated.
                    if mount_id is None:
                        is_item_valid, _, _ = self._guard.validate_path(
                            str(item.relative_to(self._guard.get_workspace())),
                            operation="list",
                        )
                        if not is_item_valid:
                            continue

                    entry = {
                        "name": item.name,
                        "path": str(item.relative_to(self._guard.get_workspace())) if mount_id is None
                                else f"{MOUNT_PREFIX}{mount_id}/{item.relative_to(resolved).as_posix()}",
                        "type": "directory" if item.is_dir() else "file",
                    }
                    if item.is_file():
                        entry["size_bytes"] = item.stat().st_size
                    result.append(entry)
                except OSError:
                    continue

            return ToolResult(
                success=True,
                output=result,
                metadata={"count": len(result), "path": path},
            )

        except Exception as e:
            logger.exception("List files failed")
            return ToolResult(success=False, error=str(e))
