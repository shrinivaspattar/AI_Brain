from __future__ import annotations

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

    def chat(self, messages: list[dict[str, str]]) -> str:
        try:
            response = self._client.chat(model=self.model, messages=messages)
        except Exception as exc:
            raise ChatUnavailableError(str(exc)) from exc

        return response.message.content or ""
