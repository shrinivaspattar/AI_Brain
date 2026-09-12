from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dedup.service import (
    DEFAULT_GROUP_LIMIT,
    DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    DeduplicationService,
)
from app.schemas.dedup import (
    ExactDuplicateGroupResponse,
    ExactDuplicatePlanResponse,
    NearDuplicatePairResponse,
)

router = APIRouter(
    prefix="/dedup",
    tags=["Deduplication"],
)


@router.get(
    "/exact",
    response_model=list[ExactDuplicateGroupResponse],
)
def find_exact_duplicates(
    db: Session = Depends(get_db),
    limit: int = Query(default=DEFAULT_GROUP_LIMIT, ge=1, le=1000),
) -> list[ExactDuplicateGroupResponse]:
    service = DeduplicationService(db)
    return service.find_exact_duplicates(limit=limit)


@router.get(
    "/near",
    response_model=list[NearDuplicatePairResponse],
)
def find_near_duplicate_documents(
    db: Session = Depends(get_db),
    threshold: float = Query(default=DEFAULT_NEAR_DUPLICATE_THRESHOLD, gt=0, le=1),
    limit: int = Query(default=DEFAULT_GROUP_LIMIT, ge=1, le=1000),
) -> list[NearDuplicatePairResponse]:
    service = DeduplicationService(db)
    return service.find_near_duplicate_documents(
        similarity_threshold=threshold,
        limit=limit,
    )


@router.get(
    "/exact/plan",
    response_model=list[ExactDuplicatePlanResponse],
)
def plan_exact_duplicate_cleanup(
    db: Session = Depends(get_db),
    limit: int = Query(default=DEFAULT_GROUP_LIMIT, ge=1, le=1000),
) -> list[ExactDuplicatePlanResponse]:
    """Dry run only: propose which copy to keep and which to delete in
    each exact-duplicate group. Never deletes anything - this returns a
    plan for a human to review, nothing more."""
    service = DeduplicationService(db)
    return service.plan_exact_duplicate_cleanup(limit=limit)
