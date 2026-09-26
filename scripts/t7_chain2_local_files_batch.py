#!/usr/bin/env python3
"""Creates a small IngestionBatch for specific files you name on the command
line (e.g. transcripts produced by transcribe_video_lecture.py, or a handful
of personal files handed over for testing) - the same BatchCreationService
path as every other real-file batch, just fed from explicit paths instead of
a selection JSON.

NO REAL PATH IS HARDCODED: you pass the paths. The envelope is computed from
real numbers, not invented: max_source_bytes/max_source_instances from the
files themselves, max_embeddings from a real extract_text + chunk_text pass
(the same functions the pipeline uses). max_runtime_seconds is a safety cap
only (the run-loop's accounting of it read 0.0 through all of batch 6).

PROVENANCE: a manifest (path, size, sha256 of each file) is written under
documents/manifests/ and its hash is what the DiscoveryRun records - durable
proof of exactly which files this batch was authorised for.

--dry-run only reads (no DB writes, no manifest): it shows the derived
envelope and confirms, via the pipeline's own pure classify/select functions,
that every file would actually be selected.

Order of operations (DB-mutating steps are run by you, attended):
  1. python scripts/t7_chain2_local_files_batch.py --label NAME --files <paths...>
  2. python scripts/t7_chain2_start_batch.py <batch_id> --database aibrain
  3. python scripts/t7_chain2_priority2_run_loop.py --batch-id <batch_id> \\
         --workspace-root documents/workspace_production --database aibrain
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.classification.batch_creation_service import BatchCreationService  # noqa: E402
from app.classification.deterministic_selector import (  # noqa: E402
    CandidateObservation,
    SelectionEnvelope,
    classify,
)
from app.classification.deterministic_selector import select as run_selection  # noqa: E402
from app.classification.discovery_run_service import DiscoveryRunService  # noqa: E402
from app.classification.policy_evaluator import text_document_batch_policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.embeddings.chunker import chunk_text  # noqa: E402
from app.ingestion.text_extractor import extract_text  # noqa: E402
from app.models.discovery_run import DiscoveryRunKind  # noqa: E402

SAFETY_MAX_RUNTIME_SECONDS = 86_400


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", type=Path, nargs="+", required=True)
    parser.add_argument("--label", required=True, help="short name recorded in classifier_version, e.g. udemy-transcripts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = [p.resolve() for p in args.files]
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise SystemExit(f"Not found: {missing}")

    sizes = {p: p.stat().st_size for p in files}
    total_chunks = 0
    for p in files:
        chunks = len(chunk_text(extract_text(p)))
        total_chunks += chunks
        print(f"  {p.name}  {sizes[p]:,} bytes  -> {chunks} chunks")

    envelope = {
        "max_source_instances": len(files),
        "max_source_bytes": sum(sizes.values()),
        "max_embeddings": total_chunks,
        "max_runtime_seconds": SAFETY_MAX_RUNTIME_SECONDS,
    }
    print(f"Envelope (computed): {envelope}")

    candidates = [
        CandidateObservation(root_t7_path=str(p), member_path=None, declared_size_bytes=sizes[p]) for p in files
    ]
    policy = text_document_batch_policy()
    selection = run_selection(
        [classify(c) for c in candidates],
        policy,
        SelectionEnvelope(
            max_source_instances=envelope["max_source_instances"], max_source_bytes=envelope["max_source_bytes"]
        ),
    )
    print(f"Pipeline would select {len(selection.selected)} of {len(candidates)} files.")
    if len(selection.selected) != len(candidates):
        raise SystemExit("Some files would NOT be selected by the batch policy - not proceeding.")

    if args.dry_run:
        print("Dry run - nothing written.")
        return

    manifest_dir = REPO_ROOT / "documents" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = manifest_dir / f"{args.label}-{stamp}.json"
    manifest_path.write_text(
        json.dumps(
            [{"path": str(p), "size_bytes": sizes[p], "sha256": _sha256(p)} for p in files], indent=1
        )
    )
    print(f"Wrote manifest: {manifest_path}")

    source_root = os.path.commonpath([str(p.parent) for p in files])
    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain"))
    print("Using database: aibrain (production)")
    with Session(engine) as db:
        discovery = DiscoveryRunService(db).record_run(
            run_kind=DiscoveryRunKind.D3_MASTER_MANIFEST,
            source_root=source_root,
            report_path=manifest_path,
            run_started_at=datetime.now(UTC),
            run_completed_at=datetime.now(UTC),
        )
        print(f"Recorded DiscoveryRun id={discovery.id}")

        batch = BatchCreationService(db).create_batch(
            discovery_run=discovery,
            candidates=candidates,
            policy=policy,
            max_source_instances=envelope["max_source_instances"],
            max_source_bytes=envelope["max_source_bytes"],
            max_extracted_bytes=None,
            max_embeddings=envelope["max_embeddings"],
            max_runtime_seconds=envelope["max_runtime_seconds"],
            classifier_version=f"local-files-{args.label}-v1",
        )
        if batch is None:
            print("No batch created - selection produced zero eligible/selected candidates.")
            return
        print(
            f"Created IngestionBatch id={batch.id} status={batch.status.value} "
            f"source_instances_selected={batch.source_instances_selected} "
            f"source_bytes_selected={batch.source_bytes_selected}"
        )


if __name__ == "__main__":
    main()
