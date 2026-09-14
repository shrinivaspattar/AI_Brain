"""Real-database tests for Implementation Milestone 7 (BatchReportService)
of the Scaled Real-T7 Ingestion design. See "Scaled Real-T7 Ingestion -
Milestone 7 Design: BatchReportService" design proposal and its Design
Review (approved decisions 1-5) for the frozen specification this
milestone implements.

No T7 access of any kind: every row here is entirely synthetic. This
service is pure read/aggregation - no claim, no reservation, no
extraction, no embedding is exercised anywhere in this module.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.batch_report_service import (
    BatchReportInvariantViolation,
    BatchReportService,
    DriftOutcomeCounts,
    Instrumented,
)
from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.worker_claim_service import WorkerClaimService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
    ContentPipelineState,
)
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.ingestion_attempt import IngestionAttemptOutcome, IngestionAttemptStage, IngestionFailureCode
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch
from app.models.source_instance import RiskTierEstimated, SourceCategory, SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    engine = _engine()
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_transaction.rollback()
    connection.close()
    engine.dispose()


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _discovery_run(db: Session) -> DiscoveryRun:
    run = DiscoveryRun(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _classification_run(db: Session) -> ClassificationRun:
    discovery = _discovery_run(db)
    run = ClassificationRun(
        classifier_version="test-m7-batch-report-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _minimal_batch_kwargs(
    classification_run_id: int,
    *,
    eligible_source_count: int = 100,
    policy_filtered_count: int = 50,
    selectable_count: int = 20,
    source_instances_selected: int,
    source_bytes_selected: int = 50_000_000,
) -> dict:
    return dict(
        classification_run_id=classification_run_id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=5000,
        max_runtime_seconds=7200,
        eligible_source_count=eligible_source_count,
        policy_filtered_count=policy_filtered_count,
        selectable_count=selectable_count,
        source_instances_selected=source_instances_selected,
        source_bytes_selected=source_bytes_selected,
        extracted_bytes_consumed=0,
        embeddings_reserved=0,
        selection_fingerprint=_unique_hash(),
        selection_policy_version="batch-class-1-text-document-v1",
        ordering_version="lexicographic-path-v1",
    )


def _batch(
    db: Session,
    classification_run: ClassificationRun,
    *,
    status: BatchStatus = BatchStatus.RUNNING,
    stop_reason: BatchStopReason | None = None,
    source_instances_selected: int,
    **kwargs,
) -> IngestionBatch:
    if status is not BatchStatus.RUNNING and status is not BatchStatus.PLANNED and stop_reason is None:
        stop_reason = BatchStopReason.MANUAL_PAUSE if status is BatchStatus.PAUSED else BatchStopReason.SOURCE_WORK_EXHAUSTED
    batch = IngestionBatch(
        status=status,
        stop_reason=stop_reason,
        **_minimal_batch_kwargs(classification_run.id, source_instances_selected=source_instances_selected, **kwargs),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _loose_instance(
    db: Session,
    run: ClassificationRun,
    *,
    content_identity_group_id: int | None = None,
    risk_tier_estimated: RiskTierEstimated | None = None,
    member_path: str | None = None,
) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
        member_path=member_path,
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
        risk_tier_estimated=risk_tier_estimated,
        content_identity_group_id=content_identity_group_id,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _archive_instance(db: Session, run: ClassificationRun) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.zip",
        evidence_snapshot={},
        source_category=SourceCategory.ARCHIVE,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _archive_member_instance(db: Session, run: ClassificationRun, *, content_identity_group_id: int | None = None) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.zip",
        member_path=f"inner/{uuid.uuid4().hex}.txt",
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
        content_identity_group_id=content_identity_group_id,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _group(db: Session, pipeline_state: ContentPipelineState) -> ContentIdentityGroup:
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=pipeline_state,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


def _record_source_attempt(
    db: Session, source_instance_id: int, *, outcome: IngestionAttemptOutcome, retryable: bool | None = None
) -> None:
    attempts = IngestionAttemptService(db)
    kwargs = {}
    if outcome is IngestionAttemptOutcome.FAILED:
        kwargs = dict(
            failure_code=IngestionFailureCode.T7_UNAVAILABLE,
            failure_detail="synthetic failure",
            retryable=retryable if retryable is not None else True,
        )
    attempts.record_identity_resolution_attempt(source_instance_id=source_instance_id, worker_id="worker-a", outcome=outcome, **kwargs)


def _record_group_attempt(
    db: Session,
    content_identity_group_id: int,
    *,
    stage: IngestionAttemptStage = IngestionAttemptStage.NORMALIZING,
    outcome: IngestionAttemptOutcome = IngestionAttemptOutcome.SUCCEEDED,
) -> None:
    kwargs = {}
    if outcome is IngestionAttemptOutcome.FAILED:
        kwargs = dict(
            failure_code=IngestionFailureCode.CORRUPT_INPUT,
            failure_detail="synthetic failure",
            retryable=True,
        )
    IngestionAttemptService(db).record_pipeline_attempt(
        content_identity_group_id=content_identity_group_id,
        attempted_stage=stage,
        worker_id="worker-a",
        outcome=outcome,
        **kwargs,
    )


# ============================================================
# 1: Fresh batch, zero attempts
# ============================================================


def test_fresh_batch_zero_attempts(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=5)
    _loose_instance(db, run)  # not counted - different run entirely would be, but this one IS under `run`

    report = BatchReportService(db).generate_report(batch.id)

    assert report.attempted_source_count == 0
    assert report.unattempted_selected_count == 5
    assert report.terminal_source_count == 0
    assert report.successful_ingestion_count == 0


# ============================================================
# 2: Mixed loose-file outcomes
# ============================================================


def test_mixed_loose_file_outcomes(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=4)

    ingested_group = _group(db, ContentPipelineState.INGESTED)
    _record_group_attempt(db, ingested_group.id, stage=IngestionAttemptStage.EMBEDDING)
    ingested_instance = _loose_instance(db, run, content_identity_group_id=ingested_group.id)

    failed_group = _group(db, ContentPipelineState.FAILED)
    failed_instance = _loose_instance(db, run, content_identity_group_id=failed_group.id)
    _record_group_attempt(db, failed_group.id, outcome=IngestionAttemptOutcome.FAILED)

    unresolved_instance = _loose_instance(db, run)  # never resolved, no attempt at all
    still_failing_instance = _loose_instance(db, run)
    _record_source_attempt(db, still_failing_instance.id, outcome=IngestionAttemptOutcome.FAILED, retryable=True)

    report = BatchReportService(db).generate_report(batch.id)

    assert report.attempted_source_count == 3  # ingested, failed-group, still-failing (not the never-attempted one)
    assert report.unattempted_selected_count == 1
    assert report.terminal_source_count == 2  # ingested + failed-group (still-failing is retryable, not terminal)
    assert report.successful_ingestion_count == 1


# ============================================================
# 3: Archive container + members excluded from root counts
# ============================================================


def test_archive_container_and_members_members_excluded_from_root_counts(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)  # ONLY the container was ever "selected"

    archive = _archive_instance(db, run)
    _record_source_attempt(db, archive.id, outcome=IngestionAttemptOutcome.SUCCEEDED)

    # Members: created during extraction, share classification_run_id,
    # but were never part of source_instances_selected.
    member_group_a = _group(db, ContentPipelineState.INGESTED)
    _archive_member_instance(db, run, content_identity_group_id=member_group_a.id)
    member_group_b = _group(db, ContentPipelineState.CHUNKED)  # not yet terminal
    _archive_member_instance(db, run, content_identity_group_id=member_group_b.id)

    report = BatchReportService(db).generate_report(batch.id)

    # Exactly 1: the container. Members must never inflate this count.
    assert report.attempted_source_count == 1
    assert report.unattempted_selected_count == 0
    assert report.terminal_source_count == 1
    assert report.successful_ingestion_count == 1


# ============================================================
# 4-5: Archive container terminal semantics
# ============================================================


def test_archive_container_durable_failure_is_terminal_and_unsuccessful(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    archive = _archive_instance(db, run)
    IngestionAttemptService(db).record_identity_resolution_attempt(
        source_instance_id=archive.id,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.FAILED,
        failure_code=IngestionFailureCode.OVERSIZED_OR_EXPANSION_LIMIT,
        failure_detail="max_depth exceeded",
        retryable=False,
    )

    report = BatchReportService(db).generate_report(batch.id)

    assert report.attempted_source_count == 1
    assert report.terminal_source_count == 1
    assert report.successful_ingestion_count == 0


def test_archive_container_retryable_only_failure_is_not_terminal(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    archive = _archive_instance(db, run)
    _record_source_attempt(db, archive.id, outcome=IngestionAttemptOutcome.FAILED, retryable=True)

    report = BatchReportService(db).generate_report(batch.id)

    assert report.attempted_source_count == 1
    assert report.terminal_source_count == 0
    assert report.successful_ingestion_count == 0


# ============================================================
# 6: risk_tier_actual explicit NULL bucket
# ============================================================


def test_risk_tier_actual_shows_explicit_null_bucket(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=2)
    _loose_instance(db, run, risk_tier_estimated=RiskTierEstimated.LOW)
    _loose_instance(db, run, risk_tier_estimated=RiskTierEstimated.HIGH)

    report = BatchReportService(db).generate_report(batch.id)

    assert report.risk_tier_estimated_distribution == {RiskTierEstimated.LOW: 1, RiskTierEstimated.HIGH: 1}
    # risk_tier_actual is never populated by any code path through M6 -
    # this must show up as an explicit, visible NULL bucket, not be
    # silently absent from the distribution.
    assert report.risk_tier_actual_distribution == {None: 2}


# ============================================================
# 7: Deferred instrumentation fields
# ============================================================


def test_deferred_instrumentation_fields_are_explicitly_unavailable(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=0)

    report = BatchReportService(db).generate_report(batch.id)

    assert report.actual_source_bytes_read == Instrumented.unavailable()
    assert report.actual_source_bytes_read.available is False
    assert report.actual_source_bytes_read.value is None
    assert report.drift_outcomes == Instrumented.unavailable()
    assert report.drift_outcomes.available is False
    assert report.drift_outcomes.value is None


def test_instrumented_of_and_unavailable_are_structurally_distinct() -> None:
    real_zero = Instrumented.of(0)
    unavailable = Instrumented[int].unavailable()

    assert real_zero.available is True
    assert real_zero.value == 0
    assert unavailable.available is False
    assert unavailable.value is None
    assert real_zero != unavailable  # a real 0 is never mistakable for "not instrumented"

    drift = Instrumented.of(
        DriftOutcomeCounts(
            observed_at_selection=1, source_present_at_execution=1, source_changed_after_selection=0, source_missing_at_execution=0
        )
    )
    assert drift.available is True
    assert drift.value.observed_at_selection == 1


# ============================================================
# 8-9: Invariant reconciliation
# ============================================================


def test_denominator_invariant_holds_with_mixed_root_and_member_rows(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=3)

    ingested_group = _group(db, ContentPipelineState.INGESTED)
    _record_group_attempt(db, ingested_group.id, stage=IngestionAttemptStage.EMBEDDING)
    _loose_instance(db, run, content_identity_group_id=ingested_group.id)
    _loose_instance(db, run)  # unattempted
    archive = _archive_instance(db, run)
    _record_source_attempt(db, archive.id, outcome=IngestionAttemptOutcome.SUCCEEDED)
    # Member rows under the SAME run - must not contaminate the invariant.
    member_group = _group(db, ContentPipelineState.NORMALIZED)
    _archive_member_instance(db, run, content_identity_group_id=member_group.id)

    report = BatchReportService(db).generate_report(batch.id)

    assert report.source_instances_selected == report.attempted_source_count + report.unattempted_selected_count
    assert report.terminal_source_count <= report.attempted_source_count <= report.source_instances_selected
    assert report.successful_ingestion_count <= report.terminal_source_count


def test_assert_invariants_raises_loudly_on_attempted_exceeding_selected(db: Session) -> None:
    service = BatchReportService(db)
    with pytest.raises(BatchReportInvariantViolation, match="attempted_source_count"):
        service._assert_invariants(
            batch_id=1, source_instances_selected=2, attempted_source_count=3, terminal_source_count=0, successful_ingestion_count=0
        )


def test_assert_invariants_raises_loudly_on_terminal_exceeding_attempted(db: Session) -> None:
    service = BatchReportService(db)
    with pytest.raises(BatchReportInvariantViolation, match="terminal_source_count"):
        service._assert_invariants(
            batch_id=1, source_instances_selected=5, attempted_source_count=2, terminal_source_count=3, successful_ingestion_count=0
        )


def test_assert_invariants_raises_loudly_on_successful_exceeding_terminal(db: Session) -> None:
    service = BatchReportService(db)
    with pytest.raises(BatchReportInvariantViolation, match="successful_ingestion_count"):
        service._assert_invariants(
            batch_id=1, source_instances_selected=5, attempted_source_count=3, terminal_source_count=2, successful_ingestion_count=3
        )


def test_assert_invariants_does_not_raise_on_consistent_numbers(db: Session) -> None:
    service = BatchReportService(db)
    service._assert_invariants(
        batch_id=1, source_instances_selected=5, attempted_source_count=3, terminal_source_count=2, successful_ingestion_count=1
    )  # no exception


def test_generate_report_raises_loudly_end_to_end_never_clamps(db: Session) -> None:
    """A genuinely inconsistent batch (source_instances_selected set
    below the real, correctly-attempted count) must surface via
    BatchReportInvariantViolation from generate_report() itself, never
    silently clamped or hidden. Both groups have a REAL recorded
    attempt (unlike a bare pipeline_state write), so
    attempted_source_count genuinely is 2 here - the violation is
    deliberately and precisely attributed to attempted_source_count
    exceeding a corrupted source_instances_selected, not an accident
    of an unrealistic fixture."""
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=2)
    group_a = _group(db, ContentPipelineState.INGESTED)
    _record_group_attempt(db, group_a.id, stage=IngestionAttemptStage.EMBEDDING)
    _loose_instance(db, run, content_identity_group_id=group_a.id)
    group_b = _group(db, ContentPipelineState.INGESTED)
    _record_group_attempt(db, group_b.id, stage=IngestionAttemptStage.EMBEDDING)
    _loose_instance(db, run, content_identity_group_id=group_b.id)
    # Corrupt the immutable selection count DOWN, below the real
    # attempted count of 2 - a genuine data-integrity bug this report
    # must never paper over.
    db.query(IngestionBatch).filter_by(id=batch.id).update({"source_instances_selected": 1})
    db.commit()

    with pytest.raises(BatchReportInvariantViolation, match="attempted_source_count"):
        BatchReportService(db).generate_report(batch.id)


# ============================================================
# 10: Any batch status
# ============================================================


@pytest.mark.parametrize(
    "status",
    [BatchStatus.PLANNED, BatchStatus.RUNNING, BatchStatus.PAUSED, BatchStatus.ABORTED, BatchStatus.COMPLETED],
)
def test_report_generatable_for_any_batch_status(db: Session, status: BatchStatus) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, status=status, source_instances_selected=1)
    _loose_instance(db, run)

    report = BatchReportService(db).generate_report(batch.id)

    assert report.status == status
    assert report.batch_id == batch.id


def test_generate_report_raises_for_unknown_batch(db: Session) -> None:
    with pytest.raises(ValueError, match="not found"):
        BatchReportService(db).generate_report(999_999_999)


# ============================================================
# 11: Report generation concurrent with an active worker (light, real-Postgres)
# ============================================================


def test_report_generation_succeeds_while_a_worker_holds_a_live_claim_and_reservation() -> None:
    """Not a race to prove (BatchReportService writes nothing and takes
    no lock) - just proof that report generation runs cleanly, with no
    error/block/deadlock, while a separate real-Postgres session
    concurrently holds an active claim and embedding reservation on
    rows the report reads."""
    engine = _engine()
    setup_db = Session(engine)
    discovery = DiscoveryRun(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    setup_db.add(discovery)
    setup_db.commit()
    setup_db.refresh(discovery)

    run = ClassificationRun(classifier_version="v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC))
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)

    batch = IngestionBatch(
        status=BatchStatus.RUNNING,
        **_minimal_batch_kwargs(run.id, source_instances_selected=1),
    )
    setup_db.add(batch)
    setup_db.commit()
    setup_db.refresh(batch)

    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=ContentPipelineState.CHUNKED,
    )
    setup_db.add(group)
    setup_db.commit()
    setup_db.refresh(group)

    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
        content_identity_group_id=group.id,
    )
    setup_db.add(instance)
    setup_db.commit()

    batch_id, run_id, discovery_id, group_id, instance_id = batch.id, run.id, discovery.id, group.id, instance.id
    setup_db.close()

    barrier = threading.Barrier(2)
    worker_error: list[Exception] = []
    report_error: list[Exception] = []
    report_holder: list[object] = []

    def worker():
        thread_db = Session(engine)
        try:
            barrier.wait()
            claimed = WorkerClaimService(thread_db).claim_content_identity_group(
                worker_id="worker-a",
                eligible_pipeline_states=[ContentPipelineState.CHUNKED],
                lease_duration=timedelta(minutes=10),
            )
            if claimed is not None:
                WorkerClaimService(thread_db).reserve_embeddings(
                    group_id=claimed.id, my_generation=claimed.claim_generation, batch_id=batch_id, n=1
                )
        except Exception as exc:  # noqa: BLE001
            worker_error.append(exc)
        finally:
            thread_db.close()

    def reporter():
        thread_db = Session(engine)
        try:
            barrier.wait()
            report_holder.append(BatchReportService(thread_db).generate_report(batch_id))
        except Exception as exc:  # noqa: BLE001
            report_error.append(exc)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=worker), threading.Thread(target=reporter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not worker_error, f"unexpected worker exception: {worker_error}"
        assert not report_error, f"unexpected report exception: {report_error}"
        assert len(report_holder) == 1
        assert report_holder[0].batch_id == batch_id
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(text("DELETE FROM ingestion_attempts WHERE source_instance_id = :id"), {"id": instance_id})
        cleanup_db.execute(text("DELETE FROM source_instances WHERE id = :id"), {"id": instance_id})
        cleanup_db.execute(text("DELETE FROM content_identity_groups WHERE id = :id"), {"id": group_id})
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()
