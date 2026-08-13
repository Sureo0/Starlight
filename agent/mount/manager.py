"""
MountManager - mount local folders so the agent can access them in place.

Unlike the old "upload a folder" flow (which copied every file into
data/uploads/), a MOUNT registers the folder's real path on disk. The agent
reads files directly from the mounted location via the read_file tool, and
every access to a mounted folder goes through the human-in-the-loop
approval system (the user must approve each read).

Design:
  - Persistence: data/mounts.json (list of {id, name, path, created_at})
  - Validation: the path must exist and be a directory; sensitive system
    paths are rejected (same blocked list as FileGuard).
  - The mount id is used as the attachment file_id in chat messages, and
    the file tools resolve "mount:<id>/rel/path" style paths against the
    mounted root.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("agent.mount")

# Sensitive paths that can never be mounted
BLOCKED_PATHS = (
    "/etc", "/var", "/usr", "/bin", "/sbin", "/lib", "/boot", "/dev",
    "/proc", "/sys", "/root", "/tmp",
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)",
    "C:\\Users\\Public", "C:\\Users\\Default",
)


class MountManager:
    """Registry of mounted folders (thread-safe, JSON-persisted)."""

    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "mounts.json"
        self._lock = threading.Lock()
        self._mounts: list[dict] = []
        self._load()

    # ----------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._mounts = [m for m in data if isinstance(m, dict) and m.get("path")]
                    # Backfill default policy for older entries
                    for m in self._mounts:
                        m.setdefault("policy", "always_ask")
        except Exception:
            logger.exception("Failed to load mounts.json; starting empty")
            self._mounts = []

    def _save(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._mounts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            logger.exception("Failed to save mounts.json")

    # ----------------------------------------------------------
    # CRUD
    # ----------------------------------------------------------

    @staticmethod
    def validate_path(path: str) -> str | None:
        """Validate a mount path. Returns an error message or None if OK."""
        if not path or not path.strip():
            return "路径不能为空"
        raw = path.strip().strip('"').strip("'")
        if not os.path.isabs(raw):
            return "请输入绝对路径（如 D:\\projects\\myapp 或 /home/user/data）"
        resolved = Path(raw).resolve()
        if not resolved.exists():
            return f"路径不存在: {raw}"
        if not resolved.is_dir():
            return f"不是文件夹: {raw}"
        # Block sensitive system paths
        lower = str(resolved).lower().replace("\\", "/")
        for blocked in BLOCKED_PATHS:
            if lower == blocked.lower().replace("\\", "/") or lower.startswith(
                blocked.lower().replace("\\", "/") + "/"
            ):
                return f"系统目录不允许挂载: {blocked}"
        return None

    def mount(self, path: str, name: str | None = None, policy: str = "always_ask", conv_id: str | None = None) -> tuple[dict | None, str | None]:
        """Register a folder. Returns (mount_dict, error).

        conv_id: the conversation this mount belongs to. A mount is only
        visible/usable in its own conversation — switching to another
        conversation (or a new one) requires mounting again.

        policy:
          - "always_ask": the agent asks the human before EVERY access.
          - "allow":      the first access asks; once approved within a run,
                          further accesses in that run proceed without asking.
        """
        if policy not in ("allow", "always_ask"):
            policy = "always_ask"
        err = self.validate_path(path)
        if err:
            return None, err
        resolved = str(Path(path.strip().strip('"').strip("'")).resolve())
        with self._lock:
            # Duplicate check (same resolved path IN THE SAME conversation)
            for m in self._mounts:
                if (m.get("conv_id") or "") == (conv_id or "") and \
                   Path(m["path"]).resolve() == Path(resolved):
                    # Update policy on re-mount
                    if m.get("policy") != policy:
                        m["policy"] = policy
                        self._save()
                    return m, None
            mount = {
                "id": uuid.uuid4().hex[:12],
                "name": (name or Path(resolved).name).strip() or "folder",
                "path": resolved,
                "policy": policy,
                "conv_id": conv_id or None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._mounts.append(mount)
            self._save()
            logger.info("Mounted folder: id=%s path=%s policy=%s conv=%s", mount["id"], resolved, policy, conv_id)
            return mount, None

    def set_policy(self, mount_id: str, policy: str) -> bool:
        """Update a mount's access policy. Returns False if not found."""
        if policy not in ("allow", "always_ask"):
            return False
        with self._lock:
            for m in self._mounts:
                if m.get("id") == mount_id:
                    m["policy"] = policy
                    self._save()
                    return True
            return False

    def unmount(self, mount_id: str) -> bool:
        with self._lock:
            before = len(self._mounts)
            self._mounts = [m for m in self._mounts if m.get("id") != mount_id]
            if len(self._mounts) != before:
                self._save()
                logger.info("Unmounted folder: id=%s", mount_id)
                return True
            return False

    def get(self, mount_id: str) -> dict | None:
        with self._lock:
            for m in self._mounts:
                if m.get("id") == mount_id:
                    return dict(m)
            return None

    def list(self, conv_id: str | None = None) -> list[dict]:
        """List mounts. With conv_id, only mounts belonging to that
        conversation are returned (mounts are conversation-scoped)."""
        with self._lock:
            if conv_id is None:
                return [dict(m) for m in self._mounts]
            return [dict(m) for m in self._mounts if (m.get("conv_id") or "") == conv_id]

    # ----------------------------------------------------------
    # Path resolution
    # ----------------------------------------------------------

    def resolve_mount_path(self, mount_id: str, rel_path: str) -> Path | None:
        """Resolve a 'mount:<id>/rel/path' style reference to an absolute
        path inside the mounted folder. Returns None if invalid."""
        mount = self.get(mount_id)
        if mount is None:
            return None
        root = Path(mount["path"]).resolve()
        if not rel_path:
            return root
        try:
            resolved = (root / rel_path.lstrip("/\\")).resolve()
            resolved.relative_to(root)  # traversal guard
            return resolved
        except (ValueError, OSError):
            return None

    def manifest(self, mount_id: str, max_entries: int = 200) -> str | None:
        """Build a text manifest of a mounted folder (same shape as the
        old folder-upload manifest, so the agent knows what's inside)."""
        mount = self.get(mount_id)
        if mount is None:
            return None
        root = Path(mount["path"]).resolve()
        if not root.exists() or not root.is_dir():
            return None
        entries = []
        for i, fp in enumerate(sorted(root.rglob("*"))):
            if i >= max_entries:
                entries.append("  - …（更多文件未列出）")
                break
            if fp.is_file():
                rel = fp.relative_to(root).as_posix()
                try:
                    size = fp.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"  - {rel} ({size} bytes)")
        if not entries:
            return None
        head = (
            f"[挂载文件夹] {mount['name']} (路径: {mount['path']}, "
            f"共 {len(entries)} 个文件):"
        )
        return head + "\n" + "\n".join(entries)


_default_manager: MountManager | None = None

