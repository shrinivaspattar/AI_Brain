from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.resource_guard import BatchResourceGuard
from app.classification.worker_claim_service import WorkerClaimService
from app.embeddings.client import EmbeddingClient
from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_attempt import (
    IngestionAttemptOutcome,
    IngestionAttemptStage,
    IngestionFailureCode,
)
from app.models.ingestion_batch import BatchStatus, IngestionBatch
from app.models.source_instance import SourceInstance

_ELIGIBLE_CLAIM_STATES = [ContentPipelineState.CHUNKED, ContentPipelineState.EMBEDDED]


class PipelineEmbeddingService:
    """Claims a ContentIdentityGroup at CHUNKED, embeds every chunk of
    its Document that does not already have an embedding, and - once
    every chunk is embedded - advances straight through EMBEDDED to
    INGESTED (the pipeline's sole success-terminal state).

    IDEMPOTENT / RESUMABLE BY CONSTRUCTION: only chunks with
    `embedding IS NULL` are ever selected for embedding. A crash after
    embedding some chunks but before the group's state advances leaves
    those chunks embedded and the rest NULL - a later claim (fresh or
    via stale-claim recovery) finds exactly the remaining NULL chunks
    and embeds only those, never re-embedding (and never re-billing an
    external embedding call for) work already durably completed. This
    directly answers the "embedding crash -> resumable/idempotent"
    requirement without needing any special crash-detection logic - it
    falls out of always querying `WHERE embedding IS NULL` fresh on
    every claim.

    This is a deliberately DIFFERENT code path from the existing
    `EmbeddingService.embed_document()` (which atomically deletes and
    recreates ALL of a document's chunks together) - that method
    remains unchanged, used only by the pre-existing personal-corpus
    `ImportJobService` flow. This pipeline's chunk/embed split is a
    different, two-claim design specifically so a crash between
    chunking and (fully) embedding is independently resumable at each
    boundary, matching the frozen `NORMALIZED -> CHUNKED -> EMBEDDED`
    state machine literally rather than collapsing it into one step.

    MILESTONE 6: wired to the fenced embedding-reservation lifecycle
    (`WorkerClaimService.reserve_embeddings`/`consume_embedding_
    reservation`/`release_embedding_reservation`) that Milestone 4
    built and froze but no caller ever exercised until now - see
    "Scaled Real-T7 Ingestion - Milestone 6 Design" section 3/4/4a for
    the full specification this implements literally, including the
    exact reservation-ownership semantics (arbitration-only lowest-id
    resolution, RUNNING-only eligibility checked twice, no cross-batch
    capacity borrowing, reservation ownership as resource-cost
    attribution only - never content/claim ownership).
    """

    def __init__(self, db: Session, embedding_client: EmbeddingClient | None = None):
        self.db = db
        self.claims = WorkerClaimService(db)
        self.attempts = IngestionAttemptService(db)
        self.embedding_client = embedding_client or EmbeddingClient()

    def embed_next(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(minutes=10),
        guard: BatchResourceGuard | None = None,
    ) -> ContentIdentityGroup | None:
        """`guard` (Milestone 6 addition, default `None` = pre-
        Milestone-6 behavior, unchanged): threaded straight into
        `reserve_embeddings`, exactly mirroring how
        `ArchiveProcessingService.process_next_archive` already threads
        `guard` into its own pre-flight reservation call (Milestone 5).
        """
        group = self.claims.claim_content_identity_group(
            worker_id=worker_id,
            eligible_pipeline_states=_ELIGIBLE_CLAIM_STATES,
            lease_duration=lease_duration,
        )
        if group is None:
            return None

        try:
            deferred = self._embed_claimed_group(group, worker_id=worker_id, guard=guard)
        except Exception:
            self.claims.release_content_identity_group_claim(group.id, claim_generation=group.claim_generation)
            raise

        if deferred:
            # A denied reservation: reserve_embeddings has already
            # released the claim, no IngestionAttempt was recorded, and
            # nothing was decided about this item - "None-equivalent"
            # to the caller, exactly matching
            # ArchiveProcessingService.process_next_archive's own
            # pre-flight-denial `return None` (Milestone 5), never the
            # stale, already-released group object.
            return None

        self.db.refresh(group)
        return group

    def _embed_claimed_group(
        self, group: ContentIdentityGroup, *, worker_id: str, guard: BatchResourceGuard | None
    ) -> bool:
        """Returns `True` only when the attempt was cleanly deferred
        (a denied reservation - the claim is already released, no
        IngestionAttempt recorded). Returns `False` (or implicitly,
        via falling through) for every other path, including both
        success and durable failure - those represent real, recorded
        outcomes the caller's `embed_next` must still return the group
        for, matching pre-Milestone-6 behavior exactly."""
        document = (
            self.db.query(Document)
            .filter(Document.content_identity_group_id == group.id)
            .one_or_none()
        )
        if document is None:
            self._fail(
                group,
                worker_id=worker_id,
                failure_code=IngestionFailureCode.EMBEDDING_UNAVAILABLE,
                failure_detail=f"no Document found for group {group.id} at CHUNKED claim time",
            )
            return False

        unembedded_chunks = (
            self.db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document.id,
                DocumentChunk.embedding.is_(None),
            )
            .order_by(DocumentChunk.chunk_index)
            .all()
        )
        n = len(unembedded_chunks)

        if n == 0:
            # Nothing left to embed - e.g. a crash-resume where a prior
            # attempt already embedded everything but did not advance
            # pipeline_state. No reservation is needed or taken for
            # zero work (Milestone 6 design section 3).
            self._succeed(group, worker_id=worker_id, document_id=document.id, reserved=False)
            return False

        owning_batch_id = self._resolve_owning_running_batch(group.id)
        reserved = False

        if owning_batch_id is not None:
            outcome = self.claims.reserve_embeddings(
                group_id=group.id,
                my_generation=group.claim_generation,
                batch_id=owning_batch_id,
                n=n,
                guard=guard,
            )
            if not outcome.reserved:
                # reserve_embeddings has ALREADY released the claim on
                # every denial path (its own existing contract). Clean
                # deferral, per the frozen "item deferred" semantics -
                # no IngestionAttempt is recorded, no embed() call is
                # made, and no fallback to any other candidate batch is
                # attempted within this same claim attempt (Milestone 6
                # design section 4a, point 6 - a named, bounded scope
                # exclusion, not an oversight).
                return True
            reserved = True

        if reserved:
            # The full reserve->embed->persist sequence must be treated
            # as one unit for reservation safety: ANY exception in this
            # block - not just the embed() call itself - must release
            # the reservation, never leave it dangling (Milestone 6
            # authorization's mandatory invariant "a failed/unavailable
            # embedding operation does not leave an invalid
            # reservation"). This intentionally widens the failure
            # boundary beyond the pre-Milestone-6 (unreserved) path
            # below, which is left byte-for-byte unchanged.
            try:
                vectors = self.embedding_client.embed([chunk.content for chunk in unembedded_chunks])
                for chunk, vector in zip(unembedded_chunks, vectors):
                    chunk.embedding = vector
                self.db.commit()
            except Exception as exc:  # noqa: BLE001 - the embedding backend is external
                self.db.rollback()
                self._fail(
                    group,
                    worker_id=worker_id,
                    failure_code=IngestionFailureCode.EMBEDDING_UNAVAILABLE,
                    failure_detail=str(exc),
                    reserved=True,
                )
                return False
        else:
            # Pre-Milestone-6 behavior, unchanged: no owning RUNNING
            # batch exists for this group (Milestone 6 design section
            # 4's explicit invariant - never strand the item at CHUNKED
            # merely for lack of an active batch to charge).
            try:
                vectors = self.embedding_client.embed([chunk.content for chunk in unembedded_chunks])
            except Exception as exc:  # noqa: BLE001 - the embedding backend is external
                self._fail(
                    group,
                    worker_id=worker_id,
                    failure_code=IngestionFailureCode.EMBEDDING_UNAVAILABLE,
                    failure_detail=str(exc),
                )
                return False

            for chunk, vector in zip(unembedded_chunks, vectors):
                chunk.embedding = vector
            self.db.commit()

        self._succeed(group, worker_id=worker_id, document_id=document.id, reserved=reserved)
        return False

    def _resolve_owning_running_batch(self, group_id: int) -> int | None:
        """Milestone 6 design section 4: deterministic (lowest-`id`,
        arbitration-only - never business/semantic priority, see design
        section 4a points 3-4) resolution of which currently-`RUNNING`
        batch's ledger is charged for this group's embedding
        reservation, among every batch whose own `SourceInstance` rows
        resolve (via already-proven content-identity convergence) to
        this group. Returns `None` if no `RUNNING` batch currently owns
        the group - the caller then proceeds unreserved (section 4's
        explicit invariant), never blocking completion merely for lack
        of an active batch to charge. This is a plain, unlocked read:
        the actual admission-safety re-check happens atomically inside
        `reserve_embeddings` itself (design section 4a, point 1), so no
        additional locking is needed or taken here."""
        return self.db.execute(
            select(IngestionBatch.id)
            .join(SourceInstance, SourceInstance.classification_run_id == IngestionBatch.classification_run_id)
            .where(
                SourceInstance.content_identity_group_id == group_id,
                IngestionBatch.status == BatchStatus.RUNNING,
            )
            .order_by(IngestionBatch.id.asc())
            .limit(1)
        ).scalar_one_or_none()

    def _succeed(
        self, group: ContentIdentityGroup, *, worker_id: str, document_id: int, reserved: bool
    ) -> None:
        self.attempts.record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.EMBEDDING,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )

        remaining_unembedded = (
            self.db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document_id,
                DocumentChunk.embedding.is_(None),
            )
            .count()
        )
        final_state = (
            ContentPipelineState.INGESTED
            if remaining_unembedded == 0
            else ContentPipelineState.EMBEDDED
        )

        if reserved:
            # consume_embedding_reservation clears the reservation
            # (fenced to claim_generation) and releases the claim with
            # final_state in the same call - do not also call
            # release_content_identity_group_claim for this path.
            self.claims.consume_embedding_reservation(
                group_id=group.id, my_generation=group.claim_generation, new_pipeline_state=final_state
            )
        else:
            self.claims.release_content_identity_group_claim(
                group.id, claim_generation=group.claim_generation, new_pipeline_state=final_state
            )

    def _fail(
        self,
        group: ContentIdentityGroup,
        *,
        worker_id: str,
        failure_code: IngestionFailureCode,
        failure_detail: str,
        reserved: bool = False,
    ) -> None:
        self.attempts.record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.EMBEDDING,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.FAILED,
            failure_code=failure_code,
            failure_detail=failure_detail,
            retryable=True,
        )
        if reserved:
            # release_embedding_reservation reads reserved_embeddings_
            # batch_id fresh from the row (never a remembered value),
            # credits it back, clears the reservation, and releases the
            # claim - fenced to claim_generation throughout. It takes
            # no pipeline_state parameter (by design - it also serves
            # stale-reservation recovery, which must never itself
            # decide a durable outcome), so it leaves pipeline_state
            # untouched. The explicit release_content_identity_group_
            # claim call below is therefore still required to durably
            # record FAILED, matching the unreserved path exactly - it
            # is a safe, idempotent, generation-fenced no-op if a
            # concurrent stale-claim reclaim has since moved the row to
            # a newer generation.
            self.claims.release_embedding_reservation(group_id=group.id, my_generation=group.claim_generation)
        self.claims.release_content_identity_group_claim(
            group.id, claim_generation=group.claim_generation, new_pipeline_state=ContentPipelineState.FAILED
        )
