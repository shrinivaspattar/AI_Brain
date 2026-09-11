from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    # JSON Schema for the tool's arguments, in the shape Ollama's
    # function-calling API expects (an "object" schema with "properties").
    parameters: dict[str, Any]
    handler: Callable[..., str]


@dataclass(frozen=True)
class ToolCallResult:
    """Outcome of executing one tool call.

    `content` is always the string to feed back to the model as the tool
    message's content — on failure, a human-readable description of what
    went wrong (Ollama's tool-calling models handle an error string in a
    tool result fine, and it lets the model adapt). `is_error`/`error`
    give callers a structured way to know it failed, for audit logging,
    rather than pattern-matching the content string.
    """

    content: str
    is_error: bool
    error: str | None = None
    duration_ms: int = 0


class ToolRegistry:
    """Holds the tools available to the chat loop and dispatches calls to them."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_ollama_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """Execute a tool by name, never raising.

        A tool failure (unknown name, bad arguments, internal error) is
        captured in the returned ToolCallResult, not raised - the model
        gets to see what went wrong (via `.content`) and can adapt,
        rather than the whole chat turn crashing over one bad tool call.
        """
        tool = self.get(name)

        if tool is None:
            message = f"Error: unknown tool '{name}'"
            return ToolCallResult(content=message, is_error=True, error=message)

        start = time.monotonic()

        try:
            content = tool.handler(**arguments)
            duration_ms = int((time.monotonic() - start) * 1000)
            return ToolCallResult(content=content, is_error=False, duration_ms=duration_ms)

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning("Tool '%s' failed: %s", name, exc, exc_info=True)
            message = f"Error running tool '{name}': {exc}"
            return ToolCallResult(
                content=message,
                is_error=True,
                error=str(exc),
                duration_ms=duration_ms,
            )
