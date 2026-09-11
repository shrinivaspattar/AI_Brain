from unittest.mock import MagicMock

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.retrieval_service import RetrievalService


def test_search_returns_empty_list_for_blank_query() -> None:
    db = MagicMock()
    embedding_client = MagicMock()

    service = RetrievalService(db, embedding_client=embedding_client)

    assert service.search("   ") == []
    embedding_client.embed.assert_not_called()
    db.execute.assert_not_called()


def test_search_returns_empty_list_for_non_positive_top_k() -> None:
    db = MagicMock()
    embedding_client = MagicMock()

    service = RetrievalService(db, embedding_client=embedding_client)

    assert service.search("hello", top_k=0) == []
    embedding_client.embed.assert_not_called()


def test_search_embeds_query_and_returns_ranked_results() -> None:
    db = MagicMock()

    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1] * 768]

    chunk = DocumentChunk(
        id=1,
        document_id="doc-1",
        chunk_index=0,
        content="hello AI_Brain",
    )
    document = Document(
        id="doc-1",
        title="notes.txt",
        source="/documents/notes.txt",
        source_type="txt",
    )

    db.execute.return_value.all.return_value = [(chunk, document, 0.2)]

    service = RetrievalService(db, embedding_client=embedding_client)

    results = service.search("hello", top_k=3)

    assert len(results) == 1
    assert results[0].chunk is chunk
    assert results[0].document is document
    assert results[0].distance == 0.2

    embedding_client.embed.assert_called_once_with(["hello"])
