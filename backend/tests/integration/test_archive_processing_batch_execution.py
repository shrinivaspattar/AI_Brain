"""Real-database tests for Implementation Milestone 5 (Archive
Processing / Extraction): batch-aware archive claiming, SourceInstance
generation fencing, the extracted-bytes pre-flight/reconciliation
envelope, staging cleanup, and real-PostgreSQL concurrency. No T7
access of any kind: every archive here is a synthetic ZIP built in a
test's own `tmp_path`, standing in for what would be a real T7 archive
in production.

Existing, already-proven scenarios (safe ordinary archive, nested
archives, crash/resume idempotency, corrupt-archive failure, duplicate-
member/provenance prevention) are NOT re-tested here - see
`test_ingestion_pipeline_execution.py`, re-verified passing unchanged
by this milestone. This file covers exactly what Milestone 5 adds.
"""

from __future__ import annotations

import shutil
import threading
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.archive_processing_service import ArchiveProcessingService
from app.classification.resource_guard import BatchResourceGuard, GuardResult, GuardTier
from app.classification.worker_claim_service import WorkerClaimService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome, IngestionFailureCode
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch
from app.models.source_instance import SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - matches this
    project's established fixture pattern."""
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
        classifier_version="test-archive-batch-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _minimal_batch_kwargs(classification_run_id: int, *, max_extracted_bytes: int | None = 10_000) -> dict:
    return dict(
        classification_run_id=classification_run_id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=max_extracted_bytes,
        max_embeddings=5000,
        max_runtime_seconds=7200,
        eligible_source_count=1000,
        policy_filtered_count=1000,
        selectable_count=1000,
        source_instances_selected=1000,
        source_bytes_selected=50_000_000,
        selection_fingerprint=_unique_hash(),
        selection_policy_version="batch-class-2-small-archive-v1",
        ordering_version="lexicographic-path-v1",
    )


def _batch(
    db: Session,
    classification_run: ClassificationRun,
    *,
    status: BatchStatus = BatchStatus.RUNNING,
    stop_reason: BatchStopReason | None = None,
    max_extracted_bytes: int | None = 10_000,
    extracted_bytes_consumed: int = 0,
) -> IngestionBatch:
    batch = IngestionBatch(
        status=status,
        stop_reason=stop_reason,
        extracted_bytes_consumed=extracted_bytes_consumed,
        **_minimal_batch_kwargs(classification_run.id, max_extracted_bytes=max_extracted_bytes),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _archive_instance(
    db: Session, classification_run: ClassificationRun, path: Path, *, declared_size_bytes: int | None = None
) -> SourceInstance:
    evidence_snapshot: dict = {}
    if declared_size_bytes is not None:
        evidence_snapshot["d0_declared_size_bytes"] = declared_size_bytes
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path=str(path),
        evidence_snapshot=evidence_snapshot,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _write_zip(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def _guard_always(tier: GuardTier) -> SimpleNamespace:
    return SimpleNamespace(
        check_before_expensive_operation=lambda batch, kind: GuardResult(
            tier=tier, stop_reason=BatchStopReason.WORKSPACE_HARD_STOP if tier is GuardTier.HARD_STOP else None,
            detail="synthetic",
        )
    )


# ============================================================
# BATCH ADMISSION / ISOLATION
# ============================================================


def test_archive_claim_respects_batch_running_admission(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    _batch(db, run, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE)
    _archive_instance(db, run, archive_path)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace", classification_run_id=run.id
    )
    assert result is None
    assert db.query(IngestionAttempt).count() == 0


def test_archive_processing_batch_scoped_claim_isolation(db: Session, tmp_path: Path) -> None:
    run_a = _classification_run(db)
    run_b = _classification_run(db)
    _batch(db, run_a)
    _batch(db, run_b)

    archive_b_path = tmp_path / "source" / "b.zip"
    _write_zip(archive_b_path, {"b.txt": "content b"})
    _archive_instance(db, run_b, archive_b_path)

    result_for_a = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace", classification_run_id=run_a.id
    )
    assert result_for_a is None  # A's batch has no eligible archive of its own

    result_for_b = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-b", workspace_root=tmp_path / "workspace", classification_run_id=run_b.id
    )
    assert result_for_b is not None
    assert result_for_b.root_t7_path == str(archive_b_path)


