#!/usr/bin/env python3
"""Minimal, safe, end-to-end proof that the full KRM/dedup pipeline
actually works: detection -> human review -> approval -> dry-run plan
-> explicit authorization -> the real DedupFilesystemExecutor -> audit.

SCOPE, DELIBERATE AND NARROW: every path this script touches is
freshly created under a scratch directory this script itself creates
and owns - never the real project, never any real personal file, never
the T7 drive. This demonstrates that the wiring between all seven
dedup services actually works when driven end-to-end in one place; it
is NOT a general-purpose dedup-execution tool and must never be pointed
at a real corpus without a proper, separately-authorized design pass
for that much larger, much more consequential step.

Runs against `aibrain_test`, never production `aibrain`. Cleans up
every row it creates at the end (in FK order), regardless of success or
failure - this session already found that leftover rows in
`aibrain_test` from tests that never cleaned up after themselves caused
a real, later test failure (a near-duplicate query pushed past its
result limit), so a one-off demo script has no excuse to leave its own
debris behind either.

Usage:
    python scripts/dedup_pipeline_synthetic_demo.py
"""

import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.dedup.authorization_service import DedupPlanAuthorizationService  # noqa: E402
from app.dedup.execution_plan_service import DedupExecutionPlanService  # noqa: E402
from app.dedup.execution_service import DedupExecutionService  # noqa: E402
from app.dedup.executor import DedupFilesystemExecutor  # noqa: E402
from app.dedup.review_service import DedupReviewService  # noqa: E402
from app.dedup.service import DeduplicationService  # noqa: E402
from app.models.dedup_execution import DedupExecution, DedupExecutionActionAudit  # noqa: E402
from app.models.dedup_execution_plan import DedupExecutionPlan, DedupExecutionPlanAction  # noqa: E402
from app.models.dedup_authorization import DedupPlanAuthorization  # noqa: E402
from app.models.dedup_review import DuplicateReview, DuplicateReviewMember, DuplicateReviewMemberRole  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.document_chunk import DocumentChunk  # noqa: E402
from app.models.import_job import ImportJob  # noqa: E402
from app.schemas.import_job import ImportJobCreate  # noqa: E402
from app.services.import_job_service import ImportJobService  # noqa: E402

DUPLICATE_CONTENT = b"This is synthetic demo content, not real data.\n"
UNIQUE_CONTENT = b"This file is unique, not part of any duplicate group.\n"


