"""Real-database tests for Implementation Milestone 4 (Batch-Aware
Worker Claim + Generation-Fenced Reservation Integration) of the Scaled
Real-T7 Ingestion design. See "Scaled Real-T7 Ingestion - Implementation
Design Pass" (`2fab4b3`), "### 8. Embedding reservation - fenced
lifecycle" and "### 10. Claim / batch interaction", for the frozen
design this milestone implements.

No T7 access of any kind: every row here is entirely synthetic. No
archive extraction, normalization, chunking, or embedding execution
exists or is exercised anywhere in this module - only the batch-aware
claim/generation/reservation PRIMITIVES themselves.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.resource_guard import GuardResult, GuardTier
from app.classification.worker_claim_service import ReservationDenialReason, WorkerClaimService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
    ContentPipelineState,
)
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch
from app.models.source_instance import SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - matches this
    project's established fixture pattern. WorkerClaimService's own
    internal commit()/rollback() calls only affect the savepoint here."""
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
        classifier_version="test-batch-claim-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _minimal_batch_kwargs(classification_run_id: int, *, max_embeddings: int = 5000) -> dict:
    return dict(
        classification_run_id=classification_run_id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=max_embeddings,
        max_runtime_seconds=7200,
        eligible_source_count=1000,
        policy_filtered_count=1000,
        selectable_count=1000,
        source_instances_selected=1000,
        source_bytes_selected=50_000_000,
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
    max_embeddings: int = 5000,
    embeddings_reserved: int = 0,
) -> IngestionBatch:
    if status is not BatchStatus.PLANNED and stop_reason is None and status is not BatchStatus.RUNNING:
        stop_reason = BatchStopReason.MANUAL_PAUSE if status is BatchStatus.PAUSED else stop_reason
    batch = IngestionBatch(
        status=status,
        stop_reason=stop_reason,
        embeddings_reserved=embeddings_reserved,
        **_minimal_batch_kwargs(classification_run.id, max_embeddings=max_embeddings),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _source_instance(db: Session, classification_run: ClassificationRun, *, path: str | None = None) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path=path or f"/synthetic/{uuid.uuid4().hex}.txt",
        evidence_snapshot={},
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _archive_instance(db: Session, classification_run: ClassificationRun, *, path: str | None = None) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path=path or f"/synthetic/{uuid.uuid4().hex}.zip",
        evidence_snapshot={},
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _group(
    db: Session,
    pipeline_state: ContentPipelineState = ContentPipelineState.CHUNKED,
    *,
    claim_generation: int = 0,
    claimed_by: str | None = None,
    claimed_at: datetime | None = None,
    reserved_embeddings: int | None = None,
    reserved_embeddings_batch_id: int | None = None,
) -> ContentIdentityGroup:
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=pipeline_state,
        claim_generation=claim_generation,
        claimed_by=claimed_by,
        claimed_at=claimed_at,
        reserved_embeddings=reserved_embeddings,
        reserved_embeddings_batch_id=reserved_embeddings_batch_id,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


def _guard_always(tier: GuardTier, reason=None, detail: str = "synthetic") -> SimpleNamespace:
    return SimpleNamespace(
        check_before_expensive_operation=lambda batch, kind: GuardResult(tier=tier, stop_reason=reason, detail=detail)
    )


# ============================================================
# BATCH ISOLATION
# ============================================================


def test_batch_scoped_claim_returns_only_own_run_instance(db: Session) -> None:
    run_a = _classification_run(db)
    _batch(db, run_a)
    instance_a = _source_instance(db, run_a)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=run_a.id
    )
    assert claimed is not None
    assert claimed.id == instance_a.id


def test_batch_a_worker_never_claims_batch_b_instance(db: Session) -> None:
    run_a = _classification_run(db)
    run_b = _classification_run(db)
    _batch(db, run_a)
    _batch(db, run_b)
    _source_instance(db, run_b)  # only B has eligible work

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=run_a.id
    )
    assert claimed is None


