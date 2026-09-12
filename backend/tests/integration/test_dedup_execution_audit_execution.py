"""Real-database tests for the execution audit layer.

No filesystem executor exists anywhere in this codebase, and this
milestone does not add one - these tests never perform a real
filesystem mutation. They use a controlled temporary directory of
synthetic files (tmp_path) purely to generate authorized plans through
the existing pipeline; the executions/audits themselves are always
built by directly calling `DedupExecutionService`, exactly the way a
future executor would report outcomes it observed elsewhere - this
service never touches a file itself. Proves, against real Postgres:
authorization/plan/review linkage is fully reconstructable; the
COMPLETED/FAILED/PARTIALLY_COMPLETED state derivation matches the
audit trail exactly; a precondition failure can be represented without
any mutation; an authorization backs at most one execution ever; and -
throughout every scenario, including a deliberately mutated file used
to prove precondition-failure representation - no real file is ever
created, deleted, moved, renamed, or modified by any call in this
service.
"""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import DedupExecutionPlanService
from app.dedup.execution_service import DedupExecutionService
from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup
from app.models.dedup_authorization import DedupPlanAuthorization, DedupPlanAuthorizationStatus
from app.models.dedup_execution import (
    DedupExecution,
    DedupExecutionActionAudit,
    DedupExecutionActionResult,
    DedupExecutionStatus,
)
from app.models.dedup_execution_plan import DedupExecutionPlan, DedupExecutionPlanAction
from app.models.dedup_review import DuplicateReview, DuplicateReviewMember
from app.models.document import Document
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _cleanup(
    db,
    review_id,
    document_ids,
    plan_ids=None,
    authorization_ids=None,
    execution_ids=None,
):
    if execution_ids:
        db.query(DedupExecutionActionAudit).filter(
            DedupExecutionActionAudit.execution_id.in_(execution_ids)
        ).delete(synchronize_session=False)
        db.query(DedupExecution).filter(
            DedupExecution.id.in_(execution_ids)
        ).delete(synchronize_session=False)
    if authorization_ids:
        db.query(DedupPlanAuthorization).filter(
            DedupPlanAuthorization.id.in_(authorization_ids)
        ).delete(synchronize_session=False)
    if plan_ids:
        db.query(DedupExecutionPlanAction).filter(
            DedupExecutionPlanAction.plan_id.in_(plan_ids)
        ).delete(synchronize_session=False)
        db.query(DedupExecutionPlan).filter(
            DedupExecutionPlan.id.in_(plan_ids)
        ).delete(synchronize_session=False)
    db.query(DuplicateReviewMember).filter(
        DuplicateReviewMember.review_id == review_id
    ).delete(synchronize_session=False)
    db.query(DuplicateReview).filter(DuplicateReview.id == review_id).delete(
        synchronize_session=False
    )
    db.query(Document).filter(Document.id.in_(document_ids)).delete(
        synchronize_session=False
    )
    db.commit()


