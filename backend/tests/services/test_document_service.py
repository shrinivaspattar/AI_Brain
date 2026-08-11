from unittest.mock import MagicMock

import pytest

from app.models.document import Document
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def test_create_document_persists_document() -> None:
    db = MagicMock()
    service = DocumentService(db)

    document_data = DocumentCreate(
        title="Test Document",
        source="/documents/test.txt",
        source_type="text",
    )

    result = service.create_document(document_data)

    assert isinstance(result, Document)
    assert result.title == "Test Document"
    assert result.source == "/documents/test.txt"
    assert result.source_type == "text"

    db.add.assert_called_once_with(result)
    db.commit.assert_called_once_with()
    db.refresh.assert_called_once_with(result)
    db.rollback.assert_not_called()


def test_create_document_rolls_back_on_commit_failure() -> None:
    db = MagicMock()
    db.commit.side_effect = RuntimeError("database failure")

    service = DocumentService(db)

    document_data = DocumentCreate(
        title="Test Document",
        source="/documents/test.txt",
        source_type="text",
    )

    with pytest.raises(RuntimeError, match="database failure"):
        service.create_document(document_data)

    db.add.assert_called_once()
    db.commit.assert_called_once_with()
    db.rollback.assert_called_once_with()
    db.refresh.assert_not_called()
