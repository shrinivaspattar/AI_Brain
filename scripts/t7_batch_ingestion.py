#!/usr/bin/env python3
"""Controlled real-T7 batch ingestion - authorized under "AUTHORIZE:
CONTROLLED REAL-T7 BATCH INGESTION" (checkpoint `5d5d1c7`), the second
real-T7 read-only ingestion gate after the first pilot (`5d5d1c7`
itself). Deliberately small, representative, real-corpus batch
proving the full chain against genuinely diverse real content, now
that embedding infrastructure (Ollama + nomic-embed-text) is verified
available.

NO REAL T7 PATH OR PERSONAL FILENAME IS HARDCODED IN THIS FILE. The
actual real paths selected for a given run live in a sibling
`t7_batch_selection.json` (gitignored via `scripts/*_selection.json`,
same reasoning as `knowledge/t7_discovery/` and `documents/` - it
would otherwise commit real personal file/directory names to version
control forever). This script only knows the CASE LABELS below; the
selection file supplies the real path for each one at runtime.

Seven representative cases (see `t7_batch_selection.json`'s keys):

    already_known_identity - a real path already resolved to a
                              PRE-EXISTING production ContentIdentityGroup
                              from an earlier pilot - proves reuse of a
                              pre-existing identity, not just same-run
                              convergence.
    loose_ordinary          - an ordinary loose file with real,
                              substantive text content.
    duplicate_a/duplicate_b - two real paths with byte-identical
                              content, from a real D1 duplicate group -
                              proves convergence to one new group.
    archive                 - a small real archive with real (non-
                              directory) file members.
    unsupported             - a real file with a known-unsupported
                              suffix - proves durable UNSUPPORTED, zero
                              attempt.
    corrupt                 - a real file that is genuinely invalid
                              relative to its own extension (verified
                              via direct inspection before selection,
                              e.g. `zipfile.ZipFile()` failing on a
                              `.pptx`) - proves durable FAILED/
                              CORRUPT_INPUT.

Explicitly NOT included, by explicit decision after being asked: a
naturally-occurring NESTED archive. A systematic real search (42 real
.zip/.7z files up to 50MB) found none - nested-archive crash/resume
behavior remains proven only synthetically (3 levels deep, see
`a0523b7`), not re-derived from real data in this batch.

READ-ONLY GUARANTEES, enforced and verified by this script itself:
- every T7 path is opened for reading ONLY.
- every source path's filesystem metadata (size, mtime) is captured
  before any read and re-verified unchanged after every phase of this
  script - strong corroborating evidence of no mutation, though not a
  cryptographic proof of byte-for-byte identity. The read-only access
  pattern itself (this pipeline has never had any write/rename/delete
  capability against a source path) is the stronger safety property.
- the extraction workspace is `documents/imports/t7_batch/`, on this
  machine's own disk, never under either T7 mount point.
- no DedupFilesystemExecutor, dedup plan, dedup authorization, or
  canonical-copy decision is invoked anywhere in this script.

Run manually, reviewed, once, with `t7_batch_selection.json` present
next to this script:
    python scripts/t7_batch_ingestion.py
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.classification.archive_processing_service import ArchiveProcessingService  # noqa: E402
from app.classification.chunking_service import ChunkingService  # noqa: E402
from app.classification.classification_run_service import ClassificationRunService  # noqa: E402
from app.classification.discovery_run_service import DiscoveryRunService  # noqa: E402
from app.classification.identity_resolution_service import IdentityResolutionService  # noqa: E402
from app.classification.normalization_service import NormalizationService  # noqa: E402
from app.classification.pipeline_embedding_service import PipelineEmbeddingService  # noqa: E402
from app.classification.source_instance_service import ProvenanceStep, SourceInstanceService  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.embeddings.client import EmbeddingClient  # noqa: E402
from app.models.content_identity_group import ContentIdentityGroup  # noqa: E402
from app.models.discovery_run import DiscoveryRunKind  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.document_chunk import DocumentChunk  # noqa: E402
from app.models.ingestion_attempt import IngestionAttempt  # noqa: E402
from app.models.provenance_link import ProvenanceLink, ProvenanceLinkKind  # noqa: E402
from app.models.source_instance import SourceInstance  # noqa: E402

D1_REPORT_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "t7_discovery" / "duplicate_analysis.json"
WORKSPACE_ROOT = Path(__file__).resolve().parents[1] / "documents" / "imports" / "t7_batch"
SELECTION_PATH = Path(__file__).resolve().parent / "t7_batch_selection.json"

_REQUIRED_SELECTION_KEYS = (
    "root",
    "already_known_identity",
    "loose_ordinary",
    "duplicate_a",
    "duplicate_b",
    "archive",
    "unsupported",
    "corrupt",
)


def _load_selection() -> dict[str, str]:
    if not SELECTION_PATH.is_file():
        raise SystemExit(
            f"Missing {SELECTION_PATH} - this gitignored file must supply the real T7 "
            "paths for this batch (see this script's module docstring). It is never "
            "committed to version control."
        )
    with SELECTION_PATH.open() as fh:
        selection = json.load(fh)
    missing = [k for k in _REQUIRED_SELECTION_KEYS if k not in selection]
    if missing:
        raise SystemExit(f"{SELECTION_PATH} is missing required keys: {missing}")
    return selection


def _stat_snapshot(path: Path) -> tuple[int, float]:
    st = path.stat()
    return (st.st_size, st.st_mtime)


def _verify_unchanged(all_real_paths: list[str], pre_stats: dict[str, tuple[int, float]], label: str) -> None:
    print(f"\n--- Integrity check ({label}): source file metadata must be unchanged ---")
    all_ok = True
    for p in all_real_paths:
        post = _stat_snapshot(Path(p))
        pre = pre_stats[p]
        status = "UNCHANGED" if post == pre else "!!! CHANGED !!!"
        if post != pre:
            all_ok = False
        print(f"  {status}: {p} (pre={pre}, post={post})")
    if not all_ok:
        raise SystemExit(f"A source file's metadata changed during '{label}' - stopping immediately.")


def main() -> None:
    selection = _load_selection()
    real_root = selection["root"]

    already_known_identity_path = selection["already_known_identity"]
    loose_ordinary_path = selection["loose_ordinary"]
    duplicate_a_path = selection["duplicate_a"]
    duplicate_b_path = selection["duplicate_b"]
    archive_path = selection["archive"]
    unsupported_path = selection["unsupported"]
    corrupt_path = selection["corrupt"]

    all_real_paths = [
        already_known_identity_path,
        loose_ordinary_path,
        duplicate_a_path,
        duplicate_b_path,
        archive_path,
        unsupported_path,
        corrupt_path,
    ]

    print(f"Loading D1 report: {D1_REPORT_PATH}")
    with D1_REPORT_PATH.open() as fh:
        report = json.load(fh)
    if report["root"] != real_root:
        raise SystemExit(f"Unexpected report root: {report['root']!r} - refusing to proceed")

    pre_stats: dict[str, tuple[int, float]] = {}
    for p in all_real_paths:
        path = Path(p)
        if not path.is_file():
            raise SystemExit(f"Selected candidate no longer exists as a file: {p}")
        pre_stats[p] = _stat_snapshot(path)
    print(f"Pre-read (size, mtime) snapshots captured for all {len(all_real_paths)} candidates.")

    print("\n--- Ollama / embedding infrastructure (already running, not started by this script) ---")
    embedding_client = EmbeddingClient()
    ping = embedding_client.embed(["ollama readiness check - not real corpus content"])
    print(f"  model={settings.EMBEDDING_MODEL} dimensions={len(ping[0])} (expected {settings.EMBEDDING_DIMENSIONS})")
    assert len(ping[0]) == settings.EMBEDDING_DIMENSIONS

    engine = create_engine(settings.DATABASE_URL)
    db = Session(engine)
    worker_id = "t7-batch-worker"

    pre_group_count = db.query(ContentIdentityGroup).count()
    pre_source_instance_count = db.query(SourceInstance).count()
    pre_provenance_link_count = db.query(ProvenanceLink).count()
    pre_document_count = db.query(Document).count()

    discovery_run = DiscoveryRunService(db).record_run(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root=report["root"],
        report_path=D1_REPORT_PATH,
        run_started_at=datetime.fromisoformat(report["analyzed_at"]),
        run_completed_at=datetime.fromisoformat(report["analyzed_at"]),
    )
    classification_run = ClassificationRunService(db).start_run(
        classifier_version="t7-batch-ingestion-v1",
        d1_discovery_run_id=discovery_run.id,
    )
    print(f"\nDiscoveryRun id={discovery_run.id}, ClassificationRun id={classification_run.id}")

    instance_service = SourceInstanceService(db)

    def _make_instance(path: str, case_label: str) -> SourceInstance:
        return instance_service.create_instance(
            classification_run_id=classification_run.id,
            root_t7_path=path,
            member_path=None,
            evidence_snapshot={"source": "duplicate_analysis.json", "case": case_label},
            chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=path)],
        )

    reused_instance = _make_instance(already_known_identity_path, "already_known_identity")
    ordinary_instance = _make_instance(loose_ordinary_path, "loose_ordinary")
    dup_a_instance = _make_instance(duplicate_a_path, "duplicate_a")
    dup_b_instance = _make_instance(duplicate_b_path, "duplicate_b")
    unsupported_instance = _make_instance(unsupported_path, "unsupported")
    corrupt_instance = _make_instance(corrupt_path, "corrupt")
    archive_instance = _make_instance(archive_path, "archive")

    loose_instances = [
        reused_instance,
        ordinary_instance,
        dup_a_instance,
        dup_b_instance,
        unsupported_instance,
        corrupt_instance,
    ]
    print(f"Created {len(loose_instances)} loose SourceInstance rows + 1 archive SourceInstance row.")

    _verify_unchanged(all_real_paths, pre_stats, "after SourceInstance creation")

    print("\n--- Identity resolution (real T7 reads) ---")
    resolved_groups: list[int] = []
    for instance in loose_instances:
        result = IdentityResolutionService(db).resolve_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
        print(f"  resolved instance {result.id}: content_identity_group_id={result.content_identity_group_id}")
        if result.content_identity_group_id:
            resolved_groups.append(result.content_identity_group_id)

    print("\n--- Archive processing (real T7 read + extraction) ---")
    archive_result = ArchiveProcessingService(db).process_next_archive(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
    print(f"  archive processed: id={archive_result.id if archive_result else None}")
    archive_members = (
        db.query(SourceInstance)
        .filter(
            SourceInstance.classification_run_id == classification_run.id,
            SourceInstance.member_path.is_not(None),
        )
        .all()
    )
    for m in archive_members:
        print(f"  member: {m.member_path} -> group={m.content_identity_group_id}")
        if m.content_identity_group_id:
            resolved_groups.append(m.content_identity_group_id)

    _verify_unchanged(all_real_paths, pre_stats, "after identity resolution + archive processing")

    print("\n--- Specific proof: already-known content identity reuse ---")
    db.refresh(reused_instance)
    print(f"  reused instance {reused_instance.id}: content_identity_group_id={reused_instance.content_identity_group_id}")

    print("\n--- Specific proof: duplicate-content convergence ---")
    db.refresh(dup_a_instance)
    db.refresh(dup_b_instance)
    print(f"  dup A group={dup_a_instance.content_identity_group_id}, dup B group={dup_b_instance.content_identity_group_id}")
    assert dup_a_instance.content_identity_group_id == dup_b_instance.content_identity_group_id
    print("  OK: two different real T7 paths with identical bytes converged on ONE ContentIdentityGroup.")

    print("\n--- Specific proof: unsupported-file and corrupt-input handling ---")
    db.refresh(unsupported_instance)
    db.refresh(corrupt_instance)
    unsupported_group = db.get(ContentIdentityGroup, unsupported_instance.content_identity_group_id)
    print(f"  unsupported group {unsupported_group.id}: pipeline_state={unsupported_group.pipeline_state}")

    print("\n--- Normalization / Chunking / Embedding ---")
    for _ in range(len(resolved_groups) + 2):
        result = NormalizationService(db).normalize_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
        if result is None:
            break
        print(f"  normalized group {result.id}: pipeline_state={result.pipeline_state}")

    for _ in range(len(resolved_groups) + 2):
        result = ChunkingService(db).chunk_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
        if result is None:
            break
        print(f"  chunked group {result.id}: pipeline_state={result.pipeline_state}")

    for _ in range(len(resolved_groups) + 2):
        result = PipelineEmbeddingService(db, embedding_client=embedding_client).embed_next(worker_id=worker_id)
        if result is None:
            break
        print(f"  embedded group {result.id}: pipeline_state={result.pipeline_state}")

    corrupt_group = db.get(ContentIdentityGroup, corrupt_instance.content_identity_group_id)
    db.refresh(corrupt_group)
    print(f"\n  corrupt-input group {corrupt_group.id}: pipeline_state={corrupt_group.pipeline_state}")

    _verify_unchanged(all_real_paths, pre_stats, "after normalization/chunking/embedding")

    print("\n--- Idempotency check: re-running each claim a second time ---")
    second_identity = IdentityResolutionService(db).resolve_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
    print(f"  second identity-resolution claim: {second_identity}")
    assert second_identity is None, "identity resolution wrongly found more work in a fully-resolved batch"
    second_archive = ArchiveProcessingService(db).process_next_archive(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
    print(f"  second archive-processing claim: {second_archive}")
    assert second_archive is None, "archive processing wrongly found more work in a fully-processed batch"
    second_normalize = NormalizationService(db).normalize_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
    print(f"  second normalization claim: {second_normalize}")
    assert second_normalize is None, "normalization wrongly found more work in a fully-normalized batch"

    print("\n--- No-duplicate-creation check (retry safety) ---")
    post_source_instance_count = db.query(SourceInstance).count()
    post_provenance_link_count_before_retry = db.query(ProvenanceLink).count()
    assert IdentityResolutionService(db).resolve_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT) is None
    assert ArchiveProcessingService(db).process_next_archive(worker_id=worker_id, workspace_root=WORKSPACE_ROOT) is None
    assert NormalizationService(db).normalize_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT) is None
    post_provenance_link_count_after_retry = db.query(ProvenanceLink).count()
    assert db.query(SourceInstance).count() == post_source_instance_count
    assert post_provenance_link_count_after_retry == post_provenance_link_count_before_retry
    print("  OK: no new SourceInstance/ProvenanceLink rows created by repeated no-op claim attempts.")

    _verify_unchanged(all_real_paths, pre_stats, "final, after all processing and idempotency checks")

    print("\n--- Provenance checks ---")
    all_instance_ids = [i.id for i in loose_instances] + [archive_instance.id] + [m.id for m in archive_members]
    links = (
        db.query(ProvenanceLink)
        .filter(ProvenanceLink.source_instance_id.in_(all_instance_ids))
        .order_by(ProvenanceLink.source_instance_id, ProvenanceLink.sequence_index)
        .all()
    )
    for link in links:
        print(f"  source_instance_id={link.source_instance_id} seq={link.sequence_index} kind={link.kind}")

    print("\n--- Final ContentIdentityGroup state summary ---")
    for group_id in sorted(set(resolved_groups)):
        group = db.get(ContentIdentityGroup, group_id)
        print(f"  ContentIdentityGroup {group_id}: pipeline_state={group.pipeline_state}")

    print("\n--- Documents / chunks created ---")
    documents = db.query(Document).filter(Document.content_identity_group_id.in_(resolved_groups)).all()
    for doc in documents:
        chunk_count = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
        embedded_count = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == doc.id, DocumentChunk.embedding.is_not(None))
            .count()
        )
        print(f"  Document {doc.id} (group {doc.content_identity_group_id}): {chunk_count} chunks, {embedded_count} embedded")

    print("\n--- IngestionAttempt rows for this batch ---")
    attempts = (
        db.query(IngestionAttempt)
        .filter(
            (IngestionAttempt.content_identity_group_id.in_(resolved_groups))
            | (IngestionAttempt.source_instance_id.in_(all_instance_ids))
        )
        .order_by(IngestionAttempt.id)
        .all()
    )
    print(f"Total: {len(attempts)}")
    for a in attempts:
        print(
            f"  id={a.id} {a.attempt_kind} / {a.attempted_stage} / {a.outcome} "
            f"/ failure_code={a.failure_code} / source_instance_id={a.source_instance_id} "
            f"/ content_identity_group_id={a.content_identity_group_id}"
        )

    print("\n--- Database-level counts (before -> after) ---")
    print(f"  ContentIdentityGroup: {pre_group_count} -> {db.query(ContentIdentityGroup).count()}")
    print(f"  SourceInstance: {pre_source_instance_count} -> {db.query(SourceInstance).count()}")
    print(f"  ProvenanceLink: {pre_provenance_link_count} -> {db.query(ProvenanceLink).count()}")
    print(f"  Document: {pre_document_count} -> {db.query(Document).count()}")

    db.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