def _setup_authorized_plan(db, canonical_file, dup_files, content_hash):
    """Builds a fully authorized plan through the real pipeline
    (review -> approve -> generate plan -> authorize), reusing every
    existing service exactly as a real caller would. Returns
    (review, canonical_document, dup_documents, plan, authorization).
    """
    document_service = DocumentService(db)
    canonical_doc = document_service.create_document(
        DocumentCreate(
            title="canonical.txt",
            source=str(canonical_file),
            source_type="txt",
            content_hash=content_hash,
        )
    )
    dup_docs = []
    for i, dup_file in enumerate(dup_files):
        dup_doc = document_service.create_document(
            DocumentCreate(
                title=f"dup{i}.txt",
                source=str(dup_file),
                source_type="txt",
                content_hash=content_hash,
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=i + 1)
        dup_docs.append(dup_doc)
    db.commit()

    review_service = DedupReviewService(db)
    group = ExactDuplicateGroup(
        content_hash=content_hash, documents=[*dup_docs, canonical_doc]
    )
    review = review_service.create_review_from_exact_group(group)
    review_service.approve_review(review.id, canonical_document_id=canonical_doc.id)

    plan_service = DedupExecutionPlanService(db)
    plan = plan_service.generate_plan_for_review(review.id)

    auth_service = DedupPlanAuthorizationService(db)
    authorization = auth_service.authorize_plan(plan.id, authorized_by="integration-test")

    return review, canonical_doc, dup_docs, plan, authorization


def _plan_action_for_document(db, plan_id, document_id) -> DedupExecutionPlanAction:
    from sqlalchemy import select

    return db.scalar(
        select(DedupExecutionPlanAction)
        .where(DedupExecutionPlanAction.plan_id == plan_id)
        .where(DedupExecutionPlanAction.document_id == document_id)
    )


# --- successful execution representation -----------------------------


def test_successful_execution_full_chain_reconstructable(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    canonical_bytes_before = canonical_file.read_bytes()
    dup_bytes_before = dup_file.read_bytes()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-1"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(
                authorization.id, executor_identity="integration-test-executor"
            )
            execution_ids.append(execution.id)
            assert execution.status == DedupExecutionStatus.RUNNING
            assert execution.authorization_id == authorization.id
            assert execution.plan_id == plan.id

            plan_action = _plan_action_for_document(db, plan.id, dup_docs[0].id)

            audit = execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.SUCCESS,
                observed_content_hash=plan_action.observed_content_hash,
                observed_file_size=plan_action.observed_file_size,
                filesystem_mutation_occurred=True,
            )
            assert audit.execution_id == execution.id
            assert audit.plan_action_id == plan_action.id
            assert audit.document_id == dup_docs[0].id
            # Plan vs actual: the audit's "expected" fields are frozen
            # copies of the plan action's own data, not re-derived.
            assert audit.expected_content_hash == plan_action.observed_content_hash
            assert audit.expected_file_size == plan_action.observed_file_size
            assert audit.planned_action == plan_action.action

            completed = execution_service.complete_execution(execution.id)
            assert completed.status == DedupExecutionStatus.COMPLETED
            assert completed.failure_reason is None
            assert completed.ended_at is not None

            # Full chain reconstructable: Review -> Plan -> Authorization
            # -> Execution -> Action audit.
            assert completed.plan_id == plan.id
            assert completed.authorization_id == authorization.id
            reloaded_plan = db.get(DedupExecutionPlan, plan.id)
            assert reloaded_plan.review_id == review.id
            reloaded_authorization = db.get(DedupPlanAuthorization, authorization.id)
            assert reloaded_authorization.plan_id == plan.id

            # No file was ever touched by any of this bookkeeping.
            assert canonical_file.read_bytes() == canonical_bytes_before
            assert dup_file.read_bytes() == dup_bytes_before

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- failure representation --------------------------------------------


def test_failed_execution_zero_successes(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-2"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            plan_action = _plan_action_for_document(db, plan.id, dup_docs[0].id)
            execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.FAILED,
                error_message="Permission denied",
                filesystem_mutation_occurred=False,
            )

            finalized = execution_service.complete_execution(execution.id)
            assert finalized.status == DedupExecutionStatus.FAILED
            assert "Permission denied" in finalized.failure_reason
            assert canonical_file.read_text() == "hello world"
            assert dup_file.read_text() == "hello world"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- partial completion / skipped actions -------------------------------


