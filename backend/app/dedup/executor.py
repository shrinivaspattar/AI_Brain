"""The filesystem executor - the ONLY code in AI_Brain that ever
performs a real filesystem mutation, and only ever a single, narrow
operation: moving a duplicate's file into a quarantine directory via
one atomic, same-filesystem `os.rename()` call. See
"Filesystem Executor Design" in AI_Brain_Architecture.md for the full
design this implementation follows.

FAIL CLOSED, ON PURPOSE: `DedupFilesystemExecutor.__init__` takes
`allowed_root` and `quarantine_root` as required positional/keyword
arguments with NO default values anywhere in this module. There is no
config setting, no environment variable, no fallback path, and no
reference anywhere in this file to any real corpus location - omitting
either argument raises `TypeError` before any other code runs. This is
the entire mechanism by which "the real corpus cannot be selected
implicitly" is true: there is nothing here to implicitly select.

This executor is not wired into any API endpoint, any router, or
`app/main.py` - it is only ever constructible from trusted Python code
(today: tests). Exposing it via HTTP, or to arbitrary user-supplied
roots, is explicitly out of scope for this milestone.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import DedupExecutionPlanService, _observe_file
from app.dedup.execution_service import DedupExecutionService
from app.models.dedup_authorization import DedupPlanAuthorizationStatus
from app.models.dedup_execution import (
    DedupExecution,
    DedupExecutionActionResult,
    DedupExecutionStatus,
)
from app.models.dedup_execution_plan import DedupExecutionPlanAction


@dataclass(frozen=True)
class _ActionOutcome:
    """Internal only - the result of attempting one action, and
    whether the caller should keep going or stop (per the
    stop-on-first-failure policy)."""

    should_continue: bool


class DedupFilesystemExecutor:
    """Performs the already-designed, already-reviewed
    DELETE-means-quarantine-move operation, and nothing else. Every
    single action, in every single run, re-validates authorization,
    plan state, and the source file itself immediately before its own
    mutation - never trusting that a check performed for an earlier
    action, or at plan/authorization time, still holds. This is not
    an optimization opportunity: it is the load-bearing safety
    property this entire multi-milestone design exists to guarantee.

    Constructing this object never touches a file. `execute()` is the
    only method that can mutate anything, and only ever within
    `allowed_root`, and only ever by moving a file into
    `quarantine_root`.

    TOCTOU DECISION (explicit, re-affirmed after a hostile security
    review - not silently assumed away): a race window exists between
    this method's final source re-validation and the `os.rename()`
    call that follows it. This window is MINIMIZED, not eliminated -
    no file descriptor pinning, no advisory locking, is used, by
    deliberate choice, because AI_Brain does not own the files it
    quarantines and holding a lock on a file another process might
    reasonably touch is not a burden this design accepts.

    What that window can and cannot produce, traced precisely:
    - The source can be deleted in the window: `os.rename()` then
      raises `OSError` -> `FAILED`, `mutation=False`. Safe.
    - The source can be replaced by a symlink: POSIX `rename()` moves
      the symlink object itself (never follows it); the post-move
      check's explicit `is_symlink()` test catches this ->
      `UNKNOWN`, never `SUCCESS`.
    - The source can be replaced by a DIFFERENT regular file: the
      rename succeeds on whatever now occupies that path, but
      post-move verification compares the DESTINATION's hash/size
      against `plan_action.observed_content_hash`/`file_size` - the
      original, plan-time expected values, not a value re-derived
      from whatever got moved. A content-different substitution is
      therefore always caught -> `UNKNOWN`, never `SUCCESS`.
    - RESIDUAL, ACCEPTED CASE: a substituted file with an IDENTICAL
      SHA-256 and byte size to the expected content would pass
      verification and be recorded `SUCCESS`. This is not a
      meaningful attack - the moved bytes are indistinguishable from
      the intended ones, so nothing different from the intended
      operation actually occurred. Closing even this residual would
      require pinning the file by descriptor before any check begins,
      which this milestone deliberately does not implement.

    This is judged ACCEPTABLE for the current synthetic-filesystem-
    only executor, which never runs against real personal data and is
    not reachable from any API. It is explicitly NOT judged acceptable
    to carry forward unexamined into a real-corpus-facing milestone -
    whoever designs that milestone must either re-affirm this
    reasoning in that new context or implement a stronger primitive
    (e.g. `openat`-then-`fstat`-then-`renameat` against a held file
    descriptor) before this executor is ever pointed at real data.
    Treat that decision as a required, explicit gate, not something
    this milestone's acceptance implicitly resolves.
    """

    def __init__(self, db: Session, allowed_root: Path, quarantine_root: Path):
        self.db = db
        self.execution_service = DedupExecutionService(db)
        self.authorization_service = DedupPlanAuthorizationService(db)
        self.plan_service = DedupExecutionPlanService(db)

        # Fail-closed threat-model decision (see AI_Brain_Architecture.md
        # "Filesystem Executor Design" for the full writeup): a root
        # argument that is ITSELF a symlink is rejected outright, never
        # silently resolved through. allowed_root/quarantine_root are
        # trusted, explicit, caller-supplied configuration, not
        # attacker-influenced input - but "trusted" is not the same as
        # "verified," and resolving through a symlinked root would mean
        # this class's own safety boundary is only as good as whatever
        # that symlink currently points to, which nothing here would
        # ever notice changing. This check MUST run on the raw,
        # unresolved argument, before resolve() below, since resolve()
        # would otherwise silently follow exactly the thing being
        # rejected. Deliberately scoped to the root argument itself,
        # not every ancestor directory above it - an ancestor symlink
        # would require the caller's own environment to already be
        # compromised at a level outside this class's threat model.
        if Path(allowed_root).is_symlink():
            raise ValueError(
                f"allowed_root {allowed_root} must not be a symlink - pass the "
                "real directory directly, not a link to it"
            )
        if Path(quarantine_root).is_symlink():
            raise ValueError(
                f"quarantine_root {quarantine_root} must not be a symlink - pass "
                "the real directory directly, not a link to it"
            )

        # Non-strict resolve() here: an existence/directory check comes
        # right after, and should surface as this class's own
        # ValueError, never a raw FileNotFoundError leaking out of
        # pathlib before this constructor has said anything.
        allowed_root = Path(allowed_root).resolve(strict=False)
        quarantine_root = Path(quarantine_root).resolve(strict=False)

        if not allowed_root.is_dir():
            raise ValueError(f"allowed_root {allowed_root} is not a directory")
        if not quarantine_root.is_dir():
            raise ValueError(f"quarantine_root {quarantine_root} is not a directory")

        if allowed_root == quarantine_root:
            raise ValueError(
                "allowed_root and quarantine_root must not be the same directory"
            )
        if _is_within(quarantine_root, allowed_root):
            raise ValueError(
                f"quarantine_root {quarantine_root} must not be inside allowed_root "
                f"{allowed_root} - a quarantined file must never be re-scannable as "
                "a document"
            )
        if _is_within(allowed_root, quarantine_root):
            raise ValueError(
                f"allowed_root {allowed_root} must not be inside quarantine_root "
                f"{quarantine_root}"
            )

        allowed_device = _device_of(allowed_root)
        quarantine_device = _device_of(quarantine_root)
        if allowed_device != quarantine_device:
            raise ValueError(
                f"allowed_root ({allowed_root}, device {allowed_device}) and "
                f"quarantine_root ({quarantine_root}, device {quarantine_device}) "
                "must be on the same filesystem - a cross-device move can never "
                "be atomic, and this executor never falls back to copy-then-delete"
            )

        self.allowed_root = allowed_root
        self.quarantine_root = quarantine_root
        self._quarantine_device = quarantine_device

    def execute(self, execution_id: int, *, confirm: bool) -> DedupExecution:
        """Process every unresolved planned action for one RUNNING
        execution, in ascending `plan_action.id` order, stopping at
        the first action that does not cleanly succeed - then
        finalize via the existing `complete_execution`.

        `confirm` has no default and must be passed explicitly - there
        is no API layer wrapping this method yet to provide that gate,
        so it is enforced here directly.

        Refuses (`ValueError`) if the execution is not currently
        `RUNNING`, or if it already has ANY recorded action results -
        this executor only ever runs once, from a fully fresh
        execution, start to (stop-on-first-failure) finish. It is
        deliberately NOT resumable: a `RUNNING` execution that already
        has partial audits (because a previous `execute()` call itself
        crashed) must go through `DedupExecutionService.
        recover_stale_execution` instead, which makes a conservative,
        never-guessed determination about the unresolved actions
        rather than this method silently attempting to pick up where
        an unknown prior attempt left off.

        Failure policy: the first action that does not cleanly succeed
        stops the run. Every action already recorded (by an earlier
        iteration of this same call) keeps its recorded result -
        nothing here is undone. Every action after the stopping point
        is explicitly recorded `NOT_ATTEMPTED`, never silently
        skipped. The execution is always finalized via
        `complete_execution` - which itself can never report
        `COMPLETED` unless every single planned action's audit row is
        `SUCCESS` (see that method).
        """
        if not confirm:
            raise ValueError(
                "confirm must be true to execute - this call performs real "
                "filesystem mutations within the configured allowed_root"
            )

        execution = self._claim_execution(execution_id)

        existing_audits = self.execution_service.get_action_audits(execution_id)
        if existing_audits:
            raise ValueError(
                f"Execution {execution_id} already has {len(existing_audits)} "
                "recorded action result(s) - this executor only runs once, from "
                "a fully fresh execution; use recover_stale_execution to close "
                "out a partially-run execution instead"
            )

        plan_actions = list(
            self.db.scalars(
                select(DedupExecutionPlanAction)
                .where(DedupExecutionPlanAction.plan_id == execution.plan_id)
                .order_by(DedupExecutionPlanAction.id)
            )
        )

        stop = False
        for plan_action in plan_actions:
            if stop:
                self.execution_service.record_action_result(
                    execution_id,
                    plan_action.id,
                    DedupExecutionActionResult.NOT_ATTEMPTED,
                )
                continue

            try:
                outcome = self._attempt_action(execution, plan_action)
            except Exception as exc:  # noqa: BLE001 - last-resort safety net
                # Something genuinely unanticipated happened before
                # _attempt_action could record anything for this
                # action itself. Never leave the execution stuck at
                # RUNNING because of an unhandled exception - record
                # the honest answer (we don't know what happened) and
                # stop, exactly like any other unresolved outcome.
                try:
                    self.execution_service.record_action_result(
                        execution_id,
                        plan_action.id,
                        DedupExecutionActionResult.UNKNOWN,
                        filesystem_mutation_occurred=None,
                        error_message=f"Unexpected error during execution: {exc}",
                    )
                except ValueError:
                    # _attempt_action had already recorded a result for
                    # this action before the exception occurred -
                    # that recorded result is authoritative; do not
                    # try to record a second, conflicting one.
                    pass
                outcome = _ActionOutcome(should_continue=False)

            if not outcome.should_continue:
                stop = True

        return self.execution_service.complete_execution(execution_id)

    def _claim_execution(self, execution_id: int) -> DedupExecution:
        """Acquire exclusive ownership of this execution before any
        action is attempted - the concurrency-exclusivity mechanism
        identified as missing by the security review preceding this
        method's introduction.

        Mechanism, deliberately explicit and database-level rather
        than relying on process topology: `SELECT ... FOR UPDATE`
        locks the `DedupExecution` row for the duration of this
        method's own transaction. A concurrent caller's own `SELECT
        ... FOR UPDATE` on the SAME row blocks - genuinely waits at
        the database level - until this transaction commits or rolls
        back, then re-reads the row's current state under its own
        lock. While holding that lock, this method checks and sets
        `claimed_at`: if it is already non-null, another caller won
        the race and this one refuses cleanly; otherwise, this caller
        sets it and commits, releasing the lock with the claim now
        durably recorded. `claimed_at` is never cleared or reused - a
        claimed execution stays claimed permanently, exactly like a
        terminal execution stays terminal; a second genuine attempt
        after a crash goes through `DedupExecutionService.
        recover_stale_execution`, never a second claim on this row.

        This is the ONLY thing standing between two concurrent
        `execute()` calls on the same `execution_id` and both entering
        the filesystem-mutation loop. Every failure path here raises a
        plain `ValueError` - never a raw `IntegrityError`, and never an
        uncaught exception from deeper in the call stack - because the
        losing caller is turned away at this single, explicit
        checkpoint before it can reach anything that would race for
        real (an `os.rename()` call or an `INSERT` against the
        `(execution_id, plan_action_id)` unique constraint).
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
                f"(status={status_value}) - only a freshly-started, RUNNING "
                "execution can be executed"
            )

        if execution.claimed_at is not None:
            claimed_at = execution.claimed_at
            self.db.rollback()
            raise ValueError(
                f"Execution {execution_id} has already been claimed by another "
                f"executor invocation at {claimed_at.isoformat()} - exactly one "
                "caller may own an execution; this one refuses rather than "
                "risk a concurrent filesystem mutation"
            )

        execution.claimed_at = datetime.now(UTC)

        try:
            self.db.commit()
            self.db.refresh(execution)
            return execution
        except Exception:
            self.db.rollback()
            raise

    def _attempt_action(
        self, execution: DedupExecution, plan_action: DedupExecutionPlanAction
    ) -> _ActionOutcome:
        """Attempt exactly one planned action. Always calls
        `record_action_result` exactly once before returning (success
        or otherwise) - the only case where this method itself may
        propagate an exception without having recorded anything is a
        failure genuinely outside its own anticipated control flow,
        handled by `execute`'s own safety net.
        """
        # Authorization-freshness mechanism, made EXPLICIT rather than
        # left dependent on SQLAlchemy's expire_on_commit default (as
        # the security review flagged): every read below this line
        # must reflect the database's current state, not whatever this
        # Session happened to cache from an earlier query. expire_all()
        # marks every object in the identity map as needing a reload,
        # forcing the very next attribute access on `authorization`,
        # `plan`, or any `Document` row (inside check_currency /
        # check_plan_validity) to issue a fresh SELECT - regardless of
        # this Session's expire_on_commit setting, and regardless of
        # whether a commit happened since the last read. This is what
        # actually guarantees "does not trust a previous action's
        # validation," not an incidental side effect of some other
        # method's commit elsewhere.
        self.db.expire_all()

        plan = self.plan_service.get_plan(execution.plan_id)
        if plan is None or plan_action.document_id == plan.canonical_document_id:
            # Should be structurally impossible (a plan action is
            # never created for the canonical) - defensive, not
            # reachable in normal operation.
            return self._refuse(
                execution.id,
                plan_action.id,
                "the action's document is the plan's own canonical document - "
                "refusing to treat a canonical as a mutation target",
            )

        # Steps 1-4: reload authorization and plan fresh, and revalidate
        # - the TOCTOU checkpoint. Never trust that a check performed
        # for authorization, plan generation, or an earlier action in
        # this same run still holds.
        authorization, validity, _ = self.authorization_service.check_currency(
            execution.authorization_id
        )
        if authorization.status != DedupPlanAuthorizationStatus.AUTHORIZED:
            return self._refuse(
                execution.id,
                plan_action.id,
                f"authorization {authorization.id} is no longer AUTHORIZED "
                f"(status={authorization.status.value})",
            )

        action_validity = next(
            (a for a in validity.actions if a.action_id == plan_action.id), None
        )
        if not validity.canonical_valid or action_validity is None or not action_validity.is_valid:
            return self._refuse(
                execution.id,
                plan_action.id,
                "plan is no longer valid for this action (stale filesystem or "
                "document state)",
            )

        # Step 5: resolve and canonicalize the source path. Step 8
        # (refuse symlinks) must be checked on the ORIGINAL, UNRESOLVED
        # path - resolve() itself follows symlinks, which would hide
        # exactly the thing being refused.
        original_path = Path(plan_action.source_path)
        if original_path.is_symlink():
            return self._refuse(
                execution.id, plan_action.id, "source path is a symlink"
            )

        try:
            source_path = original_path.resolve(strict=True)
        except OSError as exc:
            return self._refuse(
                execution.id,
                plan_action.id,
                f"source path could not be resolved: {exc}",
            )

        # Step 6: verify it remains inside the explicitly allowed root.
        # Checked on the FULLY RESOLVED path so a symlinked intermediate
        # directory cannot be used to escape allowed_root even though
        # the raw path string looked contained.
        if not _is_within(source_path, self.allowed_root):
            return self._refuse(
                execution.id,
                plan_action.id,
                f"source path {source_path} is outside the allowed mutation "
                f"root {self.allowed_root}",
            )

        # Step 7: verify it is a regular file.
        try:
            source_stat = source_path.stat()
        except OSError as exc:
            return self._refuse(
                execution.id, plan_action.id, f"could not stat source: {exc}"
            )
        if not source_path.is_file():
            return self._refuse(
                execution.id, plan_action.id, "source is not a regular file"
            )

        # Step 9: refuse multi-linked files - quarantining one name
        # would not make the underlying data go away.
        if source_stat.st_nlink > 1:
            return self._refuse(
                execution.id,
                plan_action.id,
                f"source has {source_stat.st_nlink} hard links - quarantining "
                "one name would not remove the underlying data",
            )

        # Steps 10-11: a SECOND, later hash/size re-observation,
        # deliberately redundant with what check_plan_validity already
        # confirmed in steps 1-4 - minimizing the TOCTOU window means
        # re-checking as close to the mutation as possible, not
        # trusting an earlier check because it was recent.
        observation = _observe_file(str(source_path))
        if (
            not observation.exists
            or observation.content_hash != plan_action.observed_content_hash
            or observation.file_size != plan_action.observed_file_size
        ):
            return self._refuse(
                execution.id,
                plan_action.id,
                "source file no longer matches the plan's expected hash/size",
            )

        # Step 12: verify source and quarantine share a filesystem.
        if source_stat.st_dev != self._quarantine_device:
            return self._refuse(
                execution.id,
                plan_action.id,
                "source is not on the same filesystem as the quarantine root",
            )

        # Step 13: the destination belongs to this execution/action -
        # guaranteed unique by construction (execution_id/plan_action_id
        # are both already unique), but still verified empty before
        # writing: os.rename() SILENTLY OVERWRITES an existing
        # destination on POSIX with no error, so this check is the
        # only thing standing between "nothing exists there yet" and
        # "silently destroy whatever is already there."
        destination = (
            self.quarantine_root
            / str(execution.id)
            / f"{plan_action.id}__{source_path.name}"
        )
        if destination.exists():
            return self._refuse(
                execution.id,
                plan_action.id,
                f"quarantine destination {destination} already exists - refusing "
                "to overwrite it",
                result=DedupExecutionActionResult.FAILED,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Defense in depth, identified by the security review: the
        # constructor's device check compares allowed_root/quarantine_root
        # ONCE, and step 12 above compares the SOURCE's device against
        # that same recorded quarantine_device - but neither had
        # independently confirmed that THIS SPECIFIC destination's
        # parent directory (freshly created just above) actually landed
        # on that same device. In ordinary operation it always does,
        # since destination.parent is always a subdirectory of
        # quarantine_root created moments ago in this same call - this
        # check exists purely to catch an exotic mismatch (e.g. an
        # unusual filesystem boundary inside quarantine_root itself)
        # rather than assume the ordinary case always holds.
        if _device_of(destination.parent) != self._quarantine_device:
            return self._refuse(
                execution.id,
                plan_action.id,
                f"quarantine destination parent {destination.parent} is not on "
                "the expected quarantine filesystem device",
                result=DedupExecutionActionResult.FAILED,
            )

        # Step 14: perform exactly one atomic same-filesystem rename.
        try:
            os.rename(source_path, destination)
        except OSError as exc:
            # Same-device rename is atomic: it either fully succeeds or
            # has no effect at all - a raised error proves nothing
            # moved.
            self.execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.FAILED,
                filesystem_mutation_occurred=False,
                error_message=str(exc),
            )
            return _ActionOutcome(should_continue=False)

        # Step 15: verify the resulting quarantine object before ever
        # claiming SUCCESS. A rename() that raised no error but left
        # something inconsistent at the destination (or the source
        # somehow still present) is UNKNOWN, not SUCCESS - "the OS
        # call didn't error" is not the same as "verified correct."
        # Every check below is independently named so a future reader
        # (or a failing test) can see exactly which expectation broke,
        # rather than one opaque combined condition.
        destination_is_symlink = destination.is_symlink()
        destination_is_regular_file = destination.is_file() and not destination_is_symlink
        source_still_present = source_path.exists()
        post_observation = _observe_file(str(destination))
        destination_hash_matches = (
            post_observation.exists
            and post_observation.content_hash == plan_action.observed_content_hash
            and post_observation.file_size == plan_action.observed_file_size
        )
        # "No unexpected second filesystem operation occurred" is true
        # by construction, not by a runtime check: this method contains
        # exactly one os.rename() call site (above), and nothing here
        # or anywhere else in this class ever calls copy, chmod, a
        # second rename, or any other mutating filesystem function.
        verification_passed = (
            not source_still_present
            and destination_is_regular_file
            and not destination_is_symlink
            and destination_hash_matches
        )
        if not verification_passed:
            failed_checks = []
            if source_still_present:
                failed_checks.append("source still exists")
            if not destination_is_regular_file:
                failed_checks.append("destination is not a regular file")
            if destination_is_symlink:
                failed_checks.append("destination is a symlink")
            if not destination_hash_matches:
                failed_checks.append("destination hash/size does not match expected")

            self.execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.UNKNOWN,
                observed_content_hash=post_observation.content_hash,
                observed_file_size=post_observation.file_size,
                filesystem_mutation_occurred=None,
                error_message=(
                    "rename() reported success but post-move verification did "
                    "not corroborate it - manual investigation required "
                    f"(failed: {', '.join(failed_checks)})"
                ),
            )
            return _ActionOutcome(should_continue=False)

        # Step 16: persist SUCCESS - only reachable once every prior
        # check and the post-move verification have all passed.
        self.execution_service.record_action_result(
            execution.id,
            plan_action.id,
            DedupExecutionActionResult.SUCCESS,
            observed_content_hash=post_observation.content_hash,
            observed_file_size=post_observation.file_size,
            filesystem_mutation_occurred=True,
        )
        return _ActionOutcome(should_continue=True)

    def _refuse(
        self,
        execution_id: int,
        plan_action_id: int,
        reason: str,
        result: DedupExecutionActionResult = DedupExecutionActionResult.PRECONDITION_FAILED,
    ) -> _ActionOutcome:
        """Record a refusal (never a mutation) and signal the caller
        to stop. `PRECONDITION_FAILED` is the default - by definition,
        every call site above this method reaches it strictly before
        `os.rename()` is ever attempted."""
        self.execution_service.record_action_result(
            execution_id,
            plan_action_id,
            result,
            filesystem_mutation_occurred=False,
            error_message=reason,
        )
        return _ActionOutcome(should_continue=False)


def _device_of(path: Path) -> int:
    return path.stat().st_dev


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False
