from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.classification.ingestion_attempt_service import IngestionAttemptService
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
    ) -> ContentIdentityGroup | None:
        group = self.claims.claim_content_identity_group(
            worker_id=worker_id,
            eligible_pipeline_states=_ELIGIBLE_CLAIM_STATES,
            lease_duration=lease_duration,
        )
        if group is None:
            return None

        try:
            self._embed_claimed_group(group, worker_id=worker_id)
        except Exception:
            self.claims.release_content_identity_group_claim(group.id)
            raise

        self.db.refresh(group)
        return group

    def _embed_claimed_group(self, group: ContentIdentityGroup, *, worker_id: str) -> None:
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
            return

        unembedded_chunks = (
            self.db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document.id,
                DocumentChunk.embedding.is_(None),
            )
            .order_by(DocumentChunk.chunk_index)
            .all()
        )

        if unembedded_chunks:
            try:
                vectors = self.embedding_client.embed(
                    [chunk.content for chunk in unembedded_chunks]
                )
            except Exception as exc:  # noqa: BLE001 - the embedding backend is external
                self._fail(
                    group,
                    worker_id=worker_id,
                    failure_code=IngestionFailureCode.EMBEDDING_UNAVAILABLE,
                    failure_detail=str(exc),
                )
                return

            for chunk, vector in zip(unembedded_chunks, vectors):
                chunk.embedding = vector
            self.db.commit()

        self.attempts.record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.EMBEDDING,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )

        remaining_unembedded = (
            self.db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document.id,
                DocumentChunk.embedding.is_(None),
            )
            .count()
        )
        final_state = (
            ContentPipelineState.INGESTED
            if remaining_unembedded == 0
            else ContentPipelineState.EMBEDDED
        )
        self.claims.release_content_identity_group_claim(group.id, new_pipeline_state=final_state)

    def _fail(
        self,
        group: ContentIdentityGroup,
        *,
        worker_id: str,
        failure_code: IngestionFailureCode,
        failure_detail: str,
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
        self.claims.release_content_identity_group_claim(
            group.id, new_pipeline_state=ContentPipelineState.FAILED
        )
