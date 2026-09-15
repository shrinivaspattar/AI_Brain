#!/usr/bin/env python3
"""M32 real-T7 pilot batch creation - the first real-corpus touch in
the entire Chain 2 project.

NO REAL T7 PATH IS HARDCODED IN THIS FILE. The actual selection (two
real paths, their known content hash/size, and the derived envelope)
lives in the gitignored sibling `t7_chain2_pilot_selection.json` - this
script only knows how to read that file's shape, matching the existing
`scripts/t7_batch_ingestion.py` convention exactly.

This script does NOT open, read, or touch any T7 file itself - it only
creates database rows (one DiscoveryRun referencing the already-
existing, already-hashed local D1 report; one ClassificationRun/
SourceInstance pair per selected candidate; one IngestionBatch) via the
existing, unmodified BatchCreationService. The two real files are only
ever actually read later, by the existing, unmodified
scripts/run_ingestion_batch.py, against the batch this script creates.

Run once, reviewed:
    python scripts/t7_chain2_pilot_batch.py
"""

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
from app.classification.deterministic_selector import CandidateObservation  # noqa: E402
from app.classification.discovery_run_service import DiscoveryRunService  # noqa: E402
from app.classification.policy_evaluator import text_document_batch_policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.models.discovery_run import DiscoveryRunKind  # noqa: E402

SELECTION_PATH = Path(__file__).resolve().parent / "t7_chain2_pilot_selection.json"
D1_REPORT_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "t7_discovery" / "duplicate_analysis.json"

_REQUIRED_KEYS = ("source_root", "content_hash", "size_bytes", "paths", "envelope")


def _load_selection() -> dict:
    if not SELECTION_PATH.is_file():
        raise SystemExit(
            f"Missing {SELECTION_PATH} - this gitignored file must supply the real "
            "candidate paths for this pilot (see this script's module docstring)."
        )
    with SELECTION_PATH.open() as fh:
        selection = json.load(fh)
    missing = [k for k in _REQUIRED_KEYS if k not in selection]
    if missing:
        raise SystemExit(f"{SELECTION_PATH} is missing required keys: {missing}")
    return selection


def main() -> None:
    selection = _load_selection()

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain")
    engine = create_engine(database_url)

    print("Using database: aibrain (production)")

    with Session(engine) as db:
        discovery = DiscoveryRunService(db).record_run(
            run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
            source_root=selection["source_root"],
            report_path=D1_REPORT_PATH,
            run_started_at=datetime.now(UTC),
            run_completed_at=datetime.now(UTC),
        )
        print(f"Recorded DiscoveryRun id={discovery.id} report_sha256={discovery.report_sha256[:12]}...")

        candidates = [
            CandidateObservation(
                root_t7_path=path,
                member_path=None,
                declared_size_bytes=selection["size_bytes"],
                d1_duplicate_group_id=selection["content_hash"],
            )
            for path in selection["paths"]
        ]

        policy = text_document_batch_policy()
        envelope = selection["envelope"]

        batch = BatchCreationService(db).create_batch(
            discovery_run=discovery,
            candidates=candidates,
            policy=policy,
            max_source_instances=envelope["max_source_instances"],
            max_source_bytes=envelope["max_source_bytes"],
            max_extracted_bytes=None,
            max_embeddings=envelope["max_embeddings"],
            max_runtime_seconds=envelope["max_runtime_seconds"],
            classifier_version="m32-real-t7-pilot-v1",
        )

        if batch is None:
            print("No batch created - selection produced zero eligible/selected candidates.")
            return

        print(
            f"Created IngestionBatch id={batch.id} classification_run_id={batch.classification_run_id} "
            f"status={batch.status.value} source_instances_selected={batch.source_instances_selected} "
            f"source_bytes_selected={batch.source_bytes_selected}"
        )


if __name__ == "__main__":
    main()