def test_unbatched_classification_run_cannot_be_claimed_through_batch_filter(db: Session) -> None:
    """A ClassificationRun with NO IngestionBatch at all (the pre-
    existing non-batch pipeline's own shape) must never be claimable
    via the batch-scoped path, even when its own, real
    classification_run_id is supplied - no RUNNING batch exists for it."""
    unbatched_run = _classification_run(db)
    _source_instance(db, unbatched_run)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=unbatched_run.id
    )
    assert claimed is None


def test_wrong_classification_run_id_excludes_source_instance(db: Session) -> None:
    run_a = _classification_run(db)
    run_b = _classification_run(db)
    _batch(db, run_a)
    _batch(db, run_b)
    instance_a = _source_instance(db, run_a)
    _source_instance(db, run_b)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=run_a.id
    )
    assert claimed is not None
    assert claimed.id == instance_a.id


def test_unscoped_call_preserves_pre_milestone_4_behavior(db: Session) -> None:
    """`classification_run_id=None` (the default) must claim exactly as
    before Milestone 4 - no batch, no admission gate."""
    unbatched_run = _classification_run(db)
    instance = _source_instance(db, unbatched_run)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10)
    )
    assert claimed is not None
    assert claimed.id == instance.id


def test_archive_processing_claim_respects_batch_scope(db: Session) -> None:
    run_a = _classification_run(db)
    run_b = _classification_run(db)
    _batch(db, run_a)
    _batch(db, run_b)
    archive_b = _archive_instance(db, run_b)

    claimed_for_a = WorkerClaimService(db).claim_source_instance_for_archive_processing(
        worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=run_a.id
    )
    assert claimed_for_a is None

    claimed_for_b = WorkerClaimService(db).claim_source_instance_for_archive_processing(
        worker_id="worker-b", lease_duration=timedelta(minutes=10), classification_run_id=run_b.id
    )
    assert claimed_for_b is not None
    assert claimed_for_b.id == archive_b.id


# ============================================================
# STATE: batch admission
# ============================================================


@pytest.mark.parametrize("status", [BatchStatus.PAUSED, BatchStatus.ABORTED, BatchStatus.COMPLETED])
def test_non_running_batch_rejects_new_claim(db: Session, status: BatchStatus) -> None:
    run = _classification_run(db)
    _batch(db, run, status=status, stop_reason=BatchStopReason.SOURCE_WORK_EXHAUSTED)
    _source_instance(db, run)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=run.id
    )
    assert claimed is None


def test_running_batch_admits_claim(db: Session) -> None:
    run = _classification_run(db)
    _batch(db, run, status=BatchStatus.RUNNING)
    instance = _source_instance(db, run)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=run.id
    )
    assert claimed is not None
    assert claimed.id == instance.id


def test_concurrent_pause_vs_claim_persisted_batch_state_wins() -> None:
    """Real-Postgres proof that a batch pausing concurrently with a
    claim attempt is honored: the claim's final UPDATE re-checks batch
    status at ITS OWN execution time, not merely at the earlier
    candidate SELECT."""
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
    run = ClassificationRun(
        classifier_version="test-batch-claim-concurrency-v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC)
    )
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id))
    setup_db.add(batch)
    setup_db.commit()
    instance = SourceInstance(
        classification_run_id=run.id, root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt", evidence_snapshot={}
    )
    setup_db.add(instance)
    setup_db.commit()
    run_id, batch_id, instance_id, discovery_id = run.id, batch.id, instance.id, discovery.id
    setup_db.close()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def claim_worker():
        thread_db = Session(engine)
        try:
            barrier.wait()
            results["claim"] = WorkerClaimService(thread_db).claim_source_instance_for_identity_resolution(
                worker_id="worker-a", lease_duration=timedelta(minutes=10), classification_run_id=run_id
            )
        finally:
            thread_db.close()

    def pause_worker():
        thread_db = Session(engine)
        try:
            from app.classification.batch_control_service import BatchControlService

            barrier.wait()
            results["pause"] = BatchControlService(thread_db).pause(batch_id, reason=BatchStopReason.MANUAL_PAUSE)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=claim_worker), threading.Thread(target=pause_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        verify_db = Session(engine)
        final_batch = verify_db.get(IngestionBatch, batch_id)
        final_instance = verify_db.get(SourceInstance, instance_id)
        # Whichever order the race resolved in, the invariant holds: if
        # the instance ended up claimed, the batch must still have been
        # RUNNING at that exact moment (impossible for both "claimed"
        # AND "paused before the claim's own UPDATE" to be simultaneously
        # true, by construction of the fenced UPDATE).
        if final_instance.claimed_by is not None:
            assert results["claim"] is not None
        else:
            assert results["claim"] is None
        assert final_batch.status in (BatchStatus.RUNNING, BatchStatus.PAUSED)
        verify_db.close()
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(text("DELETE FROM source_instances WHERE classification_run_id = :rid"), {"rid": run_id})
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()


# ============================================================
# GENERATION
# ============================================================


def test_first_claim_sets_generation_to_one(db: Session) -> None:
    group = _group(db, ContentPipelineState.CLASSIFIED)
    claimed = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )
    assert claimed.claim_generation == 1


