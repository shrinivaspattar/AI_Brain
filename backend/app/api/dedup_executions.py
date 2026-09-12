from datetime import datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dedup.execution_service import DedupExecutionService
from app.models.dedup_execution import (
    DedupExecution,
    DedupExecutionActionAudit,
    DedupExecutionActionResult,
    DedupExecutionStatus,
)
from app.schemas.dedup_execution import (
    DedupExecutionActionAuditResponse,
    DedupExecutionResponse,
    RecordActionResultRequest,
    RecoverStaleExecutionRequest,
    StartExecutionRequest,
)

router = APIRouter(tags=["Deduplication Executions"])

# This is a pure audit/bookkeeping API: no endpoint here performs any
# filesystem operation. There is no filesystem executor anywhere in
# this codebase - these endpoints exist to record what a future
# executor did (or decided not to do), not to make anything happen.


def _raise_for_execution_value_error(exc: ValueError) -> NoReturn:
    """Extends the same 404/409/422 convention used across the dedup
    API surface: not found -> 404; a state conflict (authorization not
    active, an execution already exists for it, an execution already
    finalized, an action result already recorded) -> 409; everything
    else - a stale plan, an incomplete audit trail, a definitional
    consistency violation, a plan-action/execution mismatch -> 422,
    since those reflect the request's own data rather than a race
    against existing state."""
    message = str(exc)

    if "not found" in message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

    if (
        "is not active" in message
        or "already has an execution" in message
        or "is not RUNNING" in message
        or "already has a recorded result" in message
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _build_execution_response(execution: DedupExecution) -> DedupExecutionResponse:
    return DedupExecutionResponse(
        id=execution.id,
        authorization_id=execution.authorization_id,
        plan_id=execution.plan_id,
        status=execution.status.value,
        executor_identity=execution.executor_identity,
        started_at=execution.started_at,
        ended_at=execution.ended_at,
        failure_reason=execution.failure_reason,
        updated_at=execution.updated_at,
    )


def _build_action_audit_response(
    audit: DedupExecutionActionAudit,
) -> DedupExecutionActionAuditResponse:
    return DedupExecutionActionAuditResponse(
        id=audit.id,
        execution_id=audit.execution_id,
        plan_action_id=audit.plan_action_id,
        document_id=audit.document_id,
        planned_action=audit.planned_action.value,
        source_path=audit.source_path,
        target_path=audit.target_path,
        expected_content_hash=audit.expected_content_hash,
        expected_file_size=audit.expected_file_size,
        result=audit.result.value,
        observed_content_hash=audit.observed_content_hash,
        observed_file_size=audit.observed_file_size,
        filesystem_mutation_occurred=audit.filesystem_mutation_occurred,
        error_message=audit.error_message,
        started_at=audit.started_at,
        ended_at=audit.ended_at,
        created_at=audit.created_at,
    )


@router.post(
    "/dedup/authorizations/{authorization_id}/executions",
    response_model=DedupExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Dedup plan authorization not found"},
        409: {
            "description": (
                "The authorization is not active, or already has an "
                "execution"
            )
        },
        422: {
            "description": (
                "Confirmation missing/false, or the plan failed a fresh "
                "validity re-check and is no longer safe to execute"
            )
        },
    },
)
def start_dedup_execution(
    authorization_id: int,
    request: StartExecutionRequest,
    db: Session = Depends(get_db),
) -> DedupExecutionResponse:
    """Begin one execution attempt under one authorization.

    This performs NO filesystem action - there is no filesystem
    executor anywhere in this codebase to consume this record yet.
    Every precondition (the authorization is currently AUTHORIZED, no
    execution already exists for it, the plan still passes a fresh
    validity re-check) is re-checked at the moment of this call, never
    assumed from the authorization having been granted at some earlier
    point. An authorization backs at most one execution ever - a new
    attempt requires a new authorization.
    """
    if not request.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirm must be true to start an execution",
        )

    service = DedupExecutionService(db)

    try:
        execution = service.start_execution(
            authorization_id,
            executor_identity=request.executor_identity,
        )
    except ValueError as exc:
        _raise_for_execution_value_error(exc)

    return _build_execution_response(execution)


@router.get(
    "/dedup/executions",
    response_model=list[DedupExecutionResponse],
)
def list_dedup_executions(
    db: Session = Depends(get_db),
    plan_id: int | None = Query(default=None),
    authorization_id: int | None = Query(default=None),
    execution_status: DedupExecutionStatus | None = Query(
        default=None, alias="status"
    ),
    started_before: datetime | None = Query(
        default=None,
        description=(
            "Advisory filter for finding stale-execution candidates - "
            "e.g. ?status=running&started_before=<ISO 8601 timestamp>. "
            "AI_Brain has no process supervision, so this never asserts "
            "a matching execution is actually stuck; it only narrows "
            "the list for a human to investigate before deciding to "
            "call POST .../recover."
        ),
    ),
) -> list[DedupExecutionResponse]:
    service = DedupExecutionService(db)
    executions = service.list_executions(
        plan_id=plan_id,
        authorization_id=authorization_id,
        status=execution_status,
        started_before=started_before,
    )
    return [_build_execution_response(e) for e in executions]


