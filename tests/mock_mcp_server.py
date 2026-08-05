"""A tiny MCP server used by tests (stdio transport).

Speaks the stdio MCP protocol properly: reads JSON-RPC requests from stdin,
answers each with the matching id. Exposes two tools:
  - echo: returns the text argument
  - add: returns a+b
"""

import json
import sys


def _write(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


TOOLS = [
    {"name": "echo", "description": "echo back the text",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "text to echo"},
     }, "required": ["text"]}},
    {"name": "add", "description": "add two numbers",
     "inputSchema": {"type": "object", "properties": {
         "a": {"type": "number"}, "b": {"type": "number"},
     }, "required": ["a", "b"]}},
]

INIT_RESULT = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "mock-mcp", "version": "1.0.0"},
}


def main():
    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method")

        if method == "initialize":
            # Echo back the protocol version the client requested (or default)
            params = msg.get("params", {})
            result = dict(INIT_RESULT)
            result["protocolVersion"] = params.get("protocolVersion", "2024-11-05")
            _write({"jsonrpc": "2.0", "id": msg_id, "result": result})
            initialized = True
        elif method == "notifications/initialized":
            pass  # no reply needed
        elif method == "ping":
            _write({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                content = [{"type": "text", "text": args.get("text", "")}]
            elif name == "add":
                try:
                    total = args.get("a", 0) + args.get("b", 0)
                    content = [{"type": "text", "text": str(total)}]
                except Exception as e:
                    content = [{"type": "text", "text": f"error: {e}"}]
            else:
                content = [{"type": "text", "text": f"unknown tool {name}"}]
                _write({"jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": content, "isError": True}})
                continue
            _write({"jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": content, "isError": False}})
        else:
            _write({"jsonrpc": "2.0", "id": msg_id,
                    "result": {"content": [], "isError": True,
                               "text": f"unhandled method {method}"}})


if __name__ == "__main__":
    main()
