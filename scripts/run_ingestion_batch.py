#!/usr/bin/env python3
"""Operator CLI entrypoint for BatchOrchestratorService (Milestone 12).

Usage:
    python scripts/run_ingestion_batch.py --batch-id <id> --workspace-root <path> [--database NAME]

Runs exactly one bounded, attended BatchOrchestratorService.run_once()
invocation against an existing IngestionBatch, then prints a plain-
text BatchReportService.generate_report() summary. Milestone 11 gave
the scaled pipeline its first real, callable entry point; this
milestone gives that entry point its first real caller outside test
code. See "M12 — Operator CLI Design & Freeze" and its Final CLI
Contract Reconciliation for the frozen contract this implements.

DATABASE SAFETY: --database defaults to "aibrain_test", never to
settings.DATABASE_URL's own embedded database name (production
"aibrain"). The engine is always built via
`make_url(settings.DATABASE_URL).set(database=args.database)` -
settings.DATABASE_URL supplies only host/credentials here, never a
database name. Reaching production requires passing `--database
aibrain` explicitly; there is no way to reach it by omission.

T7 SAFETY: this script takes no source-path argument of any kind.
Real T7 paths only ever enter the system earlier, via the separately-
gated BatchCreationService/SourceInstanceService, well before any
batch_id this script could be given. A T7-path blacklist here would
defend an attack surface this tool does not have (see the M12 Design
& Freeze's explicit T7 defense-in-depth evaluation: no guard).

ERROR HANDLING: batch existence is checked directly, once, before
calling run_once()/generate_report() - not by catching ValueError.
ValueError is raised throughout app.classification for many unrelated
reasons (write-once violations, invalid attempt fields, and more); a
blanket `except ValueError` around the orchestrator call could
mislabel a real defect as a harmless "batch not found." Once the
explicit pre-check has passed, any exception from either call is, by
construction, something else, and is left to propagate uncaught.

EXIT CODES:
    0 - ran to completion (regardless of the batch's resulting status)
    1 - the given --batch-id does not exist, or any other error caught
        and reported by this script
    2 - argparse usage error (argparse's own default)
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.classification.batch_orchestrator_service import BatchOrchestratorService  # noqa: E402
from app.classification.batch_report_service import BatchReport, BatchReportService  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.models.ingestion_batch import IngestionBatch  # noqa: E402

logger = logging.getLogger("run_ingestion_batch")


def _build_engine(database: str):
    database_url = make_url(settings.DATABASE_URL).set(database=database)
    return create_engine(database_url)


def _format_report(report: BatchReport) -> str:
    status_line = f"Batch {report.batch_id}: {report.status.value}"
    if report.stop_reason is not None:
        status_line += f" ({report.stop_reason.value})"
    lines = [
        status_line,
        f"  eligible_source_count:       {report.eligible_source_count}",
        f"  selectable_count:            {report.selectable_count}",
        f"  source_instances_selected:   {report.source_instances_selected}",
        f"  source_bytes_selected:       {report.source_bytes_selected}",
        f"  attempted_source_count:      {report.attempted_source_count}",
        f"  unattempted_selected_count:  {report.unattempted_selected_count}",
        f"  terminal_source_count:       {report.terminal_source_count}",
        f"  successful_ingestion_count:  {report.successful_ingestion_count}",
        f"  extracted_bytes_consumed:    {report.extracted_bytes_consumed}",
        f"  embeddings_reserved:         {report.embeddings_reserved}",
        f"  monotonic_runtime_seconds:   {report.monotonic_runtime_seconds_consumed:.3f}",
    ]
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-id", type=int, required=True, help="Existing IngestionBatch id to run")
    parser.add_argument(
        "--workspace-root",
        type=Path,
        required=True,
        help="Local staging directory for extracted/normalized content (never a T7 path)",
    )
    parser.add_argument(
        "--database",
        default="aibrain_test",
        help='Database name to connect to (default: "aibrain_test"). Never defaults to production.',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    print(f"Using database: {args.database}")

    engine = _build_engine(args.database)
    db = Session(bind=engine)
    try:
        batch = db.get(IngestionBatch, args.batch_id)
        if batch is None:
            print(f"IngestionBatch {args.batch_id} not found in database '{args.database}'.")
            return 1

        worker_id = f"cli-{uuid.uuid4()}"
        print(f"Running batch {args.batch_id} as worker {worker_id}...")

        orchestrator = BatchOrchestratorService(db)
        result = orchestrator.run_once(
            args.batch_id,
            worker_id=worker_id,
            workspace_root=args.workspace_root,
        )

        for stage in result.stage_results:
            print(f"  stage {stage.stage}: processed {stage.processed_count}")
        if result.completion is not None:
            print(f"  completion: applied={result.completion.applied} status={result.completion.batch.status.value}")

        report = BatchReportService(db).generate_report(args.batch_id)
        print()
        print(_format_report(report))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
