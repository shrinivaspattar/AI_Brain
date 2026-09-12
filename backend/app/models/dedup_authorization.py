from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DedupPlanAuthorizationStatus(str, Enum):
    # Authorization was granted: at the moment it was created, the
    # authorizing DuplicateReview was APPROVED and the DedupExecutionPlan
    # passed a freshly re-run validity check. This is NOT a filesystem
    # execution and NOT a guarantee that the filesystem still matches
    # that check a moment later - see DedupPlanAuthorizationService's
    # docstring for the TOCTOU boundary this status does and doesn't cover.
    AUTHORIZED = "authorized"
    # A human explicitly cancelled a previously-granted authorization
    # before anything consumed it. Kept, not deleted, as a record of
    # what was authorized and then withdrawn (mirrors DuplicateReview
    # keeping a rejected finding rather than deleting it).
    REVOKED = "revoked"

    # Deliberately no EXECUTED/FAILED value: no filesystem executor
    # exists anywhere in this codebase yet, so there is no event that
    # could ever produce that status. Inventing it now would document a
    # lifecycle stage this system cannot enter - adding it later, when
    # an executor exists to actually reach it, is an ordinary migration.


class DedupPlanAuthorization(Base):
    """Explicit permission to (eventually) act on ONE specific,
    immutable DedupExecutionPlan - deliberately a separate fact from
    both the plan's own existence and its authorizing review's
    approval. Neither `DuplicateReview.status == APPROVED` nor
    `DedupExecutionPlan.status == GENERATED` is ever treated as
    permission to touch a file; only a `DedupPlanAuthorization` row
    with `status == AUTHORIZED` (and, even then, only after a *fresh*
    re-check at the moment of use - see below) represents that.

    Creating, revoking, or reading an authorization never touches a
    file. There is still no filesystem executor anywhere in AI_Brain -
    this table exists to be the thing a future executor is required to
    check, not to perform anything itself.

    Critical TOCTOU boundary: `validity_snapshot` freezes what
    `DedupExecutionPlanService.check_plan_validity` reported at the
    moment authorization was granted. It is proof of what was true
    then - it is explicitly NOT a guarantee that the filesystem still
    matches now. A future executor MUST re-run `check_plan_validity`
    itself immediately before acting, every single time, regardless of
    what this row says or how recently it was authorized. This row
    only answers "was this ever authorized, under what conditions, and
    is that authorization still active" - never "is it still safe to
    act on right now."
    """

    __tablename__ = "dedup_plan_authorizations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # The ONE plan this authorization is bound to - never reassignable.
    # No method anywhere changes this after creation.
    plan_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dedup_execution_plans.id"),
        nullable=False,
        index=True,
    )

    status: Mapped[DedupPlanAuthorizationStatus] = mapped_column(
        SQLEnum(DedupPlanAuthorizationStatus, name="dedup_plan_authorization_status"),
        nullable=False,
        default=DedupPlanAuthorizationStatus.AUTHORIZED,
        server_default=DedupPlanAuthorizationStatus.AUTHORIZED.name,
    )

    # A frozen copy of DedupExecutionPlanService.check_plan_validity's
    # result at the exact moment authorization was granted (see
    # dataclasses.asdict(PlanValidity)) - proof the required pre-checks
    # actually passed then. Deliberately NOT a copy of the plan/actions
    # themselves (those stay reachable via plan_id, and never change).
    validity_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Optional free-text context from the human granting authorization -
    # this is a single-user system with no account model, so "who"
    # authorized something is always "the one human operating this
    # system"; this field is for their own optional notes on why,
    # mirroring DuplicateReview.reviewer_decision.
    authorized_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