# ============================================================
# PRE-FLIGHT ENVELOPE / RECONCILIATION
# ============================================================


def test_archive_preflight_admits_within_envelope_and_reconciles_on_success(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "0123456789"})  # 10 real bytes

    run = _classification_run(db)
    batch = _batch(db, run, max_extracted_bytes=10_000, extracted_bytes_consumed=0)
    _archive_instance(db, run, archive_path, declared_size_bytes=500)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace", classification_run_id=run.id
    )
    assert result is not None

    db.refresh(batch)
    # Reconciled to the REAL measured total (10 bytes), not the
    # declared estimate (500) used only for pre-flight admission.
    assert batch.extracted_bytes_consumed == 10


def test_archive_preflight_denies_when_envelope_would_be_exceeded(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    batch = _batch(db, run, max_extracted_bytes=1000, extracted_bytes_consumed=900)
    _archive_instance(db, run, archive_path, declared_size_bytes=500)  # 900 + 500 > 1000

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace", classification_run_id=run.id
    )
    assert result is None

    db.refresh(batch)
    assert batch.extracted_bytes_consumed == 900  # unchanged - never partially reserved

    # Deferred, not failed - matches the frozen numeric pass exactly:
    # nothing was decided about this item.
    assert db.query(IngestionAttempt).count() == 0

    # Claim was released, not left dangling.
    instance = db.query(SourceInstance).filter(SourceInstance.root_t7_path == str(archive_path)).one()
    assert instance.claimed_by is None


def test_extraction_failure_releases_full_reservation(db: Session, tmp_path: Path) -> None:
    fake_archive = tmp_path / "source" / "broken.zip"
    fake_archive.parent.mkdir(parents=True)
    fake_archive.write_bytes(b"not a real zip")

    run = _classification_run(db)
    batch = _batch(db, run, max_extracted_bytes=10_000, extracted_bytes_consumed=0)
    _archive_instance(db, run, fake_archive, declared_size_bytes=500)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace", classification_run_id=run.id
    )
    assert result is not None

    db.refresh(batch)
    assert batch.extracted_bytes_consumed == 0  # reservation fully released, never partially

    attempt = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == result.id).one()
    assert attempt.outcome == IngestionAttemptOutcome.FAILED
    assert attempt.failure_code == IngestionFailureCode.MALFORMED_ARCHIVE


def test_archive_preflight_null_envelope_still_requires_running(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    _batch(db, run, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE, max_extracted_bytes=None)
    _archive_instance(db, run, archive_path)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace", classification_run_id=run.id
    )
    assert result is None


# ============================================================
# max_depth: frozen outcome
# ============================================================


def test_max_depth_exceeded_produces_the_frozen_outcome(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, archive_path)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace", max_depth=0
    )
    assert result.id == root_instance.id

    attempt = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == root_instance.id).one()
    assert attempt.outcome == IngestionAttemptOutcome.FAILED
    assert attempt.failure_code == IngestionFailureCode.OVERSIZED_OR_EXPANSION_LIMIT
    assert attempt.retryable is False

    db.refresh(root_instance)
    assert root_instance.risk_tier_actual is None


# ============================================================
# GENERATION FENCING / ZOMBIE WORKER
# ============================================================


