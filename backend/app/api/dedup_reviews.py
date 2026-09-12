from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dedup.review_service import DedupReviewService
from app.models.dedup_review import DuplicateReview, DuplicateReviewMemberRole, DuplicateReviewStatus
from app.schemas.dedup_review import (
    ApproveDuplicateReviewRequest,
    DuplicateReviewMemberResponse,
    DuplicateReviewResponse,
    RejectDuplicateReviewRequest,
)
from app.schemas.document import DocumentResponse

router = APIRouter(
    prefix="/dedup/reviews",
    tags=["Deduplication Review"],
)


def _raise_for_review_value_error(exc: ValueError) -> NoReturn:
    """Maps DedupReviewService's ValueError messages to the right HTTP
    status, extending this codebase's existing "not found" -> 404,
    else -> 409 convention (see app/api/import_jobs.py,
    app/api/memory.py) with a third case: an invalid/incomplete
    canonical selection is a client input problem, not a state
    conflict, so it gets 422 rather than 409."""
    message = str(exc)

    if "not found" in message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

    if "already been reviewed" in message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _build_review_response(
    service: DedupReviewService,
    review: DuplicateReview,
) -> DuplicateReviewResponse:
    pairs = service.get_review_members_with_documents(review.id)

    members = [
        DuplicateReviewMemberResponse(
            id=member.id,
            document_id=member.document_id,
            role=member.role.value,
            document=DocumentResponse.model_validate(document),
        )
        for member, document in pairs
    ]

    recommended_canonical_document_id = next(
        (
            member.document_id
            for member, _document in pairs
            if member.role == DuplicateReviewMemberRole.RECOMMENDED_CANONICAL
        ),
        None,
    )

    return DuplicateReviewResponse(
        id=review.id,
        match_type=review.match_type.value,
        content_hash=review.content_hash,
        similarity=review.similarity,
        confidence=review.confidence,
        recommendation_reason=review.recommendation_reason,
        evidence=review.evidence,
        status=review.status.value,
        reviewer_decision=review.reviewer_decision,
        recommended_canonical_document_id=recommended_canonical_document_id,
        human_selected_canonical_document_id=review.human_selected_canonical_document_id,
        reviewed_at=review.reviewed_at,
        created_at=review.created_at,
        updated_at=review.updated_at,
        members=members,
    )


@router.get(
    "",
    response_model=list[DuplicateReviewResponse],
)
def list_duplicate_reviews(
    db: Session = Depends(get_db),
    status_filter: DuplicateReviewStatus | None = Query(default=None, alias="status"),
) -> list[DuplicateReviewResponse]:
    service = DedupReviewService(db)
    reviews = service.list_reviews(status=status_filter)
    return [_build_review_response(service, review) for review in reviews]


@router.get(
    "/{review_id}",
    response_model=DuplicateReviewResponse,
    responses={404: {"description": "Duplicate review not found"}},
)
def get_duplicate_review(
    review_id: int,
    db: Session = Depends(get_db),
) -> DuplicateReviewResponse:
    service = DedupReviewService(db)
    review = service.get_review(review_id)

    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Duplicate review {review_id} not found",
        )

    return _build_review_response(service, review)


@router.post(
    "/{review_id}/approve",
    response_model=DuplicateReviewResponse,
    responses={
        404: {"description": "Duplicate review not found"},
        409: {"description": "Duplicate review has already been reviewed"},
        422: {"description": "Missing or invalid canonical_document_id"},
    },
)
def approve_duplicate_review(
    review_id: int,
    request: ApproveDuplicateReviewRequest = ApproveDuplicateReviewRequest(),
    db: Session = Depends(get_db),
) -> DuplicateReviewResponse:
    """Records a human decision only. Never deletes, moves, renames,
    quarantines, or overwrites a file, and never modifies any Document
    row - no filesystem execution mechanism exists anywhere in this
    codebase."""
    service = DedupReviewService(db)

    try:
        review = service.approve_review(
            review_id,
            canonical_document_id=request.canonical_document_id,
            reviewer_decision=request.reviewer_decision,
        )
    except ValueError as exc:
        _raise_for_review_value_error(exc)

    return _build_review_response(service, review)


@router.post(
    "/{review_id}/reject",
    response_model=DuplicateReviewResponse,
    responses={
        404: {"description": "Duplicate review not found"},
        409: {"description": "Duplicate review has already been reviewed"},
    },
)
def reject_duplicate_review(
    review_id: int,
    request: RejectDuplicateReviewRequest = RejectDuplicateReviewRequest(),
    db: Session = Depends(get_db),
) -> DuplicateReviewResponse:
    """Records a human decision only - see approve_duplicate_review."""
    service = DedupReviewService(db)

    try:
        review = service.reject_review(
            review_id,
            reviewer_decision=request.reviewer_decision,
        )
    except ValueError as exc:
        _raise_for_review_value_error(exc)

    return _build_review_response(service, review)