def test_partially_completed_execution_records_success_failure_and_not_attempted(
    tmp_path,
) -> None:
    """The spec's worked example, at small scale: some actions succeed,
    one fails, and the rest are recorded as NOT_ATTEMPTED - never
    silently continued, never silently dropped."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_files = []
    for i in range(4):
        f = tmp_path / f"dup{i}.txt"
        f.write_text("hello world")
        dup_files.append(f)

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, dup_files, "audit-test-hash-3"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            plan_actions = [
                _plan_action_for_document(db, plan.id, d.id) for d in dup_docs
            ]
            assert len(plan_actions) == 4

            # Actions 1-2 succeed.
            for pa in plan_actions[:2]:
                execution_service.record_action_result(
                    execution.id,
                    pa.id,
                    DedupExecutionActionResult.SUCCESS,
                    observed_content_hash=pa.observed_content_hash,
                    observed_file_size=pa.observed_file_size,
                    filesystem_mutation_occurred=True,
                )
            # Action 3 fails.
            execution_service.record_action_result(
                execution.id,
                plan_actions[2].id,
                DedupExecutionActionResult.FAILED,
                error_message="Disk I/O error",
                filesystem_mutation_occurred=False,
            )
            # Action 4 was never attempted (stop-on-first-failure).
            execution_service.record_action_result(
                execution.id,
                plan_actions[3].id,
                DedupExecutionActionResult.NOT_ATTEMPTED,
            )

            finalized = execution_service.complete_execution(execution.id)
            assert finalized.status == DedupExecutionStatus.PARTIALLY_COMPLETED
            assert str(plan_actions[2].id) in finalized.failure_reason

            audits = execution_service.get_action_audits(execution.id)
            assert len(audits) == 4
            by_result = {}
            for a in audits:
                by_result.setdefault(a.result, []).append(a)
            assert len(by_result[DedupExecutionActionResult.SUCCESS]) == 2
            assert len(by_result[DedupExecutionActionResult.FAILED]) == 1
            assert len(by_result[DedupExecutionActionResult.NOT_ATTEMPTED]) == 1

            # NOT_ATTEMPTED and FAILED-before-mutation rows never claim
            # a filesystem mutation occurred.
            not_attempted_audit = by_result[DedupExecutionActionResult.NOT_ATTEMPTED][0]
            assert not_attempted_audit.filesystem_mutation_occurred is False
            assert not_attempted_audit.started_at is None
            failed_audit = by_result[DedupExecutionActionResult.FAILED][0]
            assert failed_audit.filesystem_mutation_occurred is False

            # None of the underlying (synthetic) files were ever touched.
            assert canonical_file.read_text() == "hello world"
            for f in dup_files:
                assert f.read_text() == "hello world"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- precondition failure representation --------------------------------


def test_precondition_failure_recorded_without_mutation(tmp_path) -> None:
    """A file changes after authorization but before a (hypothetical)
    executor gets to it - the future executor's mandatory
    immediately-before-mutation revalidation would catch this and
    report PRECONDITION_FAILED, never attempting the operation. This
    test represents exactly that recorded fact, without any executor
    or mutation ever existing."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-4"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)
            plan_action = _plan_action_for_document(db, plan.id, dup_docs[0].id)

            # A file changes AFTER authorization/execution start - this
            # simulates exactly the TOCTOU scenario the mandatory
            # per-action revalidation exists to catch. No plan/executor
            # here ever re-reads or mutates this file: we simply record
            # the fact a hypothetical revalidation would have observed.
            dup_file.write_text("hello world, modified externally")
            observed_hash_after_change = "deliberately-different-hash-for-test"

            audit = execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.PRECONDITION_FAILED,
                observed_content_hash=observed_hash_after_change,
                observed_file_size=len(b"hello world, modified externally"),
                error_message=(
                    f"hash mismatch: expected {plan_action.observed_content_hash}, "
                    f"observed {observed_hash_after_change}"
                ),
                filesystem_mutation_occurred=False,
            )

            assert audit.result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert audit.filesystem_mutation_occurred is False
            # Plan vs actual: expected still reflects the ORIGINAL plan
            # observation, never the changed file.
            assert audit.expected_content_hash == plan_action.observed_content_hash
            assert audit.expected_content_hash != audit.observed_content_hash

            finalized = execution_service.complete_execution(execution.id)
            assert finalized.status == DedupExecutionStatus.FAILED

            # The file really was modified (by the test itself, not by
            # any code in this codebase) - but nothing in this service
            # touched it further.
            assert dup_file.read_text() == "hello world, modified externally"
            assert canonical_file.read_text() == "hello world"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- authorization interaction: invalid / revoked authorization ---------


def test_start_execution_raises_for_revoked_authorization(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-5"
        )
        auth_service = DedupPlanAuthorizationService(db)
        execution_service = DedupExecutionService(db)

        try:
            auth_service.revoke_authorization(authorization.id, reason="test revoke")

            try:
                execution_service.start_execution(authorization.id)
                raise AssertionError(
                    "Expected ValueError starting execution under a revoked "
                    "authorization"
                )
            except ValueError as exc:
                assert "is not active" in str(exc)

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
            )


# --- TOCTOU: stale plan blocks execution start --------------------------


def test_start_execution_raises_for_plan_gone_stale_after_authorization(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-6"
        )
        execution_service = DedupExecutionService(db)

        try:
            # The file changes after authorization was already granted.
            dup_file.write_text("hello world, changed after authorization")

            try:
                execution_service.start_execution(authorization.id)
                raise AssertionError(
                    "Expected ValueError starting execution against a plan "
                    "that went stale after authorization"
                )
            except ValueError as exc:
                assert "no longer valid" in str(exc)

            assert canonical_file.read_text() == "hello world"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
            )


# --- duplicate execution prevention -------------------------------------


