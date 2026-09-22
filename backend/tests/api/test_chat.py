from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.embeddings.client import EmbeddingUnavailableError
from app.main import app
from app.services.chat_client import ChatUnavailableError


def test_send_message_returns_reply_with_citations() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "According to [1], AI_Brain is offline-first."
            message.citations = [
                {
                    "document_chunk_id": 1,
                    "document_id": "doc-1",
                    "document_title": "notes.txt",
                    "document_source": "/documents/notes.txt",
                }
            ]
            message.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

            service_class.return_value.send_message.return_value = message

            client = TestClient(app)

            response = client.post(
                "/chat",
                json={"message": "what is AI_Brain?"},
            )

            assert response.status_code == 200
            assert response.json() == {
                "conversation_id": "conv-1",
                "message": {
                    "id": 2,
                    "conversation_id": "conv-1",
                    "role": "assistant",
                    "content": "According to [1], AI_Brain is offline-first.",
                    "citations": [
                        {
                            "document_chunk_id": 1,
                            "document_id": "doc-1",
                            "document_title": "notes.txt",
                            "document_source": "/documents/notes.txt",
                            "source_occurrences": None,
                        }
                    ],
                    "created_at": "2026-08-12T10:00:00Z",
                },
            }

            service_class.return_value.send_message.assert_called_once_with(
                "what is AI_Brain?",
                conversation_id=None,
                top_k=5,
            )

    finally:
        app.dependency_overrides.clear()


def test_send_message_returns_not_found_for_unknown_conversation() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message.side_effect = ValueError(
                "Conversation conv-404 not found"
            )

            client = TestClient(app)

            response = client.post(
                "/chat",
                json={"message": "hi", "conversation_id": "conv-404"},
            )

            assert response.status_code == 404
            assert response.json() == {
                "detail": "Conversation conv-404 not found"
            }

    finally:
        app.dependency_overrides.clear()


def test_send_message_returns_service_unavailable_when_ollama_fails() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message.side_effect = (
                ChatUnavailableError("connection refused")
            )

            client = TestClient(app)

            response = client.post("/chat", json={"message": "hi"})

            assert response.status_code == 503
            assert "connection refused" in response.json()["detail"]

    finally:
        app.dependency_overrides.clear()


def test_send_message_returns_service_unavailable_when_embedding_fails() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message.side_effect = (
                EmbeddingUnavailableError("connection refused")
            )

            client = TestClient(app)

            response = client.post("/chat", json={"message": "hi"})

            assert response.status_code == 503
            assert response.json() == {
                "detail": "Embedding model unavailable: connection refused"
            }

    finally:
        app.dependency_overrides.clear()


def test_get_conversation_messages_returns_history() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        conversation = MagicMock()
        db.get.return_value = conversation

        message = MagicMock()
        message.id = 1
        message.conversation_id = "conv-1"
        message.role = "user"
        message.content = "hello"
        message.citations = None
        message.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

        db.scalars.return_value = [message]

        client = TestClient(app)

        response = client.get("/chat/conv-1")

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": 1,
                "conversation_id": "conv-1",
                "role": "user",
                "content": "hello",
                "citations": None,
                "created_at": "2026-08-12T10:00:00Z",
            }
        ]

    finally:
        app.dependency_overrides.clear()


def test_get_conversation_messages_returns_not_found() -> None:
    db = MagicMock()
    db.get.return_value = None

    app.dependency_overrides[get_db] = lambda: db

    try:
        client = TestClient(app)

        response = client.get("/chat/does-not-exist")

        assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


# ---- selectable chat model ----

from app.api.chat import resolve_chat_model  # noqa: E402
from app.core.config import settings  # noqa: E402


def test_resolve_chat_model_passes_through_an_allowed_name() -> None:
    assert resolve_chat_model("qwen3:4b") in settings.available_chat_models()
    assert resolve_chat_model("qwen3:4b") == "qwen3:4b"


def test_resolve_chat_model_falls_back_for_none_or_unknown_names() -> None:
    assert resolve_chat_model(None) is None
    assert resolve_chat_model("") is None
    assert resolve_chat_model("some-model-nobody-configured") is None


def test_list_available_models_returns_the_configured_allowlist_and_default() -> None:
    response = TestClient(app).get("/chat/models")

    assert response.status_code == 200
    body = response.json()
    assert body["default"] == settings.CHAT_MODEL
    assert settings.CHAT_MODEL in body["models"]
    assert body["models"] == settings.available_chat_models()


def test_send_message_uses_the_requested_model() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class, patch("app.api.chat.ChatClient") as client_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "hi"
            message.citations = None
            message.created_at = datetime.now(UTC)
            service_class.return_value.send_message.return_value = message

            response = TestClient(app).post(
                "/chat", json={"message": "hi", "model": "qwen3:4b"}
            )

        assert response.status_code == 200
        client_class.assert_called_once_with(model="qwen3:4b")
    finally:
        app.dependency_overrides.clear()


def test_send_message_ignores_an_unknown_model_name() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class, patch("app.api.chat.ChatClient") as client_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "hi"
            message.citations = None
            message.created_at = datetime.now(UTC)
            service_class.return_value.send_message.return_value = message

            TestClient(app).post("/chat", json={"message": "hi", "model": "not-a-real-model"})

        client_class.assert_called_once_with(model=None)
    finally:
        app.dependency_overrides.clear()
