from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.classification.archive_processing_service import ArchiveProcessingService
from app.classification.batch_completion_reconciliation_service import BatchCompletionReconciliationService
from app.classification.batch_control_service import TransitionResult
from app.classification.chunking_service import ChunkingService
from app.classification.identity_resolution_service import IdentityResolutionService
from app.classification.normalization_service import NormalizationService
from app.classification.pipeline_embedding_service import PipelineEmbeddingService
from app.models.ingestion_batch import IngestionBatch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageResult:
    """One stage's outcome for one orchestrator invocation.
    `processed_count` is the number of distinct rows this stage
    successfully claimed and ran to completion (success or a durably
    recorded failure) - it does NOT count the repeat that triggered
    this stage's own termination (see `_run_stage_to_exhaustion`)."""

    stage: str
    processed_count: int


@dataclass(frozen=True)
class OrchestratorRunResult:
    """The full result of one `BatchOrchestratorService.run_once()`
    invocation - one fixed-order pass through all five pipeline stages,
    followed by exactly one completion-reconciliation attempt."""

    batch_id: int
    worker_id: str
    stage_results: list[StageResult]
    completion: TransitionResult | None


class BatchOrchestratorService:
    """Milestone 11: a pure coordinator implementing Model A (bounded,
    attended, single-process execution) - the frozen outcome of the M11
    Design/Decision pass and its subsequent Correction/Reconciliation
    pass. Composes the six existing pipeline services exactly as they
    already exist and are already proven (Milestones 1-10); this class
    NEVER constructs, queries, or mutates `SourceInstance`/
    `ContentIdentityGroup`/`Document`/`DocumentChunk` rows directly -
    every state change happens exclusively through the existing public
    methods of the services it composes.

    ONE INVOCATION = ONE FIXED-ORDER PASS, NO OUTER SWEEP REPETITION.
    `run_once()` runs each of the five pipeline stages - archive
    processing, identity resolution, normalization, chunking, embedding,
    in that fixed order - to exhaustion exactly once, then calls
    `BatchCompletionReconciliationService.check_and_complete()` exactly
    once. This ordering is provably sufficient without repeating the
    whole pass: the stage dependency graph has no back-edges (archive
    processing and identity resolution never depend on anything
    normalization/chunking/embedding produce), and nested archives are
    already fully resolved recursively within one archive claim
    (Milestone 5's `_extract_recursive` - never claimed independently).
    An earlier draft of this design used an outer multi-sweep loop
    gated by comparing `BatchReportService`'s `attempted_source_count`/
    `terminal_source_count` before and after each sweep; the M11
    Correction/Reconciliation pass found this both unnecessary (given
    the no-back-edges property above) and actively wrong (those two
    counts are root-level-only by design, Milestone 7, so they never
    reflect archive-MEMBER-only progress - using them as a termination
    gate could stop an invocation before finishing real, waiting member
    work). This class was corrected to the single fixed-order pass
    instead, removing that flawed mechanism entirely rather than
    patching it.

    TWO DISTINCT GUARANTEES, NOT ONE - the M11 Correction/Reconciliation
    pass's own required distinction, kept explicit here because they
    were found and fixed in two separate rounds:

    (1) TERMINATION: `claim_source_instance_for_archive_processing`/
    `claim_source_instance_for_identity_resolution` release a claim by
    setting `claimed_by`/`claimed_at` back to NULL unconditionally,
    including on failure - so a row that fails DETERMINISTICALLY (even
    one durably recorded `retryable=False`, e.g. an archive exceeding
    `max_depth`) becomes immediately reclaimable again, with no lease
    wait whatsoever. A naive `while claim_next() is not None: continue`
    loop therefore never terminates for such a row - proven during the
    M11 Correction/Reconciliation pass, for both claim paths.

    (2) PROGRESS OVER DISTINCT CANDIDATES: a second, more serious defect
    found only once (1) was implemented and tested - the claim query
    orders candidates by `created_at` alone, so the SAME stuck row from
    (1) would win that ordering every iteration and starve every newer,
    genuinely distinct, processable row for the entire invocation, even
    though (1)'s own fix still made the loop terminate. Proven directly:
    an integration test with one permanently-failing archive created
    before a genuinely valid one found the valid one never attempted at
    all.

    Both are closed by the SAME mechanism, WITHOUT any new retry-
    exhaustion policy, persisted counter, or schema change (the M11
    decision explicitly rejected reopening that path - see Milestone 8's
    own, separately-scoped deferral): each stage's inner loop
    (`_run_stage_to_exhaustion`) tracks, purely in memory, the set of
    row ids already claimed THIS INVOCATION, and passes that set as
    `exclude_ids` INTO the claim query itself (not merely checking the
    result afterward) - so an already-attempted row is never
    reconsidered by the SELECT at all, guaranteeing both that the loop
    terminates (1) and that it actually reaches every other distinct,
    eligible row before stopping (2). A genuinely transient failure
    therefore gets exactly one attempt per invocation; a later, separate
    invocation starts with a fresh, empty set and tries again - the
    correct, honest behavior for an attended system where an operator,
    not an automatic counter, decides whether a repeatedly-failing item
    needs intervention.

    A related, independent, PRE-EXISTING production gap was identified
    but is explicitly NOT fixed here (named, not silently folded in,
    per the M11 authorization boundary): `claim_source_instance_for_
    archive_processing`'s `already_processed` exclusion checks only
    `EXISTS(outcome == SUCCEEDED)`, never `retryable` - so a `retryable
    =False` archive is, at the claim-eligibility level, still offered
    for reclaim forever (this class's own seen-id guard is what
    actually stops that from looping this class, not a change to that
    predicate). Hardening that predicate to also exclude a durable
    `retryable=False` failure remains a real, separately-authorizable
    follow-up, not part of this milestone.

    EXPLICITLY NOT IMPLEMENTED (frozen, deferred elsewhere): worker
    identity/heartbeat, any retry-exhaustion counter or schema, any
    envelope/runtime-exhaustion completion detection (unchanged from
    Milestone 8 - a batch that stops progressing for an envelope/
    runtime reason simply remains RUNNING after this call returns), any
    scheduler, API, or CLI.
    """

    def __init__(self, db: Session):
        self.db = db
        self.archive = ArchiveProcessingService(db)
        self.identity = IdentityResolutionService(db)
        self.normalization = NormalizationService(db)
        self.chunking = ChunkingService(db)
        self.embedding = PipelineEmbeddingService(db)
        self.reconciliation = BatchCompletionReconciliationService(db)

    def run_once(
        self,
        batch_id: int,
        *,
        worker_id: str,
        workspace_root: Path,
        max_depth: int = 10,
    ) -> OrchestratorRunResult:
        """One bounded, attended invocation against one `IngestionBatch`.
        Raises `ValueError` if `batch_id` does not exist. Does not
        itself require - or check - that the batch is `RUNNING`: the
        archive-processing/identity-resolution claim calls already
        refuse new admission once it is not (existing Milestone 4
        gate), and `claim_content_identity_group` (normalization/
        chunking/embedding) is frozen as a global, batch-unaware claim
        that correctly proceeds regardless of any one batch's status
        (Implementation Design Pass, section 19) - this method adds no
        new gate on top of either, existing behavior."""
        batch = self.db.get(IngestionBatch, batch_id)
        if batch is None:
            raise ValueError(f"IngestionBatch {batch_id} not found")

        classification_run_id = batch.classification_run_id
        logger.info("orchestrator run starting: batch_id=%s worker_id=%s", batch_id, worker_id)

        stage_results = [
            self._run_stage_to_exhaustion(
                "archive_processing",
                lambda exclude_ids: self.archive.process_next_archive(
                    worker_id=worker_id,
                    workspace_root=workspace_root,
                    max_depth=max_depth,
                    classification_run_id=classification_run_id,
                    exclude_ids=exclude_ids,
                ),
            ),
            self._run_stage_to_exhaustion(
                "identity_resolution",
                lambda exclude_ids: self.identity.resolve_next(
                    worker_id=worker_id,
                    workspace_root=workspace_root,
                    classification_run_id=classification_run_id,
                    exclude_ids=exclude_ids,
                ),
            ),
            self._run_stage_to_exhaustion(
                "normalization",
                lambda _exclude_ids: self.normalization.normalize_next(
                    worker_id=worker_id, workspace_root=workspace_root
                ),
            ),
            self._run_stage_to_exhaustion(
                "chunking",
                lambda _exclude_ids: self.chunking.chunk_next(worker_id=worker_id, workspace_root=workspace_root),
            ),
            self._run_stage_to_exhaustion(
                "embedding",
                lambda _exclude_ids: self.embedding.embed_next(worker_id=worker_id),
            ),
        ]

        completion = self.reconciliation.check_and_complete(batch_id)
        if completion is not None:
            logger.info(
                "orchestrator run: check_and_complete applied=%s stop_reason=%s",
                completion.applied,
                completion.batch.stop_reason,
            )
        else:
            logger.info(
                "orchestrator run: batch_id=%s not source-work-exhausted (or not RUNNING) - remains as-is",
                batch_id,
            )

        logger.info("orchestrator run finished: batch_id=%s worker_id=%s", batch_id, worker_id)

        return OrchestratorRunResult(
            batch_id=batch_id,
            worker_id=worker_id,
            stage_results=stage_results,
            completion=completion,
        )

    @staticmethod
    def _run_stage_to_exhaustion(
        stage_name: str, claim_once: Callable[[frozenset[int]], object | None]
    ) -> StageResult:
        """Runs one stage's own claim-and-process method repeatedly
        until it returns `None` - no more claimable, not-yet-attempted-
        this-invocation work exists. `seen_ids` is local to this one
        call, for this one stage, and is discarded the moment this
        method returns - nothing here is persisted, and this is never
        confused with an attempt count or a retry-exhaustion decision:
        a later, separate `run_once()` invocation starts a fresh,
        empty set and will attempt the same row again.

        TWO GUARANTEES, NOT ONE (the M11 Correction/Reconciliation
        pass's own required distinction - see class docstring):

        (1) TERMINATION - the claim methods release a failed claim by
        resetting `claimed_by`/`claimed_at` to NULL unconditionally, so
        a deterministically-failing row is immediately reclaimable
        again with no lease wait. Passing `seen_ids` as `exclude_ids`
        into the claim call itself (for the two stages that accept it -
        archive processing and identity resolution; `claim_content_
        identity_group`, used by the other three stages, needs no such
        parameter - see below) means such a row is never reconsidered
        by the query at all after its first attempt, so the loop always
        terminates without ever re-processing it.

        (2) PROGRESS OVER DISTINCT CANDIDATES - a defect found DURING
        this milestone's own implementation, distinct from and more
        serious than (1): the claim query orders candidates by
        `created_at` alone, so an old, permanently-stuck row would win
        that ordering every iteration and starve every newer, distinct,
        genuinely-processable row for the WHOLE invocation - even
        though (1) alone still guarantees the loop eventually stops,
        it could stop having reached only the one stuck row, never the
        others. Passing `exclude_ids` INTO the query (not merely
        checking the result afterward) is what actually fixes this: once
        a row has been claimed-and-processed (success OR durable
        failure) this invocation, the very next call's own `SELECT`
        never considers it again, so the ordering naturally advances to
        the next-oldest REMAINING, unattempted row instead.

        Normalization/chunking/embedding's shared `claim_content_
        identity_group` has no equivalent gap: a failure there sets
        `pipeline_state=FAILED` unconditionally, and no caller's
        `eligible_pipeline_states` list ever includes `FAILED` - such a
        row becomes permanently ineligible for ANY future claim the
        moment it fails, not merely "immediately reclaimable by
        timing," so it can never win a future ordering race at all.
        Their lambdas ignore the `exclude_ids` argument they are still
        passed, for a uniform call shape."""
        seen_ids: set[int] = set()
        processed_count = 0
        while True:
            result = claim_once(frozenset(seen_ids))
            if result is None:
                break
            seen_ids.add(result.id)
            processed_count += 1
        logger.info("orchestrator stage '%s' finished: processed=%d", stage_name, processed_count)
        return StageResult(stage=stage_name, processed_count=processed_count)
