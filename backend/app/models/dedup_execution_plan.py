from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DedupPlanStatus(str, Enum):
    # The only state reachable in this milestone: a plan is a point-in-
    # time snapshot of what *would* happen, successfully produced. There
    # is deliberately no EXECUTED/FAILED/ABORTED value yet - those only
    # mean something once an executor exists, and inventing them now
    # would document a lifecycle this codebase can't actually enter.
    # Adding them is a small, ordinary migration when that day comes.
    GENERATED = "generated"


class DedupPlanActionType(str, Enum):
    # The only action this system can ever propose today - a candidate
    # for removal in favor of the plan's canonical. No move/rename/
    # quarantine action type exists because no such capability exists
    # anywhere in AI_Brain; adding the enum value without the capability
    # would imply a promise this codebase doesn't keep.
    DELETE = "delete"


class DedupExecutionPlan(Base):
    """A dry-run execution plan for an already-APPROVED DuplicateReview -
    what *would* happen to which files, if a filesystem executor existed.

    No executor exists anywhere in AI_Brain. Generating a plan reads
    files from disk (to observe their current hash/size - the whole
    point is to snapshot reality, not to trust stale review-time data)
    but never writes, moves, deletes, or otherwise modifies anything.

    A plan is an immutable audit record, not a live/mutable job: every
    call to DedupExecutionPlanService.generate_plan_for_review() creates
    a brand new row rather than updating an existing one, the same way
    ToolCallRecord/Memory never overwrite history. Regenerating a plan
    for the same review is expected and unremarkable - e.g. after the
    filesystem changed and an earlier plan went stale - so plans are
    listed, not looked up singularly by review_id.

    Staleness is never a persisted field on this row: whether a plan is
    still safe to act on can only be answered by re-reading the
    filesystem *at the moment you're asking* (see
    DedupExecutionPlanService.check_plan_validity), not by trusting a
    cached verdict computed at some earlier point. Caching that verdict
    here would recreate exactly the bug class this whole feature exists
    to prevent.
    """

    __tablename__ = "dedup_execution_plans"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    review_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("duplicate_reviews.id"),
        nullable=False,
        index=True,
    )

    # Snapshot of DuplicateReview.human_selected_canonical_document_id
    # at generation time - the specific human decision that authorized
    # this plan. Copied here (not just reachable via review_id) so a
    # plan answers "which document is the keeper" on its own, without
    # requiring a join back to a review that - by design - can never
    # change after approval anyway (see DedupReviewService.approve_review).
    canonical_document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id"),
        nullable=False,
    )

    # The canonical's own observed state at generation time - stored on
    # the plan itself (not an action row) because a plan has exactly one
    # canonical; nothing "happens to" it, so it isn't a DedupExecutionPlanAction.
    canonical_source_path: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_observed_exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    canonical_observed_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    canonical_observed_file_size: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    status: Mapped[DedupPlanStatus] = mapped_column(
        SQLEnum(DedupPlanStatus, name="dedup_plan_status"),
        nullable=False,
        default=DedupPlanStatus.GENERATED,
        server_default=DedupPlanStatus.GENERATED.name,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    # Deliberately no updated_at: a plan row is never mutated after
    # creation. Every regeneration is a new row, not an edit.


class DedupExecutionPlanAction(Base):
    """One proposed filesystem action within a DedupExecutionPlan - one
    row per non-canonical document in the authorizing review.

    Deliberately does not store any file content, only metadata small
    enough to identify and later re-verify the file: its path, and the
    hash/size observed when the plan was generated. Re-observing these
    same three things later (path exists, hash matches, size matches)
    is exactly what DedupExecutionPlanService.check_plan_validity does
    before anything is ever allowed to act on this row.
    """

    __tablename__ = "dedup_execution_plan_actions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    plan_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dedup_execution_plans.id"),
        nullable=False,
        index=True,
    )

    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id"),
        nullable=False,
        index=True,
    )

    action: Mapped[DedupPlanActionType] = mapped_column(
        SQLEnum(DedupPlanActionType, name="dedup_plan_action_type"),
        nullable=False,
        default=DedupPlanActionType.DELETE,
        server_default=DedupPlanActionType.DELETE.name,
    )

    source_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Redundant with DedupExecutionPlan.canonical_document_id/
    # canonical_source_path (every action in one plan shares the same
    # canonical) - duplicated onto each row anyway so a single action
    # row is self-describing without a join, matching this codebase's
    # existing evidence-snapshot convention (e.g. Message.citations,
    # DuplicateReview.evidence).
    target_document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id"),
        nullable=False,
    )
    target_path: Mapped[str] = mapped_column(Text, nullable=False)

    observed_exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    observed_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Why this specific file was proposed for this specific action -
    # distinct from DuplicateReview.recommendation_reason (which
    # explains the finding as a whole, not one file's fate).
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
