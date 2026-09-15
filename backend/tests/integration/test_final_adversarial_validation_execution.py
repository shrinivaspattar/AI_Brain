"""Real-database tests for Implementation Milestone 9 (Final
Cross-Stage/Cross-Milestone Adversarial Validation) - frozen step 12
of the Scaled Real-T7 Ingestion 12-step implementation order. See
"Scaled Real-T7 Ingestion - Milestone 9 Design: Final Adversarial
Validation" and its Design Review for the frozen specification this
milestone implements.

This is explicitly NOT a re-proof of any single milestone's own
already-established primitive-level concurrency guarantees (M4-M8 each
already proved their own claim/reservation/completion CAS mechanisms
under real Postgres, repeated 8-10x at the time). This file proves the
INTERACTIONS between those independently-proven components - the one
thing no single milestone's own test suite could have exercised, since
it requires every stage to already exist.

No T7 access of any kind: every archive/file here is entirely
synthetic, built under a test's own tmp directory. No production
aibrain access of any kind beyond the read-only migration-head checks
this milestone's own verification report performs separately (never
inside a test). Every real-Postgres session in this file explicitly
targets `aibrain_test` via `.set(database="aibrain_test")` - never the
configured default - matching this project's established, hard
test-harness invariant.
"""

from __future__ import annotations

