from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.dedup.service import DeduplicationService
from app.models.document import Document


def _document(
    doc_id: str,
    title: str,
    content_hash: str | None,
    created_at: datetime | None = None,
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        source=f"/documents/{title}",
        source_type="txt",
        content_hash=content_hash,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
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


def test_plan_exact_duplicate_cleanup_keeps_oldest_copy() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = [("hash-a",)]

    older = _document("doc-1", "original.txt", "hash-a", datetime(2026, 1, 1, tzinfo=UTC))
    newer = _document("doc-2", "original-copy.txt", "hash-a", datetime(2026, 2, 1, tzinfo=UTC))
    # Deliberately returned out of chronological order, to prove the
    # plan sorts for itself rather than trusting caller ordering.
    db.scalars.return_value = [newer, older]

    plans = service.plan_exact_duplicate_cleanup()

    assert len(plans) == 1
    plan = plans[0]
    assert plan.content_hash == "hash-a"
    assert plan.keep is older
    assert len(plan.actions) == 1
    assert plan.actions[0].action == "delete"
    assert plan.actions[0].document is newer
    assert "original.txt" in plan.actions[0].reason


def test_plan_exact_duplicate_cleanup_breaks_ties_by_id() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = [("hash-a",)]

    same_time = datetime(2026, 1, 1, tzinfo=UTC)
    doc_b = _document("doc-b", "b.txt", "hash-a", same_time)
    doc_a = _document("doc-a", "a.txt", "hash-a", same_time)
    db.scalars.return_value = [doc_b, doc_a]

    plans = service.plan_exact_duplicate_cleanup()

    assert plans[0].keep is doc_a
    assert plans[0].actions[0].document is doc_b


def test_plan_exact_duplicate_cleanup_returns_empty_when_no_duplicates() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = []

    assert service.plan_exact_duplicate_cleanup() == []


def test_plan_exact_duplicate_cleanup_handles_multiple_extra_copies() -> None:
    db = MagicMock()
    service = DeduplicationService(db)

    db.execute.return_value.all.return_value = [("hash-a",)]

    keep = _document("doc-1", "a.txt", "hash-a", datetime(2026, 1, 1, tzinfo=UTC))
    copy1 = _document("doc-2", "a-copy1.txt", "hash-a", datetime(2026, 1, 2, tzinfo=UTC))
    copy2 = _document("doc-3", "a-copy2.txt", "hash-a", datetime(2026, 1, 3, tzinfo=UTC))
    db.scalars.return_value = [keep, copy1, copy2]

    plans = service.plan_exact_duplicate_cleanup()

    assert plans[0].keep is keep
    assert {a.document for a in plans[0].actions} == {copy1, copy2}
    assert all(a.action == "delete" for a in plans[0].actions)