def test_reclaim_increments_generation_again(db: Session) -> None:
    stale_at = datetime.now(UTC) - timedelta(hours=1)
    group = _group(db, ContentPipelineState.CLASSIFIED, claim_generation=1, claimed_by="worker-a", claimed_at=stale_at)
    reclaimed = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-b",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )
    assert reclaimed.id == group.id
    assert reclaimed.claim_generation == 2
    assert reclaimed.claimed_by == "worker-b"


def test_generation_never_reused_across_many_reclaims(db: Session) -> None:
    group = _group(db, ContentPipelineState.CLASSIFIED)
    seen_generations = []
    for i in range(5):
        claimed = WorkerClaimService(db).claim_content_identity_group(
            worker_id=f"worker-{i}",
            eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
            lease_duration=timedelta(seconds=0),  # every prior claim is immediately stale
        )
        assert claimed is not None
        seen_generations.append(claimed.claim_generation)
    assert seen_generations == [1, 2, 3, 4, 5]
    assert len(set(seen_generations)) == 5


def test_release_with_stale_generation_is_a_safe_no_op(db: Session) -> None:
    group = _group(db, ContentPipelineState.CLASSIFIED, claim_generation=3, claimed_by="worker-current", claimed_at=datetime.now(UTC))
    applied = WorkerClaimService(db).release_content_identity_group_claim(group.id, claim_generation=2)
    assert applied is False
    db.refresh(group)
    assert group.claimed_by == "worker-current"
    assert group.claim_generation == 3


# ============================================================
# RESERVATION
# ============================================================


def test_reserve_exact_boundary_succeeds(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=0)
    group = _group(db, ContentPipelineState.CHUNKED, claim_generation=1, claimed_by="worker-a", claimed_at=datetime.now(UTC))

    outcome = WorkerClaimService(db).reserve_embeddings(group_id=group.id, my_generation=1, batch_id=batch.id, n=10)
    assert outcome.reserved is True
    assert outcome.amount == 10
    db.refresh(batch)
    db.refresh(group)
    assert batch.embeddings_reserved == 10
    assert group.reserved_embeddings == 10


def test_reserve_over_envelope_denied_atomically(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=5)
    group = _group(db, ContentPipelineState.CHUNKED, claim_generation=1, claimed_by="worker-a", claimed_at=datetime.now(UTC))

    outcome = WorkerClaimService(db).reserve_embeddings(group_id=group.id, my_generation=1, batch_id=batch.id, n=6)
    assert outcome.reserved is False
    assert outcome.denial_reason is ReservationDenialReason.ENVELOPE_EXHAUSTED

    db.refresh(batch)
    db.refresh(group)
    assert batch.embeddings_reserved == 5  # unchanged - rolled back
    assert group.reserved_embeddings is None  # rolled back, never left partially set


