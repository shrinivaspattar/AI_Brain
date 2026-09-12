from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.document import DocumentResponse


class DuplicateReviewMemberResponse(BaseModel):
    id: int
    document_id: str
    # "recommended_canonical" or "duplicate" - see
    # DuplicateReviewMemberRole. Never implies a decision has been made;
    # only DuplicateReviewResponse.human_selected_canonical_document_id
    # represents an actual human choice.
    role: str
    document: DocumentResponse

    model_config = ConfigDict(from_attributes=True)


class DuplicateReviewResponse(BaseModel):
    id: int
    match_type: str
    content_hash: str | None
    similarity: float | None
    confidence: float
    recommendation_reason: str
    evidence: dict
    status: str
    reviewer_decision: str | None
    # The system's suggestion only - never treat this as final. Look at
    # `human_selected_canonical_document_id` for an actual decision.
    # Derived from `members` (whichever has role="recommended_canonical"),
    # not a separate stored field.
    recommended_canonical_document_id: str | None
    # The only field representing a real decision - set exclusively by
    # an explicit human choice at approval time, never inferred from
    # `recommended_canonical_document_id`.
    human_selected_canonical_document_id: str | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    members: list[DuplicateReviewMemberResponse]

    model_config = ConfigDict(from_attributes=True)


class ApproveDuplicateReviewRequest(BaseModel):
    # Required for an EXACT review, optional for a NEAR review - see
    # DedupReviewService.approve_review. Never defaulted server-side.
    canonical_document_id: str | None = None
    reviewer_decision: str | None = Field(default=None, max_length=4000)


class RejectDuplicateReviewRequest(BaseModel):
    reviewer_decision: str | None = Field(default=None, max_length=4000)
