"""Real-database + real-filesystem tests for the dry-run execution
planning layer.

Uses a tiny controlled temporary directory of synthetic files
(tmp_path) - never the real personal corpus. Proves, against real
Postgres and real files, the properties that matter most for this
milestone: plan generation accurately snapshots real file state,
regenerating a plan is safe and deterministic, staleness detection
(hash/size/path/existence changes) actually catches real filesystem
changes, and - the core safety property - that generating a plan and
checking its validity never modifies, moves, renames, or deletes any
file, canonical or otherwise.
"""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dedup.execution_plan_service import DedupExecutionPlanService
from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup, NearDuplicatePair
from app.models.dedup_execution_plan import (
    DedupExecutionPlan,
    DedupExecutionPlanAction,
    DedupPlanActionType,
)
from app.models.dedup_review import DuplicateReview, DuplicateReviewMember
from app.models.document import Document
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _cleanup_review(db, review_id, document_ids, plan_ids=None):
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


def test_generate_plan_for_exact_review_against_real_files_and_database(
    tmp_path,
) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    canonical_bytes_before = canonical_file.read_bytes()
    dup_bytes_before = dup_file.read_bytes()

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        older = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="plan-test-hash-1",
            )
        )
        newer = document_service.create_document(
            DocumentCreate(
                title="dup.txt",
                source=str(dup_file),
                source_type="txt",
                content_hash="plan-test-hash-1",
            )
        )
        newer.created_at = older.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="plan-test-hash-1", documents=[newer, older]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=older.id)

        plan_service = DedupExecutionPlanService(db)
        plan_ids = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            assert plan.canonical_document_id == older.id
            assert plan.canonical_observed_exists is True
            assert plan.canonical_observed_file_size == 11

            actions = plan_service.get_plan_actions_with_documents(plan.id)
            assert len(actions) == 1
            action, action_document = actions[0]
            assert action_document.id == newer.id
            assert action.action == DedupPlanActionType.DELETE
            assert action.observed_exists is True
            assert action.observed_file_size == 11

            # Safety: generating a plan must not touch either file.
            assert canonical_file.read_bytes() == canonical_bytes_before
            assert dup_file.read_bytes() == dup_bytes_before
            assert canonical_file.exists()
            assert dup_file.exists()

            # Immediately checking validity must also be a pure read.
            validity = plan_service.check_plan_validity(plan.id)
            assert validity.is_valid is True
            assert canonical_file.read_bytes() == canonical_bytes_before
            assert dup_file.read_bytes() == dup_bytes_before

        finally:
            _cleanup_review(db, review.id, [older.id, newer.id], plan_ids)


def test_generate_plan_for_near_review_with_explicit_canonical_against_real_database(
    tmp_path,
) -> None:
    file_a = tmp_path / "a.txt"
    file_a.write_text("version A of the document")
    file_b = tmp_path / "b.txt"
    file_b.write_text("version B, a bit different")

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        doc_a = document_service.create_document(
            DocumentCreate(title="a.txt", source=str(file_a), source_type="txt")
        )
        doc_b = document_service.create_document(
            DocumentCreate(title="b.txt", source=str(file_b), source_type="txt")
        )

        review_service = DedupReviewService(db)
        pair = NearDuplicatePair(document_a=doc_a, document_b=doc_b, similarity=0.9)
        review = review_service.create_review_from_near_pair(pair)
        review_service.approve_review(review.id, canonical_document_id=doc_a.id)

        plan_service = DedupExecutionPlanService(db)
        plan_ids = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            assert plan.canonical_document_id == doc_a.id
            actions = plan_service.get_plan_actions_with_documents(plan.id)
            assert len(actions) == 1
            assert actions[0][1].id == doc_b.id

        finally:
            _cleanup_review(db, review.id, [doc_a.id, doc_b.id], plan_ids)


def test_generate_plan_raises_for_near_review_approved_without_canonical(
    tmp_path,
) -> None:
    file_a = tmp_path / "a.txt"
    file_a.write_text("version A")
    file_b = tmp_path / "b.txt"
    file_b.write_text("version B")

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        doc_a = document_service.create_document(
            DocumentCreate(title="a.txt", source=str(file_a), source_type="txt")
        )
        doc_b = document_service.create_document(
            DocumentCreate(title="b.txt", source=str(file_b), source_type="txt")
        )

        review_service = DedupReviewService(db)
        pair = NearDuplicatePair(document_a=doc_a, document_b=doc_b, similarity=0.9)
        review = review_service.create_review_from_near_pair(pair)
        # Approved WITHOUT a canonical - a valid, deliberate outcome for
        # a near-duplicate review.
        review_service.approve_review(review.id)

        plan_service = DedupExecutionPlanService(db)

        try:
            try:
                plan_service.generate_plan_for_review(review.id)
                raise AssertionError(
                    "Expected ValueError for a review with no canonical decision"
                )
            except ValueError as exc:
                assert "without an explicit human-selected canonical" in str(exc)

        finally:
            _cleanup_review(db, review.id, [doc_a.id, doc_b.id])


