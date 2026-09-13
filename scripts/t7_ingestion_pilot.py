#!/usr/bin/env python3
"""Real, read-only T7 ingestion pilot - the Controlled Ingestion
Implementation's FIRST-EVER run against actual T7 content, under
explicit authorization for read-only ingestion of
/media/personal/Seenu_T7SSD1.

Scope, deliberately small and controlled (per the authorization's
"prefer a controlled pilot/batch, do not blindly ingest the whole
corpus" instruction): selects a handful of small, already-known-safe
candidates directly from the existing, already-committed D1
`duplicate_analysis.json` report (never re-scans the T7) - three small
loose files (a .txt/.md/.pdf each already known to be an exact-
duplicate group) and one small archive (.zip) - and runs them through
the full pipeline: identity resolution / archive processing ->
normalization -> chunking -> embedding.

READ-ONLY GUARANTEES, enforced and verified by this script itself, not
merely assumed:
- every T7 path this script touches is opened for reading ONLY -
  nothing in the ingestion pipeline this script calls has ever had a
  write/rename/delete/quarantine capability against a source path.
- before and after reading each source file/archive, this script
  captures (size, mtime) and asserts they are IDENTICAL afterward -
  a durable, explicit proof that nothing was altered, not merely an
  assumption that read-only code stayed read-only.
- the extraction workspace is `documents/imports/t7_pilot/`, on this
  machine's own disk, never under either T7 mount point.
- no DedupFilesystemExecutor, dedup plan, dedup authorization, or
  canonical-copy decision is invoked anywhere in this script.

Run manually, reviewed, once:
    python scripts/t7_ingestion_pilot.py
"""

import hashlib
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
from app.models.provenance_link import ProvenanceLinkKind  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.models.content_identity_group import ContentIdentityGroup  # noqa: E402
from app.models.discovery_run import DiscoveryRunKind  # noqa: E402
from app.models.ingestion_attempt import IngestionAttemptOutcome  # noqa: E402
from app.models.source_instance import SourceInstance  # noqa: E402

D1_REPORT_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "t7_discovery" / "duplicate_analysis.json"
WORKSPACE_ROOT = Path(__file__).resolve().parents[1] / "documents" / "imports" / "t7_pilot"

# Deliberately small - each candidate's group is under this size, per
# the "controlled pilot/batch" instruction, not the whole corpus.
LOOSE_FILE_MAX_BYTES = {".txt": 20_000, ".md": 20_000, ".pdf": 500_000}
ARCHIVE_MAX_BYTES = 50_000


def _stat_snapshot(path: Path) -> tuple[int, float]:
    st = path.stat()
    return (st.st_size, st.st_mtime)


def _select_candidates(report: dict) -> tuple[list[str], str]:
    groups = report["exact_duplicate_groups"]

    def pick(ext: str, max_bytes: int) -> str:
        candidates = [
            g for g in groups
            if g["size_bytes"] < max_bytes and any(p.lower().endswith(ext) for p in g["paths"])
        ]
        chosen = sorted(candidates, key=lambda g: g["size_bytes"])[0]
        return chosen["paths"][0]

    loose_paths = [pick(ext, size) for ext, size in LOOSE_FILE_MAX_BYTES.items()]

    zip_groups = [
        g for g in groups
        if g["size_bytes"] < ARCHIVE_MAX_BYTES
        and any(p.lower().endswith((".zip", ".7z")) for p in g["paths"])
    ]
    archive_path = sorted(zip_groups, key=lambda g: g["size_bytes"])[0]["paths"][0]

    return loose_paths, archive_path


