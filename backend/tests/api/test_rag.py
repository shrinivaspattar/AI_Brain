from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.embeddings.client import EmbeddingUnavailableError
from app.main import app


def test_search_returns_ranked_results() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.rag.RetrievalService") as service_class:
            result = MagicMock()
            result.chunk.id = 1
            result.chunk.chunk_index = 0
            result.chunk.content = "hello AI_Brain"
            result.document.id = "doc-1"
            result.document.title = "notes.txt"
            result.document.source = "/documents/notes.txt"
            result.distance = 0.2
            result.source_occurrences = None

            service_class.return_value.search.return_value = [result]

            client = TestClient(app)

            response = client.post(
                "/rag/search",
                json={"query": "hello", "top_k": 3},
            )

            assert response.status_code == 200
            assert response.json() == {
                "results": [
                    {
                        "chunk_id": 1,
                        "document_id": "doc-1",
                        "document_title": "notes.txt",
                        "document_source": "/documents/notes.txt",
                        "chunk_index": 0,
                        "content": "hello AI_Brain",
                        "score": 0.8,
                        "source_occurrences": None,
                    }
                ]
            }

            service_class.return_value.search.assert_called_once_with(
                "hello", top_k=3
            )

    finally:
        app.dependency_overrides.clear()


def test_search_uses_default_top_k() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.rag.RetrievalService") as service_class:
            service_class.return_value.search.return_value = []

            client = TestClient(app)

            response = client.post("/rag/search", json={"query": "hello"})

            assert response.status_code == 200
            assert response.json() == {"results": []}

            service_class.return_value.search.assert_called_once_with(
                "hello", top_k=5
            )

    finally:
        app.dependency_overrides.clear()


def test_search_rejects_top_k_out_of_range() -> None:
    client = TestClient(app)

    response = client.post(
        "/rag/search",
        json={"query": "hello", "top_k": 0},
    )

    assert response.status_code == 422


def test_search_returns_service_unavailable_when_embedding_fails() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.rag.RetrievalService") as service_class:
            service_class.return_value.search.side_effect = EmbeddingUnavailableError(
                "connection refused"
            )

            client = TestClient(app)

            response = client.post("/rag/search", json={"query": "hello"})

            assert response.status_code == 503
            assert response.json() == {
                "detail": "Embedding model unavailable: connection refused"
            }

    finally:
        app.dependency_overrides.clear()


def test_search_does_not_convert_unrelated_exceptions_to_503() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.rag.RetrievalService") as service_class:
            service_class.return_value.search.side_effect = RuntimeError("boom")

            client = TestClient(app)

            with pytest.raises(RuntimeError, match="boom"):
                client.post("/rag/search", json={"query": "hello"})

    finally:
        app.dependency_overrides.clear()
