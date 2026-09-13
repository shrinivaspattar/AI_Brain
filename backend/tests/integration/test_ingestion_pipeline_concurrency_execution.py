"""The explicitly-required real-database concurrency proof for the
Controlled Ingestion Implementation gate, at the PIPELINE level (not
just the low-level WorkerClaimService primitives already proven in
`ce50875`): concurrent workers must never process the same work item
twice, across every stage of the pipeline. Real Postgres, real
threads, never mocked - matching the standard already applied
throughout this project (Chain 1's execute() race test,
ContentIdentityService.get_or_create_group, ce50875's claim tests).

No T7 access: every source path is synthetic, under a test's own
tmp_path.
"""

import threading
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.archive_processing_service import ArchiveProcessingService
from app.classification.identity_resolution_service import IdentityResolutionService
from app.classification.normalization_service import NormalizationService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import ContentIdentityGroup
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.document import Document
from app.models.ingestion_attempt import IngestionAttempt
from app.models.provenance_link import ProvenanceLink
from app.models.source_instance import SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _setup_run(engine) -> int:
    with Session(engine) as db:
        discovery = DiscoveryRun(
            run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
            source_root="/synthetic/not-a-real-t7-path",
            report_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            run_started_at=datetime.now(UTC),
            run_completed_at=datetime.now(UTC),
        )
        db.add(discovery)
        db.commit()
        run = ClassificationRun(
            classifier_version="concurrency-test-v1",
            d1_discovery_run_id=discovery.id,
            started_at=datetime.now(UTC),
        )
        db.add(run)
        db.commit()
        return run.id


def _cleanup(engine, classification_run_id: int) -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "DELETE FROM document_chunks WHERE document_id IN ("
                "  SELECT id FROM documents WHERE content_identity_group_id IN ("
                "    SELECT content_identity_group_id FROM source_instances "
                "    WHERE classification_run_id = :cr AND content_identity_group_id IS NOT NULL"
                "  )"
                ")"
            ),
            {"cr": classification_run_id},
        )
        conn.execute(
            text(
                "DELETE FROM documents WHERE content_identity_group_id IN ("
                "  SELECT content_identity_group_id FROM source_instances "
                "  WHERE classification_run_id = :cr AND content_identity_group_id IS NOT NULL"
                ")"
            ),
            {"cr": classification_run_id},
        )
        conn.execute(
            text(
                "DELETE FROM ingestion_attempts WHERE source_instance_id IN ("
                "  SELECT id FROM source_instances WHERE classification_run_id = :cr"
                ") OR content_identity_group_id IN ("
                "  SELECT content_identity_group_id FROM source_instances "
                "  WHERE classification_run_id = :cr AND content_identity_group_id IS NOT NULL"
                ")"
            ),
            {"cr": classification_run_id},
        )
        conn.execute(
            text(
                "DELETE FROM provenance_links WHERE source_instance_id IN ("
                "  SELECT id FROM source_instances WHERE classification_run_id = :cr"
                ")"
            ),
            {"cr": classification_run_id},
        )
        group_ids_row = conn.execute(
            text(
                "SELECT content_identity_group_id FROM source_instances "
                "WHERE classification_run_id = :cr AND content_identity_group_id IS NOT NULL"
            ),
            {"cr": classification_run_id},
        ).all()
        group_ids = [row[0] for row in group_ids_row]
        conn.execute(
            text("DELETE FROM source_instances WHERE classification_run_id = :cr"),
            {"cr": classification_run_id},
        )
        if group_ids:
            # NOT a blind delete-by-id: test content is unique per
            # invocation (a uuid is embedded in every synthetic file's
            # content below), so a group should never legitimately be
            # shared across runs - but guard with NOT EXISTS anyway so
            # cleanup never raises an FK error if it somehow is.
            conn.execute(
                text(
                    "DELETE FROM content_identity_groups WHERE id = ANY(:ids) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM source_instances WHERE content_identity_group_id = content_identity_groups.id"
                    ")"
                ),
                {"ids": group_ids},
            )
        run_row = conn.execute(
            text("SELECT d1_discovery_run_id FROM classification_runs WHERE id = :cr"),
            {"cr": classification_run_id},
        ).first()
        conn.execute(text("DELETE FROM classification_runs WHERE id = :cr"), {"cr": classification_run_id})
        if run_row is not None:
            conn.execute(
                text("DELETE FROM discovery_runs WHERE id = :d"), {"d": run_row[0]}
            )
        conn.commit()


