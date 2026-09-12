from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.models.dedup_execution_plan import DedupPlanActionType


class DedupExecutionStatus(str, Enum):
    """The only states a DedupExecution can actually be in. No
    persisted "requested"/"pending" state exists before this: if
    `DedupExecutionService.start_execution`'s pre-checks fail (missing
    authorization, authorization not AUTHORIZED, an execution already
    exists for it, or a fresh plan-validity re-check fails), it raises
    before ever creating a row - mirroring how `authorize_plan` and
    `generate_plan_for_review` already behave. A row only ever comes
    into existence already RUNNING.

    No separate EXECUTION_STARTED state either: the moment a
    DedupExecution row is created IS the moment execution starts -
    there is no real, observable interval between "started" and
    "running" for this system's synchronous, one-attempt-at-a-time
    action loop, so persisting both would be recording a distinction
    that can never actually be seen, not documenting a fact.
    """

    # In progress: started_at is set, ended_at is null. A future
    # executor is (or was, if it crashed) working through this
    # execution's planned actions one at a time.
    RUNNING = "running"

    # Finished, and EVERY planned action for this execution's plan
    # succeeded (SUCCESS). The only status meaning "nothing left
    # undone, nothing failed."
    COMPLETED = "completed"

    # Finished, and NOT A SINGLE planned action succeeded before
    # execution stopped - the very first attempted action already
    # failed or hit a precondition failure. Distinct from
    # PARTIALLY_COMPLETED specifically so a caller can tell "made zero
    # progress" apart from "made some progress, then stopped."
    FAILED = "failed"

    # Finished, but stopped before every planned action was attempted
    # because the default stop-on-first-failure policy was triggered -
    # AND at least one action had already succeeded first. Only
    # introduced because it is genuinely distinguishable from FAILED
    # (>=1 success happened) and from COMPLETED (>=1 action did not
    # succeed) - not a speculative extra state.
    PARTIALLY_COMPLETED = "partially_completed"


class DedupExecutionActionResult(str, Enum):
    """The actual outcome of one planned action - never confused with
    what `DedupExecutionPlanAction` merely proposed. All four values
    are outcomes a plausible executor can really produce; none is
    invented for a capability (move/rename/quarantine, retry, etc.)
    that doesn't exist anywhere in this codebase.
    """

    # The planned operation was performed and a filesystem mutation
    # actually occurred.
    SUCCESS = "success"

    # The mandatory immediately-before-mutation revalidation (hash,
    # size, path, or type) did not match what the plan expected - the
    # executor correctly refused to act rather than guessing.
    # filesystem_mutation_occurred is always False for this result:
    # by definition, a precondition check runs strictly before any
    # mutation is attempted.
    PRECONDITION_FAILED = "precondition_failed"

    # The operation was attempted (preconditions passed) but the
    # filesystem operation itself failed - permissions, I/O error, or
    # any other OS-level failure unrelated to staleness.
    FAILED = "failed"

    # Execution stopped (per the stop-on-first-failure policy, or a
    # process interruption) before this action was ever reached.
    # filesystem_mutation_occurred is always False: nothing was ever
    # attempted. started_at/ended_at are always null for this result -
    # there is no real attempt interval to time.
    NOT_ATTEMPTED = "not_attempted"


