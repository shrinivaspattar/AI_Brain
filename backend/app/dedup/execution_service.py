from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import DedupExecutionPlanService, _observe_file
from app.models.dedup_authorization import DedupPlanAuthorizationStatus
from app.models.dedup_execution import (
    DedupExecution,
    DedupExecutionActionAudit,
    DedupExecutionActionResult,
    DedupExecutionStatus,
)
from app.models.dedup_execution_plan import DedupExecutionPlanAction

DEFAULT_LIST_LIMIT = 100

# Results that, by definition, can never involve a filesystem mutation:
# a precondition check runs strictly before any mutation is attempted,
# and a not-attempted action was never reached at all.
_NO_MUTATION_RESULTS = frozenset(
    {
        DedupExecutionActionResult.PRECONDITION_FAILED,
        DedupExecutionActionResult.NOT_ATTEMPTED,
    }
)


class DedupExecutionService:
    """The EXECUTION AUDIT layer - records what a (future) filesystem
    executor actually did, one authorized attempt and one action at a
    time. No method here performs any filesystem operation: this
    service is pure bookkeeping over facts a caller reports. There is
    still no filesystem executor anywhere in AI_Brain.

        Detection -> Recommendation -> Human Review -> Approval
            -> Dry-run Execution Plan
            -> Explicit Execution Authorization
            -> Filesystem Execution (not built)
            -> Verification (not built - see check_plan_validity reuse below)
            -> Execution Audit (this service)

    Review approval != plan generation != execution authorization !=
    execution. Each is a separate state/permission boundary, checked
    independently and fresh every time - `start_execution` does not
    trust that an authorization being AUTHORIZED still implies the
    plan is valid; it re-runs `check_plan_validity` itself, the same
    way `authorize_plan` does not trust that a plan's mere existence
    implies its review is still approved. An authorization does not
    automatically become an execution record: `start_execution` must
    be called explicitly, and can fail even for a perfectly valid,
    still-AUTHORIZED authorization if the plan has gone stale in the
    meantime.
    """

    def __init__(self, db: Session):
        self.db = db
        self.authorization_service = DedupPlanAuthorizationService(db)
        self.plan_service = DedupExecutionPlanService(db)

    def start_execution(
        self,
        authorization_id: int,
        executor_identity: str | None = None,
    ) -> DedupExecution:
        """Begin one execution attempt under one authorization. This
        does not perform any filesystem action - it only records that
        a (future) executor is beginning to act, after re-confirming
        every precondition fresh:

        1. The authorization must exist (fetched fresh).
        2. Its status must be AUTHORIZED right now - a revoked
           authorization can never be executed, no matter how recently
           it was authorized.
        3. No `DedupExecution` may already exist for this
           authorization - **duplicate execution prevention**: an
           authorization backs at most one execution ever (also
           enforced by a DB unique constraint on `authorization_id`).
           Unlike authorization's own "at most one ACTIVE" rule, this
           has no revoke-and-retry path through the SAME authorization
           - a genuine retry requires a brand new authorization, which
           itself requires a fresh plan-validity check.
        4. The plan must still pass a freshly-run `check_plan_validity`
           right now - TOCTOU: being AUTHORIZED is necessary but not
           sufficient. The filesystem may have changed in the time
           between authorization and this call, and that must be
           caught here, not assumed away.

        Raises ValueError for every failed precondition; no row is
        ever created for a failed attempt.
        """
        authorization = self.authorization_service.get_authorization(
            authorization_id
        )
        if authorization is None:
            raise ValueError(
                f"Dedup plan authorization {authorization_id} not found"
            )

        if authorization.status != DedupPlanAuthorizationStatus.AUTHORIZED:
            raise ValueError(
                f"Authorization {authorization_id} is not active "
                f"(status={authorization.status.value}) - execution requires "
                "an authorization that is currently AUTHORIZED"
            )

        existing = self._get_execution_for_authorization(authorization_id)
        if existing is not None:
            raise ValueError(
                f"Authorization {authorization_id} already has an execution "
                f"(execution {existing.id}, status={existing.status.value}) - "
                "an authorization can back at most one execution; a new "
                "attempt requires a new authorization"
            )

        validity = self.plan_service.check_plan_validity(authorization.plan_id)
        if not validity.is_valid:
            raise ValueError(
                f"Dedup execution plan {authorization.plan_id} is no longer "
                "valid - the filesystem or document state has changed since "
                "this plan was authorized. Execution refused."
            )

        execution = DedupExecution(
            authorization_id=authorization_id,
            plan_id=authorization.plan_id,
            status=DedupExecutionStatus.RUNNING,
            executor_identity=executor_identity,
            started_at=datetime.now(UTC),
        )
        self.db.add(execution)

        try:
            self.db.commit()
            self.db.refresh(execution)
            return execution
        except Exception:
            self.db.rollback()
            raise

    def record_action_result(
        self,
        execution_id: int,
        plan_action_id: int,
        result: DedupExecutionActionResult,
        *,
        observed_content_hash: str | None = None,
        observed_file_size: int | None = None,
        filesystem_mutation_occurred: bool | None = False,
        error_message: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> DedupExecutionActionAudit:
        """Record what ACTUALLY happened for one planned action. Pure
        bookkeeping - this never performs a filesystem operation
        itself; a caller (the future executor, OR a future recovery
        step reconstructing an indeterminate outcome after a crash -
        see DedupExecutionActionResult.UNKNOWN) reports an outcome it
        already observed elsewhere, and this persists it as a
        permanent, immutable fact. Audit ordering contract: the real
        attempt (or the recovery investigation) must already be
        complete before this is called - this method never precedes
        the fact it records.

        `planned_action`/`source_path`/`target_path`/
        `expected_content_hash`/`expected_file_size`/`document_id` are
        never accepted as parameters - they are always copied straight
        from the plan action's own frozen row, so a caller cannot
        describe "what was planned" any differently than the plan
        itself says. Only what was actually OBSERVED/DECIDED is
        caller-supplied.

        Preconditions (ValueError, no row created on failure):
        1. The execution must exist.
        2. The execution must still be RUNNING - a terminal execution
           (COMPLETED/FAILED/PARTIALLY_COMPLETED/NEEDS_REVIEW) never
           accepts a new action result.
        3. The plan action must belong to the SAME plan as this
           execution.
        4. No audit row may already exist for this
           (execution_id, plan_action_id) pair (also enforced by a DB
           unique constraint) - exactly one recorded outcome per
           action per execution.

        Definitional consistency checks on the TRI-STATE
        `filesystem_mutation_occurred` (True / False / None=unknown),
        each a ValueError if violated:
        - PRECONDITION_FAILED and NOT_ATTEMPTED must report False - by
          definition, a precondition check runs strictly before any
          mutation, and a not-attempted action was never reached.
        - SUCCESS must report True - today's only action type,
          DELETE, has no successful no-op form.
        - UNKNOWN must report None - the entire point of this result
          is that whether a mutation occurred is genuinely unknown;
          reporting True or False here would falsely claim a certainty
          that doesn't exist. UNKNOWN also forces `ended_at` to be
          None - there is no confirmed completion time for an
          indeterminate outcome.
        - Every other result (FAILED, and any future addition) must
          report a definite True or False, never None - only UNKNOWN
          may leave the mutation outcome indeterminate.
        """
        execution = self._get_execution_or_raise(execution_id)

        if execution.status != DedupExecutionStatus.RUNNING:
            raise ValueError(
                f"Execution {execution_id} is not RUNNING "
                f"(status={execution.status.value}) - a finalized execution "
                "cannot record further action results"
            )

        plan_action = self.db.get(DedupExecutionPlanAction, plan_action_id)
        if plan_action is None:
            raise ValueError(f"Dedup execution plan action {plan_action_id} not found")

        if plan_action.plan_id != execution.plan_id:
            raise ValueError(
                f"Plan action {plan_action_id} belongs to plan "
                f"{plan_action.plan_id}, not execution {execution_id}'s plan "
                f"{execution.plan_id}"
            )

        existing = self.db.scalar(
            select(DedupExecutionActionAudit)
            .where(DedupExecutionActionAudit.execution_id == execution_id)
            .where(DedupExecutionActionAudit.plan_action_id == plan_action_id)
        )
        if existing is not None:
            raise ValueError(
                f"Execution {execution_id} already has a recorded result for "
                f"plan action {plan_action_id} (audit {existing.id}) - an "
                "action's outcome is recorded exactly once per execution"
            )

        if result == DedupExecutionActionResult.UNKNOWN:
            if filesystem_mutation_occurred is not None:
                raise ValueError(
                    "result=unknown requires filesystem_mutation_occurred=None "
                    "- reporting True or False would falsely claim a certainty "
                    "that doesn't exist; if the outcome is actually known, use "
                    "success, precondition_failed, failed, or not_attempted "
                    "instead"
                )
            if ended_at is not None:
                raise ValueError(
                    "result=unknown requires ended_at=None - there is no "
                    "confirmed completion time for an indeterminate outcome"
                )
        elif filesystem_mutation_occurred is None:
            raise ValueError(
                f"result={result.value} requires a definite "
                "filesystem_mutation_occurred (true or false) - only "
                "result=unknown may leave it indeterminate"
            )
        elif result in _NO_MUTATION_RESULTS and filesystem_mutation_occurred:
            raise ValueError(
                f"result={result.value} can never report "
                "filesystem_mutation_occurred=True - a precondition check "
                "runs strictly before any mutation, and a not-attempted "
                "action was never reached"
            )
        elif result == DedupExecutionActionResult.SUCCESS and not filesystem_mutation_occurred:
            raise ValueError(
                "result=success requires filesystem_mutation_occurred=True - "
                "the only action type this system can propose (delete) has "
                "no successful no-op form"
            )

        audit = DedupExecutionActionAudit(
            execution_id=execution_id,
            plan_action_id=plan_action_id,
            document_id=plan_action.document_id,
            planned_action=plan_action.action,
            source_path=plan_action.source_path,
            target_path=plan_action.target_path,
            expected_content_hash=plan_action.observed_content_hash,
            expected_file_size=plan_action.observed_file_size,
            result=result,
            observed_content_hash=observed_content_hash,
            observed_file_size=observed_file_size,
            filesystem_mutation_occurred=filesystem_mutation_occurred,
            error_message=error_message,
            started_at=started_at,
            ended_at=ended_at,
        )
        self.db.add(audit)

        try:
            self.db.commit()
            self.db.refresh(audit)
            return audit
        except Exception:
            self.db.rollback()
            raise

    def complete_execution(self, execution_id: int) -> DedupExecution:
        """Finalize a RUNNING execution's overall status - DERIVED
        entirely from its own recorded `DedupExecutionActionAudit`
        rows, never accepted as caller input, so nothing can claim
        "completed" when the audit trail says otherwise.

        Requires the audit trail to be COMPLETE first: every
        `DedupExecutionPlanAction` belonging to this execution's plan
        must have exactly one corresponding audit row (raises
        ValueError otherwise) - "do not claim the whole plan
        completed" when some actions were never even recorded,
        attempted or not.

        Overall status:
        - NEEDS_REVIEW: at least one action's result is UNKNOWN - takes
          priority over every other classification below, regardless
          of how many other actions cleanly succeeded or failed. This
          system must never describe an execution as COMPLETED/FAILED/
          PARTIALLY_COMPLETED while part of its own story is
          genuinely unknown (see DedupExecutionStatus.NEEDS_REVIEW).
        - COMPLETED: every action's result is SUCCESS.
        - FAILED: zero actions succeeded (the very first attempted
          action already failed or hit a precondition failure).
        - PARTIALLY_COMPLETED: at least one action succeeded, and at
          least one did not - execution stopped partway through, per
          the default stop-on-first-failure policy.

        `failure_reason` (for any non-COMPLETED outcome) is derived
        from the first UNKNOWN audit row if one exists, else the first
        non-SUCCESS audit row, in the order those rows were recorded -
        never accepted as free-form caller input.

        Raises ValueError if the execution does not exist, or is not
        currently RUNNING (a terminal execution can never be
        re-finalized).
        """
        execution = self._get_execution_or_raise(execution_id)

        if execution.status != DedupExecutionStatus.RUNNING:
            raise ValueError(
                f"Execution {execution_id} is not RUNNING "
                f"(status={execution.status.value}) - it has already been "
                "finalized and cannot be finalized again"
            )

        plan_action_ids = set(
            self.db.scalars(
                select(DedupExecutionPlanAction.id).where(
                    DedupExecutionPlanAction.plan_id == execution.plan_id
                )
            )
        )

        audits = list(
            self.db.scalars(
                select(DedupExecutionActionAudit)
                .where(DedupExecutionActionAudit.execution_id == execution_id)
                .order_by(DedupExecutionActionAudit.id)
            )
        )
        audited_plan_action_ids = {audit.plan_action_id for audit in audits}

        if audited_plan_action_ids != plan_action_ids:
            missing = plan_action_ids - audited_plan_action_ids
            raise ValueError(
                f"Execution {execution_id} cannot be finalized - "
                f"{len(missing)} of {len(plan_action_ids)} planned action(s) "
                "have no recorded outcome yet (plan action id(s) "
                f"{sorted(missing)}). Every planned action must be recorded "
                "as SUCCESS, PRECONDITION_FAILED, FAILED, NOT_ATTEMPTED, or "
                "UNKNOWN before an execution can be finalized."
            )

        unknowns = [
            a for a in audits if a.result == DedupExecutionActionResult.UNKNOWN
        ]

        if unknowns:
            first_unknown = unknowns[0]
            status = DedupExecutionStatus.NEEDS_REVIEW
            failure_reason = (
                f"Action for plan action {first_unknown.plan_action_id} "
                f"(document {first_unknown.document_id}) has an unknown/"
                "indeterminate outcome and requires manual review before "
                "this execution's overall result can be determined"
                + (f": {first_unknown.error_message}" if first_unknown.error_message else "")
            )
        else:
            successes = [
                a for a in audits if a.result == DedupExecutionActionResult.SUCCESS
            ]
            non_successes = [
                a for a in audits if a.result != DedupExecutionActionResult.SUCCESS
            ]

            if not non_successes:
                status = DedupExecutionStatus.COMPLETED
                failure_reason = None
            else:
                first_failure = non_successes[0]
                failure_reason = (
                    f"Action for plan action {first_failure.plan_action_id} "
                    f"(document {first_failure.document_id}) reported "
                    f"{first_failure.result.value}"
                    + (f": {first_failure.error_message}" if first_failure.error_message else "")
                )
                status = (
                    DedupExecutionStatus.FAILED
                    if not successes
                    else DedupExecutionStatus.PARTIALLY_COMPLETED
                )

        try:
            execution.status = status
            execution.failure_reason = failure_reason
            execution.ended_at = datetime.now(UTC)
            self.db.commit()
            self.db.refresh(execution)
            return execution
        except Exception:
            self.db.rollback()
            raise

    def _claim_for_recovery(self, execution_id: int) -> DedupExecution:
        """Acquire exclusive ownership of a stale-execution recovery
        attempt, before any action is re-observed or (re-)recorded -
        the same `SELECT ... FOR UPDATE` concurrency discipline
        `DedupFilesystemExecutor._claim_execution` already uses for the
        primary execution path, extended here to recovery per the
        "Executor Reconciliation & TOCTOU Strategy" design pass in
        AI_Brain_Architecture.md.

        Mechanism, identical in shape to `_claim_execution`: `SELECT
        ... FOR UPDATE` locks the `DedupExecution` row for this
        method's own transaction. A concurrent caller's own `SELECT
        ... FOR UPDATE` on the SAME row blocks until this transaction
        commits or rolls back, then re-reads the row's current state
        under its own lock. While holding that lock, this method checks
        and sets `recovery_claimed_at`: if already non-null, another
        caller won the race and this one refuses cleanly; otherwise,
        this caller sets it and commits, releasing the lock with the
        claim now durably recorded. Every failure path here raises a
        plain `ValueError` - never a raw `IntegrityError` and never an
        uncaught exception - because the losing caller is turned away
        before it can reach `record_action_result`'s own
        `(execution_id, plan_action_id)` unique constraint.

        Deliberately a SEPARATE field from `claimed_at`: an execution
        eligible for recovery may never have been claimed for execution
        at all (e.g. it crashed between `start_execution` and the
        executor's first `_claim_execution` call), so gating recovery
        on `claimed_at` being set would wrongly block recovering that
        execution.

        Residual, not closed by this method (see `DedupReconciliation
        Service`'s own docstring for the analogous reconciliation-side
        residual): this protects against two concurrent
        `recover_stale_execution` calls on the same execution, NOT
        against a "stale" execution whose original executor process is
        actually still alive and calling `record_action_result`
        directly, outside this method, at the same time - that
        remaining race is bounded only by
        `DedupExecutionActionAudit`'s own unique constraint, and fully
        closing it would require restructuring `record_action_result`'s
        per-call commit behavior, out of scope for this milestone.
        """
        execution = self.db.execute(
            select(DedupExecution)
            .where(DedupExecution.id == execution_id)
            .with_for_update()
        ).scalar_one_or_none()

        if execution is None:
            self.db.rollback()
            raise ValueError(f"Dedup execution {execution_id} not found")

        if execution.status != DedupExecutionStatus.RUNNING:
            status_value = execution.status.value
            self.db.rollback()
            raise ValueError(
                f"Execution {execution_id} is not RUNNING "
                f"(status={status_value}) - only a RUNNING execution can be "
                "recovered"
            )

        if execution.recovery_claimed_at is not None:
            recovery_claimed_at = execution.recovery_claimed_at
            self.db.rollback()
            raise ValueError(
                f"Execution {execution_id} is already being recovered by "
                f"another call, claimed at {recovery_claimed_at.isoformat()} - "
                "exactly one recovery attempt may run per execution; this one "
                "refuses rather than risk a concurrent duplicate action write"
            )

        execution.recovery_claimed_at = datetime.now(UTC)

        try:
            self.db.commit()
            self.db.refresh(execution)
            return execution
        except Exception:
            self.db.rollback()
            raise

    def recover_stale_execution(self, execution_id: int) -> DedupExecution:
        """Close out a RUNNING execution that will never receive any
        further action results - the canonical reason being that its
        executor process died. This performs NO filesystem action; it
        only reads the current filesystem state (a plain, non-mutating
        observation, exactly like `check_plan_validity` already does)
        to decide, per unresolved planned action, what CAN honestly be
        recorded about it - then finalizes the execution via the
        existing `complete_execution`.

        **AI_Brain has no process supervision anywhere** - it cannot
        know whether the execution's process is actually dead. Calling
        this method is an explicit human decision, made after the
        human has independently determined, outside this system, that
        the execution truly will not progress further (the API layer
        requires an explicit `confirm: true`, the same convention used
        for `authorize_plan`/`start_execution` - `confirm` is not a
        parameter here, matching how neither of those services accepts
        it either; it is purely a request-body safeguard at the API
        boundary). There is no automatic trigger anywhere that calls
        this - see `list_executions(status=RUNNING, started_before=...)`
        for a way to find *candidates* to investigate, which is advice,
        not a judgment.

        For each of the execution's planned actions with NO existing
        audit row, the current file is re-observed and classified into
        exactly one of two outcomes - never a guess at SUCCESS, per
        the explicit rule this design exists to enforce:

        - **File confirmed unchanged** (still exists, hash AND size
          exactly match what the plan expected before any mutation):
          recorded as `NOT_ATTEMPTED`
          (`filesystem_mutation_occurred=False`). This is not a guess -
          if the delete had happened, the file could not still be
          there with its original content; the mutation demonstrably
          did not occur, whether it was never reached or was attempted
          and failed leaving the file untouched (recovery cannot tell
          those two apart, and doesn't need to: the safe next step is
          identical either way).
        - **Anything else** - the file is now missing, or exists with
          different content than expected: recorded as `UNKNOWN`
          (`filesystem_mutation_occurred=None`), with whatever was
          observed attached for a human's later benefit via
          `observed_content_hash`/`observed_file_size`/`error_message`.
          A missing file is consistent with a successful delete, but
          recovery never promotes that consistency into a claimed
          `SUCCESS` - it cannot rule out the file having vanished for
          an unrelated reason (manual deletion, external
          interference), and asserting success without proof is
          exactly what this design refuses to do.

        Raises ValueError if the execution does not exist, is not
        currently RUNNING, already has a recorded outcome for every
        planned action (nothing to recover - call `complete_execution`
        directly instead), or is already being recovered by another
        concurrent call (see `_claim_for_recovery`).
        """
        execution = self._claim_for_recovery(execution_id)

        plan_actions = list(
            self.db.scalars(
                select(DedupExecutionPlanAction).where(
                    DedupExecutionPlanAction.plan_id == execution.plan_id
                )
            )
        )
        existing_audit_plan_action_ids = {
            audit.plan_action_id for audit in self.get_action_audits(execution_id)
        }
        unresolved = [
            pa for pa in plan_actions if pa.id not in existing_audit_plan_action_ids
        ]

        if not unresolved:
            raise ValueError(
                f"Execution {execution_id} already has a recorded outcome "
                "for every planned action - there is nothing to recover; "
                "call complete_execution directly"
            )

        for plan_action in unresolved:
            observation = _observe_file(plan_action.source_path)

            file_confirmed_unchanged = (
                observation.exists
                and plan_action.observed_exists
                and observation.content_hash == plan_action.observed_content_hash
                and observation.file_size == plan_action.observed_file_size
            )

            if file_confirmed_unchanged:
                self.record_action_result(
                    execution_id,
                    plan_action.id,
                    DedupExecutionActionResult.NOT_ATTEMPTED,
                )
            else:
                self.record_action_result(
                    execution_id,
                    plan_action.id,
                    DedupExecutionActionResult.UNKNOWN,
                    observed_content_hash=observation.content_hash,
                    observed_file_size=observation.file_size,
                    filesystem_mutation_occurred=None,
                    error_message=(
                        "Recovery found the source file missing - consistent "
                        "with a successful delete, but not provably caused "
                        "by this action; manual verification required."
                        if not observation.exists
                        else "Recovery found the source file present but not "
                        "matching the plan's expected pre-mutation state "
                        "exactly; manual verification required."
                    ),
                )

        return self.complete_execution(execution_id)

    def get_execution(self, execution_id: int) -> DedupExecution | None:
        return self.db.get(DedupExecution, execution_id)

    def list_executions(
        self,
        plan_id: int | None = None,
        authorization_id: int | None = None,
        status: DedupExecutionStatus | None = None,
        started_before: datetime | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[DedupExecution]:
        """`started_before` is a plain, non-judgmental filter - "started
        earlier than this timestamp" - not an assertion that a matching
        RUNNING execution is actually stuck. AI_Brain has no process
        supervision anywhere, so it cannot know whether a RUNNING
        execution's process is still alive; this filter only helps a
        human narrow candidates (e.g. `status=running,
        started_before=<some threshold you choose>`) for their own
        judgment before deciding to call `recover_stale_execution`."""
        statement = (
            select(DedupExecution)
            .order_by(DedupExecution.started_at.desc())
            .limit(limit)
        )

        if plan_id is not None:
            statement = statement.where(DedupExecution.plan_id == plan_id)
        if authorization_id is not None:
            statement = statement.where(
                DedupExecution.authorization_id == authorization_id
            )
        if status is not None:
            statement = statement.where(DedupExecution.status == status)
        if started_before is not None:
            statement = statement.where(DedupExecution.started_at < started_before)

        return list(self.db.scalars(statement))

    def get_action_audits(self, execution_id: int) -> list[DedupExecutionActionAudit]:
        return list(
            self.db.scalars(
                select(DedupExecutionActionAudit)
                .where(DedupExecutionActionAudit.execution_id == execution_id)
                .order_by(DedupExecutionActionAudit.id)
            )
        )

    def _get_execution_for_authorization(
        self, authorization_id: int
    ) -> DedupExecution | None:
        return self.db.scalar(
            select(DedupExecution).where(
                DedupExecution.authorization_id == authorization_id
            )
        )

    def _get_execution_or_raise(self, execution_id: int) -> DedupExecution:
        execution = self.get_execution(execution_id)

        if execution is None:
            raise ValueError(f"Dedup execution {execution_id} not found")

        return execution