@router.get(
    "/dedup/executions/{execution_id}",
    response_model=DedupExecutionResponse,
    responses={404: {"description": "Dedup execution not found"}},
)
def get_dedup_execution(
    execution_id: int,
    db: Session = Depends(get_db),
) -> DedupExecutionResponse:
    service = DedupExecutionService(db)
    execution = service.get_execution(execution_id)

    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dedup execution {execution_id} not found",
        )

    return _build_execution_response(execution)


@router.get(
    "/dedup/executions/{execution_id}/actions",
    response_model=list[DedupExecutionActionAuditResponse],
    responses={404: {"description": "Dedup execution not found"}},
)
def list_dedup_execution_action_audits(
    execution_id: int,
    db: Session = Depends(get_db),
) -> list[DedupExecutionActionAuditResponse]:
    service = DedupExecutionService(db)

    if service.get_execution(execution_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dedup execution {execution_id} not found",
        )

    audits = service.get_action_audits(execution_id)
    return [_build_action_audit_response(a) for a in audits]


@router.post(
    "/dedup/executions/{execution_id}/actions",
    response_model=DedupExecutionActionAuditResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Dedup execution or plan action not found"},
        409: {
            "description": (
                "The execution is not RUNNING, or already has a recorded "
                "result for this plan action"
            )
        },
        422: {
            "description": (
                "The plan action does not belong to this execution's plan, "
                "or the result is inconsistent with filesystem_mutation_occurred"
            )
        },
    },
)
def record_dedup_execution_action_result(
    execution_id: int,
    request: RecordActionResultRequest,
    db: Session = Depends(get_db),
) -> DedupExecutionActionAuditResponse:
    """Record what ACTUALLY happened for one planned action, within
    one execution. Pure bookkeeping - this endpoint never performs a
    filesystem operation itself; a caller reports an outcome it
    already observed elsewhere, and this persists it as a permanent,
    immutable fact. What was PLANNED (source/target path, expected
    hash/size, the action type) is always copied from the plan action
    itself, never accepted from the request body - only what was
    actually observed is caller-supplied.
    """
    try:
        result = DedupExecutionActionResult(request.result)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"'{request.result}' is not a valid result - must be one of: "
                f"{', '.join(r.value for r in DedupExecutionActionResult)}"
            ),
        )

    service = DedupExecutionService(db)

    try:
        audit = service.record_action_result(
            execution_id,
            request.plan_action_id,
            result,
            observed_content_hash=request.observed_content_hash,
            observed_file_size=request.observed_file_size,
            filesystem_mutation_occurred=request.filesystem_mutation_occurred,
            error_message=request.error_message,
            started_at=request.started_at,
            ended_at=request.ended_at,
        )
    except ValueError as exc:
        _raise_for_execution_value_error(exc)

    return _build_action_audit_response(audit)


@router.post(
    "/dedup/executions/{execution_id}/complete",
    response_model=DedupExecutionResponse,
    responses={
        404: {"description": "Dedup execution not found"},
        409: {"description": "The execution is not RUNNING"},
        422: {
            "description": (
                "The action audit trail is incomplete - not every planned "
                "action has a recorded outcome yet"
            )
        },
    },
)
def complete_dedup_execution(
    execution_id: int,
    db: Session = Depends(get_db),
) -> DedupExecutionResponse:
    """Finalize a RUNNING execution. The overall status
    (COMPLETED/FAILED/PARTIALLY_COMPLETED) is derived entirely from the
    execution's own recorded action audit rows - never accepted as
    input - so nothing can claim the plan completed when the audit
    trail says otherwise."""
    service = DedupExecutionService(db)

    try:
        execution = service.complete_execution(execution_id)
    except ValueError as exc:
        _raise_for_execution_value_error(exc)

    return _build_execution_response(execution)


@router.post(
    "/dedup/executions/{execution_id}/recover",
    response_model=DedupExecutionResponse,
    responses={
        404: {"description": "Dedup execution not found"},
        409: {"description": "The execution is not RUNNING"},
        422: {
            "description": (
                "Confirmation missing/false, or every planned action "
                "already has a recorded outcome (nothing to recover)"
            )
        },
    },
)
def recover_dedup_execution(
    execution_id: int,
    request: RecoverStaleExecutionRequest,
    db: Session = Depends(get_db),
) -> DedupExecutionResponse:
    """Close out a RUNNING execution that will never receive any
    further action results (the canonical reason: its executor process
    died). This performs NO filesystem action - only a non-mutating
    re-read of the still-unresolved planned actions' files, exactly
    like `check_plan_validity` already does elsewhere.

    AI_Brain has no process supervision anywhere and cannot know
    whether the execution's process is actually dead - calling this is
    an explicit human decision, made only after independently
    confirming that outside this system. A file confirmed unchanged
    since the plan was generated is recorded NOT_ATTEMPTED (the
    mutation demonstrably did not happen); anything else - a missing
    file, or one that changed unexpectedly - is recorded UNKNOWN,
    never guessed as SUCCESS. The execution is then finalized via the
    same derivation `complete_execution` already uses; any UNKNOWN
    result makes the outcome NEEDS_REVIEW.
    """
    if not request.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirm must be true to recover a stale execution",
        )

    service = DedupExecutionService(db)

    try:
        execution = service.recover_stale_execution(execution_id)
    except ValueError as exc:
        _raise_for_execution_value_error(exc)

    return _build_execution_response(execution)
