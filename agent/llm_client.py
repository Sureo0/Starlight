"""
Extended LLM Client with dual Function Calling support.

Supports two modes:
  1. Native function calling (tools parameter) — for GPT-4, Claude, DeepSeek V3+, etc.
  2. Prompt-based function calling (XML format) — for older models, Ollama, etc.

The orchestrator auto-detects which mode to use based on config or model capability.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger("agent.llm")


# ============================================================
# Response types
# ============================================================

@dataclass
class LLMResponse:
    """Unified response from the LLM client."""

    type: str  # "text" or "tool_use"
    content: str = ""  # Text content (may be empty on tool_use)
    tool_calls: list[dict] = field(default_factory=list)  # OpenAI-format tool_calls
    model: str = ""
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)  # Original API response
    mode: str = "native"  # "native" or "prompt" — which calling mode was used
    reasoning: str = ""  # Model's thinking/reasoning content (e.g. reasoning_content)

    @property
    def has_tool_calls(self) -> bool:
        return self.type == "tool_use" and len(self.tool_calls) > 0


# ============================================================
# Tool call format tags
# ============================================================

TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"
TOOL_RESULT_OPEN = "<tool_call>result"
TOOL_RESULT_CLOSE = "</tool_call>result"


# ============================================================
# LLM Client (Agent-aware, dual function calling)
# ============================================================

class AgentLLMClient:
    """
    Agent-aware LLM client that supports dual function calling.

    Mode 1 — Native: Sends `tools` in the API payload. The model returns
    structured tool_calls in its response message.

    Mode 2 — Prompt: Appends tool descriptions and XML format instructions
    to the system prompt. The model outputs <tool_call> XML blocks which we parse.

    Auto-detection: By default, tries native first. If the API returns an error
    about unsupported `tools` parameter, falls back to prompt mode automatically.
    """

    def __init__(self, config: dict):
        self._config = config
        self._config_lock = threading.RLock()
        # Reusable connection pool
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=0,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        # Track which mode works for each backend
        self._mode_cache: dict[str, str] = {}  # backend_name -> "native" | "prompt"

    @property
    def backend_name(self) -> str:
        """Name of the active backend (for tracing)."""
        backend = self._get_active_backend()
        return backend.get("name", "") if backend else ""

    @property
    def model_name(self) -> str:
        """Model name of the active backend (for tracing)."""
        backend = self._get_active_backend()
        return backend.get("model", "") if backend else ""

    @property
    def supports_vision(self) -> bool:
        """Whether the ACTIVE backend supports image input.

        Explicit `supports_vision` in the backend config wins. Otherwise
        fall back to a curated list of known vision-capable models. Models
        not on the list are assumed to NOT support vision (avoids sending
        images to text-only endpoints, which 404s).
        """
        backend = self._get_active_backend()
        if not backend:
            return False
        explicit = backend.get("supports_vision")
        if explicit is not None:
            return bool(explicit)
        model = (backend.get("model") or "").lower()
        # Known vision-capable model families (case-insensitive substring)
        vision_markers = (
            "vision", "v-l", "vl", "multimodal", "omni",
            "gpt-4o", "gpt-4.1", "gpt-5", "claude-3", "claude-3.5", "claude-3.7",
            "claude-sonnet-4", "claude-opus-4", "gemini", "qwen-vl", "qwen2.5-vl",
            "glm-4v", "internvl", "minicpm-v", "llava", "deepseek-vl",
            "mimo",  # Xiaomi MIMO v2.5 is multimodal (vision-capable)
        )
        return any(marker in model for marker in vision_markers)

    def update_config(self, config: dict):
        """Update config (called when user changes settings)."""
        with self._config_lock:
            self._config = config

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 120,
        force_mode: str | None = None,
    ) -> LLMResponse:
        """
        Send a chat request to the LLM with optional tool support.

        Args:
            messages: List of message dicts (role, content, ...)
            tools: List of tool schemas (OpenAI function-calling format)
            tool_choice: "auto", "none", "required"
            model: Model name override
            temperature: Sampling temperature
            max_tokens: Max tokens in response
            timeout: Request timeout in seconds
            force_mode: Force "native" or "prompt" mode. None = auto-detect.

        Returns:
            LLMResponse with type="text" or type="tool_use"
        """
        backend = self._get_active_backend()
        if not backend:
            raise ValueError("No LLM backend configured.")

        api_base = backend.get("api_base", "https://api.openai.com/v1").rstrip("/")
        api_key = backend.get("api_key", "")
        model_name = model or backend.get("model", "gpt-3.5-turbo")
        backend_name = backend.get("name", api_base)

        if not api_key:
            raise ValueError(f"API key not set for backend '{backend.get('name', '')}'")

        # Determine which mode to use
        mode = force_mode or self._get_mode(backend_name)

        if mode == "prompt" and tools:
            # Prompt-based mode: inject tool descriptions into messages
            messages = self._inject_prompt_tool_calls(messages, tools)
            tools_for_api = None  # Don't send tools param to API
        else:
            tools_for_api = tools

        # Build payload
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools_for_api:
            payload["tools"] = tools_for_api
            payload["tool_choice"] = tool_choice

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        start = time.time()
        try:
            resp = self._session.post(
                f"{api_base}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"LLM connection failed: {e}")

        duration = time.time() - start

        # --- Handle API errors ---
        # If we sent tools and got a 400, it might be "tools not supported"
        if resp.status_code == 400 and tools_for_api:
            error_text = resp.text.lower()
            if any(kw in error_text for kw in ["tools", "function", "unsupported", "not supported", "invalid"]):
                logger.info(
                    "Backend '%s' doesn't support native tools, switching to prompt mode",
                    backend_name,
                )
                self._mode_cache[backend_name] = "prompt"
                # Retry with prompt mode
                return self.chat(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    force_mode="prompt",
                )

        # Any other non-200 error
        if resp.status_code != 200:
            error_text = resp.text[:500]
            # Model can't process images (vision not supported / corrupt
            # multimodal payload): surface a friendly, actionable message
            # instead of a raw 404/400 that baffles the user.
            if (
                "image input" in error_text.lower()
                or "multimodal" in error_text.lower()
                or "image" in error_text.lower() and (
                    "not support" in error_text.lower()
                    or "unsupported" in error_text.lower()
                    or "cannot be processed" in error_text.lower()
                )
            ):
                raise RuntimeError(
                    "当前大模型不支持多模态，无法查看图片。"
                    "请切换到支持图片输入的模型后再试。"
                )
            raise RuntimeError(
                f"LLM API error ({resp.status_code}): {error_text}"
            )

        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        usage = data.get("usage", {})

        logger.info(
            "LLM call: model=%s mode=%s duration=%.1fms tokens=%d",
            data.get("model", model_name),
            mode,
            duration * 1000,
            usage.get("total_tokens", 0),
        )

        # --- Parse response ---

        # 1. Check for native tool calls
        if message.get("tool_calls"):
            self._mode_cache[backend_name] = "native"
            tool_calls = self._parse_native_tool_calls(message["tool_calls"])
            return LLMResponse(
                type="tool_use",
                content=message.get("content", "") or "",
                tool_calls=tool_calls,
                model=data.get("model", model_name),
                usage=usage,
                raw=data,
                mode="native",
                reasoning=message.get("reasoning_content") or "",
            )

        # 2. Plain text response
        content = message.get("content", "")
        # Multimodal responses can carry content as a list of parts — join
        # the text parts for our string-based handling downstream.
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        text_parts.append("[图片]")
                else:
                    text_parts.append(str(part))
            content = "\n".join(p for p in text_parts if p)

        # 3. If we were in prompt mode, try to parse XML tool calls
        if mode == "prompt" and tools:
            parsed_calls, clean_content = self._parse_prompt_tool_calls(content)
            if parsed_calls:
                return LLMResponse(
                    type="tool_use",
                    content=clean_content,
                    tool_calls=parsed_calls,
                    model=data.get("model", model_name),
                    usage=usage,
                    raw=data,
                    mode="prompt",
                )

        return LLMResponse(
            type="text",
            content=content,
            model=data.get("model", model_name),
            usage=usage,
            raw=data,
            mode=mode,
            reasoning=message.get("reasoning_content") or "",
        )

    # ============================================================
    # Mode management
    # ============================================================

    def _get_mode(self, backend_name: str) -> str:
        """Get the calling mode for a backend (from cache or config)."""
        # Check explicit config override
        with self._config_lock:
            agent_config = self._config.get("agent", {})
            forced = agent_config.get("function_calling_mode")
            if forced in ("native", "prompt"):
                return forced

        # Check cache
        if backend_name in self._mode_cache:
            return self._mode_cache[backend_name]

        # Default: try native first
        return "native"

    def set_mode(self, backend_name: str, mode: str):
        """Explicitly set the calling mode for a backend."""
        if mode in ("native", "prompt"):
            self._mode_cache[backend_name] = mode

    def get_mode(self, backend_name: str) -> str:
        """Get the current effective mode for a backend."""
        return self._get_mode(backend_name)

    # ============================================================
    # Native function calling parser
    # ============================================================

    def _parse_native_tool_calls(self, raw_tool_calls: list[dict]) -> list[dict]:
        """Parse native tool_calls from the API response into our standard format."""
        calls = []
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            # Arguments might be a string or dict
            args = func.get("arguments", "{}")
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)

            calls.append({
                "id": tc.get("id", f"call_{int(time.time()*1000)}_{len(calls)}"),
                "type": "function",
                "function": {
                    "name": func.get("name", ""),
                    "arguments": args,
                },
            })
        return calls

    # ============================================================
    # Prompt-based function calling
    # ============================================================

    def _inject_prompt_tool_calls(
        self, messages: list[dict], tools: list[dict]
    ) -> list[dict]:
        """
        Inject tool descriptions and format instructions into messages
        for prompt-based function calling.

        Strategy:
        - Find the system prompt (first message with role="system")
        - Append tool descriptions and XML format instructions to it
        - No changes to other messages
        """
        if not messages:
            return messages

        # Build the tool description block
        tool_block = self._build_tool_descriptions_block(tools)
        format_block = self._build_format_instructions()

        # Find and modify the system message
        result = []
        for i, msg in enumerate(messages):
            if i == 0 and msg.get("role") == "system":
                # Append tool info to system prompt
                result.append({
                    "role": "system",
                    "content": msg["content"] + "\n\n" + tool_block + "\n\n" + format_block,
                })
            else:
                result.append(msg)

        # If there's no system message, insert one at the beginning
        if not result or result[0].get("role") != "system":
            result.insert(0, {
                "role": "system",
                "content": tool_block + "\n\n" + format_block,
            })

        return result

    def _build_tool_descriptions_block(self, tools: list[dict]) -> str:
        """
        Build a human-readable tool description block from OpenAI tool schemas.

        This is injected into the system prompt so the model knows what tools
        are available and how to call them.
        """
        lines = ["## 可用工具\n"]
        lines.append("你有以下工具可以使用。当你需要执行操作时，请使用对应的工具。\n")

        for tool_schema in tools:
            func = tool_schema.get("function", {})
            name = func.get("name", "unknown")
            desc = func.get("description", "No description")
            params = func.get("parameters", {})
            properties = params.get("properties", {})
            required = params.get("required", [])

            lines.append(f"### {name}")
            lines.append(f"描述: {desc}")

            if properties:
                lines.append("参数:")
                for param_name, param_info in properties.items():
                    param_type = param_info.get("type", "string")
                    param_desc = param_info.get("description", "")
                    is_required = param_name in required
                    req_mark = " (必填)" if is_required else " (可选)"
                    lines.append(f"  - {param_name} ({param_type}){req_mark}: {param_desc}")

            lines.append("")  # Empty line between tools

        return "\n".join(lines)

    def _build_format_instructions(self) -> str:
        """
        Build the XML format instructions for prompt-based tool calling.
        """
        return f"""## 工具调用格式

