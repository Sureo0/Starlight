"""
MCP (Model Context Protocol) integration for the agent.

MCPManager owns the lifecycle of MCP servers configured in config.yaml:

    mcp_servers:
      my_server:
        transport: stdio          # or "http"
        command: npx             # stdio: command + args
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        url: "http://localhost:8931/mcp"   # http transport
        enabled: true
        permission: user          # user | admin | read-only
        env: {}                   # extra env vars for the subprocess

Servers are connected lazily on startup (background) and re-connected on
failure. Each server's tools are exposed as MCPTool instances registered in
the ToolRegistry, so the rest of the agent (permissions, tracing, retries,
loop guards) treats them exactly like built-in tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent.mcp.manager")

# Lazy import: the `mcp` package may be installed after this module is
# imported (e.g. tests using a different interpreter). Always import inside
# the connection path and treat a missing package as an error there.
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    try:
        # SDK 2.0: snake_case entry point
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        # SDK <2.0: camelCase entry point
        from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    streamable_http_client = None
    _MCP_AVAILABLE = False

# Server states
STATE_STOPPED = "stopped"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"

# Permission levels for MCP servers
PERM_USER = "user"          # standard user permissions (read + write + execute)
PERM_ADMIN = "admin"        # all tools allowed
PERM_READONLY = "read-only"  # only tools flagged read-only


@dataclass
class MCPServerConfig:
    """Parsed config for one MCP server."""

    name: str
    transport: str = "stdio"  # "stdio" | "http"
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    enabled: bool = True
    permission: str = PERM_USER
    env: dict = field(default_factory=dict)


@dataclass
class MCPServerStatus:
    """Observable status of a server (for the UI / API)."""

    name: str
    state: str = STATE_STOPPED
    tools: int = 0
    last_error: str = ""
    connected_at: float | None = None
    tool_names: list[str] = field(default_factory=list)


class MCPServerSession:
    """One connected MCP server: session + lifecycle management."""

    def __init__(self, config: MCPServerConfig, on_tools_changed=None):
        self.config = config
        self.on_tools_changed = on_tools_changed
        self.status = MCPServerStatus(name=config.name)
        self.session: ClientSession | None = None
        self._ctx = None  # async context stack
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit_stack = None
        self._tools: list[dict] = []
        self._reconnect_until = 0.0
        self._stop = False

    # ----------------------------------------------------------
    # Connection lifecycle (run in a dedicated event loop thread)
    # ----------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Begin connecting in the given event loop (non-blocking)."""
        self._loop = loop
        self._stop = False
        self._reconnect_until = time.time() + 300  # keep trying ~5 min
        asyncio.run_coroutine_threadsafe(self._run(), loop)

    async def _run(self):
        """Connect, then keep the session alive with reconnection."""
        while not self._stop and time.time() < self._reconnect_until:
            try:
                await self._connect_once()
                # Connected: wait until the session dies, then reconnect.
                while not self._stop:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.status.state = STATE_ERROR
                self.status.last_error = str(e)[:300]
                logger.warning("MCP server '%s' connection error: %s", self.config.name, e)
                await asyncio.sleep(5)
        if not self._stop:
            self.status.state = STATE_ERROR
            self.status.last_error = "gave up reconnecting (check config)"

    async def _connect_once(self):
        if not _MCP_AVAILABLE:
            raise RuntimeError("mcp package not installed")

        self.status.state = STATE_CONNECTING
        self.status.last_error = ""
        try:
            if self.config.transport == "http":
                if not self.config.url:
                    raise RuntimeError("http transport requires url")
                ctx = streamable_http_client(self.config.url)
            else:
                if not self.config.command:
                    raise RuntimeError("stdio transport requires command")
                params = StdioServerParameters(
                    command=self.config.command,
                    args=self.config.args or [],
                    env={**os.environ, **self.config.env},
                )
                ctx = stdio_client(params)

            read, write = await ctx.__aenter__()
            session = await ClientSession(read, write).__aenter__()
            init = await session.initialize()
            # List available tools (SDK 2.x uses snake_case fields)
            tools_result = await session.list_tools()
            self._tools = []
            for t in (tools_result.tools or []):
                input_schema = (
                    getattr(t, "input_schema", None)
                    or getattr(t, "inputSchema", None)
                    or {}
                )
                self._tools.append({
                    "name": t.name,
                    "description": getattr(t, "description", "") or "",
                    "inputSchema": input_schema,
                })
            self.session = session
            self._ctx = ctx
            self.status.state = STATE_CONNECTED
            self.status.tools = len(self._tools)
            self.status.tool_names = [t["name"] for t in self._tools]
            self.status.connected_at = time.time()
            self.status.last_error = ""
            logger.info(
                "MCP server '%s' connected (%d tools): %s",
                self.config.name, len(self._tools),
                ", ".join(self.status.tool_names) or "-",
            )
            if self.on_tools_changed:
                try:
                    self.on_tools_changed()
                except Exception:
                    logger.exception("on_tools_changed callback failed")
        except Exception as e:
            raise

    async def call_tool(self, name: str, arguments: dict | None) -> dict:
        """Call a tool on this server (must be connected)."""
        if self.session is None or self.status.state != STATE_CONNECTED:
            raise RuntimeError(f"MCP server '{self.config.name}' not connected")
        result = await self.session.call_tool(name, arguments or {})
        # Normalize MCP result -> JSON-friendly dict (SDK 2.x: is_error)
        text_parts = []
        structured = None
        for content in getattr(result, "content", []) or []:
            ctype = getattr(content, "type", "") or getattr(content, "role", "")
            if ctype == "text":
                text_parts.append(getattr(content, "text", "") or "")
            elif ctype in ("structured", "data"):
                structured = getattr(content, "structured", None)
        is_error = bool(getattr(result, "is_error", False)) or bool(getattr(result, "isError", False))
        return {
            "success": not is_error,
            "output": structured if structured is not None else "\n".join(text_parts),
            "is_error": is_error,
            "raw": (
                {
                    "content": [
                        {"type": getattr(c, "type", ""), "text": getattr(c, "text", None) or ""}
                        for c in (getattr(result, "content", []) or [])
                    ]
                }
                if hasattr(result, "content")
                else None
            ),
        }

    def stop(self) -> None:
        """Disconnect (call from the loop thread context)."""
        self._stop = True
        try:
            if self.session is not None:
                asyncio.run_coroutine_threadsafe(
                    self.session.__aexit__(None, None, None), self._loop
                )
                self.session = None
        except Exception:
            pass
        self.status.state = STATE_STOPPED


