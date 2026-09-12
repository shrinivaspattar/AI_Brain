from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dedup.execution_plan_service import DedupExecutionPlanService
from app.models.dedup_execution_plan import DedupExecutionPlan
from app.schemas.dedup_execution_plan import (
    DedupExecutionPlanActionResponse,
    DedupExecutionPlanResponse,
    PlanValidityResponse,
)
from app.schemas.document import DocumentResponse

router = APIRouter(tags=["Deduplication Execution Plans"])


def _raise_for_plan_value_error(exc: ValueError) -> NoReturn:
    """Extends the same convention used for review errors
    (app/api/dedup_reviews.py): not found -> 404, a state conflict
    (review isn't approved) -> 409, everything else - the review's
    decision doesn't actually carry enough information to plan from,
    or refers to inconsistent data - -> 422, a client/data problem
    rather than a simple conflict."""
    message = str(exc)

    if "not found" in message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

    if "is not approved" in message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _build_plan_response(
    service: DedupExecutionPlanService,
    plan: DedupExecutionPlan,
) -> DedupExecutionPlanResponse:
    pairs = service.get_plan_actions_with_documents(plan.id)

    actions = [
        DedupExecutionPlanActionResponse(
            id=action.id,
            document_id=action.document_id,
            action=action.action.value,
            source_path=action.source_path,
            target_document_id=action.target_document_id,
            target_path=action.target_path,
            observed_exists=action.observed_exists,
            observed_content_hash=action.observed_content_hash,
            observed_file_size=action.observed_file_size,
            reason=action.reason,
            created_at=action.created_at,
            document=DocumentResponse.model_validate(document),
        )
        for action, document in pairs
    ]

    return DedupExecutionPlanResponse(
        id=plan.id,
        review_id=plan.review_id,
        canonical_document_id=plan.canonical_document_id,
        canonical_source_path=plan.canonical_source_path,
        canonical_observed_exists=plan.canonical_observed_exists,
        canonical_observed_content_hash=plan.canonical_observed_content_hash,
        canonical_observed_file_size=plan.canonical_observed_file_size,
        status=plan.status.value,
        created_at=plan.created_at,
        actions=actions,
    )


@router.post(
    "/dedup/reviews/{review_id}/plans",
    response_model=DedupExecutionPlanResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Duplicate review not found"},
        409: {"description": "Duplicate review is not approved"},
        422: {"description": "Review has no explicit canonical decision, or refers to inconsistent data"},
    },
)
def generate_dedup_execution_plan(
    review_id: int,
    db: Session = Depends(get_db),
) -> DedupExecutionPlanResponse:
    """Generates a dry-run execution plan for an approved review. Reads
    files from disk to observe their current hash/size - never writes,
    moves, deletes, renames, quarantines, or overwrites anything. There
    is no execution endpoint anywhere in this API."""
    service = DedupExecutionPlanService(db)

    try:
        plan = service.generate_plan_for_review(review_id)
    except ValueError as exc:
        _raise_for_plan_value_error(exc)

    return _build_plan_response(service, plan)


@router.get(
    "/dedup/plans",
    response_model=list[DedupExecutionPlanResponse],
)
def list_dedup_execution_plans(
    db: Session = Depends(get_db),
    review_id: int | None = Query(default=None),
) -> list[DedupExecutionPlanResponse]:
    service = DedupExecutionPlanService(db)
    plans = service.list_plans(review_id=review_id)
    return [_build_plan_response(service, plan) for plan in plans]


@router.get(
    "/dedup/plans/{plan_id}",
    response_model=DedupExecutionPlanResponse,
    responses={404: {"description": "Dedup execution plan not found"}},
)
def get_dedup_execution_plan(
    plan_id: int,
    db: Session = Depends(get_db),
) -> DedupExecutionPlanResponse:
    service = DedupExecutionPlanService(db)
    plan = service.get_plan(plan_id)

    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dedup execution plan {plan_id} not found",
        )

    return _build_plan_response(service, plan)


@router.get(
    "/dedup/plans/{plan_id}/validity",
    response_model=PlanValidityResponse,
    responses={404: {"description": "Dedup execution plan not found"}},
)
def get_dedup_execution_plan_validity(
    plan_id: int,
    db: Session = Depends(get_db),
) -> PlanValidityResponse:
    """Re-reads the filesystem right now and compares it against what
    this plan observed at generation time. `is_valid: false` means the
    plan must never be acted on as-is - regenerate it instead. This
    check is read-only: it never modifies the plan, the review, or any
    file."""
    service = DedupExecutionPlanService(db)

    try:
        validity = service.check_plan_validity(plan_id)
    except ValueError as exc:
        _raise_for_plan_value_error(exc)

    return PlanValidityResponse(
        plan_id=validity.plan_id,
        canonical_document_exists=validity.canonical_document_exists,
        canonical_path_changed=validity.canonical_path_changed,
        canonical_exists_now=validity.canonical_exists_now,
        canonical_type_matches=validity.canonical_type_matches,
        canonical_hash_matches=validity.canonical_hash_matches,
        canonical_size_matches=validity.canonical_size_matches,
        canonical_valid=validity.canonical_valid,
        actions=[
            {
                "action_id": a.action_id,
                "document_id": a.document_id,
                "source_path": a.source_path,
                "document_exists": a.document_exists,
                "path_changed": a.path_changed,
                "exists_now": a.exists_now,
                "type_matches": a.type_matches,
                "hash_matches": a.hash_matches,
                "size_matches": a.size_matches,
                "is_valid": a.is_valid,
            }
            for a in validity.actions
        ],
        is_valid=validity.is_valid,
    )
