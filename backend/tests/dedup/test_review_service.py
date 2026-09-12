from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup, NearDuplicatePair
from app.models.dedup_review import (
    DuplicateMatchType,
    DuplicateReview,
    DuplicateReviewMemberRole,
    DuplicateReviewStatus,
)
from app.models.document import Document


def _document(
    doc_id: str,
    title: str,
    content_hash: str | None = None,
    created_at: datetime | None = None,
    import_job_id: int | None = None,
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        source=f"/documents/{title}",
        source_type="txt",
        content_hash=content_hash,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        import_job_id=import_job_id,
    )


# --- exact duplicate candidate -------------------------------------------


def test_create_review_from_exact_group_proposes_oldest_as_canonical() -> None:
    db = MagicMock()
    service = DedupReviewService(db)

    older = _document(
        "doc-1", "a.txt", "hash-a", datetime(2026, 1, 1, tzinfo=UTC), import_job_id=5
    )
    newer = _document(
        "doc-2", "a-copy.txt", "hash-a", datetime(2026, 2, 1, tzinfo=UTC)
    )
    group = ExactDuplicateGroup(content_hash="hash-a", documents=[newer, older])

    review = service.create_review_from_exact_group(group)

    assert isinstance(review, DuplicateReview)
    assert review.match_type == DuplicateMatchType.EXACT
    assert review.content_hash == "hash-a"
    assert review.similarity is None
    assert review.confidence == 1.0
    assert review.status == DuplicateReviewStatus.PENDING
    assert "a.txt" in review.recommendation_reason

    added_members = [
        call.args[0]
        for call in db.add.call_args_list
        if hasattr(call.args[0], "role")
    ]
    canonical_members = [
        m for m in added_members if m.role == DuplicateReviewMemberRole.PROPOSED_CANONICAL
    ]
    duplicate_members = [
        m for m in added_members if m.role == DuplicateReviewMemberRole.DUPLICATE
    ]
    assert len(canonical_members) == 1
    assert canonical_members[0].document_id == "doc-1"
    assert len(duplicate_members) == 1
    assert duplicate_members[0].document_id == "doc-2"


def test_create_review_from_exact_group_evidence_snapshots_member_metadata() -> None:
    db = MagicMock()
    service = DedupReviewService(db)

    doc_a = _document(
        "doc-1", "a.txt", "hash-a", datetime(2026, 1, 1, tzinfo=UTC), import_job_id=7
    )
    doc_b = _document("doc-2", "a-copy.txt", "hash-a", datetime(2026, 2, 1, tzinfo=UTC))
    group = ExactDuplicateGroup(content_hash="hash-a", documents=[doc_a, doc_b])

    review = service.create_review_from_exact_group(group)

    assert review.evidence["match_type"] == "exact"
    assert review.evidence["content_hash"] == "hash-a"
    member_ids = {m["document_id"] for m in review.evidence["members"]}
    assert member_ids == {"doc-1", "doc-2"}
    doc_a_evidence = next(
        m for m in review.evidence["members"] if m["document_id"] == "doc-1"
    )
    assert doc_a_evidence["import_job_id"] == 7
    assert doc_a_evidence["title"] == "a.txt"


def test_create_review_from_exact_group_rejects_degenerate_group() -> None:
    db = MagicMock()
    service = DedupReviewService(db)

    group = ExactDuplicateGroup(
        content_hash="hash-a", documents=[_document("doc-1", "a.txt", "hash-a")]
    )

    with pytest.raises(ValueError, match="at least two documents"):
        service.create_review_from_exact_group(group)

    db.add.assert_not_called()


# --- near duplicate candidate / ambiguous candidate ----------------------


def test_create_review_from_near_pair_proposes_no_canonical() -> None:
    """The 'ambiguous candidate' case: a near-duplicate review is always
    created without a PROPOSED_CANONICAL member, by design."""
    db = MagicMock()
    service = DedupReviewService(db)

    doc_a = _document("doc-1", "report-v1.pdf")
    doc_b = _document("doc-2", "report-v2.pdf")
    pair = NearDuplicatePair(document_a=doc_a, document_b=doc_b, similarity=0.97)

    review = service.create_review_from_near_pair(pair)

    assert review.match_type == DuplicateMatchType.NEAR
    assert review.content_hash is None
    assert review.similarity == 0.97
    assert review.confidence == 0.97
    assert "No canonical copy is proposed" in review.recommendation_reason

    added_members = [
        call.args[0]
        for call in db.add.call_args_list
        if hasattr(call.args[0], "role")
    ]
    assert all(
        m.role == DuplicateReviewMemberRole.DUPLICATE for m in added_members
    )
    assert len(added_members) == 2


