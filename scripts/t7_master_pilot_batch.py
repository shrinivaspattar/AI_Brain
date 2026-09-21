#!/usr/bin/env python3
"""Create the pilot ingestion batch for the cleaned master copy (decision 0003,
docs/designs/master-copy-document-ingestion.md).

It only creates database ROWS through the existing, unmodified
BatchCreationService: one DiscoveryRun (kind D3_MASTER_MANIFEST, referencing
the checksum list by its SHA-256), one ClassificationRun, one SourceInstance
per selected file, one IngestionBatch. It never opens, reads or touches any
file named in the selection; documents are only read later, by the separate,
attended scripts/run_ingestion_batch.py.

NO REAL PATH IS HARDCODED. The selection (real paths) is the gitignored file
written by build_master_selection.py; its location and the checksum list are
arguments.

DATABASE SAFETY: defaults to `aibrain_test`. Any other database needs
--allow-non-test, so production cannot be reached by omission.

The embedding and runtime caps are explicit, required arguments: they are
safety limits chosen by the operator for this batch, not estimates. The
number of files and bytes are derived from the pilot list itself.

Usage:
    python scripts/t7_master_pilot_batch.py --report keep_files.csv \\
        --max-embeddings N --max-runtime-seconds N [--dry-run] [--database aibrain_test]
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.classification.batch_creation_service import BatchCreationService  # noqa: E402
from app.classification.deterministic_selector import CandidateObservation, classify  # noqa: E402
from app.classification.discovery_run_service import DiscoveryRunService  # noqa: E402
from app.classification.policy_evaluator import text_document_batch_policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.models.discovery_run import DiscoveryRunKind  # noqa: E402

DEFAULT_SELECTION = Path(__file__).resolve().parents[1] / "knowledge" / "t7_discovery" / "master_selection.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    ap.add_argument("--report", required=True, type=Path, help="the checksum list the selection was built from")
    ap.add_argument("--max-embeddings", required=True, type=int)
    ap.add_argument("--max-runtime-seconds", required=True, type=int)
    ap.add_argument("--scope", choices=["pilot", "all-normal"], default="pilot",
                    help="pilot: the selection's pilot list; all-normal: every normal-class file in the selection")
    ap.add_argument("--database", default="aibrain_test")
    ap.add_argument("--allow-non-test", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="classify and select in memory; create nothing")
    a = ap.parse_args()

    if a.database != "aibrain_test" and not a.allow_non_test:
        raise SystemExit(f"refusing to use database {a.database!r} without --allow-non-test")
    selection = json.load(open(a.selection))
    by_path = {f["path"]: f for f in selection["files"]}
    pilot = ([by_path[p] for p in selection["pilot"]] if a.scope == "pilot"
             else [f for f in selection["files"] if f["class"] == "normal"])
    candidates = [CandidateObservation(root_t7_path=f["path"], member_path=None, declared_size_bytes=f["size"]) for f in pilot]
    policy = text_document_batch_policy()
    max_instances = len(pilot)
    max_bytes = sum(f["size"] for f in pilot)
    admitted = [c for c in (classify(o) for o in candidates) if policy.matches(c)]
    print(f"files in scope: {len(pilot)} | admitted by policy {policy.selection_policy_version}: {len(admitted)} | bytes: {max_bytes:,}")
    print(f"envelope: instances={max_instances} (derived) bytes={max_bytes:,} (derived) "
          f"embeddings={a.max_embeddings} (operator cap) runtime_s={a.max_runtime_seconds} (operator cap)")
    if a.dry_run:
        print("DRY RUN - nothing created.")
        return

    engine = create_engine(make_url(settings.DATABASE_URL).set(database=a.database))
    print(f"Using database: {a.database}")
    with Session(engine) as db:
        now = datetime.now(UTC)
        discovery = DiscoveryRunService(db).record_run(
            run_kind=DiscoveryRunKind.D3_MASTER_MANIFEST, source_root=selection["root"],
            report_path=a.report, run_started_at=now, run_completed_at=now)
        print(f"DiscoveryRun id={discovery.id} kind={discovery.run_kind.value} report_sha256={discovery.report_sha256[:12]}...")
        batch = BatchCreationService(db).create_batch(
            discovery_run=discovery, candidates=candidates, policy=policy,
            max_source_instances=max_instances, max_source_bytes=max_bytes, max_extracted_bytes=None,
            max_embeddings=a.max_embeddings, max_runtime_seconds=a.max_runtime_seconds,
            classifier_version=("master-copy-pilot-v1" if a.scope == "pilot" else "master-copy-priority-v1"))
        if batch is None:
            print("No batch created - nothing eligible.")
            return
        print(f"IngestionBatch id={batch.id} classification_run_id={batch.classification_run_id} status={batch.status.value} "
              f"source_instances_selected={batch.source_instances_selected} source_bytes_selected={batch.source_bytes_selected}")


if __name__ == "__main__":
    main()
