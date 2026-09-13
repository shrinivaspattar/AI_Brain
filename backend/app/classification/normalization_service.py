from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.workspace import find_existing_workspace_content
from app.classification.worker_claim_service import WorkerClaimService
from app.ingestion.text_extractor import extract_text
from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState
from app.models.ingestion_attempt import (
    IngestionAttemptOutcome,
    IngestionAttemptStage,
    IngestionFailureCode,
)
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService

_ELIGIBLE_CLAIM_STATES = [ContentPipelineState.EXTRACTED]


class NormalizationService:
    """Claims a ContentIdentityGroup at EXTRACTED, normalizes its
    workspace content into plain text (reusing the existing,
    unmodified `extract_text()`), and creates its Document row - the
    FIRST point a Document exists for this content identity, no later
    than `CHUNKED` as `14b8063` round 5 requires, and here specifically
    at `NORMALIZED` since `DocumentChunk` rows created downstream need
    a `Document` to already exist.

    `Document.content_hash` is set to the group's own `identity_hash` -
    they are the same value by construction (the frozen design's own
    stated invariant: `content_hash` must always equal the owning
    group's `identity_hash`), never independently recomputed here.
    """

    def __init__(self, db: Session):
        self.db = db
        self.claims = WorkerClaimService(db)
        self.attempts = IngestionAttemptService(db)
        self.documents = DocumentService(db)

    def normalize_next(
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
            self._normalize_claimed_group(group, worker_id=worker_id, workspace_root=workspace_root)
        except Exception:
            self.claims.release_content_identity_group_claim(group.id)
            raise

        self.db.refresh(group)
        return group

    def _normalize_claimed_group(
        self,
        group: ContentIdentityGroup,
        *,
        worker_id: str,
        workspace_root: Path,
    ) -> None:
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
            extract_text(content_path)
        except Exception as exc:  # noqa: BLE001 - extract_text dispatches to
            # pypdf/docx/pptx/openpyxl, each with its own exception
            # hierarchy for malformed input (not just UnicodeDecodeError/
            # OSError) - any of them means "this specific file is
            # corrupt," a durable FAILED outcome, never an uncaught
            # crash out of a claimed worker.
            self._fail(
                group,
                worker_id=worker_id,
                failure_code=IngestionFailureCode.CORRUPT_INPUT,
                failure_detail=str(exc),
            )
            return

        # Idempotent resume: a crash between creating the Document and
        # recording success on a prior attempt must not create a
        # second Document for the same (UNIQUE) content_identity_group_id.
        existing_document = self._find_existing_document(group.id)

        if existing_document is None:
            self.documents.create_document(
                DocumentCreate(
                    title=content_path.name,
                    source=str(content_path),
                    source_type=content_path.suffix.lower().lstrip(".") or "unknown",
                    content_hash=group.identity_hash,
                    content_identity_group_id=group.id,
                )
            )

        self.attempts.record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.NORMALIZING,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )
        self.claims.release_content_identity_group_claim(
            group.id, new_pipeline_state=ContentPipelineState.NORMALIZED
        )

    def _find_existing_document(self, group_id: int):
        from app.models.document import Document as DocumentModel

        return (
            self.db.query(DocumentModel)
            .filter(DocumentModel.content_identity_group_id == group_id)
            .one_or_none()
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
            attempted_stage=IngestionAttemptStage.NORMALIZING,
            worker_id=worker_id,
            outcome=IngestionAttemptOutcome.FAILED,
            failure_code=failure_code,
            failure_detail=failure_detail,
            retryable=True,
        )
        self.claims.release_content_identity_group_claim(
            group.id, new_pipeline_state=ContentPipelineState.FAILED
        )