def test_stale_archive_claim_is_recovered_with_new_generation(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    instance = _archive_instance(db, run, archive_path)

    first = WorkerClaimService(db).claim_source_instance_for_archive_processing(
        worker_id="worker-a", lease_duration=timedelta(minutes=10)
    )
    assert first.claim_generation == 1

    db.execute(
        text("UPDATE source_instances SET claimed_at = :t WHERE id = :id"),
        {"t": datetime.now(UTC) - timedelta(hours=1), "id": instance.id},
    )
    db.commit()

    second = WorkerClaimService(db).claim_source_instance_for_archive_processing(
        worker_id="worker-b", lease_duration=timedelta(minutes=10)
    )
    assert second is not None
    assert second.claim_generation == 2
    assert second.claimed_by == "worker-b"


def test_concurrency_stress_one_stale_source_instance_recovery_wins_concurrently() -> None:
    """Real-Postgres proof, independent of `ContentIdentityGroup`'s own
    already-proven analogue: two workers race to reclaim the SAME stale
    archive-processing `SourceInstance`. The `SKIP LOCKED` + re-checked-
    at-UPDATE-time claim predicate (identical SHAPE to `ContentIdentity
    Group`'s, but a SEPARATE implementation on a SEPARATE model) must be
    independently shown to converge to exactly one winner, generation
    advancing by exactly 1, never 2 - required per Milestone 5's final
    review, which correctly declined to accept the `ContentIdentityGroup`
    proof as a substitute for this model's own verification."""
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
        classifier_version="test-source-instance-stale-race-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)

    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.zip",
        evidence_snapshot={},
        claim_generation=1,
        claimed_by="worker-dead",
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
    )
    setup_db.add(instance)
    setup_db.commit()
    run_id, instance_id, discovery_id = run.id, instance.id, discovery.id
    setup_db.close()

    barrier = threading.Barrier(2)
    results: list = [None, None]
    errors: list[Exception] = []

    def worker(index: int, worker_id: str):
        thread_db = Session(engine)
        try:
            barrier.wait()
            results[index] = WorkerClaimService(thread_db).claim_source_instance_for_archive_processing(
                worker_id=worker_id, lease_duration=timedelta(minutes=10)
            )
        except Exception as exc:  # noqa: BLE001 - a leaked exception here is itself a failure to report
            errors.append(exc)
        finally:
            thread_db.close()

    threads = [
        threading.Thread(target=worker, args=(0, "worker-recovery-a")),
        threading.Thread(target=worker, args=(1, "worker-recovery-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, f"unexpected leaked exceptions from the losing worker: {errors}"

        succeeded = [r for r in results if r is not None]
        assert len(succeeded) == 1, "SKIP LOCKED means exactly one recovery wins; the loser finds no eligible row"
        winner = succeeded[0]

        verify_db = Session(engine)
        final = verify_db.get(SourceInstance, instance_id)
        assert final.claim_generation == 2  # advanced exactly once, never twice
        assert final.claimed_by == winner.claimed_by  # exactly one worker owns the recovered claim
        assert final.claimed_by in ("worker-recovery-a", "worker-recovery-b")
        verify_db.close()
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(
            text("DELETE FROM provenance_links WHERE source_instance_id = :id"), {"id": instance_id}
        )
        cleanup_db.execute(text("DELETE FROM ingestion_attempts WHERE source_instance_id = :id"), {"id": instance_id})
        cleanup_db.execute(text("DELETE FROM source_instances WHERE id = :id"), {"id": instance_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()


def test_zombie_archive_worker_late_release_is_a_safe_no_op(db: Session, tmp_path: Path) -> None:
    """The exact scenario Milestone 5's design correction pass closed:
    worker A claims (generation 1); A is delayed, not crashed;
    stale-claim recovery grants the SAME archive to worker B (generation
    2); A eventually calls its own release using its stale generation 1
    - this must be a safe no-op, never clearing B's live claim."""
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    instance = _archive_instance(db, run, archive_path)
    claims = WorkerClaimService(db)

    worker_a_claim = claims.claim_source_instance_for_archive_processing(
        worker_id="worker-a", lease_duration=timedelta(minutes=10)
    )
    my_generation_a = worker_a_claim.claim_generation
    assert my_generation_a == 1

    db.execute(
        text("UPDATE source_instances SET claimed_at = :t WHERE id = :id"),
        {"t": datetime.now(UTC) - timedelta(hours=1), "id": instance.id},
    )
    db.commit()

    worker_b_claim = claims.claim_source_instance_for_archive_processing(
        worker_id="worker-b", lease_duration=timedelta(minutes=10)
    )
    assert worker_b_claim.claim_generation == 2

    # Delayed worker A finally returns and releases using its stale generation.
    applied = claims.release_source_instance_claim(instance.id, claim_generation=my_generation_a)
    assert applied is False

    db.refresh(instance)
    assert instance.claimed_by == "worker-b"  # B's live claim untouched
    assert instance.claim_generation == 2


# ============================================================
# STAGING CLEANUP
# ============================================================


def test_staging_directory_removed_after_success(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    instance = _archive_instance(db, run, archive_path)
    workspace_root = tmp_path / "workspace"

    ArchiveProcessingService(db).process_next_archive(worker_id="worker-a", workspace_root=workspace_root)

    staging_root = workspace_root / "_staging" / f"archive_{instance.id}"
    assert not staging_root.exists()


def test_staging_directory_removed_after_failure(db: Session, tmp_path: Path) -> None:
    fake_archive = tmp_path / "source" / "broken.zip"
    fake_archive.parent.mkdir(parents=True)
    fake_archive.write_bytes(b"not a real zip")

    run = _classification_run(db)
    instance = _archive_instance(db, run, fake_archive)
    workspace_root = tmp_path / "workspace"

    ArchiveProcessingService(db).process_next_archive(worker_id="worker-a", workspace_root=workspace_root)

    staging_root = workspace_root / "_staging" / f"archive_{instance.id}"
    assert not staging_root.exists()


def test_orphaned_staging_from_a_simulated_crash_is_cleaned_up_on_resume(db: Session, tmp_path: Path) -> None:
    """Simulates a crash: an earlier attempt's staging tree survives on
    disk (the process died before its own `finally` cleanup ran). The
    resuming attempt's idempotent, unconditional cleanup-before-start
    must discard it before extracting fresh."""
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    instance = _archive_instance(db, run, archive_path)
    workspace_root = tmp_path / "workspace"

    orphaned_staging = workspace_root / "_staging" / f"archive_{instance.id}"
    orphaned_staging.mkdir(parents=True)
    (orphaned_staging / "leftover_junk.bin").write_bytes(b"stale data from a crashed attempt")

    result = ArchiveProcessingService(db).process_next_archive(worker_id="worker-a", workspace_root=workspace_root)

    assert result is not None
    assert not orphaned_staging.exists()  # cleaned at both start and end


# ============================================================
# RESOURCE GUARD INTEGRATION
# ============================================================


def test_archive_claim_denied_by_hard_stop_guard(db: Session, tmp_path: Path) -> None:
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    batch = _batch(db, run, max_extracted_bytes=10_000)
    _archive_instance(db, run, archive_path, declared_size_bytes=10)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a",
        workspace_root=tmp_path / "workspace",
        classification_run_id=run.id,
        guard=_guard_always(GuardTier.HARD_STOP),
    )
    assert result is None
    db.refresh(batch)
    assert batch.extracted_bytes_consumed == 0
    assert db.query(IngestionAttempt).count() == 0


# ============================================================
# risk_tier_actual: honest non-fabrication
# ============================================================


def test_risk_tier_actual_remains_null_after_successful_extraction(db: Session, tmp_path: Path) -> None:
    """Explicit, deliberate design position (Milestone 5): no numeric
    mapping from post-extraction evidence to a risk tier has ever been
    frozen - this milestone must not invent one. A successful
    extraction leaves risk_tier_actual NULL, honestly."""
    archive_path = tmp_path / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, archive_path)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )
    assert result.id == root_instance.id

    attempt = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == root_instance.id).one()
    assert attempt.outcome == IngestionAttemptOutcome.SUCCEEDED

    db.refresh(root_instance)
    assert root_instance.risk_tier_actual is None


# ============================================================
# T7 unavailable (archive-specific)
# ============================================================


def test_archive_source_missing_produces_t7_unavailable(db: Session, tmp_path: Path) -> None:
    missing_path = tmp_path / "source" / "gone.zip"
    run = _classification_run(db)
    root_instance = _archive_instance(db, run, missing_path)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )
    assert result.id == root_instance.id

    attempt = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == root_instance.id).one()
    assert attempt.outcome == IngestionAttemptOutcome.FAILED
    assert attempt.failure_code == IngestionFailureCode.T7_UNAVAILABLE


