from unittest.mock import MagicMock

import pytest

from app.dedup.service import DeduplicationService
from app.models.document import Document


def _document(doc_id: str, title: str, content_hash: str | None) -> Document:
    return Document(
        id=doc_id,
        title=title,
        source=f"/documents/{title}",
        source_type="txt",
        content_hash=content_hash,
    )


def test_find_exact_duplicates_groups_by_hash() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    # first execute() call: the "which hashes repeat" query
    db.execute.return_value.all.return_value = [("hash-a",)]

    doc1 = _document("doc-1", "a.txt", "hash-a")
    doc2 = _document("doc-2", "b.txt", "hash-a")
    db.scalars.return_value = [doc1, doc2]

    groups = service.find_exact_duplicates()

    assert len(groups) == 1
    assert groups[0].content_hash == "hash-a"
    assert groups[0].documents == [doc1, doc2]


def test_find_exact_duplicates_returns_empty_when_no_hashes_repeat() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = []

    groups = service.find_exact_duplicates()

    assert groups == []
    db.scalars.assert_not_called()


def test_find_exact_duplicates_groups_multiple_hashes_separately() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = [("hash-a",), ("hash-b",)]

    doc1 = _document("doc-1", "a1.txt", "hash-a")
    doc2 = _document("doc-2", "a2.txt", "hash-a")
    doc3 = _document("doc-3", "b1.txt", "hash-b")
    doc4 = _document("doc-4", "b2.txt", "hash-b")
    db.scalars.return_value = [doc1, doc2, doc3, doc4]

    groups = service.find_exact_duplicates()

    assert {g.content_hash for g in groups} == {"hash-a", "hash-b"}
    by_hash = {g.content_hash: g.documents for g in groups}
    assert by_hash["hash-a"] == [doc1, doc2]
    assert by_hash["hash-b"] == [doc3, doc4]


def test_find_near_duplicate_documents_rejects_invalid_threshold() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    with pytest.raises(ValueError, match="similarity_threshold"):
        service.find_near_duplicate_documents(similarity_threshold=0)

    with pytest.raises(ValueError, match="similarity_threshold"):
        service.find_near_duplicate_documents(similarity_threshold=1.5)


def test_find_near_duplicate_documents_returns_empty_when_no_pairs() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = []

    pairs = service.find_near_duplicate_documents()

    assert pairs == []


def test_find_near_duplicate_documents_builds_pairs_with_similarity() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = [("doc-1", "doc-2", 0.05)]

    doc1 = _document("doc-1", "report-v1.pdf", None)
    doc2 = _document("doc-2", "report-v2.pdf", None)
    db.scalars.return_value = [doc1, doc2]

    pairs = service.find_near_duplicate_documents()

    assert len(pairs) == 1
    assert pairs[0].document_a is doc1
    assert pairs[0].document_b is doc2
    assert pairs[0].similarity == pytest.approx(0.95)
