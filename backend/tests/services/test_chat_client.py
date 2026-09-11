from unittest.mock import MagicMock, patch

import pytest

from app.services.chat_client import ChatClient, ChatUnavailableError


def test_chat_returns_reply_message() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        response = MagicMock()
        response.message.content = "Hello there."
        response.message.tool_calls = None
        client_class.return_value.chat.return_value = response

        client = ChatClient(model="qwen3:8b", host="http://ollama:11434")

        messages = [{"role": "user", "content": "hi"}]
        result = client.chat(messages)

        assert result is response.message
        assert result.content == "Hello there."
        client_class.return_value.chat.assert_called_once_with(
            model="qwen3:8b",
            messages=messages,
            tools=None,
        )


def test_chat_passes_tools_through() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        response = MagicMock()
        client_class.return_value.chat.return_value = response

        client = ChatClient()

        tools = [{"type": "function", "function": {"name": "get_time"}}]
        client.chat([{"role": "user", "content": "hi"}], tools=tools)

        client_class.return_value.chat.assert_called_once_with(
            model=client.model,
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        )


def test_chat_wraps_failures_in_chat_unavailable_error() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        client_class.return_value.chat.side_effect = ConnectionError("unreachable")

        client = ChatClient()

        with pytest.raises(ChatUnavailableError, match="unreachable"):
            client.chat([{"role": "user", "content": "hi"}])


def test_chat_client_defaults_from_settings() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        from app.core.config import settings

        client = ChatClient()

        assert client.model == settings.CHAT_MODEL
        client_class.assert_called_once_with(host=settings.OLLAMA_HOST)