当你需要使用工具时，请严格按以下格式输出。每个工具调用使用一对 `{TOOL_CALL_OPEN}` 和 `{TOOL_CALL_CLOSE}` 标签：

{TOOL_CALL_OPEN}
{{"name": "工具名称", "arguments": {{"参数名": "参数值"}}}}
{TOOL_CALL_CLOSE}

### 规则
1. 每个 `{TOOL_CALL_OPEN}` 块只包含一个 JSON 对象，不要添加任何其他文字
2. 你可以连续输出多个 `{TOOL_CALL_OPEN}` 块来同时调用多个工具
3. 调用完成后，请停止输出，等待工具执行结果
4. 如果不需要使用工具，直接正常回复即可，不要输出空的 `{TOOL_CALL_OPEN}` 块
5. JSON 中的 arguments 值必须是对象格式，即使只有一个参数

### 示例

用户: 帮我搜索深圳的天气
你的回复:
我来帮你搜索深圳的天气。
{TOOL_CALL_OPEN}
{{"name": "web_search", "arguments": {{"query": "深圳天气"}}}}
{TOOL_CALL_CLOSE}

用户: 帮我读取 config.yaml 的内容
你的回复:
我来读取配置文件。
{TOOL_CALL_OPEN}
{{"name": "read_file", "arguments": {{"path": "data/config.yaml"}}}}
{TOOL_CALL_CLOSE}

