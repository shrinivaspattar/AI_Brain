from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.classification.resource_guard import BatchResourceGuard, GuardTier
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch

# Exactly the six transitions frozen in "Scaled Real-T7 Ingestion -
# Implementation Design Pass" (`2fab4b3`), "### 12. Batch state machine
# - exact transitions." No other transition exists anywhere in this
# service.
_PAUSE_REASONS = frozenset(
    {BatchStopReason.WORKSPACE_SOFT_STOP, BatchStopReason.POSTGRES_SOFT_STOP, BatchStopReason.MANUAL_PAUSE}
)
# The frozen state machine names an explicit "operator aborts
# explicitly" transition, but documents it ONLY from PAUSED:
#   PAUSED -> ABORTED   --(operator aborts explicitly)-->
# RUNNING -> ABORTED's own documented causes are exactly "(hard-stop OR
# persistent Ollama failure OR safety-invariant violation)" - no
# operator-explicit cause is listed for that edge anywhere in "###
# 12. Batch state machine - exact transitions." `MANUAL_ABORT` (added
# per Implementation Milestone 3's final-correction pass) is therefore
# legal ONLY as a PAUSED -> ABORTED reason, per
# `_ABORT_REASON_LEGAL_SOURCES` below - deliberately NOT accepted from
# RUNNING, even though the other four abort reasons are legal from
# either state. A deliberate human decision to stop a still-RUNNING
# batch has no direct edge in the frozen design; the closest legitimate
# path is pause() (MANUAL_PAUSE) followed by abort() (MANUAL_ABORT) -
# two separate operator actions, matching the two separate documented
# edges. `resume()`'s own internal `abort()` call (a hard-stop detected
# while attempting to resume a PAUSED batch) uses whichever resource/
# safety reason the guard reported - never `MANUAL_ABORT`, since that
# is never guard-reported.
_ABORT_REASON_LEGAL_SOURCES: dict[BatchStopReason, tuple[BatchStatus, ...]] = {
    BatchStopReason.WORKSPACE_HARD_STOP: (BatchStatus.RUNNING, BatchStatus.PAUSED),
    BatchStopReason.POSTGRES_HARD_STOP: (BatchStatus.RUNNING, BatchStatus.PAUSED),
    BatchStopReason.OLLAMA_PERSISTENTLY_UNREACHABLE: (BatchStatus.RUNNING, BatchStatus.PAUSED),
    BatchStopReason.SAFETY_INVARIANT_VIOLATION_DETECTED: (BatchStatus.RUNNING, BatchStatus.PAUSED),
    BatchStopReason.MANUAL_ABORT: (BatchStatus.PAUSED,),
}
_ABORT_REASONS = frozenset(_ABORT_REASON_LEGAL_SOURCES)
_COMPLETE_REASONS = frozenset(
    {
        BatchStopReason.SOURCE_WORK_EXHAUSTED,
        BatchStopReason.EXTRACTED_BYTES_ENVELOPE_EXHAUSTED,
        BatchStopReason.EMBEDDINGS_ENVELOPE_EXHAUSTED,
        BatchStopReason.RUNTIME_BUDGET_EXCEEDED,
    }
)


@dataclass(frozen=True)
class TransitionResult:
    """`applied=False` covers BOTH a lost concurrency race (another
    transition reached the row first - see point 10 of the Milestone 3
    authorization) AND a plain illegal call (e.g. `resume()` on an
    already-`ABORTED` batch) with the exact same, safe mechanism: the
    conditional `UPDATE`'s `WHERE status IN (...)` clause matched zero
    rows. Neither case ever raises a raw database exception; `batch`
    always reflects the actual persisted state after the call, whether
    or not this call was the one that produced it."""

    applied: bool
    batch: IngestionBatch


