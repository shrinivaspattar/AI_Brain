#!/usr/bin/env python3
"""Second priority batch: 4,967 previously-undiscovered .md/.txt files found
under a Syncthing-synced mirror folder on the T7 (not the original curated
"priority" batch's source), covering an Obsidian vault, a Standard Notes
backup, phone/D-drive backups, and misc personal notes - see the 2026-09-23
session's discovery/exclusion analysis for how this candidate set was
derived (path diff against source_instances.root_t7_path, then 25 verified-
junk files and the 20 largest outlier/dump/export files excluded).

NO REAL T7 PATH IS HARDCODED IN THIS FILE. All 4,967 real paths, their
sizes, and the envelope (derived from a real dry-run extract+chunk pass,
not invented - see selection['envelope']) live in the gitignored sibling
`t7_chain2_priority2_selection.json`.

Same methodology as t7_chain2_pilot_expansion_batch.py: this script only
creates database rows (one DiscoveryRun, one ClassificationRun/
SourceInstance pair per candidate, one IngestionBatch) via the existing,
unmodified BatchCreationService. It does not open, read, or touch any T7
file itself - the files are only actually read later, by the existing,
unmodified scripts/run_ingestion_batch.py, against the batch this script
creates.

DATABASE-MUTATING - run this yourself in your own terminal, not via an
agent's Bash tool (see project history: the auto-mode safety classifier
blocks direct database-mutating Bash commands, and even where it
wouldn't, a batch this size touching production is squarely a "run it
yourself" action).

Run once, reviewed:
    python scripts/t7_chain2_priority2_batch.py
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

SELECTION_PATH = Path(__file__).resolve().parent / "t7_chain2_priority2_selection.json"
# The dedup keep-list this candidate set was diffed against - a D3 master
# manifest, not a D1 duplicate-analysis report (no per-group duplicate ids
# are used here; content-identity dedup against the existing corpus
# happens automatically later, at normalization time).
D3_REPORT_PATH = (
    Path(__file__).resolve().parents[1] / "knowledge" / "t7_discovery" / "plan" / "fresh" / "keep_files.csv"
)

_REQUIRED_KEYS = ("source_root", "paths", "envelope")


def _load_selection() -> dict:
    if not SELECTION_PATH.is_file():
        raise SystemExit(
            f"Missing {SELECTION_PATH} - this gitignored file must supply the real "
            "candidate paths for this batch (see this script's module docstring)."
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
            run_kind=DiscoveryRunKind.D3_MASTER_MANIFEST,
            source_root=selection["source_root"],
            report_path=D3_REPORT_PATH,
            run_started_at=datetime.now(UTC),
            run_completed_at=datetime.now(UTC),
        )
        print(f"Recorded DiscoveryRun id={discovery.id} report_sha256={discovery.report_sha256[:12]}...")

        candidates = [
            CandidateObservation(
                root_t7_path=entry["path"],
                member_path=None,
                declared_size_bytes=entry["size_bytes"],
            )
            for entry in selection["paths"]
        ]
        print(f"Built {len(candidates)} candidate observations.")

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
            classifier_version="t7-priority2-obsidian-mirror-v1",
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