def test_generate_plan_raises_for_pending_and_rejected_reviews(tmp_path) -> None:
    file_a = tmp_path / "a.txt"
    file_a.write_text("hello")
    file_b = tmp_path / "b.txt"
    file_b.write_text("hello")

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        doc_a = document_service.create_document(
            DocumentCreate(
                title="a.txt", source=str(file_a), source_type="txt",
                content_hash="plan-test-hash-2",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="b.txt", source=str(file_b), source_type="txt",
                content_hash="plan-test-hash-2",
            )
        )

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="plan-test-hash-2", documents=[doc_a, doc_b]
        )
        review = review_service.create_review_from_exact_group(group)

        plan_service = DedupExecutionPlanService(db)

        try:
            # Still PENDING.
            try:
                plan_service.generate_plan_for_review(review.id)
                raise AssertionError("Expected ValueError for a pending review")
            except ValueError as exc:
                assert "is not approved" in str(exc)

            # Now REJECTED.
            review_service.reject_review(review.id)
            try:
                plan_service.generate_plan_for_review(review.id)
                raise AssertionError("Expected ValueError for a rejected review")
            except ValueError as exc:
                assert "is not approved" in str(exc)

        finally:
            _cleanup_review(db, review.id, [doc_a.id, doc_b.id])


def test_generate_plan_raises_for_nonexistent_review() -> None:
    engine = _engine()

    with Session(engine) as db:
        plan_service = DedupExecutionPlanService(db)

        try:
            plan_service.generate_plan_for_review(999999999)
            raise AssertionError("Expected ValueError for a nonexistent review")
        except ValueError as exc:
            assert "not found" in str(exc)


def test_check_plan_validity_detects_real_file_modification(tmp_path) -> None:
    """The critical safety scenario: a file changes on disk after a
    plan was generated. Validity must catch it - and neither file may
    ever be touched by the check itself."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        older = document_service.create_document(
            DocumentCreate(
                title="canonical.txt", source=str(canonical_file), source_type="txt",
                content_hash="plan-test-hash-3",
            )
        )
        newer = document_service.create_document(
            DocumentCreate(
                title="dup.txt", source=str(dup_file), source_type="txt",
                content_hash="plan-test-hash-3",
            )
        )
        newer.created_at = older.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="plan-test-hash-3", documents=[newer, older]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=older.id)

        plan_service = DedupExecutionPlanService(db)
        plan_ids = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            valid_before = plan_service.check_plan_validity(plan.id)
            assert valid_before.is_valid is True

            # The file changes after the plan was generated - simulating
            # exactly the real-world scenario this whole feature exists
            # to guard against.
            dup_file.write_text("hello world, but now edited")

            invalid_after = plan_service.check_plan_validity(plan.id)
            assert invalid_after.actions[0].hash_matches is False
            assert invalid_after.actions[0].size_matches is False
            assert invalid_after.actions[0].is_valid is False
            assert invalid_after.is_valid is False
            # The canonical, untouched, must still read valid.
            assert invalid_after.canonical_valid is True

            # The validity check itself must never have modified anything.
            assert canonical_file.read_text() == "hello world"

        finally:
            _cleanup_review(db, review.id, [older.id, newer.id], plan_ids)


def test_check_plan_validity_detects_real_file_deletion(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        older = document_service.create_document(
            DocumentCreate(
                title="canonical.txt", source=str(canonical_file), source_type="txt",
                content_hash="plan-test-hash-4",
            )
        )
        newer = document_service.create_document(
            DocumentCreate(
                title="dup.txt", source=str(dup_file), source_type="txt",
                content_hash="plan-test-hash-4",
            )
        )
        newer.created_at = older.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="plan-test-hash-4", documents=[newer, older]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=older.id)

        plan_service = DedupExecutionPlanService(db)
        plan_ids = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            # No deletion happens anywhere in this codebase - this
            # simulates an *external* change (e.g. the user themselves
            # deleting the file) to prove staleness detection catches it.
            dup_file.unlink()

            validity = plan_service.check_plan_validity(plan.id)
            assert validity.actions[0].exists_now is False
            assert validity.actions[0].is_valid is False
            assert validity.is_valid is False

            # The canonical must remain completely untouched.
            assert canonical_file.exists()
            assert canonical_file.read_text() == "hello world"

        finally:
            _cleanup_review(db, review.id, [older.id, newer.id], plan_ids)


def test_repeated_plan_generation_creates_independent_rows(tmp_path) -> None:
    """Regenerating a plan for the same review is expected and safe -
    each call is an independent, immutable snapshot, not an update."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        older = document_service.create_document(
            DocumentCreate(
                title="canonical.txt", source=str(canonical_file), source_type="txt",
                content_hash="plan-test-hash-5",
            )
        )
        newer = document_service.create_document(
            DocumentCreate(
                title="dup.txt", source=str(dup_file), source_type="txt",
                content_hash="plan-test-hash-5",
            )
        )
        newer.created_at = older.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="plan-test-hash-5", documents=[newer, older]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=older.id)

        plan_service = DedupExecutionPlanService(db)
        plan_ids = []

        try:
            first_plan = plan_service.generate_plan_for_review(review.id)
            second_plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.extend([first_plan.id, second_plan.id])

            assert first_plan.id != second_plan.id
            assert (
                first_plan.canonical_observed_content_hash
                == second_plan.canonical_observed_content_hash
            )

            all_plans = plan_service.list_plans(review_id=review.id)
            assert {p.id for p in all_plans} == {first_plan.id, second_plan.id}

        finally:
            _cleanup_review(db, review.id, [older.id, newer.id], plan_ids)
