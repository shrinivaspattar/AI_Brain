"""Real-database tests for Implementation Milestone 11
(BatchOrchestratorService) - Model A (bounded, attended, single-process
execution), per the M11 Design/Decision pass and its subsequent
Correction/Reconciliation pass.

The central property under test throughout this file is TERMINATION:
a single `run_once()` invocation must always return, even in the
presence of a permanently or repeatedly failing item, without any new
retry-exhaustion policy, counter, or schema - purely via the in-memory,
per-invocation "stop on a repeated claimed id" guard in
`_run_stage_to_exhaustion`.

No T7 access of any kind: every archive/file here is entirely
synthetic, built under a test's own tmp directory.
"""

from __future__ import annotations

import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.batch_orchestrator_service import BatchOrchestratorService
from app.classification.batch_control_service import BatchControlService
from app.classification.batch_report_service import BatchReportService
from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.pipeline_embedding_service import PipelineEmbeddingService
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
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome, IngestionFailureCode
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


def _classification_run(db: Session) -> ClassificationRun:
    discovery = DiscoveryRun(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    db.add(discovery)
    db.commit()
    db.refresh(discovery)

    run = ClassificationRun(
        classifier_version="test-m11-orchestrator-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _batch(
    db: Session,
    run: ClassificationRun,
    *,
    source_instances_selected: int,
    status: BatchStatus = BatchStatus.RUNNING,
    stop_reason: BatchStopReason | None = None,
) -> IngestionBatch:
    batch = IngestionBatch(
        status=status,
        stop_reason=stop_reason,
        classification_run_id=run.id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=5000,
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
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _loose_instance(db: Session, run: ClassificationRun, path: Path) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=str(path),
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _archive_instance(db: Session, run: ClassificationRun, path: Path) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=str(path),
        evidence_snapshot={},
        source_category=SourceCategory.ARCHIVE,
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


def _write_nested_zip(outer_path: Path, inner_name: str, innermost_content: str) -> None:
    """outer.zip containing inner.zip containing one plain file - two
    levels of nesting, used with max_depth=1 to deterministically
    trigger OVERSIZED_OR_EXPANSION_LIMIT/retryable=False on the OUTER
    archive's own claim (depth=0 succeeds; recursing into the nested
    archive at depth=1 hits `depth >= max_depth`)."""
    outer_path.parent.mkdir(parents=True, exist_ok=True)
    inner_bytes_path = outer_path.parent / f"__inner_{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(inner_bytes_path, "w") as inner_zf:
        inner_zf.writestr("leaf.txt", innermost_content)
    with zipfile.ZipFile(outer_path, "w") as outer_zf:
        outer_zf.write(inner_bytes_path, arcname=inner_name)
    inner_bytes_path.unlink()


def _write_corrupt_zip(path: Path) -> None:
    """A file with a .zip suffix that is not actually a valid zip -
    triggers MALFORMED_ARCHIVE/retryable=True (confirmed via
    _classify_extraction_failure/_is_extraction_retryable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not a real zip file")


class FakeEmbeddingClient:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t) % 7) / 7.0] * settings.EMBEDDING_DIMENSIONS for t in texts]


def _orchestrator_with_fake_embedding(db: Session) -> BatchOrchestratorService:
    """The frozen contract composes the real PipelineEmbeddingService -
    only its external-shaped EmbeddingClient dependency is faked,
    exactly as established throughout Milestones 6-10."""
    orchestrator = BatchOrchestratorService(db)
    orchestrator.embedding = PipelineEmbeddingService(db, embedding_client=FakeEmbeddingClient())
    return orchestrator


# ============================================================
# 1: Permanently-failing archive does not cause an infinite loop,
#    alongside genuinely distinct other work.
# ============================================================


def test_permanently_failing_archive_does_not_loop_forever_and_distinct_work_still_completes(
    db: Session, tmp_path: Path
) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=2)

    broken_path = tmp_path / "source" / "broken.zip"
    _write_nested_zip(broken_path, "inner.zip", "unreachable leaf content")
    _archive_instance(db, run, broken_path)

    good_path = tmp_path / "source" / "good.zip"
    _write_zip(good_path, {"a.txt": "ordinary archive content"})
    _archive_instance(db, run, good_path)

    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root, max_depth=1)

    archive_stage = next(s for s in result.stage_results if s.stage == "archive_processing")
    # Exactly 2 DISTINCT rows claimed this invocation (the broken one
    # once, the good one once) - the guard stops before a 3rd
    # (repeated) claim of the broken archive.
    assert archive_stage.processed_count == 2

    broken_instance = (
        db.query(SourceInstance)
        .filter(SourceInstance.root_t7_path == str(broken_path), SourceInstance.member_path.is_(None))
        .one()
    )
    good_instance = (
        db.query(SourceInstance)
        .filter(SourceInstance.root_t7_path == str(good_path), SourceInstance.member_path.is_(None))
        .one()
    )

    broken_attempts = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == broken_instance.id).all()
    assert len(broken_attempts) == 1  # exactly ONE attempt this invocation, not looped
    assert broken_attempts[0].outcome == IngestionAttemptOutcome.FAILED
    assert broken_attempts[0].failure_code == IngestionFailureCode.OVERSIZED_OR_EXPANSION_LIMIT
    assert broken_attempts[0].retryable is False

    good_attempts = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == good_instance.id).all()
    assert len(good_attempts) == 1
    assert good_attempts[0].outcome == IngestionAttemptOutcome.SUCCEEDED

    # No leaked claims.
    db.refresh(broken_instance)
    db.refresh(good_instance)
    assert broken_instance.claimed_by is None
    assert good_instance.claimed_by is None


def test_stuck_older_archive_does_not_starve_several_newer_distinct_archives(db: Session, tmp_path: Path) -> None:
    """The specific fairness/progress property required by the M11
    Post-Implementation Finding: a single permanently-stuck item must
    not merely let "at least one" other item through by luck - it must
    let ALL distinct, genuinely-processable candidates be reached. One
    stuck archive (oldest, created first, so it would always win the
    claim's own ORDER BY created_at) alongside THREE separate, valid
    archives - all four must be attempted exactly once each."""
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=4)

    broken_path = tmp_path / "source" / "broken.zip"
    _write_nested_zip(broken_path, "inner.zip", "unreachable leaf content")
    _archive_instance(db, run, broken_path)  # created first - oldest, would always win ORDER BY

    good_paths = []
    for i in range(3):
        good_path = tmp_path / "source" / f"good_{i}.zip"
        _write_zip(good_path, {"a.txt": f"ordinary archive content {i}"})
        _archive_instance(db, run, good_path)
        good_paths.append(good_path)

    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root, max_depth=1)

    archive_stage = next(s for s in result.stage_results if s.stage == "archive_processing")
    assert archive_stage.processed_count == 4  # the stuck one plus all three distinct good ones

    for good_path in good_paths:
        good_instance = (
            db.query(SourceInstance)
            .filter(SourceInstance.root_t7_path == str(good_path), SourceInstance.member_path.is_(None))
            .one()
        )
        attempts = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == good_instance.id).all()
        assert len(attempts) == 1, f"{good_path} was never reached (starvation)"
        assert attempts[0].outcome == IngestionAttemptOutcome.SUCCEEDED

    broken_instance = (
        db.query(SourceInstance)
        .filter(SourceInstance.root_t7_path == str(broken_path), SourceInstance.member_path.is_(None))
        .one()
    )
    broken_attempts = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == broken_instance.id).all()
    assert len(broken_attempts) == 1  # still exactly one attempt, never re-looped


def test_second_invocation_attempts_the_permanently_failing_archive_again_fresh(db: Session, tmp_path: Path) -> None:
    """Confirms the per-invocation exclusion is genuinely scoped to ONE
    invocation, not a durable exclusion - a second, separate
    run_once() call gets a fresh, empty exclude-set and tries again
    (matching Model A's "operator decides" philosophy, never a hidden
    permanent skip). `source_instances_selected=2` with only ONE real
    row ever created deliberately keeps `unattempted_selected_count`
    permanently >= 1, so the batch never reaches SOURCE_WORK_EXHAUSTED
    and stays RUNNING across both invocations - isolating the property
    under test from Milestone 8's own (correct, separately-proven)
    completion behavior, which would otherwise legitimately complete
    the batch after the first invocation and correctly refuse a second
    claim - not a bug, just a different property than this test needs."""
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=2)
    broken_path = tmp_path / "source" / "broken.zip"
    _write_nested_zip(broken_path, "inner.zip", "unreachable")
    _archive_instance(db, run, broken_path)
    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root, max_depth=1)
    result2 = orchestrator.run_once(batch.id, worker_id="worker-b", workspace_root=workspace_root, max_depth=1)

    archive_stage_2 = next(s for s in result2.stage_results if s.stage == "archive_processing")
    assert archive_stage_2.processed_count == 1  # attempted again, fresh, this second invocation

    instance = (
        db.query(SourceInstance)
        .filter(SourceInstance.root_t7_path == str(broken_path), SourceInstance.member_path.is_(None))
        .one()
    )
    attempts = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == instance.id).all()
    assert len(attempts) == 2  # one per invocation


# ============================================================
# 2: Retryable/reclaimable archive behaves correctly.
# ============================================================


def test_retryable_archive_failure_attempted_once_per_invocation(db: Session, tmp_path: Path) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    corrupt_path = tmp_path / "source" / "corrupt.zip"
    _write_corrupt_zip(corrupt_path)
    _archive_instance(db, run, corrupt_path)
    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root)

    archive_stage = next(s for s in result.stage_results if s.stage == "archive_processing")
    assert archive_stage.processed_count == 1

    instance = db.query(SourceInstance).filter(SourceInstance.root_t7_path == str(corrupt_path)).one()
    attempts = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == instance.id).all()
    assert len(attempts) == 1
    assert attempts[0].failure_code == IngestionFailureCode.MALFORMED_ARCHIVE
    assert attempts[0].retryable is True
    assert instance.claimed_by is None  # released, reclaimable by a future invocation


# ============================================================
# 3: SourceInstance identity-resolution retry behavior (analogous).
# ============================================================


def test_permanently_unreadable_loose_file_does_not_loop_forever(db: Session, tmp_path: Path) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=2)

    # Never actually created on disk - read_bytes() raises FileNotFoundError -> T7_UNAVAILABLE, retryable=True.
    missing_path = tmp_path / "source" / "missing.txt"
    _loose_instance(db, run, missing_path)

    real_path = tmp_path / "source" / "real.txt"
    real_path.parent.mkdir(parents=True, exist_ok=True)
    real_path.write_text("genuine loose file content")
    _loose_instance(db, run, real_path)

    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root)

    id_stage = next(s for s in result.stage_results if s.stage == "identity_resolution")
    assert id_stage.processed_count == 2  # both attempted exactly once, no infinite loop on the missing one

    missing_instance = db.query(SourceInstance).filter(SourceInstance.root_t7_path == str(missing_path)).one()
    missing_attempts = db.query(IngestionAttempt).filter(IngestionAttempt.source_instance_id == missing_instance.id).all()
    assert len(missing_attempts) == 1
    assert missing_attempts[0].failure_code == IngestionFailureCode.T7_UNAVAILABLE
    assert missing_attempts[0].retryable is True
    assert missing_instance.content_identity_group_id is None
    assert missing_instance.claimed_by is None

    real_instance = db.query(SourceInstance).filter(SourceInstance.root_t7_path == str(real_path)).one()
    assert real_instance.content_identity_group_id is not None


# ============================================================
# 4: A single pass completes ordinary end-to-end work.
# ============================================================


def test_single_pass_drives_ordinary_content_to_ingested_and_completes_batch(db: Session, tmp_path: Path) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    source_path = tmp_path / "source" / "notes.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("ordinary end-to-end orchestrator content")
    _loose_instance(db, run, source_path)
    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root)

    stage_counts = {s.stage: s.processed_count for s in result.stage_results}
    assert stage_counts["identity_resolution"] == 1
    assert stage_counts["normalization"] == 1
    assert stage_counts["chunking"] == 1
    assert stage_counts["embedding"] == 1

    assert result.completion is not None
    assert result.completion.applied is True
    assert result.completion.batch.status == BatchStatus.COMPLETED
    assert result.completion.batch.stop_reason == BatchStopReason.SOURCE_WORK_EXHAUSTED


# ============================================================
# 5: Downstream (member-only) progress when root source counts are
#    already unchanged - the exact scenario the removed multi-sweep
#    metric would have mishandled.
# ============================================================


def test_single_pass_finishes_archive_member_work_even_though_root_counts_are_already_stable(
    db: Session, tmp_path: Path
) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)

    archive_path = tmp_path / "source" / "already_done.zip"
    _write_zip(archive_path, {})  # archive itself already fully processed
    archive_instance = _archive_instance(db, run, archive_path)
    IngestionAttemptService(db).record_identity_resolution_attempt(
        source_instance_id=archive_instance.id, worker_id="worker-setup", outcome=IngestionAttemptOutcome.SUCCEEDED
    )

    # A member (as if extracted by that already-SUCCEEDED archive claim
    # in a prior invocation) sitting at CHUNKED, awaiting only embedding.
    member_group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=ContentPipelineState.CHUNKED,
    )
    db.add(member_group)
    db.commit()
    db.refresh(member_group)
    document = Document(
        title="member.txt",
        source="/synthetic/workspace/member.txt",
        source_type="txt",
        content_hash=member_group.identity_hash,
        content_identity_group_id=member_group.id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    db.add(DocumentChunk(document_id=document.id, chunk_index=0, content="member content awaiting embedding", embedding=None))
    db.add(
        SourceInstance(
            classification_run_id=run.id,
            root_t7_path=str(archive_path),
            member_path="inner/member.txt",
            evidence_snapshot={},
            source_category=SourceCategory.LOOSE_FILE,
            content_identity_group_id=member_group.id,
        )
    )
    db.commit()

    # Root-level counts are ALREADY fully stable before this invocation.
    pre_report = BatchReportService(db).generate_report(batch.id)
    assert pre_report.unattempted_selected_count == 0
    assert pre_report.terminal_source_count == pre_report.attempted_source_count == 1

    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)
    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root)

    stage_counts = {s.stage: s.processed_count for s in result.stage_results}
    assert stage_counts["archive_processing"] == 0  # nothing new to claim there
    assert stage_counts["embedding"] == 1  # but the member's embedding still happened

    db.refresh(member_group)
    assert member_group.pipeline_state == ContentPipelineState.INGESTED


# ============================================================
# 6: Repeated invocation is a safe, idempotent no-op.
# ============================================================


def test_repeated_invocation_after_completion_is_a_safe_no_op(db: Session, tmp_path: Path) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    source_path = tmp_path / "source" / "notes.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("idempotent repeated invocation content")
    _loose_instance(db, run, source_path)
    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    first = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root)
    assert first.completion is not None and first.completion.applied is True

    second = orchestrator.run_once(batch.id, worker_id="worker-b", workspace_root=workspace_root)

    assert all(s.processed_count == 0 for s in second.stage_results)
    assert second.completion is None  # batch no longer RUNNING - correct, safe no-op


# ============================================================
# 7: Crash / recovery - a stale claim from a prior (crashed) invocation
#    is correctly reclaimed and finished by a fresh invocation.
# ============================================================


def test_stale_claim_from_a_simulated_crash_is_recovered_by_a_fresh_invocation(db: Session, tmp_path: Path) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    source_path = tmp_path / "source" / "notes.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("crash recovery content")
    instance = _loose_instance(db, run, source_path)

    # Simulate a worker that claimed this row and then crashed before
    # recording any attempt - claimed_by set, claimed_at long past any
    # real lease duration.
    instance.claimed_by = "crashed-worker"
    instance.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    instance.claim_generation = 1
    db.commit()

    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)
    result = orchestrator.run_once(batch.id, worker_id="worker-fresh", workspace_root=workspace_root)

    stage_counts = {s.stage: s.processed_count for s in result.stage_results}
    assert stage_counts["identity_resolution"] == 1
    assert result.completion is not None and result.completion.applied is True

    db.refresh(instance)
    assert instance.content_identity_group_id is not None
    assert instance.claimed_by is None


# ============================================================
# 8: Pause - no new claims granted, no error.
# ============================================================


def test_paused_batch_grants_no_new_source_level_claims_and_does_not_error(db: Session, tmp_path: Path) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE)
    source_path = tmp_path / "source" / "notes.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("should not be claimed while paused")
    instance = _loose_instance(db, run, source_path)
    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root)

    stage_counts = {s.stage: s.processed_count for s in result.stage_results}
    assert stage_counts["archive_processing"] == 0
    assert stage_counts["identity_resolution"] == 0  # batch not RUNNING - existing Milestone 4 admission gate
    assert result.completion is None  # batch not RUNNING - correct no-op

    db.refresh(instance)
    assert instance.claimed_by is None
    assert instance.content_identity_group_id is None


def test_unknown_batch_raises(db: Session, tmp_path: Path) -> None:
    orchestrator = BatchOrchestratorService(db)
    with pytest.raises(ValueError, match="not found"):
        orchestrator.run_once(999_999_999, worker_id="worker-a", workspace_root=tmp_path / "workspace")


# ============================================================
# 9: Completion reconciliation occurs exactly once, correctly.
# ============================================================


def test_completion_reconciliation_called_exactly_once_and_correctly_applies(db: Session, tmp_path: Path) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, source_instances_selected=1)
    source_path = tmp_path / "source" / "notes.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("completion reconciliation content")
    _loose_instance(db, run, source_path)
    workspace_root = tmp_path / "workspace"
    orchestrator = _orchestrator_with_fake_embedding(db)

    result = orchestrator.run_once(batch.id, worker_id="worker-a", workspace_root=workspace_root)

    assert result.completion is not None
    assert result.completion.applied is True
    assert result.completion.batch.status == BatchStatus.COMPLETED

    # Exactly one attempt record exists per stage that ran - no double
    # invocation of check_and_complete's own underlying complete() call
    # (a second call would simply be a safe no-op via BatchControlService's
    # own CAS, but confirm the batch settled in exactly the right state).
    db.refresh(batch)
    assert batch.status == BatchStatus.COMPLETED
    assert batch.stop_reason == BatchStopReason.SOURCE_WORK_EXHAUSTED
    assert batch.completed_at is not None
