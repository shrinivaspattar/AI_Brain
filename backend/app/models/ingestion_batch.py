from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import BigInteger, CheckConstraint, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class BatchStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"


class BatchStopReason(str, Enum):
    # Designed exhaustion - a normal, successful path to COMPLETED,
    # never a failure. See "Scaled Real-T7 Ingestion - Implementation
    # Design Pass" point 12's exact status-mapping table.
    SOURCE_WORK_EXHAUSTED = "source_work_exhausted"
    EXTRACTED_BYTES_ENVELOPE_EXHAUSTED = "extracted_bytes_envelope_exhausted"
    EMBEDDINGS_ENVELOPE_EXHAUSTED = "embeddings_envelope_exhausted"
    RUNTIME_BUDGET_EXCEEDED = "runtime_budget_exceeded"
    # Soft stop - resumable, PAUSED.
    WORKSPACE_SOFT_STOP = "workspace_soft_stop"
    POSTGRES_SOFT_STOP = "postgres_soft_stop"
    MANUAL_PAUSE = "manual_pause"
    # Hard stop / safety - terminal, ABORTED. Retry requires a brand-new
    # IngestionBatch, never resuming this one.
    WORKSPACE_HARD_STOP = "workspace_hard_stop"
    POSTGRES_HARD_STOP = "postgres_hard_stop"
    OLLAMA_PERSISTENTLY_UNREACHABLE = "ollama_persistently_unreachable"
    SAFETY_INVARIANT_VIOLATION_DETECTED = "safety_invariant_violation_detected"
    # The explicit "PAUSED -(operator aborts explicitly)-> ABORTED"
    # edge from "### 12. Batch state machine - exact transitions" -
    # added per Implementation Milestone 3's final-correction pass.
    # Deliberately distinct from every resource/safety reason above:
    # those describe something the SYSTEM detected; this describes a
    # human's deliberate decision to stop, with no detection behind it.
    # The frozen state machine documents this operator-abort edge ONLY
    # from PAUSED, never as a cause of RUNNING -> ABORTED directly (that
    # edge's only documented causes are hard-stop / persistent-Ollama-
    # failure / safety-invariant-violation) - `BatchControlService.abort()`
    # enforces this exact restriction, never accepting MANUAL_ABORT from
    # a RUNNING batch.
    MANUAL_ABORT = "manual_abort"


