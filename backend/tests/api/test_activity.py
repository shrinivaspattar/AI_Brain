from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models.ingestion_batch import BatchStatus


def _conversation(conv_id: str, title: str) -> MagicMock:
    conversation = MagicMock()
    conversation.id = conv_id
    conversation.title = title
    conversation.created_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    conversation.updated_at = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    return conversation


def _attachment(att_id: str, filename: str) -> MagicMock:
    attachment = MagicMock()
    attachment.id = att_id
    attachment.original_filename = filename
    attachment.byte_size = 1234
    attachment.created_at = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    return attachment


def _batch(batch_id: int, status: BatchStatus) -> MagicMock:
    batch = MagicMock()
    batch.id = batch_id
    batch.status = status
    batch.source_instances_selected = 42
    batch.created_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    batch.completed_at = datetime(2026, 9, 4, 11, 0, tzinfo=UTC) if status == BatchStatus.COMPLETED else None
    return batch


def test_recent_activity_returns_conversations_attachments_and_batches() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        db.scalars.side_effect = [
            [_conversation("conv-1", "hello")],
            [_attachment("att-1", "notes.txt")],
            [_batch(5, BatchStatus.COMPLETED)],
        ]

        response = TestClient(app).get("/activity/recent")

        assert response.status_code == 200
        body = response.json()
        assert body["recent_conversations"] == [
            {
                "id": "conv-1",
                "title": "hello",
                "created_at": "2026-09-01T10:00:00Z",
                "updated_at": "2026-09-02T10:00:00Z",
            }
        ]
        assert body["recent_attachments"] == [
            {
                "id": "att-1",
                "filename": "notes.txt",
                "byte_size": 1234,
                "created_at": "2026-09-03T10:00:00Z",
            }
        ]
        assert body["recent_ingestion_batches"] == [
            {
                "id": 5,
                "status": "completed",
                "source_instances_selected": 42,
                "created_at": "2026-09-04T10:00:00Z",
                "completed_at": "2026-09-04T11:00:00Z",
            }
        ]
    finally:
        app.dependency_overrides.clear()


def test_recent_activity_returns_empty_lists_when_nothing_exists() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        db.scalars.side_effect = [[], [], []]

        response = TestClient(app).get("/activity/recent")

        assert response.status_code == 200
        assert response.json() == {
            "recent_conversations": [],
            "recent_attachments": [],
            "recent_ingestion_batches": [],
        }
    finally:
        app.dependency_overrides.clear()
