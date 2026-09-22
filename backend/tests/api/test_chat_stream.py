import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models.message import Message, MessageRole
from app.services.chat_client import ChatUnavailableError


def _events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")]


def test_stream_sends_tokens_then_a_done_event_with_the_saved_message() -> None:
    app.dependency_overrides[get_db] = lambda: MagicMock()
    saved = Message(
        id=2, conversation_id="conv-1", role=MessageRole.ASSISTANT, content="Hello there",
        citations=None, created_at=datetime.now(UTC),
    )

    def fake_stream(*_args, **_kwargs):
        yield {"type": "token", "text": "Hello "}
        yield {"type": "token", "text": "there"}
        yield {"type": "done", "message": saved}

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message_stream.side_effect = fake_stream
            response = TestClient(app).post("/chat/stream", json={"message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response)
    assert [e["type"] for e in events] == ["token", "token", "done"]
    assert events[-1]["conversation_id"] == "conv-1"
    assert events[-1]["message"]["content"] == "Hello there"


def test_stream_reports_a_missing_chat_model_as_an_error_event() -> None:
    app.dependency_overrides[get_db] = lambda: MagicMock()

    def failing_stream(*_args, **_kwargs):
        raise ChatUnavailableError("timed out")
        yield  # pragma: no cover - makes this a generator

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message_stream.side_effect = failing_stream
            response = TestClient(app).post("/chat/stream", json={"message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert _events(response) == [{"type": "error", "detail": "Chat model unavailable: timed out"}]


def test_stream_reports_an_unknown_conversation_as_an_error_event() -> None:
    app.dependency_overrides[get_db] = lambda: MagicMock()

    def unknown_conversation(*_args, **_kwargs):
        raise ValueError("Conversation nope not found")
        yield  # pragma: no cover

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message_stream.side_effect = unknown_conversation
            response = TestClient(app).post("/chat/stream", json={"message": "hi", "conversation_id": "nope"})
    finally:
        app.dependency_overrides.clear()

    assert _events(response)[0]["type"] == "error"


def test_stream_uses_the_requested_model() -> None:
    from unittest.mock import patch

    app.dependency_overrides[get_db] = lambda: MagicMock()

    def fake_stream(*_args, **_kwargs):
        yield {"type": "done", "message": Message(
            id=2, conversation_id="conv-1", role=MessageRole.ASSISTANT, content="hi",
            citations=None, created_at=datetime.now(UTC),
        )}

    try:
        with patch("app.api.chat.ChatService") as service_class, patch("app.api.chat.ChatClient") as client_class:
            service_class.return_value.send_message_stream.side_effect = fake_stream
            TestClient(app).post("/chat/stream", json={"message": "hi", "model": "dolphin-mistral"})
    finally:
        app.dependency_overrides.clear()

    client_class.assert_called_once_with(model="dolphin-mistral")