def test_concurrent_identity_resolution_workers_each_resolve_a_distinct_instance(
    tmp_path,
) -> None:
    """Ten concurrent IdentityResolutionService workers, ten distinct
    unresolved loose files. Required outcome: every instance resolved
    exactly once, no instance processed by two workers, no instance
    left unprocessed."""
    engine = _engine()
    classification_run_id = _setup_run(engine)
    instance_ids: list[int] = []

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for i in range(10):
        file_path = source_dir / f"doc_{i}.txt"
        file_path.write_text(f"synthetic content number {i} - {uuid.uuid4()}")
        with Session(engine) as db:
            instance = SourceInstance(
                classification_run_id=classification_run_id,
                root_t7_path=str(file_path),
                evidence_snapshot={},
            )
            db.add(instance)
            db.commit()
            instance_ids.append(instance.id)

    workspace = tmp_path / "workspace"
    sessions = [Session(engine) for _ in range(10)]
    results: dict[int, object] = {}
    barrier = threading.Barrier(10)

    def run(index: int, db: Session) -> None:
        barrier.wait()
        resolved = IdentityResolutionService(db).resolve_next(
            worker_id=f"worker-{index}", workspace_root=workspace, lease_duration=timedelta(minutes=10)
        )
        results[index] = resolved.id if resolved is not None else None

    threads = [threading.Thread(target=run, args=(i, sessions[i])) for i in range(10)]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert all(not t.is_alive() for t in threads), "a thread did not finish"

        resolved_ids = [v for v in results.values() if v is not None]
        assert sorted(resolved_ids) == sorted(instance_ids), (
            f"expected all 10 instances resolved exactly once total, got {resolved_ids}"
        )
        assert len(resolved_ids) == len(set(resolved_ids)), "an instance was resolved twice"

        with engine.connect() as verify_conn:
            resolved_count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM source_instances "
                    "WHERE classification_run_id = :cr AND content_identity_group_id IS NOT NULL"
                ),
                {"cr": classification_run_id},
            ).scalar_one()
        assert resolved_count == 10
    finally:
        for s in sessions:
            s.close()
        _cleanup(engine, classification_run_id)
        engine.dispose()


def test_concurrent_archive_processing_workers_each_own_a_distinct_archive(tmp_path) -> None:
    """Six concurrent ArchiveProcessingService workers, six distinct
    unprocessed archives. Every archive processed exactly once - no
    archive claimed by two workers, no duplicate member creation across
    workers."""
    engine = _engine()
    classification_run_id = _setup_run(engine)
    archive_instance_ids: list[int] = []

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for i in range(6):
        archive_path = source_dir / f"archive_{i}.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("member.txt", f"content for archive {i} - {uuid.uuid4()}")
        with Session(engine) as db:
            instance = SourceInstance(
                classification_run_id=classification_run_id,
                root_t7_path=str(archive_path),
                evidence_snapshot={},
            )
            db.add(instance)
            db.commit()
            archive_instance_ids.append(instance.id)

    workspace = tmp_path / "workspace"
    sessions = [Session(engine) for _ in range(6)]
    results: dict[int, object] = {}
    barrier = threading.Barrier(6)

    def run(index: int, db: Session) -> None:
        barrier.wait()
        result = ArchiveProcessingService(db).process_next_archive(
            worker_id=f"worker-{index}", workspace_root=workspace, lease_duration=timedelta(minutes=10)
        )
        results[index] = result.id if result is not None else None

    threads = [threading.Thread(target=run, args=(i, sessions[i])) for i in range(6)]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert all(not t.is_alive() for t in threads), "a thread did not finish"

        processed_ids = [v for v in results.values() if v is not None]
        assert sorted(processed_ids) == sorted(archive_instance_ids)
        assert len(processed_ids) == len(set(processed_ids)), "an archive was claimed twice"

        with engine.connect() as verify_conn:
            member_count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM source_instances "
                    "WHERE classification_run_id = :cr AND member_path IS NOT NULL"
                ),
                {"cr": classification_run_id},
            ).scalar_one()
        # Exactly one member per archive - never duplicated.
        assert member_count == 6
    finally:
        for s in sessions:
            s.close()
        _cleanup(engine, classification_run_id)
        engine.dispose()


def test_concurrent_normalization_workers_each_own_a_distinct_group_no_duplicate_documents(
    tmp_path,
) -> None:
    """Eight concurrent NormalizationService workers, eight distinct
    EXTRACTED groups. Every group normalized exactly once - exactly one
    Document created per group, never two racing to create the same
    one (which the UNIQUE content_identity_group_id constraint would
    reject, but the service must handle that cleanly, not crash)."""
    engine = _engine()
    classification_run_id = _setup_run(engine)

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    workspace = tmp_path / "workspace"
    group_ids: list[int] = []

    for i in range(8):
        file_path = source_dir / f"doc_{i}.txt"
        file_path.write_text(f"distinct normalization content {i} - {uuid.uuid4()}")
        with Session(engine) as db:
            instance = SourceInstance(
                classification_run_id=classification_run_id,
                root_t7_path=str(file_path),
                evidence_snapshot={},
            )
            db.add(instance)
            db.commit()
            resolved = IdentityResolutionService(db).resolve_next(
                worker_id="setup-worker", workspace_root=workspace
            )
            group_ids.append(resolved.content_identity_group_id)

    sessions = [Session(engine) for _ in range(8)]
    errors: dict[int, Exception] = {}
    barrier = threading.Barrier(8)

    def run(index: int, db: Session) -> None:
        barrier.wait()
        try:
            NormalizationService(db).normalize_next(
                worker_id=f"worker-{index}", workspace_root=workspace, lease_duration=timedelta(minutes=10)
            )
        except Exception as exc:  # noqa: BLE001
            errors[index] = exc

    threads = [threading.Thread(target=run, args=(i, sessions[i])) for i in range(8)]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert all(not t.is_alive() for t in threads), "a thread did not finish"
        assert errors == {}, f"expected no errors, got {errors}"

        with engine.connect() as verify_conn:
            document_count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM documents WHERE content_identity_group_id = ANY(:ids)"
                ),
                {"ids": group_ids},
            ).scalar_one()
        assert document_count == 8
    finally:
        for s in sessions:
            s.close()
        _cleanup(engine, classification_run_id)
        engine.dispose()
