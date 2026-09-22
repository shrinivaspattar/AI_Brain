from __future__ import annotations

from typing import Any

import ollama

from app.core.config import settings


class ChatUnavailableError(RuntimeError):
    """Raised when the Ollama chat API can't be reached or fails."""


def _is_tools_unsupported_error(exc: Exception) -> bool:
    """True for Ollama's specific "this model has no tool-calling template"
    rejection (e.g. older Mistral fine-tunes like dolphin-mistral) - never
    for any other failure (a real outage must still surface as an error,
    not be silently swallowed as if it were this one, narrow case)."""
    return "does not support tools" in str(exc).lower()


class ChatClient:
    """Thin wrapper around the Ollama chat API."""

    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
        thinking: bool | None = None,
    ):
        self.model = model or settings.CHAT_MODEL
        self.thinking = settings.CHAT_THINKING_ENABLED if thinking is None else thinking
        self._client = ollama.Client(host=host or settings.OLLAMA_HOST)
        # Learned, not configured: some models (e.g. dolphin-mistral) simply
        # cannot take a `tools` argument at all. Once a turn on this model
        # hits that specific rejection, every later turn on this same
        # ChatClient stops sending tools too, rather than failing the same
        # way every time - the conversation still works, just without the
        # model being able to call search_knowledge_base/remember/etc.
        self._tools_unsupported = False

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
        effective_tools = None if self._tools_unsupported else tools
        try:
            response = self._client.chat(
                model=self.model,
                messages=messages,
                tools=effective_tools,
                think=self.thinking,
            )
        except Exception as exc:
            if effective_tools and _is_tools_unsupported_error(exc):
                self._tools_unsupported = True
                return self.chat(messages, tools=None)
            raise ChatUnavailableError(str(exc)) from exc

        return response.message

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        """Like `chat`, but yields each partial response message as the model
        produces it, so the caller can show text immediately."""
        effective_tools = None if self._tools_unsupported else tools
        try:
            for chunk in self._client.chat(
                model=self.model,
                messages=messages,
                tools=effective_tools,
                think=self.thinking,
                stream=True,
            ):
                yield chunk.message
        except Exception as exc:
            if effective_tools and _is_tools_unsupported_error(exc):
                self._tools_unsupported = True
                yield from self.chat_stream(messages, tools=None)
                return
            raise ChatUnavailableError(str(exc)) from exc