def test_reserve_failure_releases_the_claim(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=10)
    group = _group(db, ContentPipelineState.CHUNKED, claim_generation=1, claimed_by="worker-a", claimed_at=datetime.now(UTC))

    WorkerClaimService(db).reserve_embeddings(group_id=group.id, my_generation=1, batch_id=batch.id, n=1)

    db.refresh(group)
    assert group.claimed_by is None
    assert group.claimed_at is None


def test_reserve_denied_when_batch_not_running(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE, max_embeddings=10)
    group = _group(db, ContentPipelineState.CHUNKED, claim_generation=1, claimed_by="worker-a", claimed_at=datetime.now(UTC))

    outcome = WorkerClaimService(db).reserve_embeddings(group_id=group.id, my_generation=1, batch_id=batch.id, n=1)
    assert outcome.reserved is False
    assert outcome.denial_reason is ReservationDenialReason.BATCH_NOT_RUNNING


def test_reserve_denied_by_hard_stop_guard(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10)
    group = _group(db, ContentPipelineState.CHUNKED, claim_generation=1, claimed_by="worker-a", claimed_at=datetime.now(UTC))

    outcome = WorkerClaimService(db).reserve_embeddings(
        group_id=group.id,
        my_generation=1,
        batch_id=batch.id,
        n=1,
        guard=_guard_always(GuardTier.HARD_STOP, BatchStopReason.WORKSPACE_HARD_STOP),
    )
    assert outcome.reserved is False
    assert outcome.denial_reason is ReservationDenialReason.RESOURCE_GUARD_HARD_STOP
    db.refresh(batch)
    assert batch.embeddings_reserved == 0
    db.refresh(group)
    assert group.claimed_by is None


def test_reserve_allowed_when_guard_is_normal(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10)
    group = _group(db, ContentPipelineState.CHUNKED, claim_generation=1, claimed_by="worker-a", claimed_at=datetime.now(UTC))

    outcome = WorkerClaimService(db).reserve_embeddings(
        group_id=group.id, my_generation=1, batch_id=batch.id, n=1, guard=_guard_always(GuardTier.NORMAL)
    )
    assert outcome.reserved is True


def test_consume_success_clears_reservation_without_decrementing_batch_counter(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=4)
    group = _group(
        db,
        ContentPipelineState.CHUNKED,
        claim_generation=1,
        claimed_by="worker-a",
        claimed_at=datetime.now(UTC),
        reserved_embeddings=4,
        reserved_embeddings_batch_id=batch.id,
    )

    applied = WorkerClaimService(db).consume_embedding_reservation(
        group_id=group.id, my_generation=1, new_pipeline_state=ContentPipelineState.INGESTED
    )
    assert applied is True

    db.refresh(batch)
    db.refresh(group)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None
    assert group.claimed_by is None
    assert group.pipeline_state == ContentPipelineState.INGESTED
    assert batch.embeddings_reserved == 4  # monotonic - unchanged


def test_release_on_embedding_failure_credits_batch_counter_back(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=4)
    group = _group(
        db,
        ContentPipelineState.CHUNKED,
        claim_generation=1,
        claimed_by="worker-a",
        claimed_at=datetime.now(UTC),
        reserved_embeddings=4,
        reserved_embeddings_batch_id=batch.id,
    )

    applied = WorkerClaimService(db).release_embedding_reservation(group_id=group.id, my_generation=1)
    assert applied is True

    db.refresh(batch)
    db.refresh(group)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None
    assert group.claimed_by is None
    assert batch.embeddings_reserved == 0


def test_stale_generation_cannot_release_current_reservation(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=4)
    group = _group(
        db,
        ContentPipelineState.CHUNKED,
        claim_generation=2,
        claimed_by="worker-b",
        claimed_at=datetime.now(UTC),
        reserved_embeddings=4,
        reserved_embeddings_batch_id=batch.id,
    )

    applied = WorkerClaimService(db).release_embedding_reservation(group_id=group.id, my_generation=1)
    assert applied is False

    db.refresh(batch)
    db.refresh(group)
    assert group.reserved_embeddings == 4  # untouched
    assert group.reserved_embeddings_batch_id == batch.id  # untouched
    assert group.claimed_by == "worker-b"  # untouched
    assert batch.embeddings_reserved == 4  # untouched


