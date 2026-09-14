"""Real-database tests for Implementation Milestone 8
(BatchCompletionReconciliationService) of the Scaled Real-T7 Ingestion
design. See "Scaled Real-T7 Ingestion - Milestone 8 Design: Batch
Completion Reconciliation" design proposal and its Design Review
(approved decisions 1-7) for the frozen specification this milestone
implements.

No T7 access of any kind: every row here is entirely synthetic. This
service composes BatchReportService (read) and BatchControlService
(the sole completion authority) - it never mutates IngestionBatch
directly.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.batch_completion_reconciliation_service import (
    BatchCompletionReconciliationService,
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
from app.models.source_instance import SourceCategory, SourceInstance


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
        classifier_version="test-m8-completion-reconciliation-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _minimal_batch_kwargs(classification_run_id: int, *, source_instances_selected: int, max_embeddings: int = 5000) -> dict:
    return dict(
        classification_run_id=classification_run_id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=max_embeddings,
        max_runtime_seconds=7200,
        eligible_source_count=100,
        policy_filtered_count=50,
        selectable_count=20,
        source_instances_selected=source_instances_selected,
        source_bytes_selected=50_000_000,
        extracted_bytes_consumed=0,
        embeddings_reserved=0,
        selection_fingerprint=_unique_hash(),
        selection_policy_version="batch-class-1-text-document-v1",
        ordering_version="lexicographic-path-v1",
    )


def _batch(db: Session, run: ClassificationRun, *, source_instances_selected: int, max_embeddings: int = 5000) -> IngestionBatch:
    batch = IngestionBatch(
        status=BatchStatus.RUNNING,
        **_minimal_batch_kwargs(run.id, source_instances_selected=source_instances_selected, max_embeddings=max_embeddings),
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
    claimed_by: str | None = None,
    claimed_at: datetime | None = None,
) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
        content_identity_group_id=content_identity_group_id,
        claimed_by=claimed_by,
        claimed_at=claimed_at,
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


def _record_group_succeeded_attempt(db: Session, content_identity_group_id: int) -> None:

    IngestionAttemptService(db).record_pipeline_attempt(
        content_identity_group_id=content_identity_group_id,
        attempted_stage=IngestionAttemptStage.EMBEDDING,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.SUCCEEDED,
    )


def _record_archive_succeeded_attempt(db: Session, source_instance_id: int) -> None:
    IngestionAttemptService(db).record_identity_resolution_attempt(
        source_instance_id=source_instance_id, worker_id="worker-a", outcome=IngestionAttemptOutcome.SUCCEEDED
    )


# ============================================================
# 1: All selected rows terminal -> completes
# ============================================================


def test_all_rows_terminal_transitions_to_completed_source_work_exhausted(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=2)

    ingested_group = _group(db, ContentPipelineState.INGESTED)
    _record_group_succeeded_attempt(db, ingested_group.id)
    _loose_instance(db, run, content_identity_group_id=ingested_group.id)

    archive = _archive_instance(db, run)
    _record_archive_succeeded_attempt(db, archive.id)

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)

    assert result is not None
    assert result.applied is True
    assert result.batch.status == BatchStatus.COMPLETED
    assert result.batch.stop_reason == BatchStopReason.SOURCE_WORK_EXHAUSTED


def test_needs_review_counts_as_terminal_for_completion(db: Session) -> None:
    """Decision 7: a batch can legitimately complete while an item sits
    parked at NEEDS_REVIEW - COMPLETED/SOURCE_WORK_EXHAUSTED means the
    source-work lifecycle ended, not that everything was ingested."""
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)

    review_group = _group(db, ContentPipelineState.NEEDS_REVIEW)
    _record_group_succeeded_attempt(db, review_group.id)
    _loose_instance(db, run, content_identity_group_id=review_group.id)

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)

    assert result is not None
    assert result.applied is True
    assert result.batch.status == BatchStatus.COMPLETED

    from app.classification.batch_report_service import BatchReportService

    report = BatchReportService(db).generate_report(batch.id)
    assert report.terminal_source_count == 1
    assert report.successful_ingestion_count == 0  # distinct from terminal - NOT "everything succeeded"


# ============================================================
# 2-4: No transition when work genuinely remains
# ============================================================


def test_unattempted_row_remaining_no_transition(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    _loose_instance(db, run)  # never attempted

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)

    assert result is None
    db.refresh(batch)
    assert batch.status == BatchStatus.RUNNING


def test_attempted_but_non_terminal_row_no_transition(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    chunked_group = _group(db, ContentPipelineState.CHUNKED)  # attempted (chunking succeeded) but not terminal

    IngestionAttemptService(db).record_pipeline_attempt(
        content_identity_group_id=chunked_group.id,
        attempted_stage=IngestionAttemptStage.CHUNKING,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.SUCCEEDED,
    )
    _loose_instance(db, run, content_identity_group_id=chunked_group.id)

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)

    assert result is None
    db.refresh(batch)
    assert batch.status == BatchStatus.RUNNING


def test_row_claimed_but_zero_attempts_yet_no_transition(db: Session) -> None:
    """Simulates a worker mid-flight: claimed_by set, no IngestionAttempt
    recorded yet (matches the real window in IdentityResolutionService,
    which records its attempt only at the END of _resolve_claimed_instance).
    Still correctly counted as "unattempted" by BatchReportService's own
    definition, so reconciliation must not complete."""
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    _loose_instance(db, run, claimed_by="worker-mid-flight", claimed_at=datetime.now(UTC))

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)

    assert result is None
    db.refresh(batch)
    assert batch.status == BatchStatus.RUNNING


# ============================================================
# 5: Not RUNNING -> safe no-op
# ============================================================


@pytest.mark.parametrize("status", [BatchStatus.PLANNED, BatchStatus.PAUSED, BatchStatus.ABORTED, BatchStatus.COMPLETED])
def test_non_running_batch_is_a_safe_no_op(db: Session, status: BatchStatus) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    stop_reason = BatchStopReason.MANUAL_PAUSE if status is BatchStatus.PAUSED else BatchStopReason.SOURCE_WORK_EXHAUSTED
    db.query(IngestionBatch).filter_by(id=batch.id).update(
        {"status": status, "stop_reason": None if status is BatchStatus.PLANNED else stop_reason}
    )
    db.commit()

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)

    assert result is None
    db.refresh(batch)
    assert batch.status == status  # unchanged


def test_unknown_batch_raises(db: Session) -> None:
    with pytest.raises(ValueError, match="not found"):
        BatchCompletionReconciliationService(db).check_and_complete(999_999_999)


# ============================================================
# 6: Idempotency
# ============================================================


def test_idempotent_second_call_on_already_completed_batch_is_a_safe_no_op(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    ingested_group = _group(db, ContentPipelineState.INGESTED)
    _record_group_succeeded_attempt(db, ingested_group.id)
    _loose_instance(db, run, content_identity_group_id=ingested_group.id)

    service = BatchCompletionReconciliationService(db)
    first = service.check_and_complete(batch.id)
    assert first is not None
    assert first.applied is True

    second = service.check_and_complete(batch.id)  # batch is no longer RUNNING now
    assert second is None


# ============================================================
# 7: Envelope-exhausted-but-real-work-remains must NOT complete
# ============================================================


def test_embeddings_envelope_exhausted_with_remaining_work_does_not_complete(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=2, max_embeddings=1)
    db.query(IngestionBatch).filter_by(id=batch.id).update({"embeddings_reserved": 1})  # envelope full
    db.commit()

    ingested_group = _group(db, ContentPipelineState.INGESTED)
    _record_group_succeeded_attempt(db, ingested_group.id)
    _loose_instance(db, run, content_identity_group_id=ingested_group.id)
    _loose_instance(db, run)  # genuinely still unattempted - envelope exhaustion is NOT this service's job

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)

    assert result is None
    db.refresh(batch)
    assert batch.status == BatchStatus.RUNNING  # never fabricated a completion this milestone doesn't own


# ============================================================
# 8: Real-Postgres concurrency - two workers race check_and_complete()
# ============================================================


def test_concurrency_stress_two_workers_race_check_and_complete_repeated() -> None:
    """Repeated (10x) real-Postgres race: two workers concurrently call
    check_and_complete() on the SAME already-exhausted batch. Exactly
    one COMPLETED transition applies - this proves the new wiring
    delegates correctly to BatchControlService.complete()'s own
    already-proven atomic CAS (Milestone 3), not a re-proof of that
    primitive itself."""
    engine = _engine()
    iterations = 10
    for _ in range(iterations):
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

        batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id, source_instances_selected=1))
        setup_db.add(batch)
        setup_db.commit()
        setup_db.refresh(batch)

        group = ContentIdentityGroup(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash=_unique_hash(),
            pipeline_state=ContentPipelineState.INGESTED,
        )
        setup_db.add(group)
        setup_db.commit()
        setup_db.refresh(group)
        IngestionAttemptService(setup_db).record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.EMBEDDING,
            worker_id="worker-setup",
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )

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
        results: list = [None, None]
        errors: list[Exception] = []

        def worker(index: int):
            thread_db = Session(engine)
            try:
                barrier.wait()
                results[index] = BatchCompletionReconciliationService(thread_db).check_and_complete(batch_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        threads = [threading.Thread(target=worker, args=(0,)), threading.Thread(target=worker, args=(1,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"unexpected leaked exceptions: {errors}"
            applied = [r for r in results if r is not None and r.applied]
            assert len(applied) == 1, f"expected exactly one applied completion, got {len(applied)}"

            verify_db = Session(engine)
            final_batch = verify_db.get(IngestionBatch, batch_id)
            assert final_batch.status == BatchStatus.COMPLETED
            assert final_batch.stop_reason == BatchStopReason.SOURCE_WORK_EXHAUSTED
            verify_db.close()
        finally:
            cleanup_db = Session(engine)
            cleanup_db.execute(text("DELETE FROM ingestion_attempts WHERE content_identity_group_id = :id"), {"id": group_id})
            cleanup_db.execute(text("DELETE FROM source_instances WHERE id = :id"), {"id": instance_id})
            cleanup_db.execute(text("DELETE FROM content_identity_groups WHERE id = :id"), {"id": group_id})
            cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
            cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
            cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
            cleanup_db.commit()
            cleanup_db.close()

    engine.dispose()


# ============================================================
# 9: Existing repository semantics for work after completion
# (a documented lifecycle property, not a new locking mechanism)
# ============================================================


def test_no_new_source_level_claim_possible_after_completion_even_with_a_stray_row(db: Session) -> None:
    """Documents existing (Milestone 4) behavior, not new to this
    milestone: once a batch leaves RUNNING, claim_source_instance_for_
    identity_resolution/claim_source_instance_for_archive_processing
    already refuse to grant a NEW claim for that classification_run_id
    (the _running_batch_exists_for_classification_run gate). Proven
    here even against an artificially-inserted extra unattempted row -
    something that should never occur under normal operation (selection
    is immutable), included specifically to isolate "batch not RUNNING"
    as the refusal reason from "no work to claim."""
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    ingested_group = _group(db, ContentPipelineState.INGESTED)
    _record_group_succeeded_attempt(db, ingested_group.id)
    _loose_instance(db, run, content_identity_group_id=ingested_group.id)

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)
    assert result is not None and result.applied is True

    # A stray, artificially-inserted unattempted row under the SAME run,
    # inserted AFTER completion - not a normal scenario (selection is
    # immutable), constructed only to prove the claim-admission gate
    # itself, independent of whether real work happens to exist.
    stray = _loose_instance(db, run)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-late", lease_duration=timedelta(minutes=10), classification_run_id=run.id
    )

    assert claimed is None  # refused - the batch is COMPLETED, not RUNNING
    db.refresh(stray)
    assert stray.claimed_by is None  # never claimed


