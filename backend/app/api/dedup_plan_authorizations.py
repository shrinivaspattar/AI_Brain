from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import PlanValidity
from app.models.dedup_authorization import DedupPlanAuthorization, DedupPlanAuthorizationStatus
from app.schemas.dedup_execution_plan import PlanValidityResponse
from app.schemas.dedup_plan_authorization import (
    AuthorizationCurrencyResponse,
    AuthorizePlanRequest,
    DedupPlanAuthorizationResponse,
    RevokeAuthorizationRequest,
)

router = APIRouter(tags=["Deduplication Plan Authorizations"])

# Explicitly NO execution endpoint here, and no generic "execute=true"
# flag on any endpoint anywhere in this API. Authorizing a plan grants
# permission for a future executor to act on it - it performs zero
# filesystem writes itself, and there is no filesystem executor
# anywhere in this codebase yet.


def _raise_for_authorization_value_error(exc: ValueError) -> NoReturn:
    """Extends the same 404/409/422 convention used for reviews and
    plans (app/api/dedup_reviews.py, app/api/dedup_execution_plans.py):
    not found -> 404; a state conflict (review not approved, an active
    authorization already exists, an authorization is already revoked)
    -> 409; everything else - most notably a plan that failed its
    fresh validity re-check - -> 422, since that reflects the plan's
    own data/filesystem state rather than a request conflict."""
    message = str(exc)

    if "not found" in message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

    if (
        "is not approved" in message
        or "already has an active authorization" in message
        or "already been revoked" in message
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _build_validity_response(validity: PlanValidity) -> PlanValidityResponse:
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


def _build_authorization_response(
    authorization: DedupPlanAuthorization,
) -> DedupPlanAuthorizationResponse:
    return DedupPlanAuthorizationResponse(
        id=authorization.id,
        plan_id=authorization.plan_id,
        status=authorization.status.value,
        validity_snapshot=authorization.validity_snapshot,
        authorized_by=authorization.authorized_by,
        authorized_at=authorization.authorized_at,
        revoked_at=authorization.revoked_at,
        revocation_reason=authorization.revocation_reason,
        created_at=authorization.created_at,
        updated_at=authorization.updated_at,
    )


@router.post(
    "/dedup/plans/{plan_id}/authorize",
    response_model=DedupPlanAuthorizationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Dedup execution plan not found"},
        409: {
            "description": (
                "The plan's review is not approved, or the plan already "
                "has an active authorization"
            )
        },
        422: {
            "description": (
                "Confirmation missing/false, or the plan failed a fresh "
                "validity re-check and is no longer safe to authorize"
            )
        },
    },
)
def authorize_dedup_execution_plan(
    plan_id: int,
    request: AuthorizePlanRequest,
    db: Session = Depends(get_db),
) -> DedupPlanAuthorizationResponse:
    """Authorize this exact plan for future execution.

    This is NOT execution. No file is created, deleted, moved,
    renamed, or modified by this call, and there is no filesystem
    executor anywhere in this codebase to consume this authorization
    yet. Every precondition - the review's approval, the absence of a
    conflicting existing authorization, and the plan's validity - is
    re-checked fresh against the database and the filesystem at the
    moment of this call, never assumed from an earlier check. A plan
    that has gone stale since it was generated is refused outright:
    this endpoint never regenerates, updates, or silently substitutes
    plan data - a new plan must be deliberately generated and reviewed
    instead.
    """
    if not request.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirm must be true to authorize this plan",
        )

    service = DedupPlanAuthorizationService(db)

    try:
        authorization = service.authorize_plan(
            plan_id,
            authorized_by=request.authorized_by,
        )
    except ValueError as exc:
        _raise_for_authorization_value_error(exc)

    return _build_authorization_response(authorization)


@router.get(
    "/dedup/authorizations",
    response_model=list[DedupPlanAuthorizationResponse],
)
def list_dedup_plan_authorizations(
    db: Session = Depends(get_db),
    plan_id: int | None = Query(default=None),
    authorization_status: DedupPlanAuthorizationStatus | None = Query(
        default=None, alias="status"
    ),
) -> list[DedupPlanAuthorizationResponse]:
    service = DedupPlanAuthorizationService(db)
    authorizations = service.list_authorizations(
        plan_id=plan_id, status=authorization_status
    )
    return [_build_authorization_response(a) for a in authorizations]


@router.get(
    "/dedup/authorizations/{authorization_id}",
    response_model=DedupPlanAuthorizationResponse,
    responses={404: {"description": "Dedup plan authorization not found"}},
)
def get_dedup_plan_authorization(
    authorization_id: int,
    db: Session = Depends(get_db),
) -> DedupPlanAuthorizationResponse:
    service = DedupPlanAuthorizationService(db)
    authorization = service.get_authorization(authorization_id)

    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dedup plan authorization {authorization_id} not found",
        )

    return _build_authorization_response(authorization)


@router.get(
    "/dedup/authorizations/{authorization_id}/currency",
    response_model=AuthorizationCurrencyResponse,
    responses={404: {"description": "Dedup plan authorization not found"}},
)
def get_dedup_plan_authorization_currency(
    authorization_id: int,
    db: Session = Depends(get_db),
) -> AuthorizationCurrencyResponse:
    """The TOCTOU checkpoint: is this authorization still active, AND
    is its underlying plan STILL valid right now - re-checked fresh
    against the filesystem at the moment of this call, independent of
    (and possibly disagreeing with) the frozen `validity_snapshot`
    captured when the authorization was originally granted.
    `is_still_actionable: false` means stop - a future executor must
    still perform its own fresh check immediately before acting rather
    than relying on any earlier call to this endpoint, since the
    filesystem can change the instant after this response is sent."""
    service = DedupPlanAuthorizationService(db)

    try:
        authorization, validity, is_still_actionable = service.check_currency(
            authorization_id
        )
    except ValueError as exc:
        _raise_for_authorization_value_error(exc)

    return AuthorizationCurrencyResponse(
        authorization_id=authorization.id,
        plan_id=authorization.plan_id,
        authorization_status=authorization.status.value,
        current_validity=_build_validity_response(validity),
        is_still_actionable=is_still_actionable,
    )


@router.post(
    "/dedup/authorizations/{authorization_id}/revoke",
    response_model=DedupPlanAuthorizationResponse,
    responses={
        404: {"description": "Dedup plan authorization not found"},
        409: {"description": "Authorization has already been revoked"},
    },
)
def revoke_dedup_plan_authorization(
    authorization_id: int,
    request: RevokeAuthorizationRequest,
    db: Session = Depends(get_db),
) -> DedupPlanAuthorizationResponse:
    """Withdraw a previously-granted authorization. Never touches a
    file - only this authorization row's own status changes."""
    service = DedupPlanAuthorizationService(db)

    try:
        authorization = service.revoke_authorization(
            authorization_id, reason=request.reason
        )
    except ValueError as exc:
        _raise_for_authorization_value_error(exc)

    return _build_authorization_response(authorization)
