"""
Chat Completion Tool - Calls the LLM for further reasoning.

This tool allows the agent to make additional LLM calls within its loop,
useful for chain-of-thought, summarization, or multi-step reasoning.
"""

from __future__ import annotations

import logging

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("agent.tools.chat_completion")


class ChatCompletionTool(Tool):
    """
    Tool that calls the LLM for additional reasoning.

    The agent can use this to:
    - Think through a complex problem step by step
    - Summarize information gathered from other tools
    - Generate code, analysis, or structured output
    """

    # Nested LLM calls cost tokens — never auto-retry.
    retryable: bool = False

    def __init__(self, llm_client):
        """
        Args:
            llm_client: An AgentLLMClient instance for making API calls.
        """
        self._llm = llm_client

    @property
    def name(self) -> str:
        return "chat_completion"

    @property
    def description(self) -> str:
        return (
            "Call the LLM for additional reasoning, analysis, or content generation. "
            "Use this when you need to think through a problem, summarize information, "
            "or generate structured output based on data from other tools."
        )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "description": (
                        "List of messages for the LLM. Each message is an object with "
                        "'role' ('system', 'user', 'assistant') and 'content' (string)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "enum": ["system", "user", "assistant"],
                            },
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
                "temperature": {
                    "type": "number",
                    "description": "Sampling temperature (0.0-1.0). Default: 0.7",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Max tokens in response. Default: 2048",
                },
            },
            "required": ["messages"],
        }

    def execute(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs,
    ) -> ToolResult:
        """Execute an LLM chat completion call."""
        try:
            # Validate messages
            if not messages:
                return ToolResult(success=False, error="Messages list is empty")

            for msg in messages:
                if "role" not in msg or "content" not in msg:
                    return ToolResult(
                        success=False,
                        error=f"Each message must have 'role' and 'content': {msg}",
                    )

            # Call LLM without tools (this is a pure text generation call)
            response = self._llm.chat(
                messages=messages,
                tools=None,  # No tools for this call
                temperature=temperature,
                max_tokens=max_tokens,
            )

            return ToolResult(
                success=True,
                output=response.content,
                metadata={
                    "model": response.model,
                    "usage": response.usage,
                },
            )

        except Exception as e:
            logger.exception("Chat completion tool failed")
            return ToolResult(success=False, error=str(e))
