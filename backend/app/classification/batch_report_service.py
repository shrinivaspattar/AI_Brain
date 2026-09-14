from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch
from app.models.source_instance import RiskTierEstimated, SourceCategory, SourceInstance

T = TypeVar("T")

# A ContentIdentityGroup reaching any of these pipeline states is
# "done" for this report's purposes, whether the outcome was success
# or a durable non-success - exactly the frozen "### 13.
# BatchReportService" list (Implementation Design Pass, 2fab4b3),
# copied verbatim, never re-derived or guessed.
_LOOSE_TERMINAL_PIPELINE_STATES = (
    ContentPipelineState.INGESTED,
    ContentPipelineState.EXCLUDED,
    ContentPipelineState.UNSUPPORTED,
    ContentPipelineState.FAILED,
    ContentPipelineState.NEEDS_REVIEW,
)


class BatchReportInvariantViolation(RuntimeError):
    """Raised when a report's own internal counts contradict each
    other. Per the Milestone 7 authorization: never clamped, silently
    repaired, or hidden - a violation always means a real bug upstream
    (e.g. a claim released without either completing or remaining
    cleanly unattempted), and must surface loudly rather than be
    smoothed over in a report a human might trust."""


@dataclass(frozen=True)
class Instrumented(Generic[T]):
    """Structurally distinguishes a real, computed value from data
    this milestone explicitly cannot compute yet (Milestone 7 Design
    Review: "Do not use an ad-hoc string... in a numeric field... an
    explicit representation for instrumentation availability").
    `available=False` always pairs with `value=None`; a caller must
    check `available` before trusting `value` - there is no way to
    mistake "not instrumented" for a genuine zero or empty result,
    unlike a bare `Optional[int]` would allow.

    Matches this module's sibling internal-service result types
    (`ReservationOutcome`, `GuardResult`, `_PreflightResult`,
    `TransitionResult`) - a frozen dataclass, not a Pydantic schema:
    Pydantic (`app/schemas/`) is this repository's established
    convention specifically for API request/response boundary types,
    and `BatchReportService` has no API boundary in this milestone
    (explicitly out of scope, per the frozen M7 design's non-goals)."""

    available: bool
    value: T | None = None

    @classmethod
    def unavailable(cls) -> "Instrumented[T]":
        return cls(available=False, value=None)

    @classmethod
    def of(cls, value: T) -> "Instrumented[T]":
        return cls(available=True, value=value)


@dataclass(frozen=True)
class DriftOutcomeCounts:
    """The four-way outcome from "Scaled Real-T7 Ingestion - Numeric +
    Policy Definition Pass" (`3d37ec0`), section 10. Defined here as a
    typed shape ready for a future instrumentation milestone to
    populate - this milestone never constructs one with real data (see
    `BatchReport.drift_outcomes`, always `Instrumented.unavailable()`
    today)."""

    observed_at_selection: int
    source_present_at_execution: int
    source_changed_after_selection: int
    source_missing_at_execution: int


@dataclass(frozen=True)
class BatchReport:
    """A pure, point-in-time snapshot of one `IngestionBatch` - never a
    finalization operation, never itself a claim, never a write of any
    kind. Valid for a batch in any `BatchStatus` (Milestone 7 Design
    Review, decision 4)."""

    batch_id: int
    status: BatchStatus
    stop_reason: BatchStopReason | None
    stop_reason_detail: str | None

    eligible_source_count: int
    policy_filtered_count: int
    selectable_count: int
    source_instances_selected: int
    source_bytes_selected: int

    attempted_source_count: int
    unattempted_selected_count: int
    terminal_source_count: int
    successful_ingestion_count: int

    risk_tier_estimated_distribution: dict[RiskTierEstimated | None, int]
    risk_tier_actual_distribution: dict[RiskTierEstimated | None, int]

    extracted_bytes_consumed: int
    embeddings_reserved: int
    monotonic_runtime_seconds_consumed: float

    # Milestone 7 Design Review, decision 1: DEFERRED. Never computed,
    # never approximated from D0's declared size, never represented as
    # a bare 0/None that could be misread as "checked, nothing found."
    actual_source_bytes_read: Instrumented[int]
    drift_outcomes: Instrumented[DriftOutcomeCounts]


