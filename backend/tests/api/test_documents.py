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


def _mock_document(doc_id: str, title: str, import_job_id: int | None) -> MagicMock:
    document = MagicMock()
    document.id = doc_id
    document.title = title
    document.source = f"/documents/{title}"
    document.source_type = "txt"
    document.content_hash = None
    document.import_job_id = import_job_id
    document.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    document.updated_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    return document


def _mock_import_job(job_id: int) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.name = "Job"
    job.source_path = "/src"
    job.source_type = "filesystem"
    job.status = "COMPLETED"
    job.progress = 100.0
    job.files_discovered = 1
    job.files_processed = 1
    job.error_message = None
    job.started_at = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    job.finished_at = datetime(2026, 8, 12, 9, 5, tzinfo=UTC)
    job.created_at = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    job.updated_at = datetime(2026, 8, 12, 9, 5, tzinfo=UTC)
    return job


def test_get_document_provenance() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.documents.ProvenanceService") as service_class:
            from app.provenance.service import CitingMessage, DocumentProvenance

            document = _mock_document("doc-1", "a.txt", import_job_id=5)
            import_job = _mock_import_job(5)

            service_class.return_value.trace_document.return_value = DocumentProvenance(
                document=document,
                import_job=import_job,
                chunk_count=3,
                cited_in=[
                    CitingMessage(
                        message_id=10,
                        conversation_id="conv-1",
                        created_at=datetime(2026, 8, 12, 10, 30, tzinfo=UTC),
                    )
                ],
            )

            client = TestClient(app)

            response = client.get("/documents/doc-1/provenance")

            assert response.status_code == 200
            body = response.json()
            assert body["document"]["id"] == "doc-1"
            assert body["import_job"]["id"] == 5
            assert body["chunk_count"] == 3
            assert len(body["cited_in"]) == 1
            assert body["cited_in"][0]["message_id"] == 10
            assert body["cited_in"][0]["conversation_id"] == "conv-1"

            service_class.return_value.trace_document.assert_called_once_with("doc-1")

    finally:
        app.dependency_overrides.clear()


def test_get_document_provenance_without_import_job_or_citations() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.documents.ProvenanceService") as service_class:
            from app.provenance.service import DocumentProvenance

            document = _mock_document("doc-1", "a.txt", import_job_id=None)

            service_class.return_value.trace_document.return_value = DocumentProvenance(
                document=document,
                import_job=None,
                chunk_count=0,
                cited_in=[],
            )

            client = TestClient(app)

            response = client.get("/documents/doc-1/provenance")

            assert response.status_code == 200
            body = response.json()
            assert body["import_job"] is None
            assert body["chunk_count"] == 0
            assert body["cited_in"] == []

    finally:
        app.dependency_overrides.clear()


def test_get_document_provenance_returns_404_for_missing_document() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.documents.ProvenanceService") as service_class:
            service_class.return_value.trace_document.side_effect = ValueError(
                "Document missing-id not found"
            )

            client = TestClient(app)

            response = client.get("/documents/missing-id/provenance")

            assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()
