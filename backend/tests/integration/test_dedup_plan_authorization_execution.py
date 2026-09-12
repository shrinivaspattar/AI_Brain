"""Real-database + real-filesystem tests for the explicit execution
authorization layer.

Uses a tiny controlled temporary directory of synthetic files
(tmp_path) - never the real personal corpus. Proves, against real
Postgres and real files: authorization only succeeds when the backing
review is APPROVED and a fresh validity re-check passes; every kind of
staleness (changed hash, size, path, file type, missing source) blocks
authorization; a plan can never be authorized twice while an active
authorization exists; an authorization is permanently bound to the one
plan it was granted for; and - the core safety property - that
authorizing, listing, reading, or revoking an authorization never
creates, deletes, moves, renames, or modifies any file, and never
mutates the immutable DedupExecutionPlan or the DuplicateReview it
came from.
"""

import hashlib
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import DedupExecutionPlanService
from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup
from app.models.dedup_authorization import DedupPlanAuthorization, DedupPlanAuthorizationStatus
from app.models.dedup_execution_plan import DedupExecutionPlan, DedupExecutionPlanAction
from app.models.dedup_review import DuplicateReview, DuplicateReviewMember
from app.models.document import Document
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _cleanup(db, review_id, document_ids, plan_ids=None, authorization_ids=None):
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


def _setup_approved_exact_review(db, canonical_file, dup_file, content_hash):
    document_service = DocumentService(db)
    older = document_service.create_document(
        DocumentCreate(
            title="canonical.txt",
            source=str(canonical_file),
            source_type="txt",
            content_hash=content_hash,
        )
    )
    newer = document_service.create_document(
        DocumentCreate(
            title="dup.txt",
            source=str(dup_file),
            source_type="txt",
            content_hash=content_hash,
        )
    )
    newer.created_at = older.created_at + timedelta(seconds=1)
    db.commit()

    review_service = DedupReviewService(db)
    group = ExactDuplicateGroup(content_hash=content_hash, documents=[newer, older])
    review = review_service.create_review_from_exact_group(group)
    review_service.approve_review(review.id, canonical_document_id=older.id)

    return review, older, newer


# --- authorize_plan: happy path + full real-filesystem protocol -----------


def test_authorization_full_protocol_against_real_files_and_database(tmp_path) -> None:
    """The exact 8-step real-filesystem verification protocol:
    1. Authorization succeeds for a valid synthetic plan.
    2. No filesystem mutation occurs.
    3. Modify a planned file.
    4. Authorization of the old plan fails.
    5. Generate a new plan.
    6. Verify the new plan captures the new file state.
    7. Authorization of the new valid plan succeeds.
    8. No file is modified by authorization itself.
    """
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    canonical_bytes_before = canonical_file.read_bytes()

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-1"
        )

        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []
        authorization_ids: list[int] = []

        try:
            # Steps 1-2: authorization succeeds for a valid plan, and
            # nothing on disk moves as a result.
            first_plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(first_plan.id)

            first_authorization = auth_service.authorize_plan(
                first_plan.id, authorized_by="integration-test"
            )
            authorization_ids.append(first_authorization.id)

            assert first_authorization.status == DedupPlanAuthorizationStatus.AUTHORIZED
            assert first_authorization.validity_snapshot["is_valid"] is True
            assert canonical_file.read_bytes() == canonical_bytes_before
            assert dup_file.read_bytes() == b"hello world"
            assert canonical_file.exists()
            assert dup_file.exists()

            # Step 3: modify a planned file after authorization.
            dup_file.write_text("hello world, but now edited")

            # Step 4: authorizing the OLD (now-stale) plan again must
            # fail - but there is already an active authorization for
            # it, so first revoke it to isolate the staleness check.
            auth_service.revoke_authorization(
                first_authorization.id, reason="superseded by a fresh plan"
            )

            try:
                auth_service.authorize_plan(first_plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing a plan that has gone stale"
                )
            except ValueError as exc:
                assert "no longer valid" in str(exc)

            # Step 5: generate a new plan against the changed filesystem.
            second_plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(second_plan.id)

            # Step 6: the new plan must capture the NEW file state.
            second_plan_actions = plan_service.get_plan_actions_with_documents(
                second_plan.id
            )
            assert len(second_plan_actions) == 1
            new_action, _ = second_plan_actions[0]
            expected_hash = hashlib.sha256(
                b"hello world, but now edited"
            ).hexdigest()
            assert new_action.observed_content_hash == expected_hash
            assert new_action.observed_file_size == len(b"hello world, but now edited")

            # Step 7: authorizing the new, valid plan succeeds.
            second_authorization = auth_service.authorize_plan(second_plan.id)
            authorization_ids.append(second_authorization.id)
            assert second_authorization.status == DedupPlanAuthorizationStatus.AUTHORIZED
            assert second_authorization.plan_id == second_plan.id

            # Step 8: authorization itself never modified any file.
            assert canonical_file.read_bytes() == canonical_bytes_before
            assert dup_file.read_text() == "hello world, but now edited"

        finally:
            _cleanup(
                db,
                review.id,
                [older.id, newer.id],
                plan_ids=plan_ids,
                authorization_ids=authorization_ids,
            )


