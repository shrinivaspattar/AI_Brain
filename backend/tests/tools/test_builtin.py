from unittest.mock import MagicMock, patch

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.retrieval_service import RetrievedChunk
from app.tools.builtin import build_default_registry


def test_build_default_registry_registers_expected_tools() -> None:
    db = MagicMock()

    registry = build_default_registry(db)

    names = {tool.name for tool in registry.list_tools()}
    assert names == {
        "search_knowledge_base",
        "get_current_datetime",
        "list_recent_documents",
    }


def test_search_knowledge_base_formats_results() -> None:
    db = MagicMock()

    chunk = DocumentChunk(id=1, document_id="doc-1", chunk_index=0, content="hello")
    document = Document(
        id="doc-1", title="notes.txt", source="/documents/notes.txt", source_type="txt"
    )

    with patch("app.tools.builtin.RetrievalService") as retrieval_service_class:
        retrieval_service_class.return_value.search.return_value = [
            RetrievedChunk(chunk=chunk, document=document, distance=0.1)
        ]

        registry = build_default_registry(db)

    result = registry.call("search_knowledge_base", {"query": "hello"})

    assert result == "[1] notes.txt: hello"


def test_search_knowledge_base_reports_no_results() -> None:
    db = MagicMock()

    with patch("app.tools.builtin.RetrievalService") as retrieval_service_class:
        retrieval_service_class.return_value.search.return_value = []

        registry = build_default_registry(db)

    result = registry.call("search_knowledge_base", {"query": "nothing"})

    assert result == "No relevant documents found."


def test_get_current_datetime_returns_iso_format() -> None:
    db = MagicMock()
    registry = build_default_registry(db)

    result = registry.call("get_current_datetime", {})

    # Should parse as ISO 8601 without raising.
    from datetime import datetime

    datetime.fromisoformat(result)


def test_list_recent_documents_formats_results() -> None:
    db = MagicMock()

    document = Document(
        id="doc-1", title="notes.txt", source="/documents/notes.txt", source_type="txt"
    )

    with patch("app.tools.builtin.DocumentService") as document_service_class:
        document_service_class.return_value.list_documents.return_value = [document]

        registry = build_default_registry(db)

    result = registry.call("list_recent_documents", {})

    assert result == "notes.txt (txt) - /documents/notes.txt"


def test_list_recent_documents_reports_none_ingested() -> None:
    db = MagicMock()

    with patch("app.tools.builtin.DocumentService") as document_service_class:
        document_service_class.return_value.list_documents.return_value = []

        registry = build_default_registry(db)

    result = registry.call("list_recent_documents", {})

    assert result == "No documents have been ingested yet."
