"""Real-database + real-synthetic-filesystem tests for
`DedupReconciliationService` - see "Executor Reconciliation & TOCTOU
Strategy" in AI_Brain_Architecture.md for the design this implements.

These tests never exercise the real filesystem executor to PRODUCE an
UNKNOWN audit - reconciliation doesn't care how an audit became
UNKNOWN, only that it is, so tests construct that state directly via
`DedupExecutionService.record_action_result`, exactly the way a crash-
recovery step would. Every source file used is a disposable `tmp_path`
file, never the real personal corpus.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import DedupExecutionPlanService, _observe_file
from app.dedup.execution_service import DedupExecutionService
from app.dedup.reconciliation_service import DedupReconciliationService
from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup
from app.models.dedup_authorization import DedupPlanAuthorization
from app.models.dedup_execution import (
    DedupExecution,
    DedupExecutionActionAudit,
    DedupExecutionActionReconciliation,
    DedupExecutionActionResult,
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
        db.query(DedupExecutionActionReconciliation).filter(
            DedupExecutionActionReconciliation.audit_id.in_(
                db.query(DedupExecutionActionAudit.id).filter(
                    DedupExecutionActionAudit.execution_id.in_(execution_ids)
                )
            )
        ).delete(synchronize_session=False)
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


def _setup_running_execution_with_unknown_audit(db, tmp_path, content=b"hello world"):
    """Builds one real authorized plan/execution through the full
    pipeline, then directly records an UNKNOWN audit for its one
    planned action - simulating a crash-recovery finding without
    exercising the executor itself. Returns (review, canonical_doc,
    dup_doc, source_file, plan, authorization, execution, audit)."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_bytes(content)
    dup_file = tmp_path / "dup.txt"
    dup_file.write_bytes(content)

    document_service = DocumentService(db)
    canonical_doc = document_service.create_document(
        DocumentCreate(
            title="canonical.txt",
            source=str(canonical_file),
            source_type="txt",
            content_hash="reconcile-test-hash",
        )
    )
    dup_doc = document_service.create_document(
        DocumentCreate(
            title="dup.txt",
            source=str(dup_file),
            source_type="txt",
            content_hash="reconcile-test-hash",
        )
    )
    dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
    db.commit()

    review_service = DedupReviewService(db)
    group = ExactDuplicateGroup(
        content_hash="reconcile-test-hash", documents=[dup_doc, canonical_doc]
    )
    review = review_service.create_review_from_exact_group(group)
    review_service.approve_review(review.id, canonical_document_id=canonical_doc.id)

    plan_service = DedupExecutionPlanService(db)
    plan = plan_service.generate_plan_for_review(review.id)

    auth_service = DedupPlanAuthorizationService(db)
    authorization = auth_service.authorize_plan(plan.id, authorized_by="reconcile-test")

    execution_service = DedupExecutionService(db)
    execution = execution_service.start_execution(authorization.id)

    plan_action = db.scalar(
        select(DedupExecutionPlanAction).where(
            DedupExecutionPlanAction.plan_id == plan.id
        )
    )

    audit = execution_service.record_action_result(
        execution.id,
        plan_action.id,
        DedupExecutionActionResult.UNKNOWN,
        filesystem_mutation_occurred=None,
        error_message="simulated indeterminate outcome for reconciliation testing",
    )

    return review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit


# --- reconcile_action: happy paths --------------------------------------


def test_reconcile_action_success_when_source_confirmed_gone(tmp_path) -> None:
    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            # The human independently confirmed the delete really
            # happened - the source file genuinely no longer exists.
            dup_file.unlink()

            reconciliation = DedupReconciliationService(db).reconcile_action(
                audit.id,
                DedupExecutionActionResult.SUCCESS,
                verified_by="tester",
                verification_method="inspected the quarantine directory directly",
            )

            assert reconciliation.audit_id == audit.id
            assert reconciliation.verified_result == DedupExecutionActionResult.SUCCESS
            assert reconciliation.verified_by == "tester"
            assert reconciliation.observed_content_hash is None
            assert reconciliation.observed_file_size is None

            # Purely additive: the original audit row is untouched.
            db.refresh(audit)
            assert audit.result == DedupExecutionActionResult.UNKNOWN
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


def test_reconcile_action_failed_when_source_confirmed_unchanged(tmp_path) -> None:
    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            # Source file is untouched - "nothing happened" is true.
            observation = _observe_file(str(dup_file))

            reconciliation = DedupReconciliationService(db).reconcile_action(
                audit.id,
                DedupExecutionActionResult.FAILED,
                verified_by="tester",
                verification_method="inspected the source path directly, file present and unchanged",
            )

            assert reconciliation.verified_result == DedupExecutionActionResult.FAILED
            assert reconciliation.observed_content_hash == observation.content_hash
            assert reconciliation.observed_file_size == observation.file_size
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