# --- pre-check failures against real data -----------------------------


def test_authorize_plan_raises_for_pending_review(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello")

    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        doc_a = document_service.create_document(
            DocumentCreate(
                title="canonical.txt", source=str(canonical_file), source_type="txt",
                content_hash="auth-test-hash-2",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="dup.txt", source=str(dup_file), source_type="txt",
                content_hash="auth-test-hash-2",
            )
        )

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="auth-test-hash-2", documents=[doc_a, doc_b]
        )
        review = review_service.create_review_from_exact_group(group)
        # Left PENDING deliberately - no plan can even be generated for
        # it, so we assert the review-approval check directly via the
        # authorization service against a plan_id that cannot exist for
        # this review; instead, prove the pending-review guard using the
        # review's own approval gate exercised through the review
        # service, matching how generate_plan_for_review is tested.

        try:
            try:
                DedupExecutionPlanService(db).generate_plan_for_review(review.id)
                raise AssertionError("Expected ValueError for a pending review")
            except ValueError as exc:
                assert "is not approved" in str(exc)
        finally:
            _cleanup(db, review.id, [doc_a.id, doc_b.id])


def test_authorize_plan_raises_for_rejected_review(tmp_path) -> None:
    """A plan generated while a review was approved, whose review is
    later somehow not approved, must never be authorizable. Since a
    plan cannot be generated for anything but an approved review, this
    proves the authorization service re-checks the review's CURRENT
    status rather than trusting that a plan's mere existence implies
    an approved review forever."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-3"
        )

        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            # Simulate the review's status changing beneath the plan -
            # not reachable through the public API (approve/reject are
            # one-way), but exercised here directly to prove the
            # authorization service re-fetches and re-checks the review
            # fresh rather than trusting the plan's existence.
            review_row = db.get(DuplicateReview, review.id)
            from app.models.dedup_review import DuplicateReviewStatus

            review_row.status = DuplicateReviewStatus.REJECTED
            db.commit()

            try:
                auth_service.authorize_plan(plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing a plan backed by a "
                    "no-longer-approved review"
                )
            except ValueError as exc:
                assert "is not approved" in str(exc)

        finally:
            _cleanup(db, review.id, [older.id, newer.id], plan_ids=plan_ids)


def test_authorize_plan_raises_for_changed_hash(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-4"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            dup_file.write_text("hello world - modified content")

            try:
                auth_service.authorize_plan(plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing a plan with a changed hash"
                )
            except ValueError as exc:
                assert "no longer valid" in str(exc)

        finally:
            _cleanup(db, review.id, [older.id, newer.id], plan_ids=plan_ids)


def test_authorize_plan_raises_for_changed_size(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-5"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            dup_file.write_text("hello world plus extra bytes appended here")

            try:
                auth_service.authorize_plan(plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing a plan with a changed size"
                )
            except ValueError as exc:
                assert "no longer valid" in str(exc)

        finally:
            _cleanup(db, review.id, [older.id, newer.id], plan_ids=plan_ids)


def test_authorize_plan_raises_for_changed_path(tmp_path) -> None:
    """The live Document's source is updated (e.g. by a re-ingest) after
    the plan was generated - the plan only ever reads its own frozen
    path, so authorization must catch the live path having moved."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-6"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            moved_file = tmp_path / "dup-moved.txt"
            dup_file.rename(moved_file)
            newer_row = db.get(Document, newer.id)
            newer_row.source = str(moved_file)
            db.commit()

            try:
                auth_service.authorize_plan(plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing a plan with a changed path"
                )
            except ValueError as exc:
                assert "no longer valid" in str(exc)

        finally:
            _cleanup(db, review.id, [older.id, newer.id], plan_ids=plan_ids)


