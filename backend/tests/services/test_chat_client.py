from unittest.mock import MagicMock, patch

import pytest

from app.services.chat_client import ChatClient, ChatUnavailableError


def test_chat_returns_reply_content() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        response = MagicMock()
        response.message.content = "Hello there."
        client_class.return_value.chat.return_value = response

        client = ChatClient(model="qwen3:8b", host="http://ollama:11434")

        messages = [{"role": "user", "content": "hi"}]
        result = client.chat(messages)

        assert result == "Hello there."
        client_class.return_value.chat.assert_called_once_with(
            model="qwen3:8b",
            messages=messages,
        )


def test_chat_returns_empty_string_for_none_content() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        response = MagicMock()
        response.message.content = None
        client_class.return_value.chat.return_value = response

        client = ChatClient()

        assert client.chat([{"role": "user", "content": "hi"}]) == ""


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
