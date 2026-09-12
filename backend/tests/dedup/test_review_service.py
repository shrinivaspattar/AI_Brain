from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup, NearDuplicatePair
from app.models.dedup_review import (
    DuplicateMatchType,
    DuplicateReview,
    DuplicateReviewMember,
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


def _member(
    review_id: int, document_id: str, role: DuplicateReviewMemberRole
) -> DuplicateReviewMember:
    return DuplicateReviewMember(
        review_id=review_id, document_id=document_id, role=role
    )


def _pending_review(
    review_id: int = 1,
    match_type: DuplicateMatchType = DuplicateMatchType.EXACT,
    confidence: float = 1.0,
) -> DuplicateReview:
    return DuplicateReview(
        id=review_id,
        match_type=match_type,
        confidence=confidence,
        recommendation_reason="test",
        evidence={},
        status=DuplicateReviewStatus.PENDING,
    )


# --- exact duplicate candidate -------------------------------------------


def test_create_review_from_exact_group_recommends_oldest_as_canonical() -> None:
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
    assert review.human_selected_canonical_document_id is None
    assert "a.txt" in review.recommendation_reason

    added_members = [
        call.args[0]
        for call in db.add.call_args_list
        if hasattr(call.args[0], "role")
    ]
    recommended = [
        m
        for m in added_members
        if m.role == DuplicateReviewMemberRole.RECOMMENDED_CANONICAL
    ]
    duplicate_members = [
        m for m in added_members if m.role == DuplicateReviewMemberRole.DUPLICATE
    ]
    assert len(recommended) == 1
    assert recommended[0].document_id == "doc-1"
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
    # No full document content anywhere in the snapshot - metadata only.
    assert "content" not in doc_a_evidence


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


def test_create_review_from_near_pair_recommends_no_canonical() -> None:
    """The 'ambiguous candidate' case: a near-duplicate review is always
    created without a RECOMMENDED_CANONICAL member, by design."""
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
    assert review.human_selected_canonical_document_id is None
    assert "No canonical copy is proposed" in review.recommendation_reason

    added_members = [
        call.args[0]
        for call in db.add.call_args_list
        if hasattr(call.args[0], "role")
    ]
    assert all(
        m.role == DuplicateReviewMemberRole.DUPLICATE for m in added_members
    )
    assert not any(
        m.role == DuplicateReviewMemberRole.RECOMMENDED_CANONICAL
        for m in added_members
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


def test_get_review_members_with_documents_pairs_members_and_documents() -> None:
    db = MagicMock()
    service = DedupReviewService(db)

    member_a = _member(1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL)
    member_b = _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE)
    doc_a = _document("doc-1", "a.txt")
    doc_b = _document("doc-2", "a-copy.txt")

    db.scalars.side_effect = [[member_a, member_b], [doc_a, doc_b]]

    pairs = service.get_review_members_with_documents(1)

    assert len(pairs) == 2
    result_map = {member.document_id: document for member, document in pairs}
    assert result_map["doc-1"] is doc_a
    assert result_map["doc-2"] is doc_b


def test_get_review_members_with_documents_returns_empty_for_no_members() -> None:
    db = MagicMock()
    service = DedupReviewService(db)

    db.scalars.return_value = []

    assert service.get_review_members_with_documents(1) == []


# --- approve: exact requires an explicit canonical -----------------------


def test_approve_exact_review_requires_explicit_canonical() -> None:
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.EXACT)
    db.get.return_value = review
    db.scalars.return_value = [
        _member(1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
        _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
    ]

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="requires an explicit canonical_document_id"):
        service.approve_review(1)


def test_approve_exact_review_with_explicit_canonical_succeeds() -> None:
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.EXACT)
    db.get.return_value = review
    db.scalars.return_value = [
        _member(1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
        _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
    ]

    service = DedupReviewService(db)
    result = service.approve_review(
        1, canonical_document_id="doc-1", reviewer_decision="Agreed with recommendation."
    )

    assert result.status == DuplicateReviewStatus.APPROVED
    assert result.human_selected_canonical_document_id == "doc-1"
    assert result.reviewer_decision == "Agreed with recommendation."
    assert result.reviewed_at is not None


def test_approve_exact_review_rejects_canonical_not_a_member() -> None:
    """Invalid canonical selection: a document not part of this review."""
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.EXACT)
    db.get.return_value = review
    db.scalars.return_value = [
        _member(1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
        _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
    ]

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="is not a member"):
        service.approve_review(1, canonical_document_id="doc-999")


def test_approve_review_raises_when_no_members_exist() -> None:
    """Defensive: a review record should never have zero members in
    practice (the service always creates them atomically), but approval
    must not silently proceed if it somehow does."""
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.EXACT)
    db.get.return_value = review
    db.scalars.return_value = []

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="has no members"):
        service.approve_review(1, canonical_document_id="doc-1")


