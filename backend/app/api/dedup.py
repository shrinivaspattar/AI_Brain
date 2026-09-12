from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dedup.service import (
    DEFAULT_GROUP_LIMIT,
    DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    DeduplicationService,
)
from app.schemas.dedup import ExactDuplicateGroupResponse, NearDuplicatePairResponse

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
