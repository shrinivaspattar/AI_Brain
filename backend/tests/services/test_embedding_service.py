from unittest.mock import MagicMock

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.services.embedding_service import EmbeddingService


def test_embed_document_persists_chunks_with_embeddings() -> None:
    db = MagicMock()

    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1, 0.2], [0.3, 0.4]]

    service = EmbeddingService(db, embedding_client=embedding_client)

    document = Document(
        id="doc-1",
        title="Test",
        source="/documents/test.txt",
        source_type="text",
    )

    content = "a" * 1500

    result = service.embed_document(document, content)

    assert len(result) == 2
    assert all(isinstance(chunk, DocumentChunk) for chunk in result)
    assert [chunk.chunk_index for chunk in result] == [0, 1]
    assert [chunk.document_id for chunk in result] == ["doc-1", "doc-1"]
    assert result[0].embedding == [0.1, 0.2]
    assert result[1].embedding == [0.3, 0.4]

    db.add_all.assert_called_once_with(result)
    db.commit.assert_called()
    db.rollback.assert_not_called()


def test_embed_document_clears_existing_chunks_first() -> None:
    db = MagicMock()

    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1, 0.2]]

    service = EmbeddingService(db, embedding_client=embedding_client)

    document = Document(
        id="doc-1",
        title="Test",
        source="/documents/test.txt",
        source_type="text",
    )

    service.embed_document(document, "hello AI_Brain")

    db.execute.assert_called_once()


def test_embed_document_returns_empty_list_for_empty_content() -> None:
    db = MagicMock()
    embedding_client = MagicMock()

    service = EmbeddingService(db, embedding_client=embedding_client)

    document = Document(
        id="doc-1",
        title="Test",
        source="/documents/test.txt",
        source_type="text",
    )

    result = service.embed_document(document, "   ")

    assert result == []
    embedding_client.embed.assert_not_called()
    db.add_all.assert_not_called()
    db.commit.assert_called_once()


def test_embed_document_rolls_back_on_failure() -> None:
    db = MagicMock()

    embedding_client = MagicMock()
    embedding_client.embed.side_effect = RuntimeError("ollama unreachable")

    service = EmbeddingService(db, embedding_client=embedding_client)

    document = Document(
        id="doc-1",
        title="Test",
        source="/documents/test.txt",
        source_type="text",
    )

    try:
        service.embed_document(document, "hello AI_Brain")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected RuntimeError")

    db.rollback.assert_called_once()
    db.add_all.assert_not_called()
