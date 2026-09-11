from __future__ import annotations

from typing import Any

import ollama

from app.core.config import settings


class ChatUnavailableError(RuntimeError):
    """Raised when the Ollama chat API can't be reached or fails."""


class ChatClient:
    """Thin wrapper around the Ollama chat API."""

    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
    ):
        self.model = model or settings.CHAT_MODEL
        self._client = ollama.Client(host=host or settings.OLLAMA_HOST)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ollama.Message:
        """Send a chat turn and return the raw response message.

        Returning the full message (not just its text) lets callers
        inspect `.tool_calls` to drive a tool-calling loop; `.content`
        holds the plain-text reply as before.
        """
        try:
            response = self._client.chat(
                model=self.model,
                messages=messages,
                tools=tools,
            )
        except Exception as exc:
            raise ChatUnavailableError(str(exc)) from exc

        return response.message
