"""
Code Executor Tool - Execute Python code in a sandboxed environment.

Uses CodeSandbox for security: restricted imports, resource limits,
output capture and truncation.
"""

from __future__ import annotations

import logging

from agent.tools.base import Tool, ToolResult
from agent.security.sandbox import CodeSandbox, SandboxConfig

logger = logging.getLogger("agent.tools.code_executor")


class CodeExecutorTool(Tool):
    """
    Execute Python code and return the output.

    Runs code in an isolated subprocess with:
    - Restricted imports (no os, subprocess, socket, etc.)
    - Resource limits (30s timeout, 100KB output)
    - eval/exec disabled
    """

    # Code execution has side effects — never auto-retry.
    retryable: bool = False

    def __init__(self, sandbox_config: SandboxConfig | None = None, work_dir: str | None = None):
        self._sandbox = CodeSandbox(config=sandbox_config)
        self._work_dir = work_dir  # sandbox cwd; set by presets to the workspace

    @property
    def name(self) -> str:
        return "execute_code"

    @property
    def description(self) -> str:
        return (
            "Execute Python code in a sandboxed environment. Returns stdout and stderr. "
            "Use this for calculations, data processing, text manipulation, "
            "or generating structured output. Code runs with a 30-second timeout. "
            "The working directory is the same workspace where file tools write, "
            "so your code can read files you created earlier."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python code to execute.",
                },
                "description": {
                    "type": "string",
                    "description": "A brief description of what this code does (for logging).",
                },
            },
            "required": ["code"],
        }

    def execute(self, code: str, description: str = "", **kwargs) -> ToolResult:
        """Execute Python code in a sandboxed subprocess."""
        if not code.strip():
            return ToolResult(success=False, error="Code cannot be empty")

        result = self._sandbox.execute(code, description=description, work_dir=self._work_dir)

        output = {
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "returncode": result["returncode"],
        }

        metadata = {"description": description}
        if result.get("security_warnings"):
            metadata["security_warnings"] = result["security_warnings"]

        if result["returncode"] == 0:
            return ToolResult(
                success=True,
                output=output,
                metadata=metadata,
            )
        else:
            return ToolResult(
                success=False,
                output=output,
                error=result["stderr"] or f"Exit code {result['returncode']}",
                metadata=metadata,
            )