# --- reconcile_action: structural preconditions -------------------------


def test_reconcile_action_raises_for_missing_audit(tmp_path) -> None:
    engine = _engine()
    with Session(engine) as db:
        with pytest.raises(ValueError, match="not found"):
            DedupReconciliationService(db).reconcile_action(
                999_999_999,
                DedupExecutionActionResult.SUCCESS,
                verified_by="tester",
                verification_method="n/a",
            )


def test_reconcile_action_rejects_non_unknown_audit(tmp_path) -> None:

    engine = _engine()
    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            # Overwrite the simulated UNKNOWN audit's result directly to
            # a definite one, to test the precondition in isolation.
            audit.result = DedupExecutionActionResult.SUCCESS
            audit.filesystem_mutation_occurred = True
            db.commit()

            with pytest.raises(ValueError, match="not UNKNOWN"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.SUCCESS,
                    verified_by="tester",
                    verification_method="n/a",
                )
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


def test_reconcile_action_rejects_verified_result_outside_success_or_failed(
    tmp_path,
) -> None:

    engine = _engine()
    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            with pytest.raises(ValueError, match="must be SUCCESS or FAILED"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.UNKNOWN,
                    verified_by="tester",
                    verification_method="n/a",
                )
            with pytest.raises(ValueError, match="must be SUCCESS or FAILED"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.PRECONDITION_FAILED,
                    verified_by="tester",
                    verification_method="n/a",
                )
            with pytest.raises(ValueError, match="must be SUCCESS or FAILED"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.NOT_ATTEMPTED,
                    verified_by="tester",
                    verification_method="n/a",
                )
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


def test_reconcile_action_rejects_second_reconciliation_of_same_audit(tmp_path) -> None:

    engine = _engine()
    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            dup_file.unlink()
            DedupReconciliationService(db).reconcile_action(
                audit.id,
                DedupExecutionActionResult.SUCCESS,
                verified_by="tester",
                verification_method="first pass",
            )

            with pytest.raises(ValueError, match="already has a reconciliation"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.SUCCESS,
                    verified_by="tester",
                    verification_method="second pass",
                )
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


# --- reconcile_action: corroboration rejects contradictory claims -------


def test_reconcile_action_rejects_success_claim_when_source_still_exists(
    tmp_path,
) -> None:
    """The corroboration requirement in action: a human's SUCCESS claim
    ("the delete really happened") is refused if the system's own fresh
    observation shows the source file is still right there."""

    engine = _engine()
    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            # Source file deliberately left in place.
            with pytest.raises(ValueError, match="still exists"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.SUCCESS,
                    verified_by="tester",
                    verification_method="mistaken claim - file is actually still there",
                )
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


def test_reconcile_action_rejects_failed_claim_when_source_is_gone(tmp_path) -> None:
    """The corroboration requirement's mirror image: a human's FAILED
    claim ("nothing happened") is refused if the source file is
    actually gone - that's inconsistent with 'nothing happened'."""

    engine = _engine()
    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            dup_file.unlink()

            with pytest.raises(ValueError, match="no longer exists"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.FAILED,
                    verified_by="tester",
                    verification_method="mistaken claim - file is actually gone",
                )
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


def test_reconcile_action_rejects_failed_claim_when_source_content_changed(
    tmp_path,
) -> None:
    """A FAILED claim also requires the source to match its ORIGINAL
    pre-mutation hash/size, not merely to exist - a changed file is
    just as inconsistent with 'nothing happened' as a missing one."""

    engine = _engine()
    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            dup_file.write_bytes(b"something completely different now")

            with pytest.raises(ValueError, match="no longer matches"):
                DedupReconciliationService(db).reconcile_action(
                    audit.id,
                    DedupExecutionActionResult.FAILED,
                    verified_by="tester",
                    verification_method="mistaken claim - content actually changed",
                )
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )


def test_get_reconciliation_returns_none_when_absent(tmp_path) -> None:
    engine = _engine()
    with Session(engine) as db:
        review, canonical_doc, dup_doc, dup_file, plan, authorization, execution, audit = (
            _setup_running_execution_with_unknown_audit(db, tmp_path)
        )
        try:
            assert DedupReconciliationService(db).get_reconciliation(audit.id) is None
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=[execution.id],
            )