def _cleanup(db: Session, *, job_id, review_id, plan_id, authorization_id, execution_id) -> None:
    """Deletes exactly the rows this run created, in FK-safe order.
    Every argument is None if that step never completed - each delete
    is skipped for a None id rather than guessed at.

    `document_ids` is deliberately NOT a caller-supplied argument: an
    earlier version took only the duplicate-group's document ids and
    left the import job's OTHER document (the unique, non-duplicate
    file) behind, which then blocked the ImportJob delete via its own
    FK - found the hard way, by this exact IntegrityError, the first
    time this script ran. Every document belonging to this job is
    looked up fresh here instead, so cleanup is correct regardless of
    how many documents any future variant of this demo creates."""
    document_ids = (
        [row[0] for row in db.query(Document.id).filter_by(import_job_id=job_id).all()]
        if job_id is not None
        else []
    )
    if execution_id is not None:
        db.query(DedupExecutionActionAudit).filter_by(execution_id=execution_id).delete()
        db.query(DedupExecution).filter_by(id=execution_id).delete()
    if authorization_id is not None:
        db.query(DedupPlanAuthorization).filter_by(id=authorization_id).delete()
    if plan_id is not None:
        db.query(DedupExecutionPlanAction).filter_by(plan_id=plan_id).delete()
        db.query(DedupExecutionPlan).filter_by(id=plan_id).delete()
    if review_id is not None:
        db.query(DuplicateReviewMember).filter_by(review_id=review_id).delete()
        db.query(DuplicateReview).filter_by(id=review_id).delete()
    if document_ids:
        db.query(DocumentChunk).filter(DocumentChunk.document_id.in_(document_ids)).delete(
            synchronize_session=False
        )
        db.query(Document).filter(Document.id.in_(document_ids)).delete(synchronize_session=False)
    if job_id is not None:
        db.query(ImportJob).filter_by(id=job_id).delete()
    db.commit()


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="aibrain_dedup_demo_"))
    source_dir = scratch / "source"
    quarantine_dir = scratch / "quarantine"
    ingestion_dir = scratch / "ingestion_dest"
    source_dir.mkdir()
    quarantine_dir.mkdir()
    ingestion_dir.mkdir()

    (source_dir / "duplicate_a.txt").write_bytes(DUPLICATE_CONTENT)
    (source_dir / "duplicate_b.txt").write_bytes(DUPLICATE_CONTENT)
    (source_dir / "unique.txt").write_bytes(UNIQUE_CONTENT)

    print(f"Scratch root: {scratch}")
    print(f"  source:     {source_dir}  (duplicate_a.txt, duplicate_b.txt, unique.txt)")
    print(f"  quarantine: {quarantine_dir}  (empty)")
    print()

    job_id = review_id = plan_id = authorization_id = execution_id = None

    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain_test"))
    with Session(engine) as db:
        try:
            # 1. Ingest via Chain 1 (ImportJob) - creates real Document rows.
            job_service = ImportJobService(db, ingestion_dir=ingestion_dir)
            job = job_service.create_job(
                ImportJobCreate(name="dedup-demo", source_path=str(source_dir), source_type="directory")
            )
            job_id = job.id
            job_service.execute_job(job.id)
            print(f"Step 1 - ImportJob {job.id}: ingested {job.files_discovered} files")

            # 2. Detection.
            groups = DeduplicationService(db).find_exact_duplicates()
            groups = [g for g in groups if len(g.documents) >= 2]
            if not groups:
                raise SystemExit("No exact-duplicate group detected - unexpected, aborting")
            group = groups[0]
            document_ids = [d.id for d in group.documents]
            print(f"Step 2 - detected exact-duplicate group: content_hash={group.content_hash[:12]}..., "
                  f"{len(group.documents)} documents")

            # 3. Human review (materialize the finding).
            review_service = DedupReviewService(db)
            review = review_service.create_review_from_exact_group(group)
            review_id = review.id
            print(f"Step 3 - DuplicateReview {review.id} created, status={review.status.value}")

            pairs = review_service.get_review_members_with_documents(review.id)
            canonical_doc_id = next(
                document.id for member, document in pairs
                if member.role == DuplicateReviewMemberRole.RECOMMENDED_CANONICAL
            )

            # 4. Approval (human decision: confirm the canonical copy).
            review = review_service.approve_review(
                review.id, canonical_document_id=canonical_doc_id, reviewer_decision="demo-script-approval"
            )
            print(f"Step 4 - review approved, canonical_document_id={canonical_doc_id}")

            # 5. Dry-run execution plan.
            plan = DedupExecutionPlanService(db).generate_plan_for_review(review.id)
            plan_id = plan.id
            print(f"Step 5 - DedupExecutionPlan {plan.id} generated, status={plan.status.value}")

            # 6. Explicit execution authorization.
            authorization = DedupPlanAuthorizationService(db).authorize_plan(
                plan.id, authorized_by="demo-script"
            )
            authorization_id = authorization.id
            print(f"Step 6 - authorization {authorization.id} granted, status={authorization.status.value}")

            # 7. Start the execution record.
            execution = DedupExecutionService(db).start_execution(
                authorization.id, executor_identity="demo-script"
            )
            execution_id = execution.id
            print(f"Step 7 - DedupExecution {execution.id} started, status={execution.status.value}")

            # 8. The real filesystem executor - the only step that mutates anything.
            executor = DedupFilesystemExecutor(db, allowed_root=source_dir, quarantine_root=quarantine_dir)
            execution = executor.execute(execution.id, confirm=True)
            print(f"Step 8 - executor finished: execution status={execution.status.value}")

            print()
            print("Action audit trail:")
            for audit in DedupExecutionService(db).get_action_audits(execution.id):
                print(f"  action_id={audit.plan_action_id} result={audit.result.value} "
                      f"mutation_occurred={audit.filesystem_mutation_occurred} "
                      f"error={audit.error_message!r}")

            print()
            print(f"Remaining in source ({source_dir}): {sorted(p.name for p in source_dir.iterdir())}")
            print(f"Remaining in quarantine ({quarantine_dir}): {sorted(p.name for p in quarantine_dir.iterdir())}")

        finally:
            _cleanup(
                db,
                job_id=job_id,
                review_id=review_id,
                plan_id=plan_id,
                authorization_id=authorization_id,
                execution_id=execution_id,
            )
            print()
            print("Cleaned up every row this run created in aibrain_test.")

    print(f"Done. Scratch directory left at {scratch} for inspection - delete it manually when done.")


if __name__ == "__main__":
    main()
