from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


def _mock_document(doc_id: str, title: str, content_hash: str | None) -> MagicMock:
    document = MagicMock()
    document.id = doc_id
    document.title = title
    document.source = f"/documents/{title}"
    document.source_type = "txt"
    document.content_hash = content_hash
    document.import_job_id = None
    document.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    document.updated_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    return document


def test_find_exact_duplicates() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup.DeduplicationService") as service_class:
            from app.dedup.service import ExactDuplicateGroup

            doc1 = _mock_document("doc-1", "a.txt", "hash-a")
            doc2 = _mock_document("doc-2", "b.txt", "hash-a")

            service_class.return_value.find_exact_duplicates.return_value = [
                ExactDuplicateGroup(content_hash="hash-a", documents=[doc1, doc2])
            ]

            client = TestClient(app)

            response = client.get("/dedup/exact")

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["content_hash"] == "hash-a"
            assert {d["title"] for d in body[0]["documents"]} == {"a.txt", "b.txt"}

            service_class.return_value.find_exact_duplicates.assert_called_once_with(
                limit=100
            )

    finally:
        app.dependency_overrides.clear()


def test_find_exact_duplicates_returns_empty_list() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup.DeduplicationService") as service_class:
            service_class.return_value.find_exact_duplicates.return_value = []

            client = TestClient(app)

            response = client.get("/dedup/exact")

            assert response.status_code == 200
            assert response.json() == []

    finally:
        app.dependency_overrides.clear()


def test_find_near_duplicates() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup.DeduplicationService") as service_class:
            from app.dedup.service import NearDuplicatePair

            doc1 = _mock_document("doc-1", "report-v1.pdf", None)
            doc2 = _mock_document("doc-2", "report-v2.pdf", None)

            service_class.return_value.find_near_duplicate_documents.return_value = [
                NearDuplicatePair(document_a=doc1, document_b=doc2, similarity=0.97)
            ]

            client = TestClient(app)

            response = client.get("/dedup/near", params={"threshold": 0.9, "limit": 50})

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["similarity"] == 0.97
            assert body[0]["document_a"]["title"] == "report-v1.pdf"
            assert body[0]["document_b"]["title"] == "report-v2.pdf"

            service_class.return_value.find_near_duplicate_documents.assert_called_once_with(
                similarity_threshold=0.9,
                limit=50,
            )

    finally:
        app.dependency_overrides.clear()


def test_find_near_duplicates_rejects_out_of_range_threshold() -> None:
    client = TestClient(app)

    response = client.get("/dedup/near", params={"threshold": 1.5})

    assert response.status_code == 422


def test_find_exact_duplicates_rejects_out_of_range_limit() -> None:
    client = TestClient(app)

    response = client.get("/dedup/exact", params={"limit": 0})

    assert response.status_code == 422


def test_plan_exact_duplicate_cleanup() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup.DeduplicationService") as service_class:
            from app.dedup.service import DryRunAction, ExactDuplicatePlan

            keep = _mock_document("doc-1", "a.txt", "hash-a")
            duplicate = _mock_document("doc-2", "a-copy.txt", "hash-a")

            service_class.return_value.plan_exact_duplicate_cleanup.return_value = [
                ExactDuplicatePlan(
                    content_hash="hash-a",
                    keep=keep,
                    actions=[
                        DryRunAction(
                            action="delete",
                            document=duplicate,
                            reason="identical to kept copy 'a.txt'",
                        )
                    ],
                )
            ]

            client = TestClient(app)

            response = client.get("/dedup/exact/plan")

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["keep"]["title"] == "a.txt"
            assert len(body[0]["actions"]) == 1
            assert body[0]["actions"][0]["action"] == "delete"
            assert body[0]["actions"][0]["document"]["title"] == "a-copy.txt"

            service_class.return_value.plan_exact_duplicate_cleanup.assert_called_once_with(
                limit=100
            )

    finally:
        app.dependency_overrides.clear()


def test_plan_exact_duplicate_cleanup_returns_empty_list() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup.DeduplicationService") as service_class:
            service_class.return_value.plan_exact_duplicate_cleanup.return_value = []

            client = TestClient(app)

            response = client.get("/dedup/exact/plan")

            assert response.status_code == 200
            assert response.json() == []

    finally:
        app.dependency_overrides.clear()
