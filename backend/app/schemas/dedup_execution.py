from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StartExecutionRequest(BaseModel):
    # Must be explicitly True - mirrors AuthorizePlanRequest.confirm.
    # Starting an execution record is not itself a filesystem action
    # (no executor exists to perform one), but it is the boundary where
    # this system begins claiming "a future executor is now acting" -
    # worth the same explicit confirmation as authorization itself.
    confirm: bool = Field(
        description=(
            "Must be true. Confirms the caller intends to begin an "
            "execution attempt under this authorization - this call "
            "itself performs no filesystem action."
        )
    )
    executor_identity: str | None = Field(
        default=None,
        description="Optional free-text identity/version of the executor.",
    )


class RecoverStaleExecutionRequest(BaseModel):
    # Must be explicitly True - mirrors StartExecutionRequest.confirm.
    # AI_Brain has no process supervision anywhere: it cannot know
    # whether the execution's process is actually dead, so this is an
    # explicit human decision, not something inferred from a timeout.
    confirm: bool = Field(
        description=(
            "Must be true. Confirms the caller has independently "
            "determined this execution's process will not progress "
            "further - this call itself performs no filesystem action, "
            "only a non-mutating re-read of the relevant files."
        )
    )


class RecordActionResultRequest(BaseModel):
    plan_action_id: int
    result: str = Field(
        description=(
            "One of: success, precondition_failed, failed, not_attempted, "
            "unknown."
        )
    )
    observed_content_hash: str | None = None
    observed_file_size: int | None = None
    # Tri-state: True/False/null(unknown). Only result=unknown may
    # leave this null - every other result requires a definite value.
    filesystem_mutation_occurred: bool | None = False
    error_message: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class DedupExecutionResponse(BaseModel):
    id: int
    authorization_id: int
    plan_id: int
    status: str
    executor_identity: str | None
    started_at: datetime
    # Null while RUNNING.
    ended_at: datetime | None
    # Derived from the action audit trail at finalization time - never
    # caller-supplied. Null for a COMPLETED execution.
    failure_reason: str | None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DedupExecutionActionAuditResponse(BaseModel):
    id: int
    execution_id: int
    plan_action_id: int
    document_id: str
    # --- What was planned (frozen from the plan action) ---
    planned_action: str
    source_path: str
    target_path: str
    expected_content_hash: str | None
    expected_file_size: int | None
    # --- What actually happened ---
    result: str
    observed_content_hash: str | None
    observed_file_size: int | None
    # Tri-state: True (definitely mutated) / False (definitely did
    # not) / null (unknown/indeterminate - only ever paired with
    # result=unknown; see DedupExecutionActionResult.UNKNOWN).
    filesystem_mutation_occurred: bool | None
    error_message: str | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