用户: 帮我计算 1+1 然后搜索 python 教程
你的回复:
我来同时执行这两个任务。
{TOOL_CALL_OPEN}
{{"name": "execute_code", "arguments": {{"code": "print(1 + 1)"}}}}
{TOOL_CALL_CLOSE}
{TOOL_CALL_OPEN}
{{"name": "web_search", "arguments": {{"query": "python 教程"}}}}
{TOOL_CALL_CLOSE}"""

    def _parse_prompt_tool_calls(self, content: str) -> tuple[list[dict], str]:
        """
        Parse tool calls from prompt-based XML format.

        Returns:
            (tool_calls, clean_content) — parsed calls and content with XML stripped
        """
        # Pattern: <tool_call>...</tool_call>
        # The JSON inside may span multiple lines
        pattern = re.compile(
            r"<tool_call>\s*\n?(.*?)\n?\s*</tool_call>",
            re.DOTALL,
        )

        matches = list(pattern.finditer(content))
        if not matches:
            return [], content

        calls = []
        for match in matches:
            raw_json = match.group(1).strip()
            try:
                parsed = json.loads(raw_json)
                name = parsed.get("name", "")
                args = parsed.get("arguments", {})

                # Validate: name must be non-empty
                if not name:
                    logger.warning("Tool call with empty name, skipping")
                    continue

                # Ensure arguments is a dict
                if not isinstance(args, dict):
                    args = {"input": args}

                calls.append({
                    "id": f"call_{int(time.time()*1000)}_{len(calls)}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
            except json.JSONDecodeError as e:
                logger.warning("Failed to parse tool call JSON: %s (error: %s)", raw_json[:100], e)
                continue

        # Strip tool call blocks from the content to get clean text
        clean_content = pattern.sub("", content).strip()

        return calls, clean_content

    def build_tool_call_prompt_addendum(self) -> str:
        """
        Generate the full prompt addendum for prompt-based tool calling.
        Includes both tool descriptions and format instructions.
        """
        # This is called without tool schemas — return format-only instructions
        return self._build_format_instructions()

    def build_full_tool_prompt(self, tools: list[dict]) -> str:
        """
        Build the complete tool prompt section (descriptions + format).
        Used by the orchestrator when injecting into system prompt.
        """
        return self._build_tool_descriptions_block(tools) + "\n\n" + self._build_format_instructions()

    # ============================================================
    # Tool result formatting for prompt-based mode
    # ============================================================

    @staticmethod
    def format_tool_result_for_prompt(tool_name: str, result: dict) -> str:
        """
        Format a tool result as a message for prompt-based models.

        Prompt-based models don't support the 'tool' role, so we format
        results as a special user message that the model can understand.
        """
        result_str = json.dumps(result, ensure_ascii=False, indent=2)
        # Truncate very long results
        if len(result_str) > 3000:
            result_str = result_str[:3000] + "\n... [truncated]"

        return f"""{TOOL_RESULT_OPEN}
工具: {tool_name}
执行结果:
{result_str}
{TOOL_RESULT_CLOSE}"""

    # ============================================================
    # Backend utilities
    # ============================================================

    def _get_active_backend(self):
        """Get the currently active backend.

        Priority:
          1. The backend named by config['active_backend'] (if it exists)
          2. The first enabled backend
          3. The first backend
        """
        with self._config_lock:
            config = self._config
            backends = config.get("llms", {}).get("backends", [])
        if not backends:
            return None
        active_name = config.get("active_backend")
        if active_name:
            for b in backends:
                if b.get("name") == active_name:
                    return b
        for b in backends:
            if b.get("enabled", True):
                return b
        return backends[0]

    def list_models(self) -> list[str]:
        """List available models from the active backend."""
        backend = self._get_active_backend()
        if not backend:
            return []

        api_base = backend.get("api_base", "").rstrip("/")
        api_key = backend.get("api_key", "")

        try:
            resp = self._session.get(
                f"{api_base}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
        except Exception:
            pass
        return []