class MCPManager:
    """Owns all configured MCP servers and exposes their tools."""

    def __init__(self):
        self._servers: dict[str, MCPServerSession] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._tool_sink = None  # callable(tool_name, args) for tracing

    def configure(self, configs: list[MCPServerConfig]) -> None:
        """(Re)configure servers. Existing servers keep their state if the
        config is unchanged; new/changed ones are (re)started."""
        with self._lock:
            names = {c.name for c in configs}
            # Stop removed servers
            for name in list(self._servers):
                if name not in names:
                    self._servers.pop(name).stop()
            # Add or update servers
            for cfg in configs:
                if not cfg.enabled:
                    continue
                existing = self._servers.get(cfg.name)
                if existing is None:
                    server = MCPServerSession(
                        cfg, on_tools_changed=self._tools_changed
                    )
                    self._servers[cfg.name] = server
                    self._start_server(server)
                else:
                    existing.config = cfg  # allow live config refresh

    def _start_server(self, server: MCPServerSession) -> None:
        self._ensure_loop()
        server.start(self._loop)

    def _ensure_loop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            return
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="mcp-loop",
        )
        self._loop_thread.start()

    def _tools_changed(self) -> None:
        """Re-register MCP tools into the tool registry."""
        if self._tool_sink is None:
            return
        try:
            self._tool_sink()
        except Exception:
            logger.exception("tool re-registration failed")

    # ----------------------------------------------------------
    # Tool discovery
    # ----------------------------------------------------------

    def all_tools(self) -> list[dict]:
        """Flatten all connected servers' tools: [{server, name, description, inputSchema}]."""
        out = []
        with self._lock:
            servers = list(self._servers.values())
        for s in servers:
            if s.status.state != STATE_CONNECTED:
                continue
            for t in s._tools:
                out.append({
                    "server": s.config.name,
                    "permission": s.config.permission,
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                })
        return out

    def call_tool(self, server_name: str, tool_name: str, arguments: dict | None) -> dict:
        """Synchronous wrapper around the async call (blocks until done)."""
        with self._lock:
            server = self._servers.get(server_name)
        if server is None:
            return {"success": False, "error": f"MCP server '{server_name}' not configured"}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                server.call_tool(tool_name, arguments), server._loop
            )
            result = fut.result(timeout=60)
            return result
        except Exception as e:
            return {"success": False, "error": f"MCP call failed: {e}"}

    def statuses(self) -> list[dict]:
        with self._lock:
            servers = list(self._servers.values())
        return [
            {
                "name": s.config.name,
                "state": s.status.state,
                "tools": s.status.tools,
                "tool_names": s.status.tool_names,
                "last_error": s.status.last_error,
                "transport": s.config.transport,
                "permission": s.config.permission,
                "connected_at": s.status.connected_at,
            }
            for s in servers
        ]

    def shutdown(self) -> None:
        with self._lock:
            servers = list(self._servers.values())
        for s in servers:
            s.stop()


def parse_mcp_config(cfg: dict) -> list[MCPServerConfig]:
    """Parse the mcp_servers section of config.yaml."""
    out = []
    for name, spec in (cfg or {}).items():
        if not isinstance(spec, dict):
            continue
        transport = str(spec.get("transport", "stdio")).lower()
        if transport not in ("stdio", "http"):
            logger.warning("MCP server '%s': unknown transport %r, defaulting to stdio", name, transport)
            transport = "stdio"
        permission = str(spec.get("permission", PERM_USER))
        if permission not in (PERM_USER, PERM_ADMIN, PERM_READONLY):
            permission = PERM_USER
        args = spec.get("args", [])
        if isinstance(args, str):
            args = [a for a in args.split() if a]
        out.append(MCPServerConfig(
            name=name,
            transport=transport,
            command=str(spec.get("command", "")),
            args=list(args),
            url=str(spec.get("url", "")),
            enabled=bool(spec.get("enabled", True)),
            permission=permission,
            env=spec.get("env") or {},
        ))
    return out