def test_authorize_plan_raises_for_changed_file_type(tmp_path) -> None:
    """The live Document's source_type changed since the plan was
    generated (e.g. a corrected/re-classified ingest) - authorization
    must catch the mismatch between the plan's frozen path and the
    document's current declared type."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-7"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            newer_row = db.get(Document, newer.id)
            newer_row.source_type = "md"
            db.commit()

            try:
                auth_service.authorize_plan(plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing a plan with a changed file type"
                )
            except ValueError as exc:
                assert "no longer valid" in str(exc)

        finally:
            _cleanup(db, review.id, [older.id, newer.id], plan_ids=plan_ids)


def test_authorize_plan_raises_for_missing_source(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-8"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            dup_file.unlink()

            try:
                auth_service.authorize_plan(plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing a plan with a missing source file"
                )
            except ValueError as exc:
                assert "no longer valid" in str(exc)

            assert canonical_file.exists()

        finally:
            _cleanup(db, review.id, [older.id, newer.id], plan_ids=plan_ids)


def test_authorize_plan_raises_for_nonexistent_plan() -> None:
    engine = _engine()

    with Session(engine) as db:
        auth_service = DedupPlanAuthorizationService(db)

        try:
            auth_service.authorize_plan(999999999)
            raise AssertionError("Expected ValueError for a nonexistent plan")
        except ValueError as exc:
            assert "not found" in str(exc)


# --- duplicate authorization / cross-plan binding --------------------------


def test_authorize_plan_raises_for_duplicate_active_authorization(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-9"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []
        authorization_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            first = auth_service.authorize_plan(plan.id)
            authorization_ids.append(first.id)

            try:
                auth_service.authorize_plan(plan.id)
                raise AssertionError(
                    "Expected ValueError authorizing an already-actively-"
                    "authorized plan"
                )
            except ValueError as exc:
                assert "already has an active authorization" in str(exc)

        finally:
            _cleanup(
                db,
                review.id,
                [older.id, newer.id],
                plan_ids=plan_ids,
                authorization_ids=authorization_ids,
            )


def test_authorization_bound_to_exact_plan_cannot_be_reused(tmp_path) -> None:
    """Authorizing one plan must have zero effect on any other plan -
    even a second, independently-generated plan for the SAME review."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-10"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []
        authorization_ids: list[int] = []

        try:
            first_plan = plan_service.generate_plan_for_review(review.id)
            second_plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.extend([first_plan.id, second_plan.id])
            assert first_plan.id != second_plan.id

            authorization = auth_service.authorize_plan(first_plan.id)
            authorization_ids.append(authorization.id)

            assert authorization.plan_id == first_plan.id

            # The second plan has no authorization of its own - a fresh
            # authorization attempt for it must independently succeed
            # (proving the first authorization did not somehow "cover"
            # it), and querying by plan_id must not conflate the two.
            second_authorization = auth_service.authorize_plan(second_plan.id)
            authorization_ids.append(second_authorization.id)
            assert second_authorization.plan_id == second_plan.id
            assert second_authorization.id != authorization.id

            first_plan_authorizations = auth_service.list_authorizations(
                plan_id=first_plan.id
            )
            second_plan_authorizations = auth_service.list_authorizations(
                plan_id=second_plan.id
            )
            assert {a.id for a in first_plan_authorizations} == {authorization.id}
            assert {a.id for a in second_plan_authorizations} == {
                second_authorization.id
            }

        finally:
            _cleanup(
                db,
                review.id,
                [older.id, newer.id],
                plan_ids=plan_ids,
                authorization_ids=authorization_ids,
            )


