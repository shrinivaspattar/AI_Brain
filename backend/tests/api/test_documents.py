from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


def test_create_document() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.documents.DocumentService") as service_class:
            document = MagicMock()
            document.id = "test-document-id"
            document.title = "Test Document"
            document.source = "/documents/test.txt"
            document.source_type = "text"
            document.content_hash = None
            document.import_job_id = None
            document.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
            document.updated_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

            service_class.return_value.create_document.return_value = document

            client = TestClient(app)

            response = client.post(
                "/documents",
                json={
                    "title": "Test Document",
                    "source": "/documents/test.txt",
                    "source_type": "text",
                },
            )

            assert response.status_code == 201
            assert response.json() == {
                "id": "test-document-id",
                "title": "Test Document",
                "source": "/documents/test.txt",
                "source_type": "text",
                "content_hash": None,
                "import_job_id": None,
                "created_at": "2026-08-12T10:00:00Z",
                "updated_at": "2026-08-12T10:00:00Z",
            }

            service_class.return_value.create_document.assert_called_once()

    finally:
        app.dependency_overrides.clear()
