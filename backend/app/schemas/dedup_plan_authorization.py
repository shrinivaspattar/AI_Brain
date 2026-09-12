from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.dedup_execution_plan import PlanValidityResponse


class AuthorizePlanRequest(BaseModel):
    # Must be explicitly True - there is no default that lets a caller
    # authorize a plan by accident via an empty or omitted body.
    confirm: bool = Field(
        description=(
            "Must be true. Confirms the caller intends to authorize this "
            "exact plan for future execution - this is not itself "
            "execution, and no filesystem change happens as a result."
        )
    )
    authorized_by: str | None = Field(
        default=None,
        description="Optional free-text note on who/why this was authorized.",
    )


class RevokeAuthorizationRequest(BaseModel):
    reason: str | None = None


class DedupPlanAuthorizationResponse(BaseModel):
    id: int
    plan_id: int
    status: str
    # Frozen proof of what check_plan_validity found at the moment
    # authorization was granted - NOT a live value. It can and will
    # drift from reality; call GET /dedup/authorizations/{id}/currency
    # for a fresh, right-now answer.
    validity_snapshot: dict
    authorized_by: str | None
    authorized_at: datetime
    revoked_at: datetime | None
    revocation_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuthorizationCurrencyResponse(BaseModel):
    authorization_id: int
    plan_id: int
    authorization_status: str
    # A freshly-recomputed validity check, run at the moment of this
    # call - independent of, and may disagree with, validity_snapshot
    # on the authorization itself.
    current_validity: PlanValidityResponse
    # The single answer a future executor must check immediately
    # before acting: False means stop, regardless of authorization
    # status or how recently this was last true.
    is_still_actionable: bool