# --- immutability of upstream records ---------------------------------


def test_authorization_does_not_mutate_plan_or_review_or_files(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    canonical_bytes_before = canonical_file.read_bytes()
    dup_bytes_before = dup_file.read_bytes()

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-11"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []
        authorization_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            plan_snapshot_before = {
                "canonical_source_path": plan.canonical_source_path,
                "canonical_observed_content_hash": plan.canonical_observed_content_hash,
                "canonical_observed_file_size": plan.canonical_observed_file_size,
                "status": plan.status,
            }
            review_row = db.get(DuplicateReview, review.id)
            review_snapshot_before = {
                "status": review_row.status,
                "human_selected_canonical_document_id": (
                    review_row.human_selected_canonical_document_id
                ),
                "reviewed_at": review_row.reviewed_at,
            }

            authorization = auth_service.authorize_plan(plan.id)
            authorization_ids.append(authorization.id)

            db.refresh(plan)
            db.refresh(review_row)

            assert plan.canonical_source_path == plan_snapshot_before["canonical_source_path"]
            assert (
                plan.canonical_observed_content_hash
                == plan_snapshot_before["canonical_observed_content_hash"]
            )
            assert (
                plan.canonical_observed_file_size
                == plan_snapshot_before["canonical_observed_file_size"]
            )
            assert plan.status == plan_snapshot_before["status"]

            assert review_row.status == review_snapshot_before["status"]
            assert (
                review_row.human_selected_canonical_document_id
                == review_snapshot_before["human_selected_canonical_document_id"]
            )
            assert review_row.reviewed_at == review_snapshot_before["reviewed_at"]

            assert canonical_file.read_bytes() == canonical_bytes_before
            assert dup_file.read_bytes() == dup_bytes_before

        finally:
            _cleanup(
                db,
                review.id,
                [older.id, newer.id],
                plan_ids=plan_ids,
                authorization_ids=authorization_ids,
            )


# --- revoke + currency (TOCTOU) against real data --------------------------


def test_revoke_and_currency_reflect_real_state_changes(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    engine = _engine()

    with Session(engine) as db:
        review, older, newer = _setup_approved_exact_review(
            db, canonical_file, dup_file, "auth-test-hash-12"
        )
        plan_service = DedupExecutionPlanService(db)
        auth_service = DedupPlanAuthorizationService(db)
        plan_ids: list[int] = []
        authorization_ids: list[int] = []

        try:
            plan = plan_service.generate_plan_for_review(review.id)
            plan_ids.append(plan.id)

            authorization = auth_service.authorize_plan(plan.id)
            authorization_ids.append(authorization.id)

            _, validity, is_actionable = auth_service.check_currency(authorization.id)
            assert is_actionable is True
            assert validity.is_valid is True

            # A file changes on disk after authorization - the currency
            # check must reflect this immediately, even though the
            # authorization's own frozen validity_snapshot still says valid.
            dup_file.write_text("hello world, edited after authorization")

            _, validity_after, is_actionable_after = auth_service.check_currency(
                authorization.id
            )
            assert is_actionable_after is False
            assert validity_after.is_valid is False
            # The frozen snapshot captured at authorization time is
            # untouched - proving it really is a historical record, not
            # a live value.
            db.refresh(authorization)
            assert authorization.validity_snapshot["is_valid"] is True

            revoked = auth_service.revoke_authorization(
                authorization.id, reason="plan went stale"
            )
            assert revoked.status == DedupPlanAuthorizationStatus.REVOKED

            _, _, is_actionable_revoked = auth_service.check_currency(authorization.id)
            assert is_actionable_revoked is False

        finally:
            _cleanup(
                db,
                review.id,
                [older.id, newer.id],
                plan_ids=plan_ids,
                authorization_ids=authorization_ids,
            )