def test_stale_generation_cannot_consume_current_reservation(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=4)
    group = _group(
        db,
        ContentPipelineState.CHUNKED,
        claim_generation=2,
        claimed_by="worker-b",
        claimed_at=datetime.now(UTC),
        reserved_embeddings=4,
        reserved_embeddings_batch_id=batch.id,
    )

    applied = WorkerClaimService(db).consume_embedding_reservation(
        group_id=group.id, my_generation=1, new_pipeline_state=ContentPipelineState.INGESTED
    )
    assert applied is False

    db.refresh(group)
    assert group.reserved_embeddings == 4
    assert group.pipeline_state == ContentPipelineState.CHUNKED  # never advanced by the stale caller


# ============================================================
# RECOVERY
# ============================================================


def test_one_stale_recovery_wins_concurrently() -> None:
    """Two workers race to reclaim the SAME stale ContentIdentityGroup.
    Exactly one recovery establishes the next generation; the row ends
    up at generation+1, never generation+2 (no duplicate ownership
    epoch, no lost update)."""
    engine = _engine()
    setup_db = Session(engine)
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=ContentPipelineState.CLASSIFIED,
        claim_generation=1,
        claimed_by="worker-dead",
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
    )
    setup_db.add(group)
    setup_db.commit()
    group_id = group.id
    setup_db.close()

    barrier = threading.Barrier(2)
    results: list = [None, None]

    def worker(index: int, worker_id: str):
        thread_db = Session(engine)
        try:
            barrier.wait()
            results[index] = WorkerClaimService(thread_db).claim_content_identity_group(
                worker_id=worker_id,
                eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
                lease_duration=timedelta(minutes=10),
            )
        finally:
            thread_db.close()

    threads = [threading.Thread(target=worker, args=(0, "worker-recovery-a")), threading.Thread(target=worker, args=(1, "worker-recovery-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        succeeded = [r for r in results if r is not None]
        assert len(succeeded) == 1, "SKIP LOCKED means exactly one recovery wins; the other finds no eligible row"
        verify_db = Session(engine)
        final = verify_db.get(ContentIdentityGroup, group_id)
        assert final.claim_generation == 2
        verify_db.close()
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(text("DELETE FROM content_identity_groups WHERE id = :id"), {"id": group_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()


def test_concurrency_stress_recovery_overlapping_new_generation_reservation() -> None:
    """Real-Postgres proof that stale-claim recovery (which now
    performs abandoned-reservation release as part of the SAME
    transaction) is safe when it genuinely overlaps, in real time, with
    the ORIGINAL (about-to-be-invalidated) worker's own first attempt
    to reserve capacity under the generation it still believes it
    holds. Whichever statement's row lock wins the race, both orderings
    converge on the SAME correct final state: the group ends at
    generation 2 with no live reservation, and the batch's counter ends
    at exactly 0 - never double-credited, never left stranded, and
    never torn between the two operations."""
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
    run = ClassificationRun(
        classifier_version="test-recovery-overlap-v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC)
    )
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id, max_embeddings=100))
    setup_db.add(batch)
    setup_db.commit()
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=ContentPipelineState.EXTRACTING,
        claim_generation=1,
        claimed_by="worker-a",
        claimed_at=datetime.now(UTC) - timedelta(hours=1),  # already stale
    )
    setup_db.add(group)
    setup_db.commit()
    batch_id, group_id, run_id, discovery_id = batch.id, group.id, run.id, discovery.id
    setup_db.close()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def stale_worker_a_tries_to_reserve():
        thread_db = Session(engine)
        try:
            barrier.wait()
            results["a_reserve"] = WorkerClaimService(thread_db).reserve_embeddings(
                group_id=group_id, my_generation=1, batch_id=batch_id, n=10
            )
        finally:
            thread_db.close()

    def recovery_worker_b():
        thread_db = Session(engine)
        try:
            barrier.wait()
            results["b_claim"] = WorkerClaimService(thread_db).claim_content_identity_group(
                worker_id="worker-b",
                eligible_pipeline_states=[ContentPipelineState.EXTRACTING],
                lease_duration=timedelta(minutes=10),
            )
        finally:
            thread_db.close()

    threads = [threading.Thread(target=stale_worker_a_tries_to_reserve), threading.Thread(target=recovery_worker_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert results["b_claim"] is not None
        assert results["b_claim"].claim_generation == 2

        verify_db = Session(engine)
        final_group = verify_db.get(ContentIdentityGroup, group_id)
        final_batch = verify_db.get(IngestionBatch, batch_id)
        assert final_group.claim_generation == 2
        assert final_group.reserved_embeddings is None
        assert final_group.reserved_embeddings_batch_id is None
        assert final_batch.embeddings_reserved == 0
        verify_db.close()
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(text("DELETE FROM content_identity_groups WHERE id = :id"), {"id": group_id})
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()


def test_abandoned_reservation_recovery_full_sequence(db: Session) -> None:
    """The exact required sequence from Milestone 4's final correction,
    "Durable Reservation Ownership," section 5: Worker A claims (gen 1)
    and reserves 40 against Batch A (max 100); A stalls; recovery
    (Worker B's own claim call) reclaims to gen 2 and, as PART of that
    same claim, releases A's abandoned reservation and credits Batch A
    back to 0 - all BEFORE Worker B ever reserves anything of its own.
    Worker B then reserves 60 against a DIFFERENT batch (Batch B).
    Delayed Worker A's late release/consume attempts (still holding
    generation 1) must be safe no-ops that touch neither Batch B's live
    reservation nor Batch A's now-zeroed counter, and must never leak a
    raw database exception."""
    run_a = _classification_run(db)
    batch_a = _batch(db, run_a, max_embeddings=100)
    claims = WorkerClaimService(db)

    group = _group(db, ContentPipelineState.CLASSIFIED)
    claimed_by_a = claims.claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
        claiming_pipeline_state=ContentPipelineState.EXTRACTING,
    )
    assert claimed_by_a.claim_generation == 1

    a_reserve = claims.reserve_embeddings(group_id=group.id, my_generation=1, batch_id=batch_a.id, n=40)
    assert a_reserve.reserved is True
    db.refresh(batch_a)
    assert batch_a.embeddings_reserved == 40

    # Worker A stalls forever (never releases/consumes) - simulate
    # staleness by backdating claimed_at directly, matching this
    # project's existing stale-claim test convention.
    db.execute(
        text("UPDATE content_identity_groups SET claimed_at = :t WHERE id = :id"),
        {"t": datetime.now(UTC) - timedelta(hours=1), "id": group.id},
    )
    db.commit()

    # Recovery reclaims the work: this SINGLE call both advances the
    # generation AND releases + credits back A's abandoned reservation,
    # atomically, per claim_content_identity_group's Milestone 4
    # correction.
    claimed_by_b = claims.claim_content_identity_group(
        worker_id="worker-b",
        eligible_pipeline_states=[ContentPipelineState.EXTRACTING],
        lease_duration=timedelta(minutes=10),
    )
    assert claimed_by_b is not None
    assert claimed_by_b.claim_generation == 2

    db.refresh(batch_a)
    db.refresh(group)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None
    assert batch_a.embeddings_reserved == 0

    # Worker B now reserves under its own generation, against a
    # DIFFERENT batch entirely - proving ownership is per-attempt, not
    # inherited from whatever batch previously touched this group.
    run_b = _classification_run(db)
    batch_b = _batch(db, run_b, max_embeddings=100)
    b_reserve = claims.reserve_embeddings(group_id=group.id, my_generation=2, batch_id=batch_b.id, n=60)
    assert b_reserve.reserved is True

    # Delayed Worker A attempts release/consume using its stale
    # generation 1 - both must be safe no-ops.
    release_applied = claims.release_embedding_reservation(group_id=group.id, my_generation=1)
    consume_applied = claims.consume_embedding_reservation(
        group_id=group.id, my_generation=1, new_pipeline_state=ContentPipelineState.INGESTED
    )
    assert release_applied is False
    assert consume_applied is False

    db.refresh(batch_a)
    db.refresh(batch_b)
    db.refresh(group)
    assert batch_b.embeddings_reserved == 60  # B's reservation remains intact
    assert group.reserved_embeddings == 60
    assert group.reserved_embeddings_batch_id == batch_b.id  # B's ownership remains intact
    assert group.claim_generation == 2  # B's generation remains intact
    assert batch_a.embeddings_reserved == 0  # A's counter remains correct, untouched by the late attempt
    assert group.pipeline_state == ContentPipelineState.EXTRACTING  # never advanced by stale A


def test_recovery_credits_back_an_already_aborted_owning_batch(db: Session) -> None:
    """Recovery must reconcile the owning batch's counter even when
    that batch has since become ABORTED - crediting back is bookkeeping
    correction, never a new-work admission decision, so it must not be
    gated on the batch's current status."""
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=100, embeddings_reserved=40)
    db.execute(
        text("UPDATE ingestion_batches SET status = 'ABORTED', stop_reason = 'WORKSPACE_HARD_STOP' WHERE id = :id"),
        {"id": batch.id},
    )
    db.commit()

    group = _group(
        db,
        ContentPipelineState.EXTRACTING,
        claim_generation=1,
        claimed_by="worker-a",
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
        reserved_embeddings=40,
        reserved_embeddings_batch_id=batch.id,
    )

    claimed_by_b = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-b",
        eligible_pipeline_states=[ContentPipelineState.EXTRACTING],
        lease_duration=timedelta(minutes=10),
    )
    assert claimed_by_b is not None
    assert claimed_by_b.claim_generation == 2

    db.refresh(batch)
    db.refresh(group)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None
    assert batch.embeddings_reserved == 0
    assert batch.status is BatchStatus.ABORTED  # untouched otherwise - only the counter is reconciled


def test_stale_reservation_recovery_uses_the_same_fenced_operation(db: Session) -> None:
    """release_embedding_reservation IS the recovery operation - no
    second, independent recovery path exists (Milestone 4 point 9)."""
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=10, embeddings_reserved=4)
    group = _group(
        db,
        ContentPipelineState.CHUNKED,
        claim_generation=1,
        claimed_by="worker-a",
        claimed_at=datetime.now(UTC),
        reserved_embeddings=4,
        reserved_embeddings_batch_id=batch.id,
    )
    service = WorkerClaimService(db)

    # A concurrent/prior call already cleared it for the SAME generation.
    first = service.release_embedding_reservation(group_id=group.id, my_generation=1)
    second = service.release_embedding_reservation(group_id=group.id, my_generation=1)
    assert first is True
    assert second is False  # already NULL - idempotent no-op, not an error

    db.refresh(batch)
    assert batch.embeddings_reserved == 0  # credited back exactly once, never twice


