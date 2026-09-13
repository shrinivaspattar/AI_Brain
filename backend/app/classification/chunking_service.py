from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.worker_claim_service import WorkerClaimService
from app.classification.workspace import find_existing_workspace_content
from app.embeddings.chunker import chunk_text
from app.ingestion.text_extractor import extract_text
from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_attempt import (
    IngestionAttemptOutcome,
    IngestionAttemptStage,
    IngestionFailureCode,
)

_ELIGIBLE_CLAIM_STATES = [ContentPipelineState.NORMALIZED]


class ChunkingService:
    """Claims a ContentIdentityGroup at NORMALIZED, splits its
    Document's normalized text into `DocumentChunk` rows with
    `embedding = NULL` - the model's own nullable `embedding` column
    already anticipated exactly this chunked-but-not-yet-embedded
    intermediate state, which is what makes CHUNKED and EMBEDDED two
    genuinely distinct, independently resumable steps rather than one
    atomic "chunk and embed" operation.

    IDEMPOTENT, not delete-then-recreate: if chunks already exist for
    this Document (a prior attempt created them before crashing, maybe
    even before any embeddings were computed), this step is a no-op -
    it never deletes and recreates chunks, because doing so would
    discard any embeddings a later, partially-completed embedding pass
    already computed for some of them.
    """

    def __init__(self, db: Session):
        self.db = db
        self.claims = WorkerClaimService(db)
        self.attempts = IngestionAttemptService(db)

    def chunk_next(
        self,
        *,
        worker_id: str,
        workspace_root: Path,
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
            self._chunk_claimed_group(group, worker_id=worker_id, workspace_root=workspace_root)
        except Exception:
            self.claims.release_content_identity_group_claim(group.id)
            raise

        self.db.refresh(group)
        return group

    def _chunk_claimed_group(
        self,
        group: ContentIdentityGroup,
        *,
        worker_id: str,
        workspace_root: Path,
    ) -> None:
        document = (
            self.db.query(Document)
            .filter(Document.content_identity_group_id == group.id)
            .one_or_none()
        )
        if document is None:
            self._fail(
                group,
                worker_id=worker_id,
                failure_code=IngestionFailureCode.NORMALIZATION_ERROR,
                failure_detail=f"no Document found for group {group.id} at CHUNKED claim time",
            )
            return

        existing_chunk_count = (
            self.db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document.id)
            .count()
        )

        if existing_chunk_count == 0:
            content_path = find_existing_workspace_content(workspace_root, group.id)
            if content_path is None:
                self._fail(
                    group,
                    worker_id=worker_id,
                    failure_code=IngestionFailureCode.READ_ERROR_OTHER,
                    failure_detail=f"no workspace content found for group {group.id}",
                )
                return

            try:
                text = extract_text(content_path)
            except Exception as exc:  # noqa: BLE001 - see NormalizationService's
                # identical broad catch: extract_text can raise any of
                # several library-specific parse errors, not just
                # UnicodeDecodeError/OSError.
                self._fail(
                    group,
                    worker_id=worker_id,
                    failure_code=IngestionFailureCode.CHUNKING_ERROR,
                    failure_detail=str(exc),
                )
                return

            chunks = chunk_text(text)
            self.db.add_all(
                DocumentChunk(document_id=document.id, chunk_index=index, content=chunk, embedding=None)
                for index, chunk in enumerate(chunks)
            )
            self.db.commit()

        self.attempts.record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.CHUNKING,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )
        self.claims.release_content_identity_group_claim(
            group.id, new_pipeline_state=ContentPipelineState.CHUNKED
        )

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
            attempted_stage=IngestionAttemptStage.CHUNKING,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.FAILED,
            failure_code=failure_code,
            failure_detail=failure_detail,
            retryable=True,
        )
        self.claims.release_content_identity_group_claim(
            group.id, new_pipeline_state=ContentPipelineState.FAILED
        )