class DedupExecution(Base):
    """One authorized attempt to act on a DedupExecutionPlan - the
    AUDIT record of a (future) executor's run, never itself a
    filesystem action. Creating, reading, or finalizing a
    DedupExecution performs zero filesystem writes; there is still no
    filesystem executor anywhere in AI_Brain.

    Bound to exactly ONE `DedupPlanAuthorization`, enforced by the
    unique constraint on `authorization_id`: unlike authorizations
    (which permit revoke-then-reauthorize against the same plan, each
    with a fresh validity check), an authorization backs **at most one
    execution ever** - there is no retry path through the same
    authorization. A genuinely new attempt requires a brand-new
    authorization (which itself requires today's authorization to be
    revoked first, and re-passes a fresh plan-validity check) - this is
    "duplicate execution prevention," and it is deliberately stricter
    than authorization's own "at most one ACTIVE" rule, because
    retrying a filesystem action silently is exactly the behavior this
    architecture exists to prevent ("do not retry indefinitely").

    `plan_id` duplicates `DedupPlanAuthorization.plan_id` onto this row
    directly (reachable via `authorization_id` anyway) - the same
    self-description convention already used by
    `DedupExecutionPlanAction.target_document_id` and
    `DedupExecutionPlan.canonical_document_id`.

    Mutable exactly once, unlike the fully immutable `DedupExecutionPlan`:
    `status`/`ended_at`/`failure_reason` move from their initial RUNNING
    values to a terminal value exactly one time, via
    `DedupExecutionService.complete_execution`. There is no method
    anywhere that transitions a terminal execution again.
    """

    __tablename__ = "dedup_executions"
    __table_args__ = (
        UniqueConstraint(
            "authorization_id",
            name="uq_dedup_executions_authorization_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    authorization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dedup_plan_authorizations.id"),
        nullable=False,
        index=True,
    )

    plan_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dedup_execution_plans.id"),
        nullable=False,
        index=True,
    )

    status: Mapped[DedupExecutionStatus] = mapped_column(
        SQLEnum(DedupExecutionStatus, name="dedup_execution_status"),
        nullable=False,
        default=DedupExecutionStatus.RUNNING,
        server_default=DedupExecutionStatus.RUNNING.name,
    )

    # Optional free-text identity/version of whatever (future) executor
    # performed this run - unconstrained format, since no real executor
    # exists yet to define a convention for it.
    executor_identity: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Null while RUNNING - set exactly once, by complete_execution, the
    # moment a terminal status is reached.
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Human-readable summary of why this execution did not COMPLETE -
    # derived automatically from the first non-SUCCESS action audit row
    # at finalization time, never accepted as free-form caller input,
    # so a caller cannot describe a failure differently than what the
    # action audit trail actually shows. Null for COMPLETED executions.
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class DedupExecutionActionAudit(Base):
    """What ACTUALLY happened for one planned action, within one
    execution - never to be confused with `DedupExecutionPlanAction`
    (what was merely *proposed*). The plan says "what we intended to
    do"; this row says "what actually happened." Every field prefixed
    `expected_`/`planned_` is a frozen copy of the plan action's own
    data (the intent); every field prefixed `observed_`, plus
    `result`/`filesystem_mutation_occurred`/`error_message`, describes
    reality as this specific attempt found and left it.

    Immutable once inserted - like `DedupExecutionPlanAction`, no field
    is ever updated after creation (no `updated_at`, no update method
    anywhere in `DedupExecutionService`). A recorded outcome is a
    permanent historical fact.

    Bound to exactly one (execution_id, plan_action_id) pair via the
    unique constraint below - a single execution records each of its
    plan's actions' outcomes exactly once. Once an execution is
    finalized (`complete_execution`), it must have exactly one of these
    rows per `DedupExecutionPlanAction` belonging to its plan - a
    NOT_ATTEMPTED row is itself a recorded fact, not the *absence* of
    one, precisely so "not attempted" is never confused with "the audit
    system forgot to record this."
    """

    __tablename__ = "dedup_execution_action_audits"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "plan_action_id",
            name="uq_dedup_execution_action_audits_execution_id_plan_action_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    execution_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dedup_executions.id"),
        nullable=False,
        index=True,
    )

    plan_action_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dedup_execution_plan_actions.id"),
        nullable=False,
        index=True,
    )

    # The document the planned action concerned - duplicated from the
    # plan action for the same self-description reason as
    # DedupExecutionPlanAction.target_document_id.
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id"),
        nullable=False,
        index=True,
    )

    # --- What was PLANNED (frozen copies of the plan action's own
    # fields at the moment this outcome was recorded - never live
    # values, never re-derived later) ---
    planned_action: Mapped[DedupPlanActionType] = mapped_column(
        SQLEnum(DedupPlanActionType, name="dedup_plan_action_type"),
        nullable=False,
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    target_path: Mapped[str] = mapped_column(Text, nullable=False)
    expected_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    expected_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- What was ACTUALLY observed/decided at attempt time ---
    result: Mapped[DedupExecutionActionResult] = mapped_column(
        SQLEnum(DedupExecutionActionResult, name="dedup_execution_action_result"),
        nullable=False,
    )
    observed_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    observed_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Explicit, queryable proof of whether disk was actually touched -
    # never left implicit in `result` alone. Always False for
    # PRECONDITION_FAILED and NOT_ATTEMPTED (enforced by
    # DedupExecutionService.record_action_result), always True for
    # SUCCESS (today's only action type, DELETE, has no successful
    # no-op form).
    filesystem_mutation_occurred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Null for NOT_ATTEMPTED (nothing was ever attempted, so there is
    # no real interval to time); set for every other result.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
