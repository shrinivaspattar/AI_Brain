#!/usr/bin/env python3
"""Creates a small NEW IngestionBatch to retry specific ContentIdentityGroups
that failed for a transient reason (e.g. EMBEDDING_UNAVAILABLE while Ollama
was down) - the recovery path proven on the M32 pilot (groups 2 and 3) and on
batch 6's nine Ollama-dropout failures. A COMPLETED batch can never own
retries (PipelineEmbeddingService only recognises a RUNNING owner), so a new
batch is required; content-identity dedup links its new SourceInstances back
to the EXISTING groups, so nothing is re-extracted, re-chunked or duplicated.

Order of operations (each DB-mutating step is run by you, attended):
  1. python scripts/t7_chain2_pilot_reset_group.py <group_id>     (per group;
     FAILED -> CHUNKED, refuses anything not FAILED)
  2. python scripts/t7_chain2_retry_failed_batch.py --groups <ids...>
  3. python scripts/t7_chain2_start_batch.py <batch_id> --database aibrain
  4. python scripts/t7_chain2_priority2_run_loop.py --batch-id <batch_id> \\
         --workspace-root documents/workspace_production --database aibrain

NO REAL T7 PATH IS HARDCODED: paths are read from the groups' existing
SourceInstance rows, and the envelope is computed from real numbers (file
sizes on disk, un-embedded chunk counts in the DB) - nothing invented, except
max_runtime_seconds, which is a safety cap only (the run-loop's accounting
of it read 0.0 for all of batch 6, so it is not a meaningful bound).

--dry-run only reads: it prints the paths, states and derived envelope and
writes nothing, so it also works before step 1 (with a warning that the
groups are not yet CHUNKED).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.classification.batch_creation_service import BatchCreationService  # noqa: E402
from app.classification.deterministic_selector import CandidateObservation  # noqa: E402
from app.classification.discovery_run_service import DiscoveryRunService  # noqa: E402
from app.classification.policy_evaluator import text_document_batch_policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.models.discovery_run import DiscoveryRunKind  # noqa: E402

D3_REPORT_PATH = (
    Path(__file__).resolve().parents[1] / "knowledge" / "t7_discovery" / "plan" / "fresh" / "keep_files.csv"
)
SAFETY_MAX_RUNTIME_SECONDS = 86_400


def _gather(db: Session, group_ids: list[int]) -> tuple[list[dict], int]:
    rows = db.execute(
        text(
            """
            SELECT g.id, g.pipeline_state, s.root_t7_path,
                   (SELECT count(*) FROM document_chunks c JOIN documents d ON d.id = c.document_id
                    WHERE d.content_identity_group_id = g.id AND c.embedding IS NULL) AS unembedded
            FROM content_identity_groups g
            JOIN source_instances s ON s.content_identity_group_id = g.id
            WHERE g.id = ANY(:ids)
            ORDER BY g.id, s.id
            """
        ),
        {"ids": group_ids},
    ).fetchall()

    by_group: dict[int, dict] = {}
    for gid, state, path, unembedded in rows:
        entry = by_group.setdefault(gid, {"group_id": gid, "state": str(state), "paths": [], "unembedded": unembedded})
        if path not in entry["paths"]:
            entry["paths"].append(path)

    missing = [g for g in group_ids if g not in by_group]
    if missing:
        raise SystemExit(f"No SourceInstance found for group ids: {missing}")
    multi = [e["group_id"] for e in by_group.values() if len(e["paths"]) != 1]
    if multi:
        raise SystemExit(f"Groups with more than one distinct path (review by hand): {multi}")

    total_unembedded = sum(e["unembedded"] for e in by_group.values())
    return list(by_group.values()), total_unembedded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--groups", type=int, nargs="+", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain"))
    print("Using database: aibrain (production)")

    with Session(engine) as db:
        entries, total_unembedded = _gather(db, args.groups)

        sizes = {}
        for e in entries:
            path = e["paths"][0]
            if not os.path.isfile(path):
                raise SystemExit(f"Original file not found on disk (is the drive mounted?): {path}")
            sizes[path] = os.path.getsize(path)

        envelope = {
            "max_source_instances": len(entries),
            "max_source_bytes": sum(sizes.values()),
            "max_embeddings": total_unembedded,
            "max_runtime_seconds": SAFETY_MAX_RUNTIME_SECONDS,
        }

        print(f"Groups: {len(entries)}")
        for e in entries:
            print(f"  group {e['group_id']:>5} state={e['state']:<32} unembedded_chunks={e['unembedded']:>4}  {os.path.basename(e['paths'][0])[:60]}")
        print(f"Envelope (computed): {envelope}")

        not_chunked = [e["group_id"] for e in entries if not e["state"].endswith("CHUNKED")]
        if not_chunked:
            msg = f"Groups not in CHUNKED state (run t7_chain2_pilot_reset_group.py first): {not_chunked}"
            if args.dry_run:
                print(f"WARNING (dry-run only): {msg}")
            else:
                raise SystemExit(msg)

        if args.dry_run:
            print("Dry run - nothing written.")
            return

        discovery = DiscoveryRunService(db).record_run(
            run_kind=DiscoveryRunKind.D3_MASTER_MANIFEST,
            source_root=os.path.commonpath([os.path.dirname(e["paths"][0]) for e in entries]),
            report_path=D3_REPORT_PATH,
            run_started_at=datetime.now(UTC),
            run_completed_at=datetime.now(UTC),
        )
        print(f"Recorded DiscoveryRun id={discovery.id}")

        candidates = [
            CandidateObservation(root_t7_path=e["paths"][0], member_path=None, declared_size_bytes=sizes[e["paths"][0]])
            for e in entries
        ]

        batch = BatchCreationService(db).create_batch(
            discovery_run=discovery,
            candidates=candidates,
            policy=text_document_batch_policy(),
            max_source_instances=envelope["max_source_instances"],
            max_source_bytes=envelope["max_source_bytes"],
            max_extracted_bytes=None,
            max_embeddings=envelope["max_embeddings"],
            max_runtime_seconds=envelope["max_runtime_seconds"],
            classifier_version="t7-retry-transient-embedding-failures-v1",
        )

        if batch is None:
            print("No batch created - selection produced zero eligible/selected candidates.")
            return

        print(
            f"Created IngestionBatch id={batch.id} status={batch.status.value} "
            f"source_instances_selected={batch.source_instances_selected} source_bytes_selected={batch.source_bytes_selected}"
        )


if __name__ == "__main__":
    main()
