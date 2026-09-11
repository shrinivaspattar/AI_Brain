from __future__ import annotations

import logging
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

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool by name, never raising.

        A tool failure (unknown name, bad arguments, internal error) is
        returned as a result string describing the failure, not raised -
        the model gets to see what went wrong and can adapt, rather than
        the whole chat turn crashing over one bad tool call.
        """
        tool = self.get(name)

        if tool is None:
            return f"Error: unknown tool '{name}'"

        try:
            return tool.handler(**arguments)
        except Exception as exc:
            logger.warning("Tool '%s' failed: %s", name, exc, exc_info=True)
            return f"Error running tool '{name}': {exc}"
