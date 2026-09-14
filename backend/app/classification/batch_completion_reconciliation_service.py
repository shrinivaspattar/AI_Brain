from __future__ import annotations

from sqlalchemy.orm import Session

from app.classification.batch_control_service import BatchControlService, TransitionResult
from app.classification.batch_report_service import BatchReportService
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch


class BatchCompletionReconciliationService:
    """Milestone 8: the one piece of frozen step 9 ("worker/claim
    integration - the loop that ties 3-8 together per batch") no prior
    milestone closed. `BatchControlService.complete()`'s own docstring
    has, since Milestone 3, stated plainly: "This method does not
    itself verify 'zero remaining claimable/in-progress work' -
    determining that fact is a future worker-integration milestone's
    job." This is that milestone - and only for the narrowest,
    unambiguous slice of it: SOURCE_WORK_EXHAUSTED detection.
    Envelope-exhaustion and runtime-budget-exhaustion detection are
    explicitly NOT implemented here (Milestone 8 Design Review,
    decision 5) - a separate, future design decision.

    ARCHITECTURAL CONTRACT (Milestone 8 Design Review's required
    correction, stated here verbatim as the load-bearing invariant):
    this service may observe a STALE, positive ("exhausted") reading
    under concurrent activity - its own read of `BatchReportService`'s
    denominators and its subsequent call to `BatchControlService.
    complete()` are two separate operations, not one atomic unit. It
    NEVER directly mutates `IngestionBatch` itself. `BatchControlService
    .complete()` remains the SOLE completion authority - its existing
    atomic conditional `UPDATE ... WHERE status IN (RUNNING) RETURNING`
    (unchanged, Milestone 3) is what actually decides whether a
    completion transition applies, exactly once, under real
    concurrency. This service supplies a decision; `complete()` is what
    makes it safe to act on that decision concurrently.

    Correctness under the current data model does not come from any
    new locking this service introduces (it introduces none) - it
    comes from three already-existing, already-verified properties: (1)
    `IngestionBatch.source_instances_selected`/batch membership are
    immutable once set, so the denominator this service reads from
    never grows after batch creation; (2) no existing claim query's
    `eligible_pipeline_states` ever include a terminal `ContentPipelineState`
    (FAILED/EXCLUDED/UNSUPPORTED/NEEDS_REVIEW/INGESTED), so a row this
    service counted as terminal can never be reclaimed and regress
    afterward; (3) `claim_source_instance_for_identity_resolution`/
    `claim_source_instance_for_archive_processing`, when given a
    `classification_run_id`, already refuse to grant a NEW claim once
    the owning batch leaves `RUNNING` (existing Milestone 4 admission
    gate) - so no new SourceInstance-level work can begin against this
    batch after it completes. `claim_content_identity_group` remains
    the one exception: it is frozen (Implementation Design Pass,
    section 19) as a GLOBAL, batch-unaware claim, and its activity is
    never gated by any particular batch's status - this is unchanged,
    pre-existing, intentional behavior this milestone neither relies
    on nor alters, and is exercised directly by this milestone's own
    test suite (see "existing lifecycle property" tests) rather than
    silently assumed.

    NEEDS_REVIEW is treated as terminal for this decision (Milestone 8
    Design Review, decision 7): `COMPLETED`/`SOURCE_WORK_EXHAUSTED`
    means the batch's own source-work lifecycle has run to its natural
    end, NOT that every item was successfully ingested - items parked
    at `NEEDS_REVIEW` may legitimately coexist with a completed batch.
    `successful_ingestion_count` (from the same `BatchReport`) remains
    the field that answers "how much actually succeeded," deliberately
    distinct from this decision.
    """

    def __init__(self, db: Session):
        self.db = db
        self.reports = BatchReportService(db)
        self.control = BatchControlService(db)

    def check_and_complete(self, batch_id: int) -> TransitionResult | None:
        """Returns `None` when no completion decision applies - either
        the batch is not currently `RUNNING`, or its source-work is not
        yet exhausted. Raises `ValueError` if `batch_id` does not exist
        at all. Returns the `TransitionResult`
        from `BatchControlService.complete()` when exhaustion is
        detected - `result.applied` may still be `False` if a
        concurrent call (or an operator's own pause/abort) already
        moved the batch out of `RUNNING` between this read and the
        `complete()` call; that is `complete()`'s own, already-proven-
        safe, race-safe behavior, not a new failure mode this service
        introduces.
        """
        batch = self.db.get(IngestionBatch, batch_id)
        if batch is None:
            raise ValueError(f"IngestionBatch {batch_id} not found")

        if batch.status is not BatchStatus.RUNNING:
            return None

        report = self.reports.generate_report(batch_id)

        source_work_exhausted = (
            report.unattempted_selected_count == 0 and report.terminal_source_count == report.attempted_source_count
        )
        if not source_work_exhausted:
            return None

        return self.control.complete(batch_id, reason=BatchStopReason.SOURCE_WORK_EXHAUSTED)
