"""
CodeSandbox - Sandboxed Python code execution.

Security measures:
  - Restricted imports (block dangerous modules)
  - Resource limits (CPU time, output size)
  - Working directory isolation
  - Network access blocking
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass

logger = logging.getLogger("agent.security.sandbox")


@dataclass
class SandboxConfig:
    """Configuration for the code execution sandbox."""

    timeout: int = 30
    max_output_bytes: int = 100_000
    network_allowed: bool = False

    BLOCKED_MODULES: frozenset = frozenset({
        # Full process/network control — never safe.
        "subprocess", "shutil", "signal", "ctypes",
        "socket", "http", "urllib", "requests", "aiohttp", "httpx",
        "pickle", "marshal", "shelve", "codeop", "compileall",
        "importlib", "pkgutil",
        "multiprocessing", "threading",
        "pdb", "profile", "cProfile", "trace", "traceback",
        "cffi",
        # os is allowed for read-only use (os.path, os.listdir, os.environ);
        # destructive operations are blocked at RUNTIME in the wrapper
        # (os.remove/unlink/rmdir, os.system, os.chmod, os.rename, ...).
    })

    ALWAYS_ALLOWED: frozenset = frozenset({
        "math", "random", "json", "re", "datetime", "time",
        "collections", "itertools", "functools", "operator",
        "string", "textwrap", "difflib", "unicodedata",
        "decimal", "fractions", "statistics",
        "copy", "pprint", "io", "csv", "configparser",
        "hashlib", "hmac", "base64",
        "enum", "dataclasses", "typing", "abc",
    })


class CodeSandbox:
    """
    Sandboxed Python code execution environment.

    Runs code in an isolated subprocess with:
    - Restricted imports via wrapper script
    - Resource limits
    - Output capture and truncation
    """

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()

    def execute(self, code: str, description: str = "", work_dir: str | None = None) -> dict:
        """Execute Python code in a sandboxed subprocess.

        work_dir: working directory for the subprocess (defaults to the
        system temp dir). The agent passes its workspace so scripts can read
        files the agent wrote there.
        """
        if not code.strip():
            return {
                "stdout": "",
                "stderr": "Error: Empty code",
                "returncode": 1,
                "security_warnings": [],
            }

        warnings = self._prescan_code(code)
        wrapper_path, code_file_path = self._create_wrapper(code, work_dir=work_dir)
        cwd = work_dir or tempfile.gettempdir()
        try:
            os.makedirs(cwd, exist_ok=True)
        except OSError:
            cwd = tempfile.gettempdir()

        try:
            # text=True with explicit UTF-8: on Windows the default encoding is
            # GBK, and any non-GBK byte in the child's output (e.g. UTF-8
            # Chinese) crashed the reader thread with UnicodeDecodeError.
            # errors="replace" guarantees we always get *something* back.
            result = subprocess.run(
                [sys.executable, wrapper_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout,
                cwd=cwd,
                env=self._build_safe_env(),
            )

            stdout = (result.stdout or "")[:self.config.max_output_bytes]
            stderr = (result.stderr or "")[:self.config.max_output_bytes]

            if len(result.stdout or "") > self.config.max_output_bytes:
                stdout += f"\n... [truncated at {self.config.max_output_bytes} bytes]"
            if len(result.stderr or "") > self.config.max_output_bytes:
                stderr += f"\n... [truncated at {self.config.max_output_bytes} bytes]"

            return {
                "stdout": stdout,
                "stderr": stderr,
                "returncode": result.returncode,
                "security_warnings": warnings,
            }

        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {self.config.timeout} seconds",
                "returncode": -1,
                "security_warnings": warnings,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"Sandbox error: {e}",
                "returncode": -1,
                "security_warnings": warnings,
            }
        finally:
            # Clean up BOTH temp files (wrapper + user code) so the sandbox
            # never leaks files into the system temp dir.
            for path in (wrapper_path, code_file_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _prescan_code(self, code: str) -> list[str]:
        """Pre-scan code for potential security issues."""
        warnings = []

        import_pattern = re.compile(r'(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_.]*)')
        for match in import_pattern.finditer(code):
            module = match.group(1).split(".")[0]
            if module in self.config.BLOCKED_MODULES and module not in self.config.ALWAYS_ALLOWED:
                warnings.append(f"Blocked import: '{module}'")

        if re.search(r'\beval\s*\(', code):
            warnings.append("Detected 'eval()' call")
        if re.search(r'\bexec\s*\(', code):
            warnings.append("Detected 'exec()' call")
        if re.search(r'\.\./', code):
            warnings.append("Detected path traversal pattern")

        return warnings

    def _create_wrapper(self, code: str, work_dir: str | None = None) -> tuple[str, str]:
        """
        Create a wrapper script that restricts imports.
        Writes user code directly to a temp file (no exec/eval).
        Returns (wrapper_path, code_file_path).
        """
        blocked_modules = sorted(
            m for m in self.config.BLOCKED_MODULES
            if m not in self.config.ALWAYS_ALLOWED
        )

        blocked_list = repr(blocked_modules)

        # Write user code to a separate temp file
        code_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        code_file.write(code)
        code_file.close()

        # The allowed deletion root: files INSIDE the agent workspace can be
        # removed by the model (its own scratch files); everything else is
        # protected. Quoted via repr so path separators survive.
        _work_dir_repr = repr(work_dir or tempfile.gettempdir())

        # Create the sandbox wrapper
        wrapper = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        # Plain template (NOT an f-string — the generated code contains its
        # own braces/f-strings). Placeholders are replaced below.
        wrapper_template = '''#!/usr/bin/env python3
"""Sandbox wrapper — auto-generated."""
import sys
import builtins

# Blocked modules
_BLOCKED = __BLOCKED_LIST__

# Save original import
_ORIGINAL_IMPORT = builtins.__import__

def _restricted_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root in _BLOCKED:
        raise ImportError(
            f"Import of '{root}' is blocked in sandbox mode. "
            f"Hint: use only safe built-ins (open, len, str, ...) and allowed "
            f"modules (math, json, re, datetime, csv, os.path, ...)."
        )
    return _ORIGINAL_IMPORT(name, *args, **kwargs)

builtins.__import__ = _restricted_import

# Runtime guard: destructive filesystem operations are restricted to the
# agent's own workspace (its scratch files). Deleting/renaming anything
# OUTSIDE the workspace is blocked; read-only os usage is always fine.
import os as _os
_WORK_DIR = __WORK_DIR__
def _inside_workdir(path):
    try:
        ap = _os.path.abspath(_os.fspath(path))
        return _os.path.commonpath([ap, _os.path.abspath(_WORK_DIR)]) == _os.path.abspath(_WORK_DIR)
    except Exception:
        return False

_ORIGINALS = {_n: getattr(_os, _n) for _n in
              ("remove", "unlink", "rmdir", "removedirs", "system",
               "chmod", "chown", "rename", "replace") if hasattr(_os, _n)}
for _name, _orig in _ORIGINALS.items():
    def _blocked(*a, _n=_name, _fn=_orig, **k):
        target = a[0] if a else k.get("src") or k.get("path")
        if target is not None and _inside_workdir(target):
            return _fn(*a, **k)  # allowed: model cleans up its own workspace
        raise PermissionError(
            f"os.{_n}() outside the sandbox workspace is blocked. "
            f"You may only modify files inside your workspace; use the "
            f"built-in open() for reading/writing elsewhere."
        )
    setattr(_os, _name, _blocked)

# Execute user code from file
_code_file = r"__CODE_FILE__"
with open(_code_file, "r", encoding="utf-8") as f:
    _code = f.read()

exec(compile(_code, _code_file, "exec"))
'''
        wrapper.write(
            wrapper_template
            .replace("__BLOCKED_LIST__", blocked_list)
            .replace("__WORK_DIR__", _work_dir_repr)
            .replace("__CODE_FILE__", code_file.name)
        )
        wrapper.close()
        # Return both paths so execute() can clean up BOTH temp files.
        return wrapper.name, code_file.name

    def _build_safe_env(self) -> dict:
        """Build a minimal safe environment for subprocess.

        Platform-aware: the hardcoded POSIX PATH broke Windows (Python could
        not initialize its hash randomization -> fatal error on every run).
        We keep PATH/HOME from the parent on Windows; on POSIX we use a
        minimal PATH but keep HOME so the interpreter works.
        """
        if os.name == "nt":  # Windows: inherit PATH/HOME, override only Python knobs
            return {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("USERPROFILE") or os.environ.get("HOME", tempfile.gettempdir()),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "TEMP": os.environ.get("TEMP", tempfile.gettempdir()),
                "TMP": os.environ.get("TMP", tempfile.gettempdir()),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": "",
            }
        return {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": tempfile.gettempdir(),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": "",
            "LD_LIBRARY_PATH": "",
        }