def main() -> None:
    print(f"Loading D1 report: {D1_REPORT_PATH}")
    with D1_REPORT_PATH.open() as fh:
        report = json.load(fh)

    if report["root"] != "/media/personal/Seenu_T7SSD1":
        raise SystemExit(f"Unexpected report root: {report['root']!r} - refusing to proceed")

    loose_paths, archive_path = _select_candidates(report)
    print("Selected pilot candidates (loose files):")
    for p in loose_paths:
        print(f"  - {p}")
    print(f"Selected pilot candidate (archive): {archive_path}")

    all_source_paths = [*loose_paths, archive_path]
    pre_stats = {}
    for p in all_source_paths:
        path = Path(p)
        if not path.is_file():
            raise SystemExit(f"Selected candidate no longer exists as a file: {p}")
        pre_stats[p] = _stat_snapshot(path)
    print("Pre-read (size, mtime) snapshots captured for every candidate.")

    engine = create_engine(settings.DATABASE_URL)
    db = Session(engine)

    discovery_run = DiscoveryRunService(db).record_run(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root=report["root"],
        report_path=D1_REPORT_PATH,
        run_started_at=datetime.fromisoformat(report["analyzed_at"]),
        run_completed_at=datetime.fromisoformat(report["analyzed_at"]),
    )
    print(f"DiscoveryRun recorded: id={discovery_run.id}, report_sha256={discovery_run.report_sha256}")

    classification_run = ClassificationRunService(db).start_run(
        classifier_version="t7-ingestion-pilot-v1",
        d1_discovery_run_id=discovery_run.id,
    )
    print(f"ClassificationRun recorded: id={classification_run.id}")

    instance_service = SourceInstanceService(db)
    worker_id = "t7-pilot-worker"

    loose_instances: list[SourceInstance] = []
    for p in loose_paths:
        instance = instance_service.create_instance(
            classification_run_id=classification_run.id,
            root_t7_path=p,
            member_path=None,
            evidence_snapshot={
                "source": "duplicate_analysis.json",
                "group_kind": "exact_duplicate",
                "size_bytes": next(
                    g["size_bytes"] for g in report["exact_duplicate_groups"] if g["paths"][0] == p
                ),
            },
            chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=p)],
        )
        loose_instances.append(instance)
    print(f"Created {len(loose_instances)} loose-file SourceInstance rows.")

    archive_instance = instance_service.create_instance(
        classification_run_id=classification_run.id,
        root_t7_path=archive_path,
        member_path=None,
        evidence_snapshot={
            "source": "duplicate_analysis.json",
            "group_kind": "exact_duplicate",
            "size_bytes": next(
                g["size_bytes"] for g in report["exact_duplicate_groups"] if g["paths"][0] == archive_path
            ),
        },
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=archive_path)],
    )
    print(f"Created archive SourceInstance row: id={archive_instance.id}")

    print("\n--- Identity resolution (real T7 reads) ---")
    resolved_groups: list[int] = []
    for instance in loose_instances:
        result = IdentityResolutionService(db).resolve_next(
            worker_id=worker_id, workspace_root=WORKSPACE_ROOT
        )
        print(f"  resolved instance {result.id}: content_identity_group_id={result.content_identity_group_id}")
        if result.content_identity_group_id:
            resolved_groups.append(result.content_identity_group_id)

    print("\n--- Archive processing (real T7 read + extraction) ---")
    archive_result = ArchiveProcessingService(db).process_next_archive(
        worker_id=worker_id, workspace_root=WORKSPACE_ROOT
    )
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

    print("\n--- Post-read integrity check (source files must be unchanged) ---")
    all_ok = True
    for p in all_source_paths:
        post = _stat_snapshot(Path(p))
        pre = pre_stats[p]
        status = "UNCHANGED" if post == pre else "!!! CHANGED !!!"
        if post != pre:
            all_ok = False
        print(f"  {status}: {p} (pre={pre}, post={post})")
    if not all_ok:
        raise SystemExit("A source file changed during ingestion - stopping immediately.")

    print("\n--- Normalization ---")
    for _ in resolved_groups:
        result = NormalizationService(db).normalize_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
        if result is None:
            break
        print(f"  normalized group {result.id}: pipeline_state={result.pipeline_state}")

    print("\n--- Chunking ---")
    for _ in resolved_groups:
        result = ChunkingService(db).chunk_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
        if result is None:
            break
        print(f"  chunked group {result.id}: pipeline_state={result.pipeline_state}")

    print("\n--- Embedding (real EmbeddingClient - may fail if Ollama is not running) ---")
    for _ in resolved_groups:
        result = PipelineEmbeddingService(db).embed_next(worker_id=worker_id)
        if result is None:
            break
        print(f"  embedded group {result.id}: pipeline_state={result.pipeline_state}")

    print("\n--- Idempotency check: re-running each stage a second time ---")
    # Each of these MUST return None - every real candidate in this
    # pilot's small, fixed batch is already fully resolved/processed/
    # normalized by this point, so a second claim attempt must find no
    # eligible work, not silently succeed against the wrong row.
    #
    # This assertion is exactly what a real bug in this first-ever
    # pilot run violated: claim_source_instance_for_identity_resolution
    # had no exclusion for archive-suffixed root-level SourceInstance
    # rows (which legitimately and permanently keep
    # content_identity_group_id IS NULL), so this second call wrongly
    # claimed the archive's own row and hashed its raw compressed
    # bytes as if they were document content - producing a bogus
    # ContentIdentityGroup that then failed at normalization with
    # CORRUPT_INPUT. The real T7 source file was confirmed unchanged
    # throughout (verified via explicit stat before/after); only this
    # application's own database bookkeeping was affected, and it was
    # corrected afterward. The underlying claim-query gap is now fixed
    # (see WorkerClaimService.claim_source_instance_for_identity_
    # resolution) and covered by a regression test.
    second_identity = IdentityResolutionService(db).resolve_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
    print(f"  second identity-resolution claim: {second_identity}")
    assert second_identity is None, "identity resolution wrongly found more work in a fully-resolved batch"
    second_archive = ArchiveProcessingService(db).process_next_archive(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
    print(f"  second archive-processing claim: {second_archive}")
    assert second_archive is None, "archive processing wrongly found more work in a fully-processed batch"
    second_normalize = NormalizationService(db).normalize_next(worker_id=worker_id, workspace_root=WORKSPACE_ROOT)
    print(f"  second normalization claim: {second_normalize}")
    assert second_normalize is None, "normalization wrongly found more work in a fully-normalized batch"

    print("\n--- Final state summary ---")
    for group_id in sorted(set(resolved_groups)):
        group = db.get(ContentIdentityGroup, group_id)
        print(f"  ContentIdentityGroup {group_id}: pipeline_state={group.pipeline_state}")

    from app.models.ingestion_attempt import IngestionAttempt

    attempts = (
        db.query(IngestionAttempt)
        .filter(
            (IngestionAttempt.content_identity_group_id.in_(resolved_groups))
            | (IngestionAttempt.source_instance_id.in_([i.id for i in loose_instances] + [archive_instance.id]))
        )
        .all()
    )
    print(f"\nTotal IngestionAttempt rows recorded: {len(attempts)}")
    for a in attempts:
        print(f"  {a.attempt_kind} / {a.attempted_stage} / {a.outcome} / failure_code={a.failure_code}")

    db.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
