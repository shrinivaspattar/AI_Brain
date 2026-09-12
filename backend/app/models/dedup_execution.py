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

    # Finalized, but at least one of its action audits has result=UNKNOWN
    # - some action's fate could not be confirmed (the classic case: an
    # executor process died between attempting a mutation and persisting
    # its outcome). Takes priority over COMPLETED/FAILED/PARTIALLY_COMPLETED
    # regardless of how many other actions cleanly succeeded or failed:
    # this system must never describe an execution as "completed" or
    # "failed" while part of its own story is unknown. This is a real,
    # reachable state - not speculative - because DedupExecutionActionResult.
    # UNKNOWN exists specifically to represent a crash-recovery finding
    # (see that enum's docstring), and complete_execution refuses to
    # finalize an execution with any UNRECORDED action at all - so an
    # UNKNOWN row is exactly how an unresolved crash gets represented
    # once someone (a future recovery pass, an operator) is ready to
    # close the execution's bookkeeping without pretending certainty
    # that doesn't exist.
    NEEDS_REVIEW = "needs_review"


class DedupExecutionActionResult(str, Enum):
    """The actual outcome of one planned action - never confused with
    what `DedupExecutionPlanAction` merely proposed. Every value is an
    outcome a plausible executor can really produce; none is invented
    for a capability (move/rename/quarantine, retry, etc.) that
    doesn't exist anywhere in this codebase.

    Audit ordering contract, load-bearing for every value below: a
    future executor MUST attempt the filesystem operation first, then
    call `DedupExecutionService.record_action_result` exactly once
    with the outcome it actually observed - never the reverse, and
    never speculatively before the attempt. If the process dies
    between those two steps, NO row is ever written for that action;
    the gap is indistinguishable, from the database alone, from "never
    reached." Resolving that gap requires independent evidence (e.g.
    re-observing the filesystem) gathered by a future recovery step -
    out of scope for this codebase today - which then writes exactly
    one row per unresolved action through this same method: either a
    definite result if it could independently confirm one, or UNKNOWN
    if it genuinely could not.
    """

    # The planned operation was performed and a filesystem mutation
    # actually occurred. filesystem_mutation_occurred is always True.
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
    # any other OS-level failure unrelated to staleness. The executor
    # is alive and reporting a definite, observed outcome here -
    # filesystem_mutation_occurred may be True or False, since a
    # failure can occur on either side of the actual mutation and the
    # executor's own report is what's trusted. Distinct from UNKNOWN:
    # FAILED means "we got a definite answer, and it was an error."
    FAILED = "failed"

    # Execution stopped (per the stop-on-first-failure policy) before
    # this action was ever reached. filesystem_mutation_occurred is
    # always False: nothing was ever attempted. started_at/ended_at
    # are always null for this result - there is no real attempt
    # interval to time.
    NOT_ATTEMPTED = "not_attempted"

    # The action was (or may have been) attempted, but its outcome
    # could not be confirmed and persisted through the normal path -
    # the canonical case is an executor process crashing after
    # performing (or starting) the filesystem operation but before
    # calling record_action_result. This row, when it exists, is
    # always written well after the fact by a future recovery step,
    # never by a live in-progress executor. filesystem_mutation_occurred
    # is always None/NULL for this result - the entire point is that
    # whether a mutation occurred is genuinely unknown, and a plain
    # True/False would falsely claim certainty that doesn't exist.
    # ended_at is always null (there is no confirmed completion time
    # for an indeterminate outcome); started_at MAY be set if a
    # recovery step has independent evidence of when the attempt
    # began. An execution containing any UNKNOWN row is always
    # classified DedupExecutionStatus.NEEDS_REVIEW, never COMPLETED/
    # FAILED/PARTIALLY_COMPLETED - see that enum's docstring. This
    # value must NEVER be silently treated as equivalent to SUCCESS or
    # to FAILED by any code in this codebase.
    UNKNOWN = "unknown"


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

    # Set exactly once, by DedupFilesystemExecutor._claim_execution,
    # under a SELECT ... FOR UPDATE lock on this row - the exclusivity
    # mechanism that makes "exactly one executor may own this
    # execution" a real, database-enforced guarantee rather than an
    # assumption about process topology. Never cleared or reused: a
    # claimed execution stays claimed even if the claiming process
    # then crashes, exactly like a terminal execution stays terminal -
    # a second attempt requires DedupExecutionService.
    # recover_stale_execution, never a second claim on the same row.
    # Deliberately separate from `status`, which stays RUNNING for the
    # entire loop and cannot itself distinguish "not yet started" from
    # "in progress" the way this field does.
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set exactly once, by DedupExecutionService.recover_stale_execution,
    # under the same SELECT ... FOR UPDATE discipline _claim_execution
    # already uses - the concurrency-exclusivity parity fix identified
    # during the Executor Reconciliation & TOCTOU Strategy design pass.
    # Guards against two concurrent recover_stale_execution calls on the
    # same execution. Does NOT guard against a still-alive "crashed"
    # executor concurrently calling record_action_result outside the
    # recovery path itself - that residual is bounded only by
    # DedupExecutionActionAudit's own (execution_id, plan_action_id)
    # unique constraint, and fully closing it would require restructuring
    # record_action_result's per-call commit behavior, out of scope for
    # this milestone. Deliberately separate from claimed_at, which an
    # execution may never have acquired at all (e.g. it crashed between
    # start_execution and the executor's first _claim_execution call) -
    # recovery must remain possible in that case too, so this column
    # cannot simply reuse claimed_at's own semantics.
    recovery_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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

    # Explicit, queryable, TRI-STATE proof of whether disk was actually
    # touched - never left implicit in `result` alone, and never
    # forced into a binary answer when the honest answer is "we don't
    # know." True = the mutation demonstrably occurred. False = the
    # mutation demonstrably did NOT occur (never attempted, or
    # attempted and the filesystem confirmed unchanged). NULL/None =
    # unknown/indeterminate - reserved exclusively for
    # result=UNKNOWN, and must never be treated as equivalent to
    # False. Enforced by DedupExecutionService.record_action_result:
    # always False for PRECONDITION_FAILED/NOT_ATTEMPTED, always True
    # for SUCCESS (today's only action type, DELETE, has no successful
    # no-op form), always None for UNKNOWN, and either True or False
    # (never None) for FAILED.
    #
    # Deliberately NO column-level `default=` here: SQLAlchemy applies
    # a scalar column default whenever the value being flushed is
    # `None`, regardless of whether that `None` was an explicit,
    # deliberate assignment or simply never set - it cannot tell the
    # difference. A `default=False` would silently coerce every
    # genuinely-unknown UNKNOWN row into `False` at insert time,
    # destroying the exact distinction this column exists to make.
    # `DedupExecutionService.record_action_result` supplies an
    # explicit value on every insert (defaulting to `False` at the
    # Python call-site, not here), so no column-level default is ever
    # needed.
    filesystem_mutation_occurred: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Null for NOT_ATTEMPTED (nothing was ever attempted, so there is
    # no real interval to time); set for every other result except
    # UNKNOWN, where started_at MAY be set (a recovery step might have
    # independent evidence of when the attempt began) but ended_at is
    # ALWAYS null - there is no confirmed completion time for an
    # indeterminate outcome, by definition.
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


class DedupExecutionActionReconciliation(Base):
    """A human's later, independently-verified finding about one
    UNKNOWN `DedupExecutionActionAudit` row - see "Executor
    Reconciliation & TOCTOU Strategy" in AI_Brain_Architecture.md for
    the full design this implements. Purely additive: creating this row
    NEVER edits the audit row it reconciles, and NEVER changes
    `DedupExecution.status` - a reconciliation answers "what do we now
    believe happened," not "what should history have said."

    Bound to exactly one audit via the unique constraint below - once
    an UNKNOWN action is reconciled, it is reconciled once, permanently;
    there is no update method anywhere in `DedupReconciliationService`.
    """

    __tablename__ = "dedup_execution_action_reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "audit_id",
            name="uq_dedup_execution_action_reconciliations_audit_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    audit_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dedup_execution_action_audits.id"),
        nullable=False,
        index=True,
    )

    # Restricted, at the service layer only (DedupReconciliationService.
    # reconcile_action), to exactly SUCCESS or FAILED - reusing the
    # existing DedupExecutionActionResult enum's own storage rather than
    # inventing a near-duplicate two-value enum. UNKNOWN reconciled to
    # UNKNOWN would be a no-op not worth a row; PRECONDITION_FAILED and
    # NOT_ATTEMPTED never need reconciling in the first place, since
    # only an audit whose result is already UNKNOWN is eligible for
    # reconciliation at all. Enforced in Python, not by a database CHECK
    # constraint - matching this codebase's existing convention of
    # enforcing definitional invariants at the service layer (e.g.
    # `record_action_result`'s own tri-state `filesystem_mutation_
    # occurred` checks) rather than pushing them into the schema.
    verified_result: Mapped[DedupExecutionActionResult] = mapped_column(
        SQLEnum(DedupExecutionActionResult, name="dedup_execution_action_result"),
        nullable=False,
    )

    # Free text, same "no user accounts anywhere in this codebase"
    # convention as DedupPlanAuthorization.authorized_by - but
    # deliberately REQUIRED (that field is nullable, this one is not):
    # a reconciliation exists specifically to record who made a manual
    # judgment call resolving an ambiguous, previously-indeterminate
    # filesystem outcome, so leaving this attribution optional would
    # undermine the accountability the whole record exists to provide.
    verified_by: Mapped[str] = mapped_column(Text, nullable=False)

    # Free text describing HOW the human verified this outcome (e.g.
    # "inspected the quarantine directory directly, confirmed the file
    # present with the expected name and content"). Never parsed or
    # interpreted by any code - purely for a later human reader.
    verification_method: Mapped[str] = mapped_column(Text, nullable=False)

    # DedupReconciliationService's OWN fresh, independent re-observation
    # of the audit's source_path, taken at reconciliation time -
    # captured regardless of the human's claim, and required to be
    # CONSISTENT with verified_result before this row is ever created
    # (see DedupReconciliationService.reconcile_action). Never derived
    # from, or copied out of, the human's verification_method text -
    # this is what the SYSTEM independently saw, not what the human
    # reported seeing.
    observed_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    observed_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