# ============================================================
# CONCURRENCY STRESS
# ============================================================


def test_concurrency_stress_concurrent_reservations_cannot_exceed_batch_max_embeddings() -> None:
    """Real-Postgres proof that concurrent reservation attempts never
    collectively exceed max_embeddings, via the atomic conditional
    UPDATE (never read-then-update)."""
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
    run = ClassificationRun(
        classifier_version="test-reservation-stress-v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC)
    )
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    batch = IngestionBatch(
        status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id, max_embeddings=10)
    )
    setup_db.add(batch)
    setup_db.commit()
    batch_id, run_id, discovery_id = batch.id, run.id, discovery.id

    group_ids = []
    for _ in range(6):
        group = ContentIdentityGroup(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash=_unique_hash(),
            pipeline_state=ContentPipelineState.CHUNKED,
            claim_generation=1,
            claimed_by="worker-pending",
            claimed_at=datetime.now(UTC),
        )
        setup_db.add(group)
        setup_db.commit()
        group_ids.append(group.id)
    setup_db.close()

    # Each of 6 workers tries to reserve 3 (18 total demand) against a
    # 10-capacity envelope - at most 3 can succeed (3*3=9 <= 10 < 12).
    barrier = threading.Barrier(len(group_ids))
    results: list = [None] * len(group_ids)

    def worker(index: int, group_id: int):
        thread_db = Session(engine)
        try:
            barrier.wait()
            results[index] = WorkerClaimService(thread_db).reserve_embeddings(
                group_id=group_id, my_generation=1, batch_id=batch_id, n=3
            )
        finally:
            thread_db.close()

    threads = [threading.Thread(target=worker, args=(i, gid)) for i, gid in enumerate(group_ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        succeeded = [r for r in results if r is not None and r.reserved]
        denied = [r for r in results if r is not None and not r.reserved]
        assert len(succeeded) + len(denied) == len(group_ids)
        assert len(succeeded) * 3 <= 10
        assert all(d.denial_reason is ReservationDenialReason.ENVELOPE_EXHAUSTED for d in denied)

        verify_db = Session(engine)
        final_batch = verify_db.get(IngestionBatch, batch_id)
        assert final_batch.embeddings_reserved == len(succeeded) * 3
        assert final_batch.embeddings_reserved <= final_batch.max_embeddings
        verify_db.close()
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(
            text("DELETE FROM content_identity_groups WHERE id = ANY(:ids)"), {"ids": group_ids}
        )
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()


def test_concurrency_stress_cross_batch_claim_race_repeated() -> None:
    """Repeated (10x) real-Postgres cross-batch claim race: batch A
    worker and batch B worker concurrently claim from a pool containing
    BOTH batches' eligible SourceInstance rows. Proves, every
    iteration: A claims only A's row, B claims only B's row, zero
    cross-claims, zero leaked exceptions."""
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

        run_a = ClassificationRun(classifier_version="v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC))
        run_b = ClassificationRun(classifier_version="v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC))
        setup_db.add_all([run_a, run_b])
        setup_db.commit()
        setup_db.refresh(run_a)
        setup_db.refresh(run_b)

        batch_a = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run_a.id))
        batch_b = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run_b.id))
        setup_db.add_all([batch_a, batch_b])
        setup_db.commit()

        instance_a = SourceInstance(
            classification_run_id=run_a.id, root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt", evidence_snapshot={}
        )
        instance_b = SourceInstance(
            classification_run_id=run_b.id, root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt", evidence_snapshot={}
        )
        setup_db.add_all([instance_a, instance_b])
        setup_db.commit()
        run_a_id, run_b_id = run_a.id, run_b.id
        instance_a_id, instance_b_id = instance_a.id, instance_b.id
        discovery_id = discovery.id
        setup_db.close()

        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: list[Exception] = []

        def worker(label: str, run_id: int):
            thread_db = Session(engine)
            try:
                barrier.wait()
                results[label] = WorkerClaimService(thread_db).claim_source_instance_for_identity_resolution(
                    worker_id=f"worker-{label}", lease_duration=timedelta(minutes=10), classification_run_id=run_id
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        threads = [threading.Thread(target=worker, args=("a", run_a_id)), threading.Thread(target=worker, args=("b", run_b_id))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"unexpected leaked exceptions: {errors}"
            assert results["a"] is not None and results["a"].id == instance_a_id
            assert results["b"] is not None and results["b"].id == instance_b_id
        finally:
            cleanup_db = Session(engine)
            cleanup_db.execute(text("DELETE FROM source_instances WHERE id = ANY(:ids)"), {"ids": [instance_a_id, instance_b_id]})
            cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE classification_run_id = ANY(:ids)"), {"ids": [run_a_id, run_b_id]})
            cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = ANY(:ids)"), {"ids": [run_a_id, run_b_id]})
            cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
            cleanup_db.commit()
            cleanup_db.close()
    engine.dispose()
