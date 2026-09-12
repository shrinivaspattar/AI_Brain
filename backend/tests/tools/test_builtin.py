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
        "remember",
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

    assert result.content == "[1] notes.txt: hello"
    assert result.is_error is False


def test_search_knowledge_base_reports_no_results() -> None:
    db = MagicMock()

    with patch("app.tools.builtin.RetrievalService") as retrieval_service_class:
        retrieval_service_class.return_value.search.return_value = []

        registry = build_default_registry(db)

    result = registry.call("search_knowledge_base", {"query": "nothing"})

    assert result.content == "No relevant documents found."


def test_get_current_datetime_returns_iso_format() -> None:
    db = MagicMock()
    registry = build_default_registry(db)

    result = registry.call("get_current_datetime", {})

    # Should parse as ISO 8601 without raising.
    from datetime import datetime

    datetime.fromisoformat(result.content)
    assert result.is_error is False


def test_list_recent_documents_formats_results() -> None:
    db = MagicMock()

    document = Document(
        id="doc-1", title="notes.txt", source="/documents/notes.txt", source_type="txt"
    )

    with patch("app.tools.builtin.DocumentService") as document_service_class:
        document_service_class.return_value.list_documents.return_value = [document]

        registry = build_default_registry(db)

    result = registry.call("list_recent_documents", {})

    assert result.content == "notes.txt (txt) - /documents/notes.txt"


def test_list_recent_documents_reports_none_ingested() -> None:
    db = MagicMock()

    with patch("app.tools.builtin.DocumentService") as document_service_class:
        document_service_class.return_value.list_documents.return_value = []

        registry = build_default_registry(db)

    result = registry.call("list_recent_documents", {})

    assert result.content == "No documents have been ingested yet."


def test_remember_proposes_a_pending_memory() -> None:
    db = MagicMock()

    with patch("app.tools.builtin.MemoryService") as memory_service_class:
        proposed = MagicMock()
        proposed.id = 7
        memory_service_class.return_value.propose_memory.return_value = proposed

        registry = build_default_registry(db, conversation_id="conv-1")

    result = registry.call(
        "remember",
        {"content": "The user's name is Alex.", "confidence": 0.9},
    )

    memory_service_class.return_value.propose_memory.assert_called_once_with(
        content="The user's name is Alex.",
        confidence=0.9,
        conversation_id="conv-1",
    )
    assert result.is_error is False
    assert "pending review" in result.content
    assert "The user's name is Alex." in result.content


def test_remember_invokes_on_memory_proposed_callback() -> None:
    db = MagicMock()
    proposed_ids: list[int] = []

    with patch("app.tools.builtin.MemoryService") as memory_service_class:
        proposed = MagicMock()
        proposed.id = 42
        memory_service_class.return_value.propose_memory.return_value = proposed

        registry = build_default_registry(
            db,
            conversation_id="conv-1",
            on_memory_proposed=proposed_ids.append,
        )

    registry.call("remember", {"content": "test fact"})

    assert proposed_ids == [42]


def test_remember_works_without_a_callback() -> None:
    """on_memory_proposed is optional - registry.call() must not raise
    when it's absent (e.g. a caller that doesn't care about linkage)."""
    db = MagicMock()

    with patch("app.tools.builtin.MemoryService") as memory_service_class:
        proposed = MagicMock()
        proposed.id = 1
        memory_service_class.return_value.propose_memory.return_value = proposed

        registry = build_default_registry(db)

    result = registry.call("remember", {"content": "test fact"})

    assert result.is_error is False
