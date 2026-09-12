from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.document import DocumentResponse


class DedupExecutionPlanActionResponse(BaseModel):
    id: int
    document_id: str
    action: str
    source_path: str
    target_document_id: str
    target_path: str
    # What was actually on disk when the plan was generated - not a live
    # value. Re-check via GET /dedup/plans/{id}/validity before ever
    # trusting this for anything.
    observed_exists: bool
    observed_content_hash: str | None
    observed_file_size: int | None
    reason: str
    created_at: datetime
    document: DocumentResponse

    model_config = ConfigDict(from_attributes=True)


class DedupExecutionPlanResponse(BaseModel):
    id: int
    review_id: int
    canonical_document_id: str
    canonical_source_path: str
    canonical_observed_exists: bool
    canonical_observed_content_hash: str | None
    canonical_observed_file_size: int | None
    status: str
    created_at: datetime
    actions: list[DedupExecutionPlanActionResponse]
    # Non-canonical review members that were left OUT of `actions`
    # because their file no longer existed at generation time (see
    # "Execution Recovery & Partial-Replanning Design" in
    # AI_Brain_Architecture.md) - computed by comparing the review's
    # own members against this plan's actions, not a stored field.
    # Transparency for a human reading a smaller-than-expected plan:
    # this tells them WHY, without claiming to know whether it's
    # because an earlier execution succeeded or for some other reason.
    excluded_document_ids: list[str] = []

    model_config = ConfigDict(from_attributes=True)


class PlanActionValidityResponse(BaseModel):
    action_id: int
    document_id: str
    source_path: str
    document_exists: bool
    path_changed: bool
    exists_now: bool
    type_matches: bool
    hash_matches: bool
    size_matches: bool
    is_valid: bool


class PlanValidityResponse(BaseModel):
    plan_id: int
    canonical_document_exists: bool
    canonical_path_changed: bool
    canonical_exists_now: bool
    canonical_type_matches: bool
    canonical_hash_matches: bool
    canonical_size_matches: bool
    canonical_valid: bool
    actions: list[PlanActionValidityResponse]
    # The single answer to check before ever considering execution:
    # False means abort - something about the filesystem or the
    # underlying Document rows no longer matches what this plan was
    # generated against.
    is_valid: bool