# ============================================================
# CONCURRENCY: real PostgreSQL, separate sessions
# ============================================================


def test_concurrency_stress_two_workers_claim_different_archives_in_same_batch() -> None:
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
        classifier_version="test-archive-concurrency-v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC)
    )
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id, max_extracted_bytes=None))
    setup_db.add(batch)
    setup_db.commit()

    import tempfile

    tmp_dir = Path(tempfile.mkdtemp())
    archive_a = tmp_dir / "source" / "a.zip"
    archive_b = tmp_dir / "source" / "b.zip"
    _write_zip(archive_a, {"a.txt": "content a"})
    _write_zip(archive_b, {"b.txt": "content b"})

    instance_a = SourceInstance(classification_run_id=run.id, root_t7_path=str(archive_a), evidence_snapshot={})
    instance_b = SourceInstance(classification_run_id=run.id, root_t7_path=str(archive_b), evidence_snapshot={})
    setup_db.add_all([instance_a, instance_b])
    setup_db.commit()
    run_id, batch_id = run.id, batch.id
    instance_a_id, instance_b_id = instance_a.id, instance_b.id
    discovery_id = discovery.id
    setup_db.close()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[Exception] = []

    def worker(label: str):
        thread_db = Session(engine)
        try:
            barrier.wait()
            results[label] = ArchiveProcessingService(thread_db).process_next_archive(
                worker_id=f"worker-{label}", workspace_root=tmp_dir / "workspace", classification_run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=worker, args=("1",)), threading.Thread(target=worker, args=("2",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, f"unexpected leaked exceptions: {errors}"
        claimed_ids = {r.id for r in results.values() if r is not None}
        assert claimed_ids == {instance_a_id, instance_b_id}, "each worker must claim a DIFFERENT archive"
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(
            text("DELETE FROM provenance_links WHERE source_instance_id IN "
                 "(SELECT id FROM source_instances WHERE classification_run_id = :rid)"),
            {"rid": run_id},
        )
        cleanup_db.execute(text("DELETE FROM ingestion_attempts WHERE source_instance_id = ANY(:ids)"), {"ids": [instance_a_id, instance_b_id]})
        cleanup_db.execute(text("DELETE FROM source_instances WHERE classification_run_id = :rid"), {"rid": run_id})
        import hashlib as _hashlib

        cleanup_db.execute(
            text("DELETE FROM content_identity_groups WHERE identity_hash = ANY(:hashes)"),
            {"hashes": [_hashlib.sha256(b"content a").hexdigest(), _hashlib.sha256(b"content b").hexdigest()]},
        )
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_concurrency_stress_pause_vs_archive_claim_race() -> None:
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
        classifier_version="test-archive-pause-race-v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC)
    )
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id, max_extracted_bytes=None))
    setup_db.add(batch)
    setup_db.commit()

    import tempfile

    tmp_dir = Path(tempfile.mkdtemp())
    archive_path = tmp_dir / "source" / "a.zip"
    _write_zip(archive_path, {"a.txt": "content"})
    instance = SourceInstance(classification_run_id=run.id, root_t7_path=str(archive_path), evidence_snapshot={})
    setup_db.add(instance)
    setup_db.commit()
    run_id, batch_id, instance_id, discovery_id = run.id, batch.id, instance.id, discovery.id
    setup_db.close()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[Exception] = []

    def claim_worker():
        thread_db = Session(engine)
        try:
            barrier.wait()
            results["claim"] = ArchiveProcessingService(thread_db).process_next_archive(
                worker_id="worker-a", workspace_root=tmp_dir / "workspace", classification_run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            thread_db.close()

    def pause_worker():
        thread_db = Session(engine)
        try:
            from app.classification.batch_control_service import BatchControlService

            barrier.wait()
            results["pause"] = BatchControlService(thread_db).pause(batch_id, reason=BatchStopReason.MANUAL_PAUSE)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=claim_worker), threading.Thread(target=pause_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, f"unexpected leaked exceptions: {errors}"
        verify_db = Session(engine)
        final_instance = verify_db.get(SourceInstance, instance_id)
        final_batch = verify_db.get(IngestionBatch, batch_id)
        # Whichever order the race resolved in, the invariant holds: no
        # raw exception, and the final states are mutually consistent.
        assert final_batch.status in (BatchStatus.RUNNING, BatchStatus.PAUSED)
        if results["claim"] is None:
            assert final_instance.claimed_by is None
        verify_db.close()
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(
            text("DELETE FROM provenance_links WHERE source_instance_id IN "
                 "(SELECT id FROM source_instances WHERE classification_run_id = :rid)"),
            {"rid": run_id},
        )
        cleanup_db.execute(text("DELETE FROM ingestion_attempts WHERE source_instance_id = :id"), {"id": instance_id})
        cleanup_db.execute(text("DELETE FROM source_instances WHERE classification_run_id = :rid"), {"rid": run_id})
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_concurrency_stress_reservations_cannot_exceed_extracted_bytes_envelope() -> None:
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
        classifier_version="test-archive-envelope-race-v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC)
    )
    setup_db.add(run)
    setup_db.commit()
    setup_db.refresh(run)
    batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id, max_extracted_bytes=1000))
    setup_db.add(batch)
    setup_db.commit()

    import tempfile

    tmp_dir = Path(tempfile.mkdtemp())
    instance_ids = []
    for i in range(6):
        archive_path = tmp_dir / "source" / f"a{i}.zip"
        _write_zip(archive_path, {"f.txt": "content"})
        instance = SourceInstance(
            classification_run_id=run.id,
            root_t7_path=str(archive_path),
            evidence_snapshot={"d0_declared_size_bytes": 300},
        )
        setup_db.add(instance)
        setup_db.commit()
        instance_ids.append(instance.id)
    run_id, batch_id, discovery_id = run.id, batch.id, discovery.id
    setup_db.close()

    # 6 workers each try to admit 300 declared bytes against a
    # 1000-byte envelope - at most 3 can succeed (3*300=900 <= 1000 < 1200).
    barrier = threading.Barrier(6)
    results: list = [None] * 6
    errors: list[Exception] = []

    def worker(index: int):
        thread_db = Session(engine)
        try:
            barrier.wait()
            results[index] = ArchiveProcessingService(thread_db).process_next_archive(
                worker_id=f"worker-{index}", workspace_root=tmp_dir / "workspace", classification_run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, f"unexpected leaked exceptions: {errors}"
        succeeded = [r for r in results if r is not None]
        assert len(succeeded) <= 3
        verify_db = Session(engine)
        final_batch = verify_db.get(IngestionBatch, batch_id)
        assert final_batch.extracted_bytes_consumed <= final_batch.max_extracted_bytes
        verify_db.close()
    finally:
        cleanup_db = Session(engine)
        cleanup_db.execute(
            text("DELETE FROM provenance_links WHERE source_instance_id IN "
                 "(SELECT id FROM source_instances WHERE classification_run_id = :rid)"),
            {"rid": run_id},
        )
        cleanup_db.execute(text("DELETE FROM ingestion_attempts WHERE source_instance_id = ANY(:ids)"), {"ids": instance_ids})
        cleanup_db.execute(text("DELETE FROM source_instances WHERE classification_run_id = :rid"), {"rid": run_id})
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": run_id})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
        import hashlib as _hashlib

        cleanup_db.execute(
            text("DELETE FROM content_identity_groups WHERE identity_hash = :h"),
            {"h": _hashlib.sha256(b"content").hexdigest()},
        )
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)