def test_claim_content_identity_group_remains_globally_unaffected_by_batch_completion(db: Session) -> None:
    """Documents the OTHER half of the existing lifecycle property,
    explicitly per Milestone 8 Design Review's instruction not to
    silently "fix" this: claim_content_identity_group is frozen
    (Implementation Design Pass, section 19) as a GLOBAL claim with no
    batch-status gate at all. A ContentIdentityGroup's own claimability
    is never affected by whether any particular batch referencing it
    has completed - proven directly here by completing a batch and then
    successfully claiming an UNRELATED, still-eligible group via the
    ordinary global claim path, exactly as if no batch had completed at
    all."""
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    ingested_group = _group(db, ContentPipelineState.INGESTED)
    _record_group_succeeded_attempt(db, ingested_group.id)
    _loose_instance(db, run, content_identity_group_id=ingested_group.id)

    result = BatchCompletionReconciliationService(db).check_and_complete(batch.id)
    assert result is not None and result.applied is True

    # An unrelated, still-eligible group - not tied to the completed
    # batch's own denominators in any way.
    unrelated_group = _group(db, ContentPipelineState.CHUNKED)

    claimed = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-unrelated",
        eligible_pipeline_states=[ContentPipelineState.CHUNKED],
        lease_duration=timedelta(minutes=10),
    )

    assert claimed is not None
    assert claimed.id == unrelated_group.id  # global claim entirely unaffected by the OTHER batch's completion