import tempfile
import threading
import time
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
from app.classification.batch_completion_reconciliation_service import (
    BatchCompletionReconciliationService,
)
from app.classification.batch_control_service import BatchControlService
from app.classification.batch_report_service import BatchReportInvariantViolation, BatchReportService
from app.classification.identity_resolution_service import IdentityResolutionService
from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.normalization_service import NormalizationService
from app.classification.chunking_service import ChunkingService
from app.classification.pipeline_embedding_service import PipelineEmbeddingService
from app.classification.resource_guard import GuardResult, GuardTier
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
from app.models.ingestion_attempt import IngestionAttemptOutcome, IngestionAttemptStage
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch
from app.models.source_instance import SourceCategory, SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - for the single-
    threaded deterministic scenario (6) only. Every multi-threaded
    scenario in this file uses its own explicit, separately-committed
    Session per worker instead, per this project's established
    real-concurrency test pattern."""
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


def _new_discovery_run(session: Session) -> int:
    run = DiscoveryRun(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run.id


def _new_classification_run(session: Session, discovery_id: int) -> int:
    run = ClassificationRun(
        classifier_version="test-m9-adversarial-v1", d1_discovery_run_id=discovery_id, started_at=datetime.now(UTC)
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run.id


def _batch_kwargs(
    classification_run_id: int,
    *,
    source_instances_selected: int,
    max_embeddings: int = 5000,
    max_extracted_bytes: int | None = None,
) -> dict:
    return dict(
        classification_run_id=classification_run_id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=max_extracted_bytes,
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


def _new_batch(
    session: Session,
    classification_run_id: int,
    *,
    source_instances_selected: int,
    max_embeddings: int = 5000,
    max_extracted_bytes: int | None = None,
) -> int:
    batch = IngestionBatch(
        status=BatchStatus.RUNNING,
        **_batch_kwargs(
            classification_run_id,
            source_instances_selected=source_instances_selected,
            max_embeddings=max_embeddings,
            max_extracted_bytes=max_extracted_bytes,
        ),
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch.id


def _new_loose_instance(session: Session, classification_run_id: int, path: Path | None = None) -> int:
    instance = SourceInstance(
        classification_run_id=classification_run_id,
        root_t7_path=str(path) if path is not None else f"/synthetic/{uuid.uuid4().hex}.txt",
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance.id


def _new_archive_instance(session: Session, classification_run_id: int, path: Path) -> int:
    instance = SourceInstance(
        classification_run_id=classification_run_id,
        root_t7_path=str(path),
        evidence_snapshot={},
        source_category=SourceCategory.ARCHIVE,
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance.id


def _write_zip(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def _new_group(session: Session, pipeline_state: ContentPipelineState) -> int:
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=pipeline_state,
    )
    session.add(group)
    session.commit()
    session.refresh(group)
    return group.id


def _guard_hard_stop() -> SimpleNamespace:
    return SimpleNamespace(
        check_before_claim=lambda batch: GuardResult(
            tier=GuardTier.HARD_STOP, stop_reason=BatchStopReason.WORKSPACE_HARD_STOP, detail="synthetic hard stop"
        )
    )


class FakeEmbeddingClient:
    def __init__(self, delay_seconds: float = 0.0):
        self.calls: list[list[str]] = []
        self.delay_seconds = delay_seconds

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        self.calls.append(list(texts))
        return [[float(len(t) % 7) / 7.0] * settings.EMBEDDING_DIMENSIONS for t in texts]


def _cleanup_runs(engine, run_ids: list[int], discovery_ids: list[int]) -> None:
    """Universal, FK-safe cleanup for one or several classification_runs
    (and their batches), sharing or not sharing discovery_run(s) -
    correct even for cross-run content-identity convergence, since
    group ids are captured BEFORE any source_instance referencing them
    is deleted."""
    cleanup_db = Session(engine)
    try:
        group_ids = [
            row[0]
            for row in cleanup_db.execute(
                text(
                    "SELECT DISTINCT content_identity_group_id FROM source_instances "
                    "WHERE classification_run_id = ANY(:rids) AND content_identity_group_id IS NOT NULL"
                ),
                {"rids": run_ids},
            ).fetchall()
        ]
        if group_ids:
            cleanup_db.execute(
                text(
                    "DELETE FROM document_chunks WHERE document_id IN "
                    "(SELECT id FROM documents WHERE content_identity_group_id = ANY(:gids))"
                ),
                {"gids": group_ids},
            )
            cleanup_db.execute(text("DELETE FROM documents WHERE content_identity_group_id = ANY(:gids)"), {"gids": group_ids})
            cleanup_db.execute(text("DELETE FROM ingestion_attempts WHERE content_identity_group_id = ANY(:gids)"), {"gids": group_ids})
        cleanup_db.execute(
            text("DELETE FROM ingestion_attempts WHERE source_instance_id IN (SELECT id FROM source_instances WHERE classification_run_id = ANY(:rids))"),
            {"rids": run_ids},
        )
        cleanup_db.execute(
            text("DELETE FROM provenance_links WHERE source_instance_id IN (SELECT id FROM source_instances WHERE classification_run_id = ANY(:rids))"),
            {"rids": run_ids},
        )
        cleanup_db.execute(text("DELETE FROM source_instances WHERE classification_run_id = ANY(:rids)"), {"rids": run_ids})
        if group_ids:
            cleanup_db.execute(text("DELETE FROM content_identity_groups WHERE id = ANY(:gids)"), {"gids": group_ids})
        cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE classification_run_id = ANY(:rids)"), {"rids": run_ids})
        cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = ANY(:rids)"), {"rids": run_ids})
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = ANY(:dids)"), {"dids": discovery_ids})
        cleanup_db.commit()
    finally:
        cleanup_db.close()


# ============================================================
# 1. Mixed-stage claim fencing: identity-resolution + archive-
#    processing workers racing over a shared, mixed batch.
# ============================================================


def test_mixed_stage_claim_fencing_repeated() -> None:
    engine = _engine()
    iterations = 8
    for _ in range(iterations):
        setup_db = Session(engine)
        discovery_id = _new_discovery_run(setup_db)
        run_id = _new_classification_run(setup_db, discovery_id)
        batch_id = _new_batch(setup_db, run_id, source_instances_selected=4)

        workspace_root = Path(tempfile.mkdtemp()) / "workspace"
        source_root = Path(tempfile.mkdtemp()) / "source"
        source_root.mkdir(parents=True)

        loose_a_path = source_root / "a.txt"
        loose_a_path.write_text("loose file a content")
        loose_b_path = source_root / "b.txt"
        loose_b_path.write_text("loose file b content")
        archive_a_path = source_root / "a.zip"
        _write_zip(archive_a_path, {"inner_a.txt": "archive a member"})
        archive_b_path = source_root / "b.zip"
        _write_zip(archive_b_path, {"inner_b.txt": "archive b member"})

        loose_a_id = _new_loose_instance(setup_db, run_id, loose_a_path)
        loose_b_id = _new_loose_instance(setup_db, run_id, loose_b_path)
        archive_a_id = _new_archive_instance(setup_db, run_id, archive_a_path)
        archive_b_id = _new_archive_instance(setup_db, run_id, archive_b_path)
        setup_db.close()

        barrier = threading.Barrier(4)
        results: dict[str, object] = {}
        errors: list[Exception] = []

        def id_worker(label: str):
            thread_db = Session(engine)
            try:
                barrier.wait()
                results[label] = IdentityResolutionService(thread_db).resolve_next(
                    worker_id=label, workspace_root=workspace_root, classification_run_id=run_id
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        def archive_worker(label: str):
            thread_db = Session(engine)
            try:
                barrier.wait()
                results[label] = ArchiveProcessingService(thread_db).process_next_archive(
                    worker_id=label, workspace_root=workspace_root, classification_run_id=run_id
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        threads = [
            threading.Thread(target=id_worker, args=("id-a",)),
            threading.Thread(target=id_worker, args=("id-b",)),
            threading.Thread(target=archive_worker, args=("arch-a",)),
            threading.Thread(target=archive_worker, args=("arch-b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"unexpected leaked exceptions: {errors}"
            id_claimed_ids = {results[label].id for label in ("id-a", "id-b") if results[label] is not None}
            archive_claimed_ids = {results[label].id for label in ("arch-a", "arch-b") if results[label] is not None}

            # Fencing: identity-resolution workers claimed ONLY the two
            # loose files; archive workers claimed ONLY the two archives.
            # Never a cross-claim, under real concurrent contention.
            assert id_claimed_ids == {loose_a_id, loose_b_id}
            assert archive_claimed_ids == {archive_a_id, archive_b_id}
        finally:
            _cleanup_runs(engine, [run_id], [discovery_id])

    engine.dispose()


# ============================================================
# 2. Three-way cross-batch embedding arbitration.
# ============================================================


def test_three_way_cross_batch_embedding_arbitration_repeated() -> None:
    """Extends Milestone 6's two-batch proof to three RUNNING batches
    converged on the same ContentIdentityGroup - the lowest-id batch
    must always be charged, never the other two, under real
    concurrent contention."""
    engine = _engine()
    iterations = 8
    for _ in range(iterations):
        setup_db = Session(engine)
        discovery_id = _new_discovery_run(setup_db)
        run_ids = [_new_classification_run(setup_db, discovery_id) for _ in range(3)]
        batch_ids = [_new_batch(setup_db, rid, source_instances_selected=1) for rid in run_ids]
        assert batch_ids == sorted(batch_ids)

        group_id = _new_group(setup_db, ContentPipelineState.CHUNKED)
        document = None
        from app.models.document import Document
        from app.models.document_chunk import DocumentChunk

        document = Document(
            title="synthetic.txt",
            source="/synthetic/workspace/synthetic.txt",
            source_type="txt",
            content_hash=_unique_hash(),
            content_identity_group_id=group_id,
        )
        setup_db.add(document)
        setup_db.commit()
        setup_db.refresh(document)
        setup_db.add(DocumentChunk(document_id=document.id, chunk_index=0, content="race content", embedding=None))
        setup_db.commit()

        for rid in run_ids:
            setup_db.add(
                SourceInstance(
                    classification_run_id=rid,
                    root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
                    evidence_snapshot={},
                    source_category=SourceCategory.LOOSE_FILE,
                    content_identity_group_id=group_id,
                )
            )
        setup_db.commit()
        setup_db.close()

        barrier = threading.Barrier(3)
        fakes = {label: FakeEmbeddingClient() for label in ("a", "b", "c")}
        results: dict[str, object] = {}
        errors: list[Exception] = []

        def worker(label: str):
            thread_db = Session(engine)
            try:
                barrier.wait()
                results[label] = PipelineEmbeddingService(thread_db, embedding_client=fakes[label]).embed_next(
                    worker_id=f"worker-{label}"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        threads = [threading.Thread(target=worker, args=(label,)) for label in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"unexpected leaked exceptions: {errors}"
            total_calls = sum(len(f.calls) for f in fakes.values())
            assert total_calls == 1

            verify_db = Session(engine)
            batches = [verify_db.get(IngestionBatch, bid) for bid in batch_ids]
            lowest_id_batch, other_batches = batches[0], batches[1:]
            assert lowest_id_batch.embeddings_reserved == 1
            assert all(b.embeddings_reserved == 0 for b in other_batches)
            final_group = verify_db.get(ContentIdentityGroup, group_id)
            assert final_group.pipeline_state == ContentPipelineState.INGESTED
            verify_db.close()
        finally:
            _cleanup_runs(engine, run_ids, [discovery_id])

    engine.dispose()


# ============================================================
# 3. Embedding reservation vs check_and_complete().
# ============================================================


def test_reconciliation_never_completes_while_reservation_outstanding() -> None:
    """Controlled two-phase proof (deterministic, not timing-based -
    per the Design Review, this scenario need not be a luck-based
    race): a reservation is deliberately held open in one real Postgres
    session while another session repeatedly calls check_and_complete()
    - every call during the held window must return None; the very
    next call after the reservation is genuinely consumed must
    complete the batch."""
    engine = _engine()
    setup_db = Session(engine)
    discovery_id = _new_discovery_run(setup_db)
    run_id = _new_classification_run(setup_db, discovery_id)
    batch_id = _new_batch(setup_db, run_id, source_instances_selected=1, max_embeddings=100)
    group_id = _new_group(setup_db, ContentPipelineState.CHUNKED)
    setup_db.add(
        SourceInstance(
            classification_run_id=run_id,
            root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
            evidence_snapshot={},
            source_category=SourceCategory.LOOSE_FILE,
            content_identity_group_id=group_id,
        )
    )
    setup_db.commit()

    try:
        holder_db = Session(engine)
        claimed = WorkerClaimService(holder_db).claim_content_identity_group(
            worker_id="worker-holder", eligible_pipeline_states=[ContentPipelineState.CHUNKED], lease_duration=timedelta(minutes=10)
        )
        assert claimed is not None
        outcome = WorkerClaimService(holder_db).reserve_embeddings(
            group_id=claimed.id, my_generation=claimed.claim_generation, batch_id=batch_id, n=1
        )
        assert outcome.reserved is True

        # Reservation is now genuinely outstanding (group still CHUNKED,
        # reserved_embeddings set) - poll check_and_complete() repeatedly
        # from a SEPARATE session during this window.
        for _ in range(5):
            poll_db = Session(engine)
            try:
                result = BatchCompletionReconciliationService(poll_db).check_and_complete(batch_id)
                assert result is None, "must never complete while a reservation is genuinely outstanding"
            finally:
                poll_db.close()

        # Now genuinely finish the work - record the real attempt FIRST
        # (matching PipelineEmbeddingService's own _succeed ordering),
        # since BatchReportService's attempted_source_count requires a
        # real IngestionAttempt to exist, never inferring "attempted"
        # from pipeline_state alone.
        IngestionAttemptService(holder_db).record_pipeline_attempt(
            content_identity_group_id=claimed.id,
            attempted_stage=IngestionAttemptStage.EMBEDDING,
            worker_id="worker-holder",
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )
        consumed = WorkerClaimService(holder_db).consume_embedding_reservation(
            group_id=claimed.id, my_generation=claimed.claim_generation, new_pipeline_state=ContentPipelineState.INGESTED
        )
        assert consumed is True
        holder_db.close()

        final_db = Session(engine)
        try:
            result = BatchCompletionReconciliationService(final_db).check_and_complete(batch_id)
            assert result is not None and result.applied is True
        finally:
            final_db.close()
    finally:
        _cleanup_runs(engine, [run_id], [discovery_id])
        engine.dispose()


def test_reconciliation_race_with_real_embedding_completion_repeated() -> None:
    """A genuine, barrier-synced race between the worker finishing the
    last embedding and a reconciler polling concurrently - proves no
    exception, no deadlock, and eventual correct convergence under
    real thread interleaving (not just the controlled proof above)."""
    engine = _engine()
    iterations = 8
    for _ in range(iterations):
        setup_db = Session(engine)
        discovery_id = _new_discovery_run(setup_db)
        run_id = _new_classification_run(setup_db, discovery_id)
        batch_id = _new_batch(setup_db, run_id, source_instances_selected=1, max_embeddings=100)
        group_id = _new_group(setup_db, ContentPipelineState.CHUNKED)
        setup_db.add(
            SourceInstance(
                classification_run_id=run_id,
                root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
                evidence_snapshot={},
                source_category=SourceCategory.LOOSE_FILE,
                content_identity_group_id=group_id,
            )
        )
        setup_db.commit()
        setup_db.close()

        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        reconcile_results: list[object] = []

        def embed_worker():
            thread_db = Session(engine)
            try:
                barrier.wait()
                PipelineEmbeddingService(thread_db, embedding_client=FakeEmbeddingClient(delay_seconds=0.05)).embed_next(
                    worker_id="worker-embed"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        def reconcile_worker():
            thread_db = Session(engine)
            try:
                barrier.wait()
                for _ in range(10):
                    result = BatchCompletionReconciliationService(thread_db).check_and_complete(batch_id)
                    reconcile_results.append(result)
                    time.sleep(0.01)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        threads = [threading.Thread(target=embed_worker), threading.Thread(target=reconcile_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"unexpected leaked exceptions: {errors}"
            # One final, authoritative check after both threads finish.
            final_db = Session(engine)
            final_result = BatchCompletionReconciliationService(final_db).check_and_complete(batch_id)
            final_batch = final_db.get(IngestionBatch, batch_id)
            # Either the race thread or this final call completed it -
            # either way, it must be COMPLETED by now (embedding finished).
            assert final_batch.status == BatchStatus.COMPLETED or (final_result and final_result.applied)
            final_db.close()
        finally:
            _cleanup_runs(engine, [run_id], [discovery_id])

    engine.dispose()


# ============================================================
# 4. Dual-archive concurrent processing with simulated crash/recovery.
# ============================================================


def test_dual_archive_concurrent_crash_isolation_and_idempotent_retry() -> None:
    """Two archives under one batch, processed concurrently by two
    workers. Proves: the crash is isolated to its own archive's staging
    (the other archive's processing is unaffected), the crashed claim
    is cleanly released (idempotent retry succeeds), and calling
    process_next_archive again afterward (both archives already done)
    is a safe no-op - Milestone 9's composite idempotency proof
    (scenario 8).

    DISCOVERED DURING M9 IMPLEMENTATION, reported per the frozen
    design's own instruction ("if the fixture is correct and
    production behavior differs from assumption, stop and report -
    do not force the design's original premise"): raising inside
    `ArchiveExtractor.extract()` itself does NOT simulate a genuine
    "worker crash" - `_process_claimed_archive`'s own internal
    try/except already catches exactly that (any exception from
    `_extract_recursive`, which calls `extract()`) and converts it into
    a durable, classified `FAILED` `IngestionAttempt` - an intentional,
    correct, ALREADY-EXISTING safety behavior (extraction failures are
    supposed to be caught and classified, not crash the worker), not a
    gap. To genuinely simulate an uncaught worker crash (one that
    propagates all the way out of `process_next_archive`, matching
    what M5's own crash-resume tests exercise via direct
    process-interruption-style reasoning), the raise must happen
    OUTSIDE that internal try/except - `_preflight_admit` runs before
    `_process_claimed_archive` is ever entered and has no such
    boundary, so it is the correct injection point for THIS test's
    actual intent."""
    engine = _engine()
    setup_db = Session(engine)
    discovery_id = _new_discovery_run(setup_db)
    run_id = _new_classification_run(setup_db, discovery_id)
    batch_id = _new_batch(setup_db, run_id, source_instances_selected=2, max_extracted_bytes=1_000_000)

    source_root = Path(tempfile.mkdtemp()) / "source"
    source_root.mkdir(parents=True)
    archive_1_path = source_root / "one.zip"
    _write_zip(archive_1_path, {"one.txt": "archive one content"})
    archive_2_path = source_root / "two.zip"
    _write_zip(archive_2_path, {"two.txt": "archive two content"})
    _new_archive_instance(setup_db, run_id, archive_1_path)
    _new_archive_instance(setup_db, run_id, archive_2_path)
    setup_db.close()

    workspace_root = Path(tempfile.mkdtemp()) / "workspace"
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[Exception] = []

    def good_worker():
        thread_db = Session(engine)
        try:
            barrier.wait()
            results["good"] = ArchiveProcessingService(thread_db).process_next_archive(
                worker_id="worker-good", workspace_root=workspace_root, classification_run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            thread_db.close()

    def crashing_worker():
        thread_db = Session(engine)
        service = ArchiveProcessingService(thread_db)
        real_preflight_admit = service._preflight_admit
        call_count = {"n": 0}

        def flaky_preflight_admit(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated worker crash before extraction begins")
            return real_preflight_admit(*args, **kwargs)

        service._preflight_admit = flaky_preflight_admit
        try:
            barrier.wait()
            service.process_next_archive(worker_id="worker-crash", workspace_root=workspace_root, classification_run_id=run_id)
        except RuntimeError as exc:
            results["crashed"] = exc
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=good_worker), threading.Thread(target=crashing_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, f"unexpected leaked exceptions: {errors}"
        assert results.get("good") is not None, "the non-crashing worker must have succeeded on its own archive"
        assert "crashed" in results, "the crashing worker's simulated exception must have propagated (a real crash)"

        verify_db = Session(engine)
        # The crashed claim must have been released (finally-block
        # cleanup), leaving it claimable again.
        remaining = ArchiveProcessingService(verify_db).process_next_archive(
            worker_id="worker-retry", workspace_root=workspace_root, classification_run_id=run_id
        )
        assert remaining is not None, "the crashed archive must be cleanly re-claimable and now succeed"
        verify_db.close()

        # Idempotency (scenario 8): a further call finds nothing left.
        idle_db = Session(engine)
        assert (
            ArchiveProcessingService(idle_db).process_next_archive(
                worker_id="worker-idle", workspace_root=workspace_root, classification_run_id=run_id
            )
            is None
        )
        idle_db.close()

        # No leaked staging directories for either archive.
        assert not any(workspace_root.glob("_staging/*")) or all(
            not any(p.iterdir()) for p in workspace_root.glob("_staging/*") if p.is_dir()
        )

        # Batch-level accounting reconciles: two SUCCEEDED attempts total.
        count_db = Session(engine)
        succeeded = (
            count_db.execute(
                text(
                    "SELECT count(*) FROM ingestion_attempts ia JOIN source_instances si ON si.id = ia.source_instance_id "
                    "WHERE si.classification_run_id = :rid AND ia.outcome = 'SUCCEEDED'"
                ),
                {"rid": run_id},
            ).scalar()
        )
        assert succeeded == 2
        count_db.close()
    finally:
        _cleanup_runs(engine, [run_id], [discovery_id])
        engine.dispose()


# ============================================================
# 5. Cross-batch identical-content convergence with concurrent
#    completion.
# ============================================================


def test_cross_batch_convergence_with_concurrent_completion() -> None:
    """Two batches, two SourceInstances with IDENTICAL content
    (converging on one ContentIdentityGroup after concurrent identity
    resolution), carried through to INGESTED, then both batches'
    check_and_complete() called concurrently. Both must legitimately
    complete - each batch's own denominator only cares about its own
    selected SourceInstance having reached a terminal outcome,
    independent of which batch's worker actually got charged for the
    shared embedding reservation."""
    engine = _engine()
    setup_db = Session(engine)
    discovery_id = _new_discovery_run(setup_db)
    run_a = _new_classification_run(setup_db, discovery_id)
    run_b = _new_classification_run(setup_db, discovery_id)
    batch_a = _new_batch(setup_db, run_a, source_instances_selected=1, max_embeddings=100)
    batch_b = _new_batch(setup_db, run_b, source_instances_selected=1, max_embeddings=100)
    assert batch_a < batch_b

    source_root = Path(tempfile.mkdtemp()) / "source"
    source_root.mkdir(parents=True)
    identical_content = "identical content shared across two batches"
    path_a = source_root / "a.txt"
    path_a.write_text(identical_content)
    path_b = source_root / "b.txt"
    path_b.write_text(identical_content)
    instance_a_id = _new_loose_instance(setup_db, run_a, path_a)
    instance_b_id = _new_loose_instance(setup_db, run_b, path_b)
    setup_db.close()

    workspace_root = Path(tempfile.mkdtemp()) / "workspace"
    barrier = threading.Barrier(2)
    resolve_errors: list[Exception] = []

    def resolve_worker(label: str, run_id: int):
        thread_db = Session(engine)
        try:
            barrier.wait()
            IdentityResolutionService(thread_db).resolve_next(
                worker_id=label, workspace_root=workspace_root, classification_run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            resolve_errors.append(exc)
        finally:
            thread_db.close()

    threads = [
        threading.Thread(target=resolve_worker, args=("resolver-a", run_a)),
        threading.Thread(target=resolve_worker, args=("resolver-b", run_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not resolve_errors, f"unexpected leaked exceptions during resolution: {resolve_errors}"

        verify_db = Session(engine)
        instance_a = verify_db.get(SourceInstance, instance_a_id)
        instance_b = verify_db.get(SourceInstance, instance_b_id)
        assert instance_a.content_identity_group_id is not None
        # Convergence: identical bytes resolve to the SAME group.
        assert instance_a.content_identity_group_id == instance_b.content_identity_group_id
        group_id = instance_a.content_identity_group_id
        verify_db.close()

        # Sequential (globally-claimed/exclusive anyway) normalize -> chunk -> embed.
        NormalizationService(Session(engine)).normalize_next(worker_id="worker-norm", workspace_root=workspace_root)
        ChunkingService(Session(engine)).chunk_next(worker_id="worker-chunk", workspace_root=workspace_root)
        PipelineEmbeddingService(Session(engine), embedding_client=FakeEmbeddingClient()).embed_next(worker_id="worker-embed")

        check_db = Session(engine)
        final_group = check_db.get(ContentIdentityGroup, group_id)
        assert final_group.pipeline_state == ContentPipelineState.INGESTED
        check_db.close()

        # Now race BOTH batches' completion concurrently.
        barrier2 = threading.Barrier(2)
        completion_results: dict[str, object] = {}
        completion_errors: list[Exception] = []

        def complete_worker(label: str, batch_id: int):
            thread_db = Session(engine)
            try:
                barrier2.wait()
                completion_results[label] = BatchCompletionReconciliationService(thread_db).check_and_complete(batch_id)
            except Exception as exc:  # noqa: BLE001
                completion_errors.append(exc)
            finally:
                thread_db.close()

        threads2 = [
            threading.Thread(target=complete_worker, args=("a", batch_a)),
            threading.Thread(target=complete_worker, args=("b", batch_b)),
        ]
        for t in threads2:
            t.start()
        for t in threads2:
            t.join()

        assert not completion_errors, f"unexpected leaked exceptions during completion: {completion_errors}"
        final_db = Session(engine)
        final_a = final_db.get(IngestionBatch, batch_a)
        final_b = final_db.get(IngestionBatch, batch_b)
        assert final_a.status == BatchStatus.COMPLETED
        assert final_a.stop_reason == BatchStopReason.SOURCE_WORK_EXHAUSTED
        assert final_b.status == BatchStatus.COMPLETED
        assert final_b.stop_reason == BatchStopReason.SOURCE_WORK_EXHAUSTED
        final_db.close()
    finally:
        _cleanup_runs(engine, [run_a, run_b], [discovery_id])
        engine.dispose()


# ============================================================
# 6. Pause -> hard-stop/abort -> reconciliation: state-gate is
#    authoritative over arithmetic, not just an additional check.
# ============================================================


def test_pause_hard_stop_abort_then_reconciliation_is_a_safe_no_op(db: Session) -> None:
    discovery_id = _new_discovery_run(db)
    run_id = _new_classification_run(db, discovery_id)
    batch_id = _new_batch(db, run_id, source_instances_selected=1)

    ingested_group_id = _new_group(db, ContentPipelineState.INGESTED)
    IngestionAttemptService(db).record_pipeline_attempt(
        content_identity_group_id=ingested_group_id,
        attempted_stage=IngestionAttemptStage.EMBEDDING,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.SUCCEEDED,
    )
    db.add(
        SourceInstance(
            classification_run_id=run_id,
            root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
            evidence_snapshot={},
            source_category=SourceCategory.LOOSE_FILE,
            content_identity_group_id=ingested_group_id,
        )
    )
    db.commit()

    control = BatchControlService(db)
    paused = control.pause(batch_id, reason=BatchStopReason.MANUAL_PAUSE)
    assert paused.applied is True

    resumed = control.resume(batch_id, guard=_guard_hard_stop())
    assert resumed.applied is True  # the abort() call itself applies
    assert resumed.batch.status == BatchStatus.ABORTED
    assert resumed.batch.stop_reason == BatchStopReason.WORKSPACE_HARD_STOP

    # Arithmetically, every selected row IS terminal - but the state
    # gate must win: reconciliation must be a no-op against an ABORTED
    # batch, never "helpfully" completing it anyway.
    result = BatchCompletionReconciliationService(db).check_and_complete(batch_id)
    assert result is None


# ============================================================
# 7. Live BatchReportService reads during multi-stage activity.
# ============================================================


def test_live_report_reads_never_raise_during_multi_stage_activity() -> None:
    engine = _engine()
    setup_db = Session(engine)
    discovery_id = _new_discovery_run(setup_db)
    run_id = _new_classification_run(setup_db, discovery_id)
    batch_id = _new_batch(setup_db, run_id, source_instances_selected=3)

    source_root = Path(tempfile.mkdtemp()) / "source"
    source_root.mkdir(parents=True)
    loose_path = source_root / "loose.txt"
    loose_path.write_text("loose content for live reporting")
    archive_path = source_root / "archive.zip"
    _write_zip(archive_path, {"member.txt": "archive member content"})
    _new_loose_instance(setup_db, run_id, loose_path)
    _new_archive_instance(setup_db, run_id, archive_path)

    pre_chunked_group_id = _new_group(setup_db, ContentPipelineState.CHUNKED)
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    document = Document(
        title="pre.txt", source="/synthetic/pre.txt", source_type="txt", content_hash=_unique_hash(), content_identity_group_id=pre_chunked_group_id
    )
    setup_db.add(document)
    setup_db.commit()
    setup_db.refresh(document)
    setup_db.add(DocumentChunk(document_id=document.id, chunk_index=0, content="pre-existing chunk", embedding=None))
    setup_db.commit()
    setup_db.add(
        SourceInstance(
            classification_run_id=run_id,
            root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
            evidence_snapshot={},
            source_category=SourceCategory.LOOSE_FILE,
            content_identity_group_id=pre_chunked_group_id,
        )
    )
    setup_db.commit()
    setup_db.close()

    workspace_root = Path(tempfile.mkdtemp()) / "workspace"
    stop_flag = threading.Event()
    report_errors: list[Exception] = []
    invariant_violations: list[Exception] = []
    stage_errors: list[Exception] = []

    def reporter():
        while not stop_flag.is_set():
            thread_db = Session(engine)
            try:
                BatchReportService(thread_db).generate_report(batch_id)
            except BatchReportInvariantViolation as exc:
                invariant_violations.append(exc)
            except Exception as exc:  # noqa: BLE001
                report_errors.append(exc)
            finally:
                thread_db.close()
            time.sleep(0.005)

    def identity_worker():
        thread_db = Session(engine)
        try:
            IdentityResolutionService(thread_db).resolve_next(
                worker_id="worker-id", workspace_root=workspace_root, classification_run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            stage_errors.append(exc)
        finally:
            thread_db.close()

    def archive_worker():
        thread_db = Session(engine)
        try:
            ArchiveProcessingService(thread_db).process_next_archive(
                worker_id="worker-archive", workspace_root=workspace_root, classification_run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001
            stage_errors.append(exc)
        finally:
            thread_db.close()

    def embedding_worker():
        thread_db = Session(engine)
        try:
            PipelineEmbeddingService(thread_db, embedding_client=FakeEmbeddingClient()).embed_next(worker_id="worker-embed")
        except Exception as exc:  # noqa: BLE001
            stage_errors.append(exc)
        finally:
            thread_db.close()

    reporter_thread = threading.Thread(target=reporter)
    reporter_thread.start()

    stage_threads = [
        threading.Thread(target=identity_worker),
        threading.Thread(target=archive_worker),
        threading.Thread(target=embedding_worker),
    ]
    for t in stage_threads:
        t.start()
    for t in stage_threads:
        t.join()

    stop_flag.set()
    reporter_thread.join()

    try:
        assert not invariant_violations, f"BatchReportInvariantViolation raised on legitimate transient data: {invariant_violations}"
        assert not report_errors, f"unexpected report exceptions: {report_errors}"
        assert not stage_errors, f"unexpected stage exceptions: {stage_errors}"

        final_db = Session(engine)
        final_report = BatchReportService(final_db).generate_report(batch_id)
        assert final_report.attempted_source_count >= 2  # loose + archive, at minimum
        final_db.close()
    finally:
        _cleanup_runs(engine, [run_id], [discovery_id])
        engine.dispose()
