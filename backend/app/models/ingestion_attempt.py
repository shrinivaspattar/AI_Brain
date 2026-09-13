from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class IngestionAttemptKind(str, Enum):
    # Advancing a ContentIdentityGroup's pipeline_state one step
    # forward (EXTRACTING/NORMALIZING/CHUNKING/EMBEDDING).
    PIPELINE_ADVANCE = "pipeline_advance"
    # Resolving a root-level SourceInstance's unknown content identity
    # (reading/hashing a uniquely-sized loose file D1 never hashed).
    IDENTITY_RESOLUTION = "identity_resolution"


class IngestionAttemptStage(str, Enum):
    IDENTITY_RESOLUTION = "identity_resolution"
    EXTRACTING = "extracting"
    NORMALIZING = "normalizing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"


class IngestionAttemptOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IngestionFailureCode(str, Enum):
    """A small, controlled vocabulary matching the failure/retry matrix
    in AI_Brain_Architecture.md's "Controlled T7 -> AI_Brain Ingestion
    Design" section (`6491dad`) - not an open free-text field, so a
    future retry sweep can filter reliably."""

    CORRUPT_INPUT = "corrupt_input"
    MALFORMED_ARCHIVE = "malformed_archive"
    OVERSIZED_OR_EXPANSION_LIMIT = "oversized_or_expansion_limit"
    EXTRACTION_ERROR_OTHER = "extraction_error_other"
    NORMALIZATION_ERROR = "normalization_error"
    CHUNKING_ERROR = "chunking_error"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    T7_UNAVAILABLE = "t7_unavailable"
    INSUFFICIENT_DISK_SPACE = "insufficient_disk_space"
    PERMISSION_DENIED = "permission_denied"
    READ_ERROR_OTHER = "read_error_other"


class IngestionAttempt(Base):
    """One durable, immutable audit row per ingestion attempt - matching
    this codebase's existing one-row-per-event precedent
    (`DedupExecutionActionAudit`, `DiscoveryRun`, `ClassificationRun`),
    not a mutable "latest failure" column bolted onto a parent row.
    Recorded for EVERY attempt, success or failure, so a full retry
    history is always reconstructable - not only the most recent
    outcome.

    CORRECT PARENT RELATIONSHIP: exactly one of `content_identity_
    group_id` (for a `PIPELINE_ADVANCE` attempt) or `source_instance_id`
    (for an `IDENTITY_RESOLUTION` attempt) is set, matching
    `attempt_kind` - enforced by the CHECK constraint below, mirroring
    `DuplicateReview`'s existing "exactly one of content_hash/similarity,
    depending on match_type" pattern rather than a generic polymorphic
    association table.

    Failure fields (`failure_code`/`failure_detail`/`retryable`) are
    populated if and only if `outcome = FAILED` - enforced by CHECK,
    mirroring `SourceInstance.canonical_status`'s own evidence-required
    constraint. They are never populated for, and never used to
    distinguish, `EXCLUDED`/`UNSUPPORTED`/`NEEDS_REVIEW` -
    `ContentPipelineState` values with their own, different reasoning
    that lives elsewhere (`SourceInstance.evidence_snapshot`, policy
    documentation) - this table exists only for genuine `FAILED`
    attempts and their `SUCCEEDED` counterparts, nothing else.
    """

    __tablename__ = "ingestion_attempts"
    __table_args__ = (
        CheckConstraint(
            "(attempt_kind = 'PIPELINE_ADVANCE' "
            " AND content_identity_group_id IS NOT NULL "
            " AND source_instance_id IS NULL "
            " AND attempted_stage != 'IDENTITY_RESOLUTION') "
            "OR "
            "(attempt_kind = 'IDENTITY_RESOLUTION' "
            " AND source_instance_id IS NOT NULL "
            " AND content_identity_group_id IS NULL "
            " AND attempted_stage = 'IDENTITY_RESOLUTION')",
            name="ck_ingestion_attempts_parent_matches_kind",
        ),
        CheckConstraint(
            "outcome = 'SUCCEEDED' OR ("
            "failure_code IS NOT NULL "
            "AND failure_detail IS NOT NULL "
            "AND retryable IS NOT NULL"
            ")",
            name="ck_ingestion_attempts_failure_requires_detail",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    attempt_kind: Mapped[IngestionAttemptKind] = mapped_column(
        SQLEnum(IngestionAttemptKind, name="ingestion_attempt_kind"),
        nullable=False,
    )

    content_identity_group_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("content_identity_groups.id"),
        nullable=True,
        index=True,
    )

    source_instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("source_instances.id"),
        nullable=True,
        index=True,
    )

    attempted_stage: Mapped[IngestionAttemptStage] = mapped_column(
        SQLEnum(IngestionAttemptStage, name="ingestion_attempt_stage"),
        nullable=False,
    )

    outcome: Mapped[IngestionAttemptOutcome] = mapped_column(
        SQLEnum(IngestionAttemptOutcome, name="ingestion_attempt_outcome"),
        nullable=False,
    )

    failure_code: Mapped[IngestionFailureCode | None] = mapped_column(
        SQLEnum(IngestionFailureCode, name="ingestion_failure_code"),
        nullable=True,
    )

    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Whether THIS failure is expected to succeed on a later retry
    # (e.g. embedding service transiently unreachable) vs. requiring
    # intervention (e.g. corrupt input) - populated only alongside
    # failure_code/failure_detail, never guessed at implementation time
    # per attempt without being recorded here.
    retryable: Mapped[bool | None] = mapped_column(nullable=True)

    worker_id: Mapped[str] = mapped_column(Text, nullable=False)

    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    # Deliberately no updated_at: an audit row is written once and never
    # edited, matching DedupExecutionActionAudit/DiscoveryRun/
    # ClassificationRun's existing immutable-audit-row convention.
