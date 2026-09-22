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
            think=False,
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
            think=False,
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


def test_thinking_is_off_by_default() -> None:
    with patch("app.services.chat_client.ollama.Client"):
        assert ChatClient().thinking is False


def test_thinking_can_be_enabled_per_client_and_is_sent_to_ollama() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        client_class.return_value.chat.return_value = MagicMock()

        ChatClient(thinking=True).chat([{"role": "user", "content": "hi"}])

        assert client_class.return_value.chat.call_args.kwargs["think"] is True


def test_thinking_follows_the_setting_when_not_given(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_THINKING_ENABLED", True)
    with patch("app.services.chat_client.ollama.Client"):
        assert ChatClient().thinking is True


# ---- graceful fallback for models that reject `tools` entirely ----

def test_chat_retries_without_tools_when_the_model_does_not_support_them() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        ok_response = MagicMock()
        ok_response.message.content = "Hi! (no tools)"
        ok_response.message.tool_calls = None
        client_class.return_value.chat.side_effect = [
            RuntimeError("dolphin-mistral does not support tools"),
            ok_response,
        ]

        client = ChatClient()
        tools = [{"type": "function", "function": {"name": "get_time"}}]
        result = client.chat([{"role": "user", "content": "hi"}], tools=tools)

        assert result is ok_response.message
        assert client_class.return_value.chat.call_count == 2
        first_call, second_call = client_class.return_value.chat.call_args_list
        assert first_call.kwargs["tools"] == tools
        assert second_call.kwargs["tools"] is None


def test_chat_remembers_tools_are_unsupported_for_later_turns_on_the_same_client() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        first_ok = MagicMock()
        second_ok = MagicMock()
        client_class.return_value.chat.side_effect = [
            RuntimeError("model does not support tools"),
            first_ok,
            second_ok,
        ]

        client = ChatClient()
        tools = [{"type": "function", "function": {"name": "get_time"}}]
        client.chat([{"role": "user", "content": "hi"}], tools=tools)
        client.chat([{"role": "user", "content": "again"}], tools=tools)

        assert client_class.return_value.chat.call_count == 3
        assert client._tools_unsupported is True
        assert client_class.return_value.chat.call_args_list[-1].kwargs["tools"] is None


def test_chat_does_not_swallow_an_unrelated_failure_as_tools_unsupported() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        client_class.return_value.chat.side_effect = ConnectionError("connection refused")

        client = ChatClient()

        with pytest.raises(ChatUnavailableError, match="connection refused"):
            client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

        assert client_class.return_value.chat.call_count == 1
        assert client._tools_unsupported is False


def test_chat_stream_retries_without_tools_when_the_model_does_not_support_them() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        good_chunk = MagicMock()
        good_chunk.message.content = "Hi"

        def side_effect(*_args, **kwargs):
            if kwargs.get("tools"):
                raise RuntimeError("does not support tools")
            return iter([good_chunk])

        client_class.return_value.chat.side_effect = side_effect

        client = ChatClient()
        tools = [{"type": "function", "function": {"name": "get_time"}}]
        pieces = list(client.chat_stream([{"role": "user", "content": "hi"}], tools=tools))

        assert pieces == [good_chunk.message]
        assert client_class.return_value.chat.call_count == 2


def test_chat_stream_does_not_swallow_an_unrelated_failure() -> None:
    with patch("app.services.chat_client.ollama.Client") as client_class:
        def side_effect(*_args, **_kwargs):
            raise ConnectionError("connection refused")

        client_class.return_value.chat.side_effect = side_effect
        client = ChatClient()

        with pytest.raises(ChatUnavailableError, match="connection refused"):
            list(client.chat_stream([{"role": "user", "content": "hi"}], tools=[{"type": "function"}]))
