from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.ingestion_attempt import (
    IngestionAttempt,
    IngestionAttemptKind,
    IngestionAttemptOutcome,
    IngestionAttemptStage,
    IngestionFailureCode,
)


class IngestionAttemptService:
    """Records one durable, immutable audit row per ingestion attempt -
    see IngestionAttempt's docstring for why this is a per-attempt
    table rather than mutable columns on ContentIdentityGroup/
    SourceInstance. Every attempt is recorded, success or failure, so
    full retry history is always reconstructable.

    This service only records outcomes a caller already determined -
    it performs no extraction, normalization, chunking, or embedding
    itself, and never will in this schema-extension milestone.
    """

    def __init__(self, db: Session):
        self.db = db

    def record_pipeline_attempt(
        self,
        *,
        content_identity_group_id: int,
        attempted_stage: IngestionAttemptStage,
        worker_id: str,
        outcome: IngestionAttemptOutcome,
        failure_code: IngestionFailureCode | None = None,
        failure_detail: str | None = None,
        retryable: bool | None = None,
    ) -> IngestionAttempt:
        if attempted_stage == IngestionAttemptStage.IDENTITY_RESOLUTION:
            raise ValueError(
                "IDENTITY_RESOLUTION is not a valid stage for a "
                "PIPELINE_ADVANCE attempt - use record_identity_"
                "resolution_attempt instead."
            )
        self._validate_failure_fields(outcome, failure_code, failure_detail, retryable)

        attempt = IngestionAttempt(
            attempt_kind=IngestionAttemptKind.PIPELINE_ADVANCE,
            content_identity_group_id=content_identity_group_id,
            source_instance_id=None,
            attempted_stage=attempted_stage,
            outcome=outcome,
            failure_code=failure_code,
            failure_detail=failure_detail,
            retryable=retryable,
            worker_id=worker_id,
            attempted_at=datetime.now(UTC),
        )
        self.db.add(attempt)
        self.db.commit()
        self.db.refresh(attempt)
        return attempt

    def record_identity_resolution_attempt(
        self,
        *,
        source_instance_id: int,
        worker_id: str,
        outcome: IngestionAttemptOutcome,
        failure_code: IngestionFailureCode | None = None,
        failure_detail: str | None = None,
        retryable: bool | None = None,
    ) -> IngestionAttempt:
        self._validate_failure_fields(outcome, failure_code, failure_detail, retryable)

        attempt = IngestionAttempt(
            attempt_kind=IngestionAttemptKind.IDENTITY_RESOLUTION,
            content_identity_group_id=None,
            source_instance_id=source_instance_id,
            attempted_stage=IngestionAttemptStage.IDENTITY_RESOLUTION,
            outcome=outcome,
            failure_code=failure_code,
            failure_detail=failure_detail,
            retryable=retryable,
            worker_id=worker_id,
            attempted_at=datetime.now(UTC),
        )
        self.db.add(attempt)
        self.db.commit()
        self.db.refresh(attempt)
        return attempt

    @staticmethod
    def _validate_failure_fields(
        outcome: IngestionAttemptOutcome,
        failure_code: IngestionFailureCode | None,
        failure_detail: str | None,
        retryable: bool | None,
    ) -> None:
        if outcome == IngestionAttemptOutcome.SUCCEEDED:
            return
        if failure_code is None or failure_detail is None or retryable is None:
            raise ValueError(
                "A FAILED attempt requires failure_code, failure_detail, "
                "and retryable - never inferred, never left partially "
                "recorded."
            )
