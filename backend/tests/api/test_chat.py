from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
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