class IngestionBatch(Base):
    """One scaled, bounded real-T7 ingestion batch - the durable
    record of a single controlled-envelope run, per "Scaled Real-T7
    Ingestion Design" (`f2b9815`), its numeric/policy baseline
    (`3d37ec0`), and its implementation design (`2fab4b3`).

    SCHEMA/MODEL MILESTONE ONLY: this model implements the durable
    shape the frozen design specifies. No BatchCreationService,
    PolicyEvaluator, deterministic selector, BatchResourceGuard,
    runtime-accounting loop, extraction/embedding-reservation lifecycle,
    or batch-aware worker claiming exists yet - those are separate,
    later, independently-authorized implementation milestones. This
    row can be constructed and persisted, and its invariants are
    enforced at the database level, but nothing in this codebase yet
    populates or advances one as part of real ingestion.

    EXCLUSIVE 1:1 OWNERSHIP (frozen invariant): `classification_run_id`
    is UNIQUE - a `ClassificationRun` belongs to at most one
    `IngestionBatch`, ever. Batch membership is therefore exactly "the
    `SourceInstance` rows under this `classification_run_id`" - never
    re-derived by re-running selection (see `ClassificationRun`,
    `SourceInstance`).

    IMMUTABILITY (service-level, matching this codebase's existing
    convention for write-once fields - e.g. `SourceInstance.content_
    identity_group_id`'s write-once contract is enforced the same way,
    not by a DB trigger): `classification_run_id`, every `max_*`
    envelope field, `eligible_source_count`/`policy_filtered_count`/
    `selectable_count`/`source_instances_selected`/
    `source_bytes_selected`, `selection_fingerprint`,
    `selection_policy_version`, and `ordering_version` are written
    exactly once, at creation, by whatever future batch-creation
    service exists - no method in this codebase updates them once set.
    Only `status`, `stop_reason`, `stop_reason_detail`, the three
    runtime counters, and the three timestamps are ever mutated
    post-creation.

    COUNTER SEMANTICS: `extracted_bytes_consumed` and
    `embeddings_reserved` are intended to be updated exclusively via
    atomic conditional `UPDATE ... WHERE ... <= max_* RETURNING`
    statements (never read-then-write) once the reservation lifecycle
    is implemented - the CHECK constraints below are a DB-level
    backstop for that invariant, not a replacement for it.
    `monotonic_runtime_seconds_consumed` accumulates via plain
    (non-conditional) increments at checkpoint time.

    TIMESTAMPS are wall-clock, audit-only - never authoritative for
    runtime-budget decisions (`monotonic_runtime_seconds_consumed` is).

    `review_required` is set once, at creation, from a projection
    check - purely advisory, orthogonal to `status`; it never itself
    pauses or aborts anything.

    `stop_reason`/`stop_reason_detail` are set together with `status`
    whenever leaving `RUNNING` (see `BatchStopReason`): the four
    "*_EXHAUSTED"/"RUNTIME_BUDGET_EXCEEDED" reasons are the designed,
    successful path to `COMPLETED`; the two soft-stop reasons lead to
    resumable `PAUSED`; the remaining hard-stop/safety reasons lead to
    terminal `ABORTED`. Enforced by the CHECK constraint below: `stop_
    reason` is NULL if and only if `status` is `PLANNED`/`RUNNING`.

    `selection_fingerprint` is a SHA-256 hex digest of a canonical
    serialization of (D0 report hash, `selection_policy_version`,
    `ordering_version`, the envelope values, and the sorted selected
    source references) - durable proof of exactly what was
    authorized/executed. `selection_policy_version` identifies WHICH
    selection predicate was used (bumped only when the predicate logic
    itself changes, never for a numeric envelope change).
    `ordering_version` identifies the deterministic ordering algorithm
    separately, so a future ordering change is distinguishable from a
    policy change.
    """

    __tablename__ = "ingestion_batches"
    __table_args__ = (
        UniqueConstraint(
            "classification_run_id",
            name="uq_ingestion_batches_classification_run_id",
        ),
        CheckConstraint(
            "(status IN ('PLANNED', 'RUNNING') AND stop_reason IS NULL) "
            "OR (status IN ('PAUSED', 'COMPLETED', 'ABORTED') AND stop_reason IS NOT NULL)",
            name="ck_ingestion_batches_stop_reason_matches_status",
        ),
        CheckConstraint("max_source_instances > 0", name="ck_ingestion_batches_max_source_instances_positive"),
        CheckConstraint("max_source_bytes > 0", name="ck_ingestion_batches_max_source_bytes_positive"),
        CheckConstraint(
            "max_extracted_bytes IS NULL OR max_extracted_bytes >= 0",
            name="ck_ingestion_batches_max_extracted_bytes_non_negative",
        ),
        CheckConstraint("max_embeddings > 0", name="ck_ingestion_batches_max_embeddings_positive"),
        CheckConstraint("max_runtime_seconds > 0", name="ck_ingestion_batches_max_runtime_seconds_positive"),
        CheckConstraint("eligible_source_count >= 0", name="ck_ingestion_batches_eligible_source_count_non_negative"),
        CheckConstraint("policy_filtered_count >= 0", name="ck_ingestion_batches_policy_filtered_count_non_negative"),
        CheckConstraint("selectable_count >= 0", name="ck_ingestion_batches_selectable_count_non_negative"),
        CheckConstraint(
            "source_instances_selected >= 0",
            name="ck_ingestion_batches_source_instances_selected_non_negative",
        ),
        CheckConstraint(
            "source_bytes_selected >= 0", name="ck_ingestion_batches_source_bytes_selected_non_negative"
        ),
        CheckConstraint(
            "extracted_bytes_consumed >= 0", name="ck_ingestion_batches_extracted_bytes_consumed_non_negative"
        ),
        CheckConstraint("embeddings_reserved >= 0", name="ck_ingestion_batches_embeddings_reserved_non_negative"),
        CheckConstraint(
            "monotonic_runtime_seconds_consumed >= 0",
            name="ck_ingestion_batches_monotonic_runtime_non_negative",
        ),
        CheckConstraint(
            "embeddings_reserved <= max_embeddings",
            name="ck_ingestion_batches_embeddings_reserved_within_envelope",
        ),
        CheckConstraint(
            "max_extracted_bytes IS NULL OR extracted_bytes_consumed <= max_extracted_bytes",
            name="ck_ingestion_batches_extracted_bytes_within_envelope",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # UNIQUE - exclusive 1:1 ownership, see class docstring.
    classification_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("classification_runs.id"),
        nullable=False,
    )

    status: Mapped[BatchStatus] = mapped_column(
        SQLEnum(BatchStatus, name="batch_status"),
        nullable=False,
        default=BatchStatus.PLANNED,
        server_default=BatchStatus.PLANNED.name,
    )

    stop_reason: Mapped[BatchStopReason | None] = mapped_column(
        SQLEnum(BatchStopReason, name="batch_stop_reason"),
        nullable=True,
    )

    stop_reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Advisory only - set once at creation from a projection check.
    # Orthogonal to status; never itself pauses or aborts anything.
    review_required: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
    )

    # -- Envelope: immutable once set (see class docstring) ----------

    max_source_instances: Mapped[int] = mapped_column(Integer, nullable=False)
    max_source_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL = not applicable (e.g. a batch class admitting no archives) -
    # a real, meaningful NULL, distinct from "not yet decided."
    max_extracted_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_embeddings: Mapped[int] = mapped_column(Integer, nullable=False)
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    # -- Selection-time facts: immutable once set (see class docstring) --

    eligible_source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_filtered_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selectable_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_instances_selected: Mapped[int] = mapped_column(Integer, nullable=False)
    source_bytes_selected: Mapped[int] = mapped_column(BigInteger, nullable=False)

    selection_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_policy_version: Mapped[str] = mapped_column(String(200), nullable=False)
    ordering_version: Mapped[str] = mapped_column(String(200), nullable=False)

    # -- Runtime counters: mutable only via narrow, purpose-built ----
    # -- service methods once the reservation/accounting lifecycle is --
    # -- implemented (a later milestone) - see class docstring. ------

    extracted_bytes_consumed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    embeddings_reserved: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    monotonic_runtime_seconds_consumed: Mapped[float] = mapped_column(
        Float, nullable=False, default=0, server_default="0"
    )

    # -- Timestamps: wall-clock, audit-only (see class docstring) ----

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
