#!/usr/bin/env python3
"""One-off, attended cleanup of the stray IngestionBatch created by an
accidental duplicate re-run of t7_chain2_pilot_batch.py during the M32
pilot (operator pasted the full prior terminal transcript back into
their own shell).

The batch never started (status stays PLANNED, no claim/process ever
touched its SourceInstance rows), so no reservation, embedding, or
document work exists to unwind. The frozen batch state machine has no
PLANNED -> terminal transition (BatchControlService.abort() only
accepts RUNNING/PAUSED sources), so there is no legitimate service
call that "closes" a PLANNED batch - the correct fix for a row that
should never have been created is deleting it and its own dependents,
nothing else.

Deletes, in FK order, ONLY the rows created by the stray duplicate run:
  IngestionBatch(batch_id)
  -> ProvenanceLink rows for those SourceInstance rows (each SourceInstance
     gets its own root T7_FILE link at creation time, independent of
     whether identity resolution / any pipeline stage ever ran)
  -> SourceInstance rows with classification_run_id = that batch's run
  -> ClassificationRun(that run)
  -> DiscoveryRun(that run's discovery_run_id)

Refuses unless the batch is still PLANNED (never started) and has zero
IngestionAttempt rows against any of its SourceInstance rows - the same
"only touch what's provably untouched" guard as
t7_chain2_pilot_reset_group.py's FAILED-only guard.

Usage:
    python scripts/t7_chain2_cleanup_stray_batch.py <ingestion_batch_id>
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.classification_run import ClassificationRun  # noqa: E402
from app.models.discovery_run import DiscoveryRun  # noqa: E402
from app.models.ingestion_attempt import IngestionAttempt  # noqa: E402
from app.models.ingestion_batch import BatchStatus, IngestionBatch  # noqa: E402
from app.models.provenance_link import ProvenanceLink  # noqa: E402
from app.models.source_instance import SourceInstance  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python t7_chain2_cleanup_stray_batch.py <ingestion_batch_id>")
    batch_id = int(sys.argv[1])

    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain"))
    with Session(engine) as db:
        batch = db.get(IngestionBatch, batch_id)
        if batch is None:
            raise SystemExit(f"IngestionBatch {batch_id} not found")

        if batch.status != BatchStatus.PLANNED:
            raise SystemExit(
                f"Refusing to delete: status is {batch.status.value}, not PLANNED. "
                "This script only handles a batch that was created but never started."
            )

        run_id = batch.classification_run_id
        run = db.get(ClassificationRun, run_id)
        if run is None:
            raise SystemExit(f"ClassificationRun {run_id} not found")
        discovery_run_ids = [
            did
            for did in (run.d0_discovery_run_id, run.d1_discovery_run_id, run.d2_discovery_run_id)
            if did is not None
        ]

        instances = db.execute(
            select(SourceInstance).where(SourceInstance.classification_run_id == run_id)
        ).scalars().all()
        instance_ids = [i.id for i in instances]

        attempts = []
        if instance_ids:
            attempts = db.execute(
                select(IngestionAttempt).where(IngestionAttempt.source_instance_id.in_(instance_ids))
            ).scalars().all()
        if attempts:
            raise SystemExit(
                f"Refusing to delete: {len(attempts)} IngestionAttempt row(s) reference this batch's "
                "SourceInstance rows - this batch was touched by processing, not just created."
            )

        provenance_links = []
        if instance_ids:
            provenance_links = db.execute(
                select(ProvenanceLink).where(ProvenanceLink.source_instance_id.in_(instance_ids))
            ).scalars().all()

        print(f"About to delete:")
        print(f"  IngestionBatch id={batch.id} status={batch.status.value}")
        print(f"  ClassificationRun id={run.id}")
        print(f"  SourceInstance ids={instance_ids}")
        print(f"  ProvenanceLink ids={[pl.id for pl in provenance_links]}")
        print(f"  DiscoveryRun ids={discovery_run_ids} (only ones with no other ClassificationRun reference)")

        # Flush each deletion in FK order explicitly - these models have no
        # ORM relationship() between them (only raw FK columns), so
        # autoflush has no dependency graph to order these correctly on
        # its own.
        db.delete(batch)
        db.flush()
        for link in provenance_links:
            db.delete(link)
        db.flush()
        for instance in instances:
            db.delete(instance)
        db.flush()
        db.delete(run)
        db.flush()

        for discovery_run_id in discovery_run_ids:
            other_runs_on_discovery = db.execute(
                select(ClassificationRun.id).where(
                    ClassificationRun.id != run_id,
                    (ClassificationRun.d0_discovery_run_id == discovery_run_id)
                    | (ClassificationRun.d1_discovery_run_id == discovery_run_id)
                    | (ClassificationRun.d2_discovery_run_id == discovery_run_id),
                )
            ).scalars().all()
            if not other_runs_on_discovery:
                discovery = db.get(DiscoveryRun, discovery_run_id)
                if discovery is not None:
                    db.delete(discovery)
                    print(f"  (deleting DiscoveryRun id={discovery_run_id} too - no other run references it)")
            else:
                print(
                    f"  (keeping DiscoveryRun id={discovery_run_id} - referenced by other run(s) "
                    f"{other_runs_on_discovery})"
                )

        db.commit()
        print("Done.")


if __name__ == "__main__":
    main()
