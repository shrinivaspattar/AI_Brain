"""Real-database, synthetic-fixture-only tests for the Controlled
Ingestion Implementation gate: proves the full chain

    SourceInstance -> identity resolution -> ContentIdentityGroup
    -> claim -> extract -> normalize -> Document -> chunk -> embed
    -> INGESTED

end to end, plus each of the nine required scenarios named in the
authorization message. No T7 access anywhere in this file: every
`root_t7_path` is a path under a test's own `tmp_path`, standing in for
what would be a real T7 path in production. No embeddings are ever
computed by a real model - a small deterministic fake stands in for
`EmbeddingClient` throughout.
"""

import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.archive_processing_service import ArchiveProcessingService
from app.classification.chunking_service import ChunkingService
from app.classification.identity_resolution_service import IdentityResolutionService
from app.classification.normalization_service import NormalizationService
from app.classification.pipeline_embedding_service import PipelineEmbeddingService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome, IngestionFailureCode
from app.models.provenance_link import ProvenanceLink
from app.models.source_instance import SourceInstance

_EMBEDDING_DIMENSIONS = settings.EMBEDDING_DIMENSIONS


class FakeEmbeddingClient:
    """Deterministic, no-network stand-in for EmbeddingClient - never
    calls a real embedding model, matching the "no real T7 embeddings"
    hard boundary and keeping these tests hermetic/fast."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t) % 7) / 7.0] * _EMBEDDING_DIMENSIONS for t in texts]


class FailingEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding backend unreachable (simulated)")


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
        classifier_version="test-classifier-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _loose_instance(db: Session, classification_run: ClassificationRun, path) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path=str(path),
        evidence_snapshot={},
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _archive_instance(db: Session, classification_run: ClassificationRun, path) -> SourceInstance:
    return _loose_instance(db, classification_run, path)


# -- Identity resolution -----------------------------------------------------


def test_identity_resolution_succeeds_for_eligible_loose_file(db: Session, tmp_path) -> None:
    source_file = tmp_path / "source" / "notes.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("hello synthetic world")

    run = _classification_run(db)
    instance = _loose_instance(db, run, source_file)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    assert resolved is not None
    assert resolved.id == instance.id
    assert resolved.content_identity_group_id is not None
    assert resolved.claimed_by is None  # released

    group = db.get(ContentIdentityGroup, resolved.content_identity_group_id)
    assert group.pipeline_state == ContentPipelineState.EXTRACTED


def test_identity_resolution_excluded_for_c9r(db: Session, tmp_path) -> None:
    source_file = tmp_path / "source" / "vault_chunk.c9r"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"ciphertext-not-real-c9r-bytes")

    run = _classification_run(db)
    _loose_instance(db, run, source_file)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    group = db.get(ContentIdentityGroup, resolved.content_identity_group_id)
    assert group.pipeline_state == ContentPipelineState.EXCLUDED
    # Never content-ingested: no workspace content copy for an EXCLUDED group.
    assert not (tmp_path / "workspace" / f"group_{group.id}").exists()


def test_identity_resolution_unsupported_for_known_binary_format(db: Session, tmp_path) -> None:
    source_file = tmp_path / "source" / "photo.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")

    run = _classification_run(db)
    _loose_instance(db, run, source_file)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    group = db.get(ContentIdentityGroup, resolved.content_identity_group_id)
    assert group.pipeline_state == ContentPipelineState.UNSUPPORTED


def test_identity_resolution_t7_unavailable_simulation_leaves_source_represented(
    db: Session, tmp_path
) -> None:
    """The source path never exists (simulating the T7 being
    disconnected/unavailable at read time). Required outcome: the
    SourceInstance row is NOT deleted or mutated destructively - it
    remains, still unresolved, with a durable FAILED IngestionAttempt
    explaining why - never silently dropped."""
    missing_path = tmp_path / "source" / "gone.txt"  # never created

    run = _classification_run(db)
    instance = _loose_instance(db, run, missing_path)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    assert resolved.id == instance.id
    assert resolved.content_identity_group_id is None  # still unresolved
    # The source is still represented - nothing destructive happened.
    still_there = db.get(SourceInstance, instance.id)
    assert still_there is not None
    assert still_there.root_t7_path == str(missing_path)

    attempt = (
        db.query(IngestionAttempt)
        .filter(IngestionAttempt.source_instance_id == instance.id)
        .one()
    )
    assert attempt.outcome == IngestionAttemptOutcome.FAILED
    assert attempt.failure_code == IngestionFailureCode.T7_UNAVAILABLE
    assert attempt.retryable is True


def test_identity_resolution_returns_none_when_no_work_available(db: Session, tmp_path) -> None:
    result = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )
    assert result is None


def test_same_identity_from_two_source_instances_converges_on_one_group(
    db: Session, tmp_path
) -> None:
    """The first required scenario: same identity -> one ContentIdentityGroup.
    Two DIFFERENT loose files with IDENTICAL bytes must resolve to the
    SAME ContentIdentityGroup - content identity is about the bytes,
    never about the path."""
    run = _classification_run(db)

    file_a = tmp_path / "source" / "copy_a.txt"
    file_b = tmp_path / "source" / "copy_b.txt"
    file_a.parent.mkdir(parents=True)
    file_a.write_text("identical content in two different places")
    file_b.write_text("identical content in two different places")

    _loose_instance(db, run, file_a)
    _loose_instance(db, run, file_b)

    service = IdentityResolutionService(db)
    resolved_a = service.resolve_next(worker_id="worker-a", workspace_root=tmp_path / "workspace")
    resolved_b = service.resolve_next(worker_id="worker-a", workspace_root=tmp_path / "workspace")

    assert resolved_a.content_identity_group_id == resolved_b.content_identity_group_id

    count = (
        db.query(ContentIdentityGroup)
        .filter(ContentIdentityGroup.id == resolved_a.content_identity_group_id)
        .count()
    )
    assert count == 1


# -- Archive / nested archive processing -------------------------------------


def test_archive_processing_creates_members_and_resolves_leaf_identity(
    db: Session, tmp_path
) -> None:
    archive_path = tmp_path / "source" / "outer.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("a.txt", "content A")
        zf.writestr("b.txt", "content B")

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, archive_path)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    assert result.id == root_instance.id
    members = (
        db.query(SourceInstance)
        .filter(
            SourceInstance.classification_run_id == run.id,
            SourceInstance.id != root_instance.id,
        )
        .all()
    )
    assert {m.member_path for m in members} == {"a.txt", "b.txt"}
    assert all(m.content_identity_group_id is not None for m in members)

    attempt = (
        db.query(IngestionAttempt)
        .filter(IngestionAttempt.source_instance_id == root_instance.id)
        .one()
    )
    assert attempt.outcome == IngestionAttemptOutcome.SUCCEEDED


def test_nested_archive_creates_full_provenance_chain_and_resolves_deep_member(
    db: Session, tmp_path
) -> None:
    inner_zip = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner_zip, "w") as zf:
        zf.writestr("deep.txt", "deeply nested content")

    archive_path = tmp_path / "source" / "outer.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("top.txt", "top level content")
        zf.write(inner_zip, "nested.zip")

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, archive_path)

    ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    nested_archive_instance = (
        db.query(SourceInstance)
        .filter(SourceInstance.member_path == "nested.zip")
        .one()
    )
    deep_instance = (
        db.query(SourceInstance)
        .filter(SourceInstance.member_path == "nested.zip/deep.txt")
        .one()
    )

    # The nested archive is a container - never gets its own identity.
    assert nested_archive_instance.content_identity_group_id is None
    # The leaf inside it IS resolved.
    assert deep_instance.content_identity_group_id is not None

    links = (
        db.query(ProvenanceLink)
        .filter(ProvenanceLink.source_instance_id == deep_instance.id)
        .order_by(ProvenanceLink.sequence_index)
        .all()
    )
    assert [link.path for link in links] == [str(archive_path), "nested.zip", "deep.txt"]


def test_identity_resolution_never_claims_a_fully_processed_root_archive(
    db: Session, tmp_path
) -> None:
    """Regression test for a real bug found during the first real-T7
    ingestion pilot: a root-level archive SourceInstance legitimately
    and permanently keeps content_identity_group_id IS NULL even after
    being fully, successfully processed (its raw container bytes are
    never "content"). Before this fix, claim_source_instance_for_
    identity_resolution had no suffix exclusion, so once every other
    unresolved loose file was exhausted it would wrongly claim the
    archive's own row and hash its raw compressed bytes as if they
    were document content - producing a bogus ContentIdentityGroup
    that then failed at normalization with CORRUPT_INPUT. This proves
    a second, later call to resolve_next() (e.g. an idempotency /
    "is there more work" check) finds nothing to do, rather than
    wrongly claiming the archive."""
    archive_path = tmp_path / "source" / "outer.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("a.txt", "content A")

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, archive_path)

    ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )
    db.refresh(root_instance)
    assert root_instance.content_identity_group_id is None  # correct, permanent

    result = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )
    assert result is None

    group_count_before = db.query(ContentIdentityGroup).count()
    assert group_count_before == 1  # only "a.txt"'s group - no bogus archive group


def test_identity_resolution_never_claims_a_nested_archives_own_instance(
    db: Session, tmp_path
) -> None:
    """A nested archive's own member-row also permanently keeps
    content_identity_group_id IS NULL (it is itself a container, not a
    leaf) - proving the member_path exclusion, not just the root-level
    suffix exclusion, closes the hazard for archives discovered during
    extraction too."""
    inner_zip = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner_zip, "w") as zf:
        zf.writestr("deep.txt", "deeply nested content")

    archive_path = tmp_path / "source" / "outer.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.write(inner_zip, "nested.zip")

    run = _classification_run(db)
    _archive_instance(db, run, archive_path)

    ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    nested_archive_instance = (
        db.query(SourceInstance).filter(SourceInstance.member_path == "nested.zip").one()
    )
    assert nested_archive_instance.content_identity_group_id is None  # correct, permanent

    result = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )
    assert result is None


def test_archive_crash_halfway_is_resumable_without_duplicate_members(
    db: Session, tmp_path
) -> None:
    """Simulates a crash: one member ("a.txt") was already discovered
    (its SourceInstance + ProvenanceLink already created) by an earlier
    attempt that crashed before finishing. Required outcome: re-running
    archive processing must NOT create a second "a.txt" SourceInstance,
    and must still complete the rest of the archive (and finish
    resolving "a.txt"'s identity, since that part hadn't happened yet).
    """
    archive_path = tmp_path / "source" / "outer.zip"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("a.txt", "content A")
        zf.writestr("b.txt", "content B")

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, archive_path)

    # Simulate the partial prior attempt.
    from app.classification.source_instance_service import ProvenanceStep, SourceInstanceService
    from app.models.provenance_link import ProvenanceLinkKind

    partial = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path=str(archive_path),
        member_path="a.txt",
        evidence_snapshot={"discovered_during_extraction": True, "parent_source_instance_id": root_instance.id},
        chain=[
            ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=str(archive_path)),
            ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="a.txt"),
        ],
    )
    assert partial.content_identity_group_id is None  # crash landed before identity resolution

    ArchiveProcessingService(db).process_next_archive(
        worker_id="resume-worker", workspace_root=tmp_path / "workspace"
    )

    members = (
        db.query(SourceInstance)
        .filter(
            SourceInstance.classification_run_id == run.id,
            SourceInstance.id != root_instance.id,
        )
        .all()
    )
    assert len(members) == 2, f"expected exactly 2 member rows (no duplicate), got {len(members)}"
    assert partial.id in [m.id for m in members]

    db.refresh(partial)
    assert partial.content_identity_group_id is not None  # now completed


def test_nested_archive_crash_resume_across_three_levels(db: Session, tmp_path) -> None:
    """The synthetic-only pre-real-T7 review's required deeper case:

        A.zip
          `-- B.zip
               |-- file1.txt
               |-- file2.txt
               `-- C.zip
                    `-- file3.txt

    Simulates a crash after PARTIAL member creation: B.zip's own
    SourceInstance and file1.txt's SourceInstance + identity were
    already created and resolved by an earlier, crashed attempt;
    file2.txt and C.zip (and file3.txt inside it) were not yet
    discovered at all. Required outcomes on resume, all checked
    explicitly: existing members (B.zip, file1.txt) are REUSED, not
    duplicated; only the missing members (file2.txt, C.zip, file3.txt)
    are created; no duplicate SourceInstances anywhere; no duplicate
    ProvenanceLinks for the pre-existing member; file1.txt's already-
    recorded evidence_snapshot is byte-for-byte unchanged; the full,
    correct 4-link provenance chain exists for the newly-discovered
    deepest file (file3.txt); and a second, immediate re-run (with
    nothing left to do) is itself idempotent - no further changes,
    still succeeds.
    """
    from app.classification.content_identity_service import ContentIdentityService
    from app.classification.source_instance_service import ProvenanceStep, SourceInstanceService
    from app.models.content_identity_group import ContentIdentityAlgorithm, ContentIdentityKind, ContentPipelineState
    from app.models.provenance_link import ProvenanceLinkKind

    c_zip = tmp_path / "C.zip"
    with zipfile.ZipFile(c_zip, "w") as zf:
        zf.writestr("file3.txt", "content of file 3")

    b_zip = tmp_path / "B.zip"
    with zipfile.ZipFile(b_zip, "w") as zf:
        zf.writestr("file1.txt", "content of file 1")
        zf.writestr("file2.txt", "content of file 2")
        zf.write(c_zip, "C.zip")

    a_zip = tmp_path / "source" / "A.zip"
    a_zip.parent.mkdir(parents=True)
    with zipfile.ZipFile(a_zip, "w") as zf:
        zf.write(b_zip, "B.zip")

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, a_zip)

    # Simulate the partial prior attempt: B.zip discovered, file1.txt
    # discovered AND identity-resolved; nothing else yet.
    b_instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path=str(a_zip),
        member_path="B.zip",
        evidence_snapshot={"discovered_during_extraction": True, "parent_source_instance_id": root_instance.id},
        chain=[
            ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=str(a_zip)),
            ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="B.zip"),
        ],
    )
    file1_instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path=str(a_zip),
        member_path="B.zip/file1.txt",
        evidence_snapshot={
            "discovered_during_extraction": True,
            "parent_source_instance_id": b_instance.id,
            "pre_crash_marker": "must-survive-unchanged",
        },
        chain=[
            ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=str(a_zip)),
            ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="B.zip"),
            ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="file1.txt"),
        ],
    )
    import hashlib

    file1_hash = hashlib.sha256(b"content of file 1").hexdigest()
    group1 = ContentIdentityService(db).get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=file1_hash,
        initial_pipeline_state=ContentPipelineState.EXTRACTED,
    )
    ContentIdentityService(db).assign_content_identity(file1_instance.id, group1)

    pre_existing_snapshot = dict(file1_instance.evidence_snapshot)
    pre_existing_file1_link_ids = sorted(
        link.id
        for link in db.query(ProvenanceLink).filter(
            ProvenanceLink.source_instance_id == file1_instance.id
        )
    )
    workspace = tmp_path / "workspace"

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="resume-worker", workspace_root=workspace
    )
    assert result.id == root_instance.id

    all_instances = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == run.id)
        .all()
    )
    member_paths = [si.member_path for si in all_instances if si.member_path]
    # No duplicates anywhere.
    assert len(member_paths) == len(set(member_paths)), f"duplicate member(s) in {member_paths}"
    assert set(member_paths) == {"B.zip", "B.zip/file1.txt", "B.zip/file2.txt", "B.zip/C.zip", "B.zip/C.zip/file3.txt"}

    # Pre-existing members REUSED, not duplicated (same row ids).
    db.refresh(b_instance)
    db.refresh(file1_instance)
    by_path = {si.member_path: si for si in all_instances}
    assert by_path["B.zip"].id == b_instance.id
    assert by_path["B.zip/file1.txt"].id == file1_instance.id

    # evidence_snapshot on the pre-existing member is byte-for-byte unchanged.
    assert file1_instance.evidence_snapshot == pre_existing_snapshot

    # No duplicate ProvenanceLinks for the pre-existing member.
    file1_link_ids_after = sorted(
        link.id
        for link in db.query(ProvenanceLink).filter(
            ProvenanceLink.source_instance_id == file1_instance.id
        )
    )
    assert file1_link_ids_after == pre_existing_file1_link_ids

    # Only the missing members are newly created, and they're fully resolved.
    file2_instance = by_path["B.zip/file2.txt"]
    c_instance = by_path["B.zip/C.zip"]
    file3_instance = by_path["B.zip/C.zip/file3.txt"]
    assert file2_instance.content_identity_group_id is not None
    assert c_instance.content_identity_group_id is None  # a container, never gets one
    assert file3_instance.content_identity_group_id is not None

    # Nested provenance ancestry is correct for the newly-discovered deepest file.
    file3_links = (
        db.query(ProvenanceLink)
        .filter(ProvenanceLink.source_instance_id == file3_instance.id)
        .order_by(ProvenanceLink.sequence_index)
        .all()
    )
    assert [(link.sequence_index, link.kind, link.path) for link in file3_links] == [
        (0, ProvenanceLinkKind.T7_FILE, str(a_zip)),
        (1, ProvenanceLinkKind.ARCHIVE_MEMBER, "B.zip"),
        (2, ProvenanceLinkKind.ARCHIVE_MEMBER, "C.zip"),
        (3, ProvenanceLinkKind.ARCHIVE_MEMBER, "file3.txt"),
    ]

    # A second, immediate re-run has nothing left to do (the root
    # archive's own SUCCEEDED attempt already excludes it from
    # re-claiming) - proving the retry path itself is idempotent, not
    # merely that a lucky first resume worked.
    second_run_result = ArchiveProcessingService(db).process_next_archive(
        worker_id="second-resume-worker", workspace_root=workspace
    )
    assert second_run_result is None

    instances_after_second_run = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == run.id)
        .count()
    )
    assert instances_after_second_run == len(all_instances)


def test_corrupt_archive_records_durable_failed_attempt(db: Session, tmp_path) -> None:
    """"unsupported/corrupt input -> durable FAILED/UNSUPPORTED", the
    archive-level case: a file with a .zip extension that is not
    actually a valid zip must fail durably, not crash the caller."""
    fake_archive = tmp_path / "source" / "broken.zip"
    fake_archive.parent.mkdir(parents=True)
    fake_archive.write_bytes(b"this is not a real zip file")

    run = _classification_run(db)
    root_instance = _archive_instance(db, run, fake_archive)

    result = ArchiveProcessingService(db).process_next_archive(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    assert result.id == root_instance.id
    assert result.content_identity_group_id is None  # container, never gets one anyway

    attempt = (
        db.query(IngestionAttempt)
        .filter(IngestionAttempt.source_instance_id == root_instance.id)
        .one()
    )
    assert attempt.outcome == IngestionAttemptOutcome.FAILED
    assert attempt.failure_code == IngestionFailureCode.MALFORMED_ARCHIVE
    assert attempt.retryable is True


# -- Normalization / chunking / embedding / full chain -----------------------


def _resolve_one_eligible_document(db: Session, tmp_path, content: str = "hello pipeline world"):
    source_file = tmp_path / "source" / "doc.txt"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(content)

    run = _classification_run(db)
    _loose_instance(db, run, source_file)

    workspace = tmp_path / "workspace"
    resolved = IdentityResolutionService(db).resolve_next(worker_id="worker-a", workspace_root=workspace)
    return resolved, workspace


def test_full_pipeline_chain_reaches_ingested(db: Session, tmp_path) -> None:
    instance, workspace = _resolve_one_eligible_document(db, tmp_path)
    group_id = instance.content_identity_group_id

    NormalizationService(db).normalize_next(worker_id="worker-a", workspace_root=workspace)
    group = db.get(ContentIdentityGroup, group_id)
    assert group.pipeline_state == ContentPipelineState.NORMALIZED

    document = db.query(Document).filter(Document.content_identity_group_id == group_id).one()
    assert document is not None

    ChunkingService(db).chunk_next(worker_id="worker-a", workspace_root=workspace)
    db.refresh(group)
    assert group.pipeline_state == ContentPipelineState.CHUNKED

    chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).all()
    assert len(chunks) >= 1
    assert all(c.embedding is None for c in chunks)

    PipelineEmbeddingService(db, embedding_client=FakeEmbeddingClient()).embed_next(worker_id="worker-a")
    db.refresh(group)
    assert group.pipeline_state == ContentPipelineState.INGESTED

    db.refresh(chunks[0])
    for chunk in db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id):
        assert chunk.embedding is not None


def test_normalization_corrupt_pdf_fails_durably_not_a_crash(db: Session, tmp_path) -> None:
    source_file = tmp_path / "source" / "broken.pdf"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"this is not a real pdf file, just garbage bytes")

    run = _classification_run(db)
    _loose_instance(db, run, source_file)
    workspace = tmp_path / "workspace"

    resolved = IdentityResolutionService(db).resolve_next(worker_id="worker-a", workspace_root=workspace)
    group_id = resolved.content_identity_group_id

    NormalizationService(db).normalize_next(worker_id="worker-a", workspace_root=workspace)

    group = db.get(ContentIdentityGroup, group_id)
    assert group.pipeline_state == ContentPipelineState.FAILED

    attempt = (
        db.query(IngestionAttempt)
        .filter(IngestionAttempt.content_identity_group_id == group_id)
        .one()
    )
    assert attempt.outcome == IngestionAttemptOutcome.FAILED
    assert attempt.failure_code == IngestionFailureCode.CORRUPT_INPUT


def test_retry_after_normalization_does_not_duplicate_document(db: Session, tmp_path) -> None:
    """"retry -> no duplicated Document/Chunk/Embedding state": force a
    group back to EXTRACTED after a successful normalize (simulating a
    supervisor retry-sweep) and normalize again - must reuse the
    existing Document, never create a second one (which the UNIQUE
    constraint would reject anyway, but the service must not even try).
    """
    instance, workspace = _resolve_one_eligible_document(db, tmp_path)
    group_id = instance.content_identity_group_id

    NormalizationService(db).normalize_next(worker_id="worker-a", workspace_root=workspace)
    first_document = db.query(Document).filter(Document.content_identity_group_id == group_id).one()

    # Simulate a retry-sweep resetting state (e.g. after an operator
    # decided to force reprocessing) - back to EXTRACTED so it's
    # claimable again.
    db.query(ContentIdentityGroup).filter_by(id=group_id).update(
        {"pipeline_state": ContentPipelineState.EXTRACTED}
    )
    db.commit()

    NormalizationService(db).normalize_next(worker_id="worker-b", workspace_root=workspace)

    documents = db.query(Document).filter(Document.content_identity_group_id == group_id).all()
    assert len(documents) == 1
    assert documents[0].id == first_document.id


def test_embedding_crash_is_resumable_and_idempotent(db: Session, tmp_path) -> None:
    """"embedding crash -> resumable/idempotent": some chunks already
    have embeddings (as if a prior attempt embedded them before
    crashing), others do not. Re-running the embedding step must only
    embed the remaining NULL ones - the already-embedded ones must be
    left untouched (proven via the fake client's call log: it must
    never be asked to embed already-embedded content) - and the group
    must still correctly reach INGESTED.
    """
    instance, workspace = _resolve_one_eligible_document(
        db, tmp_path, content="one two three four five six seven eight nine ten"
    )
    group_id = instance.content_identity_group_id

    NormalizationService(db).normalize_next(worker_id="worker-a", workspace_root=workspace)
    ChunkingService(db).chunk_next(worker_id="worker-a", workspace_root=workspace)

    document = db.query(Document).filter(Document.content_identity_group_id == group_id).one()
    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .order_by(DocumentChunk.chunk_index)
        .all()
    )
    assert len(chunks) >= 1

    # Simulate a prior, partially-successful embedding attempt: the
    # first chunk already has an embedding; the rest do not.
    already_embedded_content = chunks[0].content
    chunks[0].embedding = [0.5] * _EMBEDDING_DIMENSIONS
    db.commit()
    db.query(ContentIdentityGroup).filter_by(id=group_id).update(
        {"pipeline_state": ContentPipelineState.CHUNKED}
    )
    db.commit()

    fake_client = FakeEmbeddingClient()
    PipelineEmbeddingService(db, embedding_client=fake_client).embed_next(worker_id="resume-worker")

    # The already-embedded chunk's content was never re-sent for embedding.
    all_texts_sent = [text for call in fake_client.calls for text in call]
    assert already_embedded_content not in all_texts_sent

    group = db.get(ContentIdentityGroup, group_id)
    assert group.pipeline_state == ContentPipelineState.INGESTED
    for chunk in db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id):
        assert chunk.embedding is not None


def test_embedding_backend_failure_fails_durably_and_never_marks_chunks_embedded(
    db: Session, tmp_path
) -> None:
    instance, workspace = _resolve_one_eligible_document(db, tmp_path)
    group_id = instance.content_identity_group_id

    NormalizationService(db).normalize_next(worker_id="worker-a", workspace_root=workspace)
    ChunkingService(db).chunk_next(worker_id="worker-a", workspace_root=workspace)

    PipelineEmbeddingService(db, embedding_client=FailingEmbeddingClient()).embed_next(
        worker_id="worker-a"
    )

    group = db.get(ContentIdentityGroup, group_id)
    assert group.pipeline_state == ContentPipelineState.FAILED

    document = db.query(Document).filter(Document.content_identity_group_id == group_id).one()
    for chunk in db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id):
        assert chunk.embedding is None

    attempt = (
        db.query(IngestionAttempt)
        .filter(IngestionAttempt.content_identity_group_id == group_id)
        .filter(IngestionAttempt.outcome == IngestionAttemptOutcome.FAILED)
        .one()
    )
    assert attempt.failure_code == IngestionFailureCode.EMBEDDING_UNAVAILABLE
    assert attempt.retryable is True
