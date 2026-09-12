from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dedup.execution_plan_service import DedupExecutionPlanService, PlanValidity
from app.dedup.review_service import DedupReviewService
from app.models.dedup_authorization import (
    DedupPlanAuthorization,
    DedupPlanAuthorizationStatus,
)
from app.models.dedup_review import DuplicateReviewStatus

DEFAULT_LIST_LIMIT = 100


class DedupPlanAuthorizationService:
    """The EXPLICIT EXECUTION AUTHORIZATION stage - strictly downstream
    of an already-generated dry-run DedupExecutionPlan, and strictly
    upstream of a filesystem executor that does not exist anywhere in
    this codebase:

        Detection -> Recommendation -> Human Review -> Approval
            -> Dry-run Execution Plan
            -> Explicit Execution Authorization (this service)
            -> Filesystem Execution (not built)
            -> Verification (not built)
            -> Execution Audit (not built)

    Neither `DuplicateReview.status == APPROVED` nor
    `DedupExecutionPlan.status == GENERATED` is ever treated as
    permission to touch a file. Only a `DedupPlanAuthorization` row
    with `status == AUTHORIZED` represents that a human has explicitly
    authorized acting on one specific, immutable plan - and even that
    is not a permanent guarantee (see `check_currency` below).

    Creating, listing, reading, or revoking an authorization never
    touches a file. This service performs zero filesystem writes.
    """

    def __init__(self, db: Session):
        self.db = db
        self.plan_service = DedupExecutionPlanService(db)
        self.review_service = DedupReviewService(db)

    def authorize_plan(
        self,
        plan_id: int,
        authorized_by: str | None = None,
    ) -> DedupPlanAuthorization:
        """Grant explicit authorization to act on exactly one immutable
        plan, after re-validating every precondition fresh - never
        trusting an earlier check, a cached value, or the caller's
        assertion that everything is fine.

        Order of checks, each a hard stop (raises ValueError, no row is
        ever created for a failed attempt):
        1. The plan must exist (fetched fresh).
        2. Its review must exist (fetched fresh) and be APPROVED - a
           pending or rejected review is never sufficient.
        3. No other ACTIVE (non-revoked) authorization may already
           exist for this plan - authorizing twice is a conflict, not
           a silent no-op or a silent replacement.
        4. The plan must pass a freshly-run `check_plan_validity` RIGHT
           NOW - a plan that was valid when generated but has since
           gone stale (a source file changed, moved, or vanished) is
           refused. This method never regenerates, updates, or
           patches a stale plan on the caller's behalf; a human must
           deliberately generate a new plan and try again.

        The passing `PlanValidity` result is frozen into
        `validity_snapshot` as proof of what was true at authorization
        time. It is NOT a promise that stays true afterward - the
        filesystem can change the instant after this call returns.
        Whoever eventually builds a filesystem executor MUST call
        `check_currency` (or equivalently re-run `check_plan_validity`
        itself) immediately before every mutation, every time,
        regardless of how recently a plan was authorized. This method
        grants permission to act on a specific past validity finding;
        it does not and cannot vouch for the future.
        """
        plan = self.plan_service.get_plan(plan_id)
        if plan is None:
            raise ValueError(f"Dedup execution plan {plan_id} not found")

        review = self.review_service.get_review(plan.review_id)
        if review is None:
            raise ValueError(
                f"Duplicate review {plan.review_id} referenced by plan "
                f"{plan_id} not found"
            )

        if review.status != DuplicateReviewStatus.APPROVED:
            raise ValueError(
                f"Duplicate review {review.id} backing plan {plan_id} is not "
                f"approved (status={review.status.value}) - a plan cannot be "
                "authorized unless its review is approved"
            )

        existing_active = self._get_active_authorization(plan_id)
        if existing_active is not None:
            raise ValueError(
                f"Dedup execution plan {plan_id} already has an active "
                f"authorization (authorization {existing_active.id}) - "
                "revoke it first if a new authorization is genuinely needed"
            )

        validity = self.plan_service.check_plan_validity(plan_id)
        if not validity.is_valid:
            raise ValueError(
                f"Dedup execution plan {plan_id} is no longer valid - the "
                "filesystem or document state has changed since this plan "
                "was generated. Authorization refused. Generate a new plan "
                "and review it before authorizing; this plan will not be "
                "automatically regenerated or updated."
            )

        authorization = DedupPlanAuthorization(
            plan_id=plan_id,
            status=DedupPlanAuthorizationStatus.AUTHORIZED,
            validity_snapshot=_validity_to_json(validity),
            authorized_by=authorized_by,
            authorized_at=datetime.now(UTC),
        )
        self.db.add(authorization)

        try:
            self.db.commit()
            self.db.refresh(authorization)
            return authorization
        except Exception:
            self.db.rollback()
            raise

    def get_authorization(self, authorization_id: int) -> DedupPlanAuthorization | None:
        return self.db.get(DedupPlanAuthorization, authorization_id)

    def list_authorizations(
        self,
        plan_id: int | None = None,
        status: DedupPlanAuthorizationStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[DedupPlanAuthorization]:
        statement = (
            select(DedupPlanAuthorization)
            .order_by(DedupPlanAuthorization.authorized_at.desc())
            .limit(limit)
        )

        if plan_id is not None:
            statement = statement.where(DedupPlanAuthorization.plan_id == plan_id)
        if status is not None:
            statement = statement.where(DedupPlanAuthorization.status == status)

        return list(self.db.scalars(statement))

    def revoke_authorization(
        self,
        authorization_id: int,
        reason: str | None = None,
    ) -> DedupPlanAuthorization:
        """Withdraw a previously-granted authorization before anything
        has consumed it. Never touches a file - this only changes the
        authorization row's own status. Raises ValueError (409) if the
        authorization is already revoked; revoking twice is a conflict,
        not a silent no-op.
        """
        authorization = self._get_authorization_or_raise(authorization_id)

        if authorization.status == DedupPlanAuthorizationStatus.REVOKED:
            raise ValueError(
                f"Authorization {authorization_id} has already been revoked"
            )

        try:
            authorization.status = DedupPlanAuthorizationStatus.REVOKED
            authorization.revoked_at = datetime.now(UTC)
            authorization.revocation_reason = reason
            self.db.commit()
            self.db.refresh(authorization)
            return authorization
        except Exception:
            self.db.rollback()
            raise

    def check_currency(self, authorization_id: int) -> tuple[DedupPlanAuthorization, PlanValidity, bool]:
        """The TOCTOU checkpoint: is this authorization still ACTIVE,
        AND is its underlying plan STILL valid RIGHT NOW - as of this
        exact call, not as of whenever authorization was granted.

        Returns (authorization, fresh PlanValidity, is_still_actionable)
        where `is_still_actionable` is True only when the authorization
        has not been revoked AND the freshly-recomputed validity is
        still fully valid. This is deliberately distinct from the
        authorization's own frozen `validity_snapshot`: that snapshot
        is historical proof of what was checked at authorization time
        and never changes; this method re-derives the answer from
        scratch every time it is called and can flip from True to
        False between two calls a second apart if a file changes on
        disk in between. A future filesystem executor MUST treat a
        False result here as an absolute stop, and must still perform
        its own fresh check immediately before mutating anything rather
        than trusting a `check_currency` call made even slightly
        earlier.
        """
        authorization = self._get_authorization_or_raise(authorization_id)
        validity = self.plan_service.check_plan_validity(authorization.plan_id)

        is_still_actionable = (
            authorization.status == DedupPlanAuthorizationStatus.AUTHORIZED
            and validity.is_valid
        )

        return authorization, validity, is_still_actionable

    def _get_active_authorization(self, plan_id: int) -> DedupPlanAuthorization | None:
        return self.db.scalar(
            select(DedupPlanAuthorization)
            .where(DedupPlanAuthorization.plan_id == plan_id)
            .where(DedupPlanAuthorization.status == DedupPlanAuthorizationStatus.AUTHORIZED)
        )

    def _get_authorization_or_raise(self, authorization_id: int) -> DedupPlanAuthorization:
        authorization = self.get_authorization(authorization_id)

        if authorization is None:
            raise ValueError(f"Dedup plan authorization {authorization_id} not found")

        return authorization


def _validity_to_json(validity: PlanValidity) -> dict:
    return asdict(validity)
