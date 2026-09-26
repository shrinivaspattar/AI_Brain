#!/usr/bin/env python3
"""Drives an existing IngestionBatch to completion by repeatedly calling
BatchOrchestratorService.run_once() - each call is one bounded, attended
pass (Milestone 11's design), never a background daemon of its own, so
finishing a multi-hour batch means re-invoking it until the batch reaches
a terminal status (COMPLETED/ABORTED).

RESUMABLE BY CONSTRUCTION: if this process is killed or the machine
restarts, just run this script again with the same --batch-id. Nothing
here holds state outside the database; a stale claim from an interrupted
run_once() call expires on its own lease and gets reclaimed normally, the
same recovery path already proven for a normal single run_once() call.

DATABASE-MUTATING - run this yourself in your own terminal, never via an
agent's Bash tool. To run it in the background so it survives closing the
terminal:

    nohup python scripts/t7_chain2_priority2_run_loop.py \\
        --batch-id <id> --workspace-root documents/workspace_production \\
        --database aibrain > /tmp/priority2_ingestion.log 2>&1 &
    disown

Then check progress any time with:
    tail -f /tmp/priority2_ingestion.log
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.classification.batch_orchestrator_service import BatchOrchestratorService  # noqa: E402
from app.classification.batch_report_service import BatchReportService  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.models.ingestion_batch import BatchStatus, IngestionBatch  # noqa: E402

logger = logging.getLogger("t7_chain2_priority2_run_loop")

_TERMINAL_STATUSES = {BatchStatus.COMPLETED, BatchStatus.ABORTED}
# A pause between passes, not a rate limit on the work itself - avoids a
# tight loop if a pass happens to process zero items (e.g. everything
# momentarily claimed by a lease that hasn't expired yet).
_PASS_INTERVAL_SECONDS = 5


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-id", type=int, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--database", default="aibrain_test")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    engine = create_engine(make_url(settings.DATABASE_URL).set(database=args.database))
    print(f"Using database: {args.database}")

    pass_number = 0
    while True:
        pass_number += 1
        with Session(engine) as db:
            batch = db.get(IngestionBatch, args.batch_id)
            if batch is None:
                print(f"IngestionBatch {args.batch_id} not found in database '{args.database}'.")
                return 1

            if batch.status in _TERMINAL_STATUSES:
                report = BatchReportService(db).generate_report(args.batch_id)
                print(f"Batch {args.batch_id} reached terminal status: {batch.status.value}")
                print(f"  successful_ingestion_count: {report.successful_ingestion_count}")
                print(f"  terminal_source_count:      {report.terminal_source_count}")
                print(f"  embeddings_reserved:        {report.embeddings_reserved}")
                print(f"  monotonic_runtime_seconds:  {report.monotonic_runtime_seconds_consumed:.1f}")
                return 0

            worker_id = f"loop-{uuid.uuid4()}"
            result = BatchOrchestratorService(db).run_once(
                args.batch_id,
                worker_id=worker_id,
                workspace_root=args.workspace_root,
            )

            processed_total = sum(stage.processed_count for stage in result.stage_results)
            report = BatchReportService(db).generate_report(args.batch_id)
            print(
                f"[pass {pass_number}] processed={processed_total} "
                f"successful={report.successful_ingestion_count} "
                f"terminal={report.terminal_source_count}/{report.source_instances_selected} "
                f"embeddings={report.embeddings_reserved}",
                flush=True,
            )

        time.sleep(_PASS_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