# --- approve: near duplicates never get an automatic canonical -----------


def test_approve_near_review_without_canonical_succeeds() -> None:
    """Approving a near-duplicate review without picking a canonical is
    a valid, deliberate outcome - 'confirmed as related, no canonical
    chosen' - and must never be filled in automatically."""
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.NEAR, confidence=0.9)
    db.get.return_value = review
    db.scalars.return_value = [
        _member(1, "doc-1", DuplicateReviewMemberRole.DUPLICATE),
        _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
    ]

    service = DedupReviewService(db)
    result = service.approve_review(1, reviewer_decision="Confirmed related.")

    assert result.status == DuplicateReviewStatus.APPROVED
    assert result.human_selected_canonical_document_id is None


def test_approve_near_review_with_explicit_human_canonical_succeeds() -> None:
    """A human MAY explicitly choose a canonical for a near-duplicate
    review - that's a real human decision, not an automatic one, and is
    allowed as long as it references an actual member."""
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.NEAR, confidence=0.9)
    db.get.return_value = review
    db.scalars.return_value = [
        _member(1, "doc-1", DuplicateReviewMemberRole.DUPLICATE),
        _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
    ]

    service = DedupReviewService(db)
    result = service.approve_review(1, canonical_document_id="doc-1")

    assert result.status == DuplicateReviewStatus.APPROVED
    assert result.human_selected_canonical_document_id == "doc-1"


def test_approve_near_review_rejects_canonical_not_a_member() -> None:
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.NEAR, confidence=0.9)
    db.get.return_value = review
    db.scalars.return_value = [
        _member(1, "doc-1", DuplicateReviewMemberRole.DUPLICATE),
        _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
    ]

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="is not a member"):
        service.approve_review(1, canonical_document_id="doc-999")


# --- reject --------------------------------------------------------------


def test_reject_review_sets_status_and_timestamp() -> None:
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.NEAR, confidence=0.9)
    db.get.return_value = review

    service = DedupReviewService(db)
    result = service.reject_review(1, reviewer_decision="Not actually related.")

    assert result.status == DuplicateReviewStatus.REJECTED
    assert result.reviewer_decision == "Not actually related."
    assert result.reviewed_at is not None
    assert result.human_selected_canonical_document_id is None


# --- already-reviewed / conflicting review attempts -----------------------


def test_approve_review_raises_for_already_approved_review() -> None:
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.EXACT)
    review.status = DuplicateReviewStatus.APPROVED
    db.get.return_value = review

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="already been reviewed"):
        service.approve_review(1, canonical_document_id="doc-1")


def test_approve_review_raises_for_already_rejected_review() -> None:
    """Deliberate divergence from MemoryService's permissive precedent:
    dedup review enforces a one-way PENDING -> decided transition, since
    this is the stage that will eventually gate a real filesystem
    action and 'do not silently allow inconsistent decisions' was an
    explicit requirement for this milestone specifically."""
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.EXACT)
    review.status = DuplicateReviewStatus.REJECTED
    db.get.return_value = review

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="already been reviewed"):
        service.approve_review(1, canonical_document_id="doc-1")


def test_reject_review_raises_for_already_approved_review() -> None:
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.NEAR, confidence=0.9)
    review.status = DuplicateReviewStatus.APPROVED
    db.get.return_value = review

    service = DedupReviewService(db)

    with pytest.raises(ValueError, match="already been reviewed"):
        service.reject_review(1)


def test_conflicting_sequential_review_calls_second_call_is_rejected() -> None:
    """Real concurrent HTTP requests are serialized by the database, so
    this is the realistic shape of a 'conflicting review attempt': the
    first call succeeds, and the second - whichever it is - is rejected
    with a clear error rather than silently overwriting the first
    reviewer's decision."""
    db = MagicMock()
    review = _pending_review(match_type=DuplicateMatchType.NEAR, confidence=0.9)
    db.get.return_value = review
    db.scalars.return_value = [
        _member(1, "doc-1", DuplicateReviewMemberRole.DUPLICATE),
        _member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
    ]

    service = DedupReviewService(db)

    approved = service.approve_review(1, reviewer_decision="First reviewer")
    assert approved.status == DuplicateReviewStatus.APPROVED

    with pytest.raises(ValueError, match="already been reviewed"):
        service.reject_review(1, reviewer_decision="Second reviewer disagrees")

    # The first decision stands, untouched by the rejected second attempt.
    assert review.status == DuplicateReviewStatus.APPROVED
    assert review.reviewer_decision == "First reviewer"


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