class BatchControlService:
    """The small batch control-plane abstraction the Milestone 3
    authorization asked for (point 13): start/pause/resume/abort/
    complete, plus the runtime-accounting bookkeeping those five
    operations require. Deliberately NOT a large orchestration
    framework - no scheduling, no retry loop, no worker integration
    (that is a later milestone; see the class docstring's scope note
    below).

    CONCURRENCY SAFETY: every transition is a single conditional
    `UPDATE ... WHERE id = :id AND status IN (:expected...) RETURNING`.
    Postgres's own row-level locking makes this safe under real
    concurrent callers without any explicit `SELECT ... FOR UPDATE`:
    two racing UPDATEs against the same row serialize at the database:
    whichever commits first changes `status` away from the expected
    value(s), so the second one's `WHERE` clause matches zero rows when
    it is finally evaluated - it applies nothing, and `TransitionResult.
    applied` reports `False`. `status` and `stop_reason`/`stop_reason_
    detail` are always written together in that same single `UPDATE`,
    per the frozen design's "### 6. BatchResourceGuard" persistence
    rule, extended here to also cover the runtime-counter delta and
    `started_at`/`completed_at` in the identical statement.

    RUNTIME ACCOUNTING: exactly the frozen model from "### 9. Runtime
    accounting - honest guarantees". `time.monotonic()` session-start
    timestamps live ONLY in this service INSTANCE's own memory
    (`_session_starts`, keyed by batch_id) - never persisted, never
    read from any other instance. This is a deliberate, honest
    modeling choice: a fresh `BatchControlService` instance (e.g. a new
    process after a crash) has no memory of any prior session, so
    calling `pause`/`abort`/`complete` against a batch this instance
    never itself `start()`-ed or `resume()`-ed contributes a runtime
    delta of exactly `0.0` - matching the frozen design's own statement
    that "the delta since the LAST checkpoint is LOST" on a crash,
    never fabricated as if it were known.

    SCOPE EXCLUSIONS (explicit, per the Milestone 3 authorization): no
    `WorkerClaimService` integration, no stale-claim changes, no
    extraction or embedding execution, no `BatchReportService`, no
    real-T7 access of any kind. This class governs whether a batch MAY
    continue, pause, or terminate - it does not itself do any ingestion
    work.
    """

    def __init__(self, db: Session):
        self.db = db
        self._session_starts: dict[int, float] = {}

    def start(self, batch_id: int) -> TransitionResult:
        """PLANNED -> RUNNING. Begins this instance's first monotonic
        session for the batch and records `started_at` (wall-clock,
        audit-only, never itself used for runtime-budget decisions)."""
        result = self._apply(
            batch_id,
            expected=(BatchStatus.PLANNED,),
            new_status=BatchStatus.RUNNING,
            set_started_at=True,
        )
        if result.applied:
            self._session_starts[batch_id] = time.monotonic()
        return result

    def pause(self, batch_id: int, *, reason: BatchStopReason, detail: str | None = None) -> TransitionResult:
        """RUNNING -> PAUSED. `reason` must be one of the frozen
        design's soft-stop/manual-pause reasons - anything else raises
        `ValueError` rather than silently persisting a reason the
        `stop_reason` CHECK constraint's PAUSED bucket was never meant
        to hold."""
        if reason not in _PAUSE_REASONS:
            raise ValueError(
                f"{reason!r} is not a valid pause reason; must be one of "
                f"{sorted(r.value for r in _PAUSE_REASONS)}"
            )
        delta = self._peek_session_delta(batch_id)
        result = self._apply(
            batch_id,
            expected=(BatchStatus.RUNNING,),
            new_status=BatchStatus.PAUSED,
            stop_reason=reason,
            stop_reason_detail=detail,
            runtime_delta=delta,
        )
        if result.applied:
            self._pop_session(batch_id)
        return result

    def resume(self, batch_id: int, *, guard: BatchResourceGuard) -> TransitionResult:
        """PAUSED -> RUNNING, gated on a fresh resource check (point 12
        of the Milestone 3 authorization: "resume re-checks resources").
        A batch that is not currently `PAUSED` (including `ABORTED`,
        which is always terminal - see the class docstring's
        concurrency-safety note) simply reports `applied=False`; no
        special-cased error path exists for that versus a lost race,
        by design.

        A fresh HARD_STOP observed at resume time aborts the batch
        immediately rather than resuming it - the frozen state machine
        does not have a PAUSED -> PAUSED-with-a-worse-reason transition,
        and silently resuming into a hard-stop condition would
        contradict the guard's own decision one line later. A fresh
        SOFT_STOP simply refuses to resume (`applied=False`), leaving
        the batch exactly as it was."""
        batch = self.db.get(IngestionBatch, batch_id)
        if batch is None:
            raise ValueError(f"IngestionBatch {batch_id} does not exist")
        if batch.status is not BatchStatus.PAUSED:
            return TransitionResult(applied=False, batch=batch)

        guard_result = guard.check_before_claim(batch)
        if guard_result.tier is GuardTier.HARD_STOP:
            assert guard_result.stop_reason is not None
            return self.abort(batch_id, reason=guard_result.stop_reason, detail=guard_result.detail)
        if guard_result.tier is GuardTier.SOFT_STOP:
            return TransitionResult(applied=False, batch=batch)

        result = self._apply(
            batch_id,
            expected=(BatchStatus.PAUSED,),
            new_status=BatchStatus.RUNNING,
        )
        if result.applied:
            self._session_starts[batch_id] = time.monotonic()
        return result

    def abort(self, batch_id: int, *, reason: BatchStopReason, detail: str | None = None) -> TransitionResult:
        """RUNNING -> ABORTED or PAUSED -> ABORTED, depending on
        `reason` - terminal either way. Each reason's legal source
        state(s) come from `_ABORT_REASON_LEGAL_SOURCES`: the four
        resource/safety reasons are legal from either RUNNING or
        PAUSED, but `MANUAL_ABORT` (the explicit operator-abort path)
        is legal ONLY from PAUSED, matching exactly what the frozen
        state machine documents - see that mapping's module comment for
        the verified reasoning. The source-state restriction is
        enforced by the SAME conditional `UPDATE`'s `WHERE status IN
        (...)` clause as every other transition, so a `MANUAL_ABORT`
        call against a RUNNING batch simply reports `applied=False`
        (a clean rejection, not a raw exception) rather than silently
        aborting it anyway.

        Deliberately does NOT clean up any active extraction/embedding
        work in progress - persisting the terminal state and refusing
        further admission is this milestone's entire responsibility;
        actual in-flight-work cleanup is a later worker-integration
        milestone's job, per the Milestone 3 authorization's explicit
        scope exclusion."""
        legal_sources = _ABORT_REASON_LEGAL_SOURCES.get(reason)
        if legal_sources is None:
            raise ValueError(
                f"{reason!r} is not a valid abort reason; must be one of "
                f"{sorted(r.value for r in _ABORT_REASONS)}"
            )
        delta = self._peek_session_delta(batch_id)
        result = self._apply(
            batch_id,
            expected=legal_sources,
            new_status=BatchStatus.ABORTED,
            stop_reason=reason,
            stop_reason_detail=detail,
            runtime_delta=delta,
            set_completed_at=True,
        )
        if result.applied:
            self._pop_session(batch_id)
        return result

    def complete(self, batch_id: int, *, reason: BatchStopReason, detail: str | None = None) -> TransitionResult:
        """RUNNING -> COMPLETED only, per the frozen state machine -
        there is no PAUSED -> COMPLETED transition. `reason` must be one
        of the four designed-exhaustion reasons; never marks COMPLETED
        while any hard-stop/soft-stop/safety reason is being reported,
        by construction (those simply are not in `_COMPLETE_REASONS`).
        This method does not itself verify "zero remaining claimable/
        in-progress work" - determining that fact is a future worker-
        integration milestone's job; this method only persists whatever
        completion the caller has already established."""
        if reason not in _COMPLETE_REASONS:
            raise ValueError(
                f"{reason!r} is not a valid completion reason; must be one of "
                f"{sorted(r.value for r in _COMPLETE_REASONS)}"
            )
        delta = self._peek_session_delta(batch_id)
        result = self._apply(
            batch_id,
            expected=(BatchStatus.RUNNING,),
            new_status=BatchStatus.COMPLETED,
            stop_reason=reason,
            stop_reason_detail=detail,
            runtime_delta=delta,
            set_completed_at=True,
        )
        if result.applied:
            self._pop_session(batch_id)
        return result

    # -- internals --------------------------------------------------

    def _peek_session_delta(self, batch_id: int) -> float:
        """Computes, without consuming, the elapsed time since this
        instance's own last `start()`/`resume()` checkpoint for this
        batch - `0.0` if this instance never observed one. Deliberately
        non-destructive: the caller only removes the session (via
        `_pop_session`) once its own transition attempt has actually
        been confirmed `applied` - an attempt that turns out illegal
        for the batch's real current status (e.g. `MANUAL_ABORT`
        against a RUNNING batch) must never discard a still-live
        session's bookkeeping just because it was attempted."""
        start = self._session_starts.get(batch_id)
        if start is None:
            return 0.0
        return time.monotonic() - start

    def _pop_session(self, batch_id: int) -> None:
        self._session_starts.pop(batch_id, None)

    def _apply(
        self,
        batch_id: int,
        *,
        expected: tuple[BatchStatus, ...],
        new_status: BatchStatus,
        stop_reason: BatchStopReason | None = None,
        stop_reason_detail: str | None = None,
        runtime_delta: float = 0.0,
        set_started_at: bool = False,
        set_completed_at: bool = False,
    ) -> TransitionResult:
        values: dict = {"status": new_status}
        if new_status is BatchStatus.RUNNING:
            # The CHECK constraint requires stop_reason IS NULL whenever
            # status is RUNNING - `start()` never had one to begin with,
            # but `resume()` must explicitly CLEAR the PAUSED reason it
            # is leaving behind, never leave it stale on the now-RUNNING
            # row.
            values["stop_reason"] = None
            values["stop_reason_detail"] = None
        elif stop_reason is not None:
            values["stop_reason"] = stop_reason
            values["stop_reason_detail"] = stop_reason_detail
        if runtime_delta:
            values["monotonic_runtime_seconds_consumed"] = (
                IngestionBatch.monotonic_runtime_seconds_consumed + runtime_delta
            )
        if set_started_at:
            values["started_at"] = datetime.now(UTC)
        if set_completed_at:
            values["completed_at"] = datetime.now(UTC)

        applied = (
            self.db.execute(
                update(IngestionBatch)
                .where(IngestionBatch.id == batch_id, IngestionBatch.status.in_(expected))
                .values(**values)
                .returning(IngestionBatch.id)
            ).first()
            is not None
        )
        self.db.commit()

        batch = self.db.get(IngestionBatch, batch_id)
        if batch is None:
            raise ValueError(f"IngestionBatch {batch_id} does not exist")
        return TransitionResult(applied=applied, batch=batch)
