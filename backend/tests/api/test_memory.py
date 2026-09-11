from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


def test_create_memory() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.memory.MemoryService") as service_class:
            memory = MagicMock()
            memory.id = 1
            memory.content = "User prefers dark mode."
            memory.confidence = None
            memory.conversation_id = None
            memory.message_id = None
            memory.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
            memory.updated_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

            service_class.return_value.create_memory.return_value = memory

            client = TestClient(app)

            response = client.post(
                "/memory",
                json={"content": "User prefers dark mode."},
            )

            assert response.status_code == 201
            assert response.json() == {
                "id": 1,
                "content": "User prefers dark mode.",
                "confidence": None,
                "conversation_id": None,
                "message_id": None,
                "created_at": "2026-08-12T10:00:00Z",
                "updated_at": "2026-08-12T10:00:00Z",
            }

            service_class.return_value.create_memory.assert_called_once()

    finally:
        app.dependency_overrides.clear()


def test_create_memory_rejects_confidence_out_of_range() -> None:
    client = TestClient(app)

    response = client.post(
        "/memory",
        json={"content": "test", "confidence": 1.5},
    )

    assert response.status_code == 422


def test_list_memories() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.memory.MemoryService") as service_class:
            memory = MagicMock()
            memory.id = 1
            memory.content = "test"
            memory.confidence = 0.8
            memory.conversation_id = "conv-1"
            memory.message_id = 3
            memory.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
            memory.updated_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

            service_class.return_value.list_memories.return_value = [memory]

            client = TestClient(app)

            response = client.get("/memory")

            assert response.status_code == 200
            assert response.json() == [
                {
                    "id": 1,
                    "content": "test",
                    "confidence": 0.8,
                    "conversation_id": "conv-1",
                    "message_id": 3,
                    "created_at": "2026-08-12T10:00:00Z",
                    "updated_at": "2026-08-12T10:00:00Z",
                }
            ]

    finally:
        app.dependency_overrides.clear()


def test_delete_memory_returns_no_content() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.memory.MemoryService") as service_class:
            client = TestClient(app)

            response = client.delete("/memory/1")

            assert response.status_code == 204
            service_class.return_value.delete_memory.assert_called_once_with(1)

    finally:
        app.dependency_overrides.clear()


def test_delete_memory_returns_not_found() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.memory.MemoryService") as service_class:
            service_class.return_value.delete_memory.side_effect = ValueError(
                "Memory 42 not found"
            )

            client = TestClient(app)

            response = client.delete("/memory/42")

            assert response.status_code == 404
            assert response.json() == {"detail": "Memory 42 not found"}

    finally:
        app.dependency_overrides.clear()