class BatchReportService:
    """Frozen Milestone 7 design: a pure read/aggregation service over
    already-existing Milestone 1-6 data. Takes no claim, holds no
    lock, writes nothing, has no reservation to leak and no retry
    semantics of its own - the only failure mode is `batch_id` not
    found. Safe to call concurrently with active claim/reservation/
    extraction workers with no special synchronization; plain READ
    COMMITTED (this codebase's existing default everywhere else) is
    sufficient, per the Milestone 7 Design Review's explicit approval
    of decision 5.

    Reuses existing primitives only - IngestionBatch's own immutable
    selection fields and mutable counters, SourceInstance's
    classification columns, ContentIdentityGroup.pipeline_state,
    IngestionAttempt's existing (source_instance_id XOR
    content_identity_group_id)-scoped attempt history. No new schema,
    no migration, no change to any Milestone 1-6 write path.
    """

    def __init__(self, db: Session):
        self.db = db

    def generate_report(self, batch_id: int) -> BatchReport:
        batch = self.db.get(IngestionBatch, batch_id)
        if batch is None:
            raise ValueError(f"IngestionBatch {batch_id} not found")

        run_id = batch.classification_run_id

        attempted_source_count = self._attempted_source_count(run_id)
        unattempted_selected_count = batch.source_instances_selected - attempted_source_count

        loose_terminal, loose_successful = self._loose_file_terminal_and_successful_counts(run_id)
        archive_terminal, archive_successful = self._archive_container_terminal_and_successful_counts(run_id)
        terminal_source_count = loose_terminal + archive_terminal
        successful_ingestion_count = loose_successful + archive_successful

        self._assert_invariants(
            batch_id=batch_id,
            source_instances_selected=batch.source_instances_selected,
            attempted_source_count=attempted_source_count,
            terminal_source_count=terminal_source_count,
            successful_ingestion_count=successful_ingestion_count,
        )

        return BatchReport(
            batch_id=batch.id,
            status=batch.status,
            stop_reason=batch.stop_reason,
            stop_reason_detail=batch.stop_reason_detail,
            eligible_source_count=batch.eligible_source_count,
            policy_filtered_count=batch.policy_filtered_count,
            selectable_count=batch.selectable_count,
            source_instances_selected=batch.source_instances_selected,
            source_bytes_selected=batch.source_bytes_selected,
            attempted_source_count=attempted_source_count,
            unattempted_selected_count=unattempted_selected_count,
            terminal_source_count=terminal_source_count,
            successful_ingestion_count=successful_ingestion_count,
            risk_tier_estimated_distribution=self._risk_tier_distribution(
                run_id, SourceInstance.risk_tier_estimated
            ),
            risk_tier_actual_distribution=self._risk_tier_distribution(run_id, SourceInstance.risk_tier_actual),
            extracted_bytes_consumed=batch.extracted_bytes_consumed,
            embeddings_reserved=batch.embeddings_reserved,
            monotonic_runtime_seconds_consumed=batch.monotonic_runtime_seconds_consumed,
            # Milestone 7 Design Review, decision 1 (DEFER): neither
            # field is computed from evidence_snapshot or anywhere
            # else - no code in Milestones 1-6 ever captures actual
            # bytes read or a drift classification, and evidence_
            # snapshot is independently frozen as immutable-after-
            # creation (SourceInstance's own docstring), making it
            # structurally unable to hold either fact today. A future,
            # separately-authorized milestone must decide where this
            # data would actually live before it can be reported.
            actual_source_bytes_read=Instrumented.unavailable(),
            drift_outcomes=Instrumented.unavailable(),
        )

    # -- internals -----------------------------------------------------

    def _attempted_source_count(self, classification_run_id: int) -> int:
        """Milestone 7 Design Review, decision 2 (APPROVED): a
        selected root-level SourceInstance (`member_path IS NULL` -
        archive MEMBER rows share their parent's classification_run_id
        but were never part of `source_instances_selected`, and must
        never contaminate this count) is "attempted" if it has either
        a direct `source_instance_id`-scoped IngestionAttempt (identity
        resolution / archive processing), or its resolved
        ContentIdentityGroup has at least one `content_identity_
        group_id`-scoped attempt (normalization / chunking /
        embedding)."""
        direct_attempt = exists().where(IngestionAttempt.source_instance_id == SourceInstance.id)
        group_attempt = exists().where(
            IngestionAttempt.content_identity_group_id == SourceInstance.content_identity_group_id
        )

        return self.db.execute(
            select(func.count(SourceInstance.id)).where(
                SourceInstance.classification_run_id == classification_run_id,
                SourceInstance.member_path.is_(None),
                direct_attempt
                | and_(SourceInstance.content_identity_group_id.is_not(None), group_attempt),
            )
        ).scalar_one()

    def _loose_file_terminal_and_successful_counts(self, classification_run_id: int) -> tuple[int, int]:
        """Non-archive, root-level rows: terminal/successful is
        entirely defined by the resolved ContentIdentityGroup's own
        `pipeline_state` (section 13's frozen list) - unchanged,
        never re-derived from IngestionAttempt.retryable, since a
        group's pipeline_state already durably reflects the outcome
        (e.g. FAILED) regardless of whether the underlying attempt
        happened to be marked retryable."""
        base_where = (
            SourceInstance.classification_run_id == classification_run_id,
            SourceInstance.member_path.is_(None),
            SourceInstance.source_category != SourceCategory.ARCHIVE,
            SourceInstance.content_identity_group_id.is_not(None),
        )

        terminal = self.db.execute(
            select(func.count(SourceInstance.id))
            .join(ContentIdentityGroup, ContentIdentityGroup.id == SourceInstance.content_identity_group_id)
            .where(*base_where, ContentIdentityGroup.pipeline_state.in_(_LOOSE_TERMINAL_PIPELINE_STATES))
        ).scalar_one()

        successful = self.db.execute(
            select(func.count(SourceInstance.id))
            .join(ContentIdentityGroup, ContentIdentityGroup.id == SourceInstance.content_identity_group_id)
            .where(*base_where, ContentIdentityGroup.pipeline_state == ContentPipelineState.INGESTED)
        ).scalar_one()

        return terminal, successful

    def _archive_container_terminal_and_successful_counts(self, classification_run_id: int) -> tuple[int, int]:
        """Milestone 7 Design Review, decision 3 (APPROVED): an archive
        container never receives a ContentIdentityGroup (frozen,
        unchanged - only its extracted MEMBERS do), so terminal/
        successful status is read from the container's own
        `IngestionAttempt` history directly, reusing the EXACT same
        "at least one SUCCEEDED attempt" signal
        `claim_source_instance_for_archive_processing` already uses to
        recognize a fully-processed archive - not a new source of
        truth. A durable (non-retryable) failure is terminal but not
        successful; a retryable-only failure is neither (still
        eligible for a future retry claim)."""
        succeeded = exists().where(
            IngestionAttempt.source_instance_id == SourceInstance.id,
            IngestionAttempt.outcome == IngestionAttemptOutcome.SUCCEEDED,
        )
        durable_failure = exists().where(
            IngestionAttempt.source_instance_id == SourceInstance.id,
            IngestionAttempt.outcome == IngestionAttemptOutcome.FAILED,
            IngestionAttempt.retryable.is_(False),
        )
        base_where = (
            SourceInstance.classification_run_id == classification_run_id,
            SourceInstance.member_path.is_(None),
            SourceInstance.source_category == SourceCategory.ARCHIVE,
        )

        successful = self.db.execute(
            select(func.count(SourceInstance.id)).where(*base_where, succeeded)
        ).scalar_one()

        terminal_unsuccessful = self.db.execute(
            select(func.count(SourceInstance.id)).where(*base_where, ~succeeded, durable_failure)
        ).scalar_one()

        return successful + terminal_unsuccessful, successful

    def _risk_tier_distribution(
        self, classification_run_id: int, column
    ) -> dict[RiskTierEstimated | None, int]:
        """Explicit NULL bucket always included, never hidden - e.g.
        `risk_tier_actual` reads 100% NULL today (Milestone 5's
        honest, deliberate non-population), and that must be visible
        in the report, not silently dropped by an inner join or a
        WHERE ... IS NOT NULL filter."""
        rows = self.db.execute(
            select(column, func.count(SourceInstance.id))
            .where(
                SourceInstance.classification_run_id == classification_run_id,
                SourceInstance.member_path.is_(None),
            )
            .group_by(column)
        ).all()
        return {tier: count for tier, count in rows}

    def _assert_invariants(
        self,
        *,
        batch_id: int,
        source_instances_selected: int,
        attempted_source_count: int,
        terminal_source_count: int,
        successful_ingestion_count: int,
    ) -> None:
        """Milestone 7 Design Review's explicit requirement: fail
        loudly, never clamp/repair/hide. `unattempted_selected_count =
        source_instances_selected - attempted_source_count` is
        guaranteed non-negative exactly when the first check below
        holds, so no separate check is needed for it."""
        if attempted_source_count > source_instances_selected:
            raise BatchReportInvariantViolation(
                f"batch {batch_id}: attempted_source_count ({attempted_source_count}) "
                f"> source_instances_selected ({source_instances_selected})"
            )
        if terminal_source_count > attempted_source_count:
            raise BatchReportInvariantViolation(
                f"batch {batch_id}: terminal_source_count ({terminal_source_count}) "
                f"> attempted_source_count ({attempted_source_count})"
            )
        if successful_ingestion_count > terminal_source_count:
            raise BatchReportInvariantViolation(
                f"batch {batch_id}: successful_ingestion_count ({successful_ingestion_count}) "
                f"> terminal_source_count ({terminal_source_count})"
            )