def test_create_review_from_near_pair_evidence_notes_no_canonical_selection() -> None:
    db = MagicMock()
    service = DedupReviewService(db)

    pair = NearDuplicatePair(
        document_a=_document("doc-1", "a.pdf"),
        document_b=_document("doc-2", "b.pdf"),
        similarity=0.9,
    )

    review = service.create_review_from_near_pair(pair)

    assert review.evidence["match_type"] == "near"
    assert review.evidence["similarity"] == 0.9
    assert "canonical_note" in review.evidence


# --- listing / lookup -----------------------------------------------------


def test_get_review_returns_none_when_missing() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = DedupReviewService(db)

    assert service.get_review(999) is None


def test_list_reviews_filters_by_status() -> None:
    db = MagicMock()
    service = DedupReviewService(db)

    service.list_reviews(status=DuplicateReviewStatus.PENDING)

    statement = db.scalars.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "duplicate_reviews.status" in compiled


# --- already-reviewed / conflicting review attempts -----------------------


def test_approve_review_sets_status_and_timestamp() -> None:
    db = MagicMock()
    review = DuplicateReview(
        id=1,
        match_type=DuplicateMatchType.EXACT,
        confidence=1.0,
        recommendation_reason="test",
        evidence={},
        status=DuplicateReviewStatus.PENDING,
    )
    db.get.return_value = review

    service = DedupReviewService(db)
    result = service.approve_review(1, reviewer_decision="Looks right.")

    assert result.status == DuplicateReviewStatus.APPROVED
    assert result.reviewer_decision == "Looks right."
    assert result.reviewed_at is not None


def test_reject_review_sets_status_and_timestamp() -> None:
    db = MagicMock()
    review = DuplicateReview(
        id=1,
        match_type=DuplicateMatchType.NEAR,
        confidence=0.9,
        recommendation_reason="test",
        evidence={},
        status=DuplicateReviewStatus.PENDING,
    )
    db.get.return_value = review

    service = DedupReviewService(db)
    result = service.reject_review(1, reviewer_decision="Not actually related.")

    assert result.status == DuplicateReviewStatus.REJECTED
    assert result.reviewer_decision == "Not actually related."
    assert result.reviewed_at is not None


def test_approve_review_on_already_rejected_review_overwrites_status() -> None:
    """Documents current, deliberate behavior (matching MemoryService's
    approve_memory/reject_memory precedent): no guard against reviewing
    an already-decided review. The frontend (not built in this
    milestone) would only ever show actions for a PENDING review, but
    the service itself places no restriction on it."""
    db = MagicMock()
    review = DuplicateReview(
        id=1,
        match_type=DuplicateMatchType.EXACT,
        confidence=1.0,
        recommendation_reason="test",
        evidence={},
        status=DuplicateReviewStatus.REJECTED,
    )
    db.get.return_value = review

    service = DedupReviewService(db)
    result = service.approve_review(1)

    assert result.status == DuplicateReviewStatus.APPROVED


def test_conflicting_sequential_review_calls_last_write_wins() -> None:
    """No optimistic-locking/version column exists on DuplicateReview,
    so two conflicting review requests processed one after another (as
    real concurrent HTTP requests would be, serialized by the database)
    both succeed without raising - whichever is applied last determines
    the final status."""
    db = MagicMock()
    review = DuplicateReview(
        id=1,
        match_type=DuplicateMatchType.EXACT,
        confidence=1.0,
        recommendation_reason="test",
        evidence={},
        status=DuplicateReviewStatus.PENDING,
    )
    db.get.return_value = review

    service = DedupReviewService(db)

    approved = service.approve_review(1, reviewer_decision="First reviewer")
    assert approved.status == DuplicateReviewStatus.APPROVED

    rejected = service.reject_review(1, reviewer_decision="Second reviewer disagrees")
    assert rejected.status == DuplicateReviewStatus.REJECTED
    assert rejected.reviewer_decision == "Second reviewer disagrees"
    assert rejected is approved  # same row, mutated in place


# --- invalid candidate ------------------------------------------------


def test_approve_review_raises_for_missing_review() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="Duplicate review 999 not found"):
        service.approve_review(999)


def test_reject_review_raises_for_missing_review() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="Duplicate review 999 not found"):
        service.reject_review(999)