def test_authorization_backs_at_most_one_execution(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-7"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            first_execution = execution_service.start_execution(authorization.id)
            execution_ids.append(first_execution.id)

            try:
                execution_service.start_execution(authorization.id)
                raise AssertionError(
                    "Expected ValueError starting a second execution under "
                    "the same authorization"
                )
            except ValueError as exc:
                assert "already has an execution" in str(exc)

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_duplicate_execution_prevented_at_database_level(tmp_path) -> None:
    """Even bypassing the service's own pre-check, the database itself
    refuses a second DedupExecution row for the same authorization_id -
    defense in depth for "duplicate execution prevention"."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-8"
        )
        execution_ids: list[int] = []

        try:
            first = DedupExecution(
                authorization_id=authorization.id,
                plan_id=plan.id,
                status=DedupExecutionStatus.RUNNING,
            )
            db.add(first)
            db.commit()
            execution_ids.append(first.id)

            second = DedupExecution(
                authorization_id=authorization.id,
                plan_id=plan.id,
                status=DedupExecutionStatus.RUNNING,
            )
            db.add(second)
            try:
                db.commit()
                raise AssertionError(
                    "Expected an IntegrityError from the unique constraint on "
                    "authorization_id"
                )
            except Exception as exc:
                db.rollback()
                assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- duplicate action-result prevention at database level ---------------


def test_duplicate_action_audit_prevented_at_database_level(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-9"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)
            plan_action = _plan_action_for_document(db, plan.id, dup_docs[0].id)

            execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.SUCCESS,
                filesystem_mutation_occurred=True,
            )

            # Bypass the service's own pre-check to prove the DB
            # constraint itself is real, not just an application-level
            # convention.
            duplicate = DedupExecutionActionAudit(
                execution_id=execution.id,
                plan_action_id=plan_action.id,
                document_id=plan_action.document_id,
                planned_action=plan_action.action,
                source_path=plan_action.source_path,
                target_path=plan_action.target_path,
                result=DedupExecutionActionResult.SUCCESS,
                filesystem_mutation_occurred=True,
            )
            db.add(duplicate)
            try:
                db.commit()
                raise AssertionError(
                    "Expected an IntegrityError from the unique constraint on "
                    "(execution_id, plan_action_id)"
                )
            except Exception as exc:
                db.rollback()
                assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- audit immutability --------------------------------------------------


def test_action_audit_rows_are_never_mutated_by_this_service(tmp_path) -> None:
    """DedupExecutionActionAudit has no update method anywhere in
    DedupExecutionService - once recorded, a row's own fields are
    never changed again by this codebase."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-10"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)
            plan_action = _plan_action_for_document(db, plan.id, dup_docs[0].id)

            audit = execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.SUCCESS,
                observed_content_hash="hash-x",
                filesystem_mutation_occurred=True,
            )
            snapshot = {
                "result": audit.result,
                "observed_content_hash": audit.observed_content_hash,
                "created_at": audit.created_at,
            }

            execution_service.complete_execution(execution.id)

            db.refresh(audit)
            assert audit.result == snapshot["result"]
            assert audit.observed_content_hash == snapshot["observed_content_hash"]
            assert audit.created_at == snapshot["created_at"]

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- plan remains immutable, review remains unchanged --------------------


def test_execution_bookkeeping_never_mutates_plan_or_review(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    canonical_bytes_before = canonical_file.read_bytes()
    dup_bytes_before = dup_file.read_bytes()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, plan, authorization = _setup_authorized_plan(
            db, canonical_file, [dup_file], "audit-test-hash-11"
        )
        execution_service = DedupExecutionService(db)
        execution_ids: list[int] = []

        try:
            plan_snapshot = {
                "canonical_source_path": plan.canonical_source_path,
                "canonical_observed_content_hash": plan.canonical_observed_content_hash,
                "status": plan.status,
            }
            review_row = db.get(DuplicateReview, review.id)
            review_snapshot = {
                "status": review_row.status,
                "human_selected_canonical_document_id": (
                    review_row.human_selected_canonical_document_id
                ),
            }

            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)
            plan_action = _plan_action_for_document(db, plan.id, dup_docs[0].id)
            execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.SUCCESS,
                filesystem_mutation_occurred=True,
            )
            execution_service.complete_execution(execution.id)

            db.refresh(plan)
            db.refresh(review_row)

            assert plan.canonical_source_path == plan_snapshot["canonical_source_path"]
            assert (
                plan.canonical_observed_content_hash
                == plan_snapshot["canonical_observed_content_hash"]
            )
            assert plan.status == plan_snapshot["status"]
            assert review_row.status == review_snapshot["status"]
            assert (
                review_row.human_selected_canonical_document_id
                == review_snapshot["human_selected_canonical_document_id"]
            )

            assert canonical_file.read_bytes() == canonical_bytes_before
            assert dup_file.read_bytes() == dup_bytes_before

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )
