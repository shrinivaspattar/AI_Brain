"""Real-database + real-(synthetic)-filesystem tests for the
filesystem executor - the only code in AI_Brain that performs a real
filesystem mutation, and the only test file in this codebase that
allows one.

Every mutation in every test here happens exclusively within a
`tmp_path`-derived `allowed_root`/`quarantine_root` pair, freshly
created per test and torn down by pytest automatically. **This suite
never references, reads, writes, or could possibly reach the real
personal corpus** - `DedupFilesystemExecutor` has no default root of
any kind (missing arguments raise `TypeError`, proven below), and
nothing in this file passes it anything but a synthetic `tmp_path`
subdirectory.
"""

import inspect
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import DedupExecutionPlanService
from app.dedup.execution_service import DedupExecutionService
from app.dedup.executor import DedupFilesystemExecutor, _device_of
from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup
from app.models.dedup_authorization import DedupPlanAuthorization
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


def _setup_authorized_plan(
    db, allowed_root, dup_count=1, content=b"hello world", content_hash="exec-test-hash"
):
    """Creates a canonical + N duplicate files directly inside
    allowed_root, and drives them through the full real pipeline
    (documents -> review -> approve -> plan -> authorize) exactly as a
    real caller would. Returns (review, canonical_doc, dup_docs,
    canonical_file, dup_files, plan, authorization)."""
    canonical_file = allowed_root / "canonical.txt"
    canonical_file.write_bytes(content)
    dup_files = []
    for i in range(dup_count):
        f = allowed_root / f"dup{i}.txt"
        f.write_bytes(content)
        dup_files.append(f)

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
    for i, f in enumerate(dup_files):
        d = document_service.create_document(
            DocumentCreate(
                title=f"dup{i}.txt",
                source=str(f),
                source_type="txt",
                content_hash=content_hash,
            )
        )
        d.created_at = canonical_doc.created_at + timedelta(seconds=i + 1)
        dup_docs.append(d)
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
    authorization = auth_service.authorize_plan(plan.id, authorized_by="executor-test")

    return review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization


def _plan_actions_ordered(db, plan_id):
    return list(
        db.scalars(
            select(DedupExecutionPlanAction)
            .where(DedupExecutionPlanAction.plan_id == plan_id)
            .order_by(DedupExecutionPlanAction.id)
        )
    )


# --- constructor: fail-closed properties --------------------------------


def test_executor_requires_explicit_roots_no_defaults() -> None:
    """The entire "cannot be selected implicitly" property: there is
    no default value for either argument anywhere - omitting one is a
    TypeError before any other code runs."""
    with pytest.raises(TypeError):
        DedupFilesystemExecutor()  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        DedupFilesystemExecutor(db=None)  # type: ignore[call-arg]


def test_executor_source_never_references_real_corpus_path() -> None:
    """Belt-and-suspenders proof, not just an assertion about
    argument defaults: the executor's own source code contains no
    string reference to the real corpus location at all."""
    source = inspect.getsource(DedupFilesystemExecutor)
    assert "/mnt" not in source
    assert "t7ssd" not in source
    assert "vscode/data" not in source


def test_executor_rejects_nonexistent_allowed_root(tmp_path) -> None:
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()

    with pytest.raises(ValueError, match="not a directory"):
        DedupFilesystemExecutor(
            db=None, allowed_root=tmp_path / "does-not-exist", quarantine_root=quarantine
        )


def test_executor_rejects_nonexistent_quarantine_root(tmp_path) -> None:
    allowed = tmp_path / "corpus"
    allowed.mkdir()

    with pytest.raises(ValueError, match="not a directory"):
        DedupFilesystemExecutor(
            db=None, allowed_root=allowed, quarantine_root=tmp_path / "does-not-exist"
        )


def test_executor_rejects_quarantine_nested_inside_allowed_root(tmp_path) -> None:
    allowed = tmp_path / "corpus"
    allowed.mkdir()
    quarantine = allowed / "quarantine"
    quarantine.mkdir()

    with pytest.raises(ValueError, match="must not be inside allowed_root"):
        DedupFilesystemExecutor(db=None, allowed_root=allowed, quarantine_root=quarantine)


def test_executor_rejects_allowed_root_nested_inside_quarantine(tmp_path) -> None:
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    allowed = quarantine / "corpus"
    allowed.mkdir()

    with pytest.raises(ValueError, match="must not be inside quarantine_root"):
        DedupFilesystemExecutor(db=None, allowed_root=allowed, quarantine_root=quarantine)


def test_executor_rejects_same_directory(tmp_path) -> None:
    same = tmp_path / "same"
    same.mkdir()

    with pytest.raises(ValueError, match="must not be the same directory"):
        DedupFilesystemExecutor(db=None, allowed_root=same, quarantine_root=same)


def test_executor_rejects_cross_device_roots(tmp_path) -> None:
    """Simulated via a patched device lookup, since this sandboxed test
    environment cannot rely on a second real mounted filesystem being
    available. The constructor's device-equality requirement is the
    same one documented (and manually verified against the real
    deployment) in AI_Brain_Architecture.md's Filesystem Executor
    Design section."""
    allowed = tmp_path / "corpus"
    allowed.mkdir()
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()

    def fake_device(path):
        return 1 if str(path).endswith("corpus") else 2

    with patch("app.dedup.executor._device_of", side_effect=fake_device):
        with pytest.raises(ValueError, match="must be on the same filesystem"):
            DedupFilesystemExecutor(db=None, allowed_root=allowed, quarantine_root=quarantine)


def test_device_of_matches_for_siblings_under_tmp_path(tmp_path) -> None:
    """Sanity check of the real (unpatched) device lookup: two sibling
    directories under the same tmp_path really are on the same
    device, which is what makes the rest of this file's tests valid
    without needing a genuinely separate filesystem."""
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    assert _device_of(a) == _device_of(b)


# --- execute: happy path ---------------------------------------------


def test_execute_successful_quarantine(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root, dup_count=1)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)
            plan_action = _plan_actions_ordered(db, plan.id)[0]

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.COMPLETED
            assert finalized.failure_reason is None

            audits = execution_service.get_action_audits(execution.id)
            assert len(audits) == 1
            assert audits[0].result == DedupExecutionActionResult.SUCCESS
            assert audits[0].filesystem_mutation_occurred is True

            # The source is gone; the canonical is completely untouched.
            assert not dup_files[0].exists()
            assert canonical_file.exists()
            assert canonical_file.read_bytes() == b"hello world"

            # The quarantined file exists with the exact original content.
            destination = (
                quarantine_root / str(execution.id) / f"{plan_action.id}__dup0.txt"
            )
            assert destination.is_file()
            assert destination.read_bytes() == b"hello world"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_requires_confirm_true(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with pytest.raises(ValueError, match="confirm must be true"):
                executor.execute(execution.id, confirm=False)

            # Absolutely nothing happened.
            assert dup_files[0].exists()
            assert execution_service.get_action_audits(execution.id) == []

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- precondition failures ---------------------------------------------


def test_execute_hash_mismatch_is_precondition_failed(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            # Changed AFTER authorization - proves the executor's own
            # re-check catches it, not just authorize_plan's.
            dup_files[0].write_bytes(b"different content entirely")

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert audits[0].filesystem_mutation_occurred is False
            assert dup_files[0].exists()
            assert dup_files[0].read_bytes() == b"different content entirely"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_size_mismatch_is_precondition_failed(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            dup_files[0].write_bytes(b"hello world plus extra trailing bytes")

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert dup_files[0].exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_missing_source_is_precondition_failed(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            dup_files[0].unlink()

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert canonical_file.exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_symlink_source_is_precondition_failed(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")
        real_target = allowed_root / "real_dup.txt"
        real_target.write_bytes(b"hello world")
        symlink_path = allowed_root / "dup_symlink.txt"
        symlink_path.symlink_to(real_target)

        document_service = DocumentService(db)
        canonical_doc = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="exec-symlink-hash",
            )
        )
        dup_doc = document_service.create_document(
            DocumentCreate(
                title="dup_symlink.txt",
                source=str(symlink_path),
                source_type="txt",
                content_hash="exec-symlink-hash",
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="exec-symlink-hash", documents=[dup_doc, canonical_doc]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=canonical_doc.id)

        plan_service = DedupExecutionPlanService(db)
        plan = plan_service.generate_plan_for_review(review.id)
        auth_service = DedupPlanAuthorizationService(db)
        authorization = auth_service.authorize_plan(plan.id)

        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert "symlink" in audits[0].error_message
            assert symlink_path.exists()
            assert real_target.exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_hard_linked_source_is_precondition_failed(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")
        dup_file = allowed_root / "dup.txt"
        dup_file.write_bytes(b"hello world")
        second_link = allowed_root / "dup_hardlink.txt"
        os.link(dup_file, second_link)

        document_service = DocumentService(db)
        canonical_doc = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="exec-hardlink-hash",
            )
        )
        dup_doc = document_service.create_document(
            DocumentCreate(
                title="dup.txt",
                source=str(dup_file),
                source_type="txt",
                content_hash="exec-hardlink-hash",
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="exec-hardlink-hash", documents=[dup_doc, canonical_doc]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=canonical_doc.id)

        plan_service = DedupExecutionPlanService(db)
        plan = plan_service.generate_plan_for_review(review.id)
        auth_service = DedupPlanAuthorizationService(db)
        authorization = auth_service.authorize_plan(plan.id)

        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert "hard link" in audits[0].error_message
            assert dup_file.exists()
            assert second_link.exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_outside_allowed_root_is_precondition_failed(tmp_path) -> None:
    """A document whose source somehow points outside the configured
    allowed_root (a corrupted/unexpected Document row) must be refused,
    never silently acted on."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")
        outside_file = outside_dir / "outside_dup.txt"
        outside_file.write_bytes(b"hello world")

        document_service = DocumentService(db)
        canonical_doc = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="exec-outside-hash",
            )
        )
        dup_doc = document_service.create_document(
            DocumentCreate(
                title="outside_dup.txt",
                source=str(outside_file),
                source_type="txt",
                content_hash="exec-outside-hash",
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="exec-outside-hash", documents=[dup_doc, canonical_doc]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=canonical_doc.id)

        plan_service = DedupExecutionPlanService(db)
        plan = plan_service.generate_plan_for_review(review.id)
        auth_service = DedupPlanAuthorizationService(db)
        authorization = auth_service.authorize_plan(plan.id)

        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert "outside the allowed mutation root" in audits[0].error_message
            assert outside_file.exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_authorization_invalidation_stops_execution(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        auth_service = DedupPlanAuthorizationService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            auth_service.revoke_authorization(authorization.id, reason="test revoke")

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert "AUTHORIZED" in audits[0].error_message
            assert dup_files[0].exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_stale_plan_via_live_document_change_is_precondition_failed(
    tmp_path,
) -> None:
    """Distinct from a raw file hash/size change: the LIVE Document
    row's source_type changes after authorization, caught by
    check_plan_validity's type_matches dimension - a staleness kind
    the executor's own later file-only checks would never catch on
    their own."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            live_doc = db.get(Document, dup_docs[0].id)
            live_doc.source_type = "md"
            db.commit()

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert "plan is no longer valid" in audits[0].error_message
            assert dup_files[0].exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- destination collision -----------------------------------------------


def test_execute_destination_collision_is_failed(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)
            plan_action = _plan_actions_ordered(db, plan.id)[0]

            # Pre-occupy the exact destination the executor will compute.
            destination = (
                quarantine_root / str(execution.id) / f"{plan_action.id}__dup0.txt"
            )
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"something already sitting here")

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.FAILED
            assert "already exists" in audits[0].error_message
            # Neither the source nor the pre-existing destination content
            # was touched - os.rename() was never called.
            assert dup_files[0].exists()
            assert destination.read_bytes() == b"something already sitting here"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- permission failure ---------------------------------------------------


def test_execute_permission_failure_is_failed(tmp_path) -> None:
    if os.getuid() == 0:
        pytest.skip("permission checks are bypassed when running as root")

    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        original_mode = allowed_root.stat().st_mode
        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            # Renaming requires write permission on the containing
            # directory, not the file itself.
            allowed_root.chmod(0o555)

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.FAILED
            assert audits[0].filesystem_mutation_occurred is False

        finally:
            allowed_root.chmod(original_mode)
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- stop-on-first-failure / partial completion --------------------------


def test_execute_stops_on_first_failure_and_marks_remaining_not_attempted(
    tmp_path,
) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root, dup_count=3)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            plan_actions = _plan_actions_ordered(db, plan.id)
            assert len(plan_actions) == 3
            # Corrupt the file for the MIDDLE action (by processing order).
            middle_action = plan_actions[1]
            middle_doc = db.get(Document, middle_action.document_id)
            

            Path(middle_doc.source).write_bytes(b"corrupted")

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.PARTIALLY_COMPLETED

            audits_by_action = {
                a.plan_action_id: a
                for a in execution_service.get_action_audits(execution.id)
            }
            assert audits_by_action[plan_actions[0].id].result == (
                DedupExecutionActionResult.SUCCESS
            )
            assert audits_by_action[plan_actions[1].id].result == (
                DedupExecutionActionResult.PRECONDITION_FAILED
            )
            assert audits_by_action[plan_actions[2].id].result == (
                DedupExecutionActionResult.NOT_ATTEMPTED
            )

            # The first duplicate is gone (quarantined); the third was
            # never touched at all.
            first_doc = db.get(Document, plan_actions[0].document_id)
            third_doc = db.get(Document, plan_actions[2].document_id)
            assert not Path(first_doc.source).exists()
            assert Path(third_doc.source).exists()
            assert canonical_file.exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_first_action_fails_is_failed_status(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root, dup_count=2)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            plan_actions = _plan_actions_ordered(db, plan.id)
            first_doc = db.get(Document, plan_actions[0].document_id)
            

            Path(first_doc.source).unlink()

            finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits_by_action = {
                a.plan_action_id: a
                for a in execution_service.get_action_audits(execution.id)
            }
            assert audits_by_action[plan_actions[0].id].result == (
                DedupExecutionActionResult.PRECONDITION_FAILED
            )
            assert audits_by_action[plan_actions[1].id].result == (
                DedupExecutionActionResult.NOT_ATTEMPTED
            )

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- verification failure / UNKNOWN representation ------------------------


def test_execute_post_move_verification_mismatch_is_unknown(tmp_path) -> None:
    """A rename() that raises no error but leaves something the
    post-move check cannot corroborate must never be reported SUCCESS
    - "the OS call didn't error" is not the same as "verified
    correct." Simulated by patching the post-move observation to
    return mismatched content while the real move still happens."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        from app.dedup.execution_plan_service import _observe_file as real_observe_file
        from app.dedup.execution_plan_service import FileObservation

        def fake_observe(path_str):
            if str(quarantine_root) in path_str:
                return FileObservation(
                    exists=True, content_hash="deliberately-wrong-hash", file_size=999
                )
            return real_observe_file(path_str)

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with patch("app.dedup.executor._observe_file", side_effect=fake_observe):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.NEEDS_REVIEW
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.UNKNOWN
            assert audits[0].filesystem_mutation_occurred is None
            assert "post-move verification" in audits[0].error_message

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_unexpected_exception_is_recorded_unknown(tmp_path) -> None:
    """The outer safety net: something genuinely unanticipated during
    an action's processing must never leave the execution stuck at
    RUNNING - it is recorded UNKNOWN and the run stops cleanly."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with patch.object(
                executor.plan_service,
                "get_plan",
                side_effect=RuntimeError("simulated unexpected failure"),
            ):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.NEEDS_REVIEW
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.UNKNOWN
            assert "Unexpected error" in audits[0].error_message
            assert dup_files[0].exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- repeated execution attempts ------------------------------------------


def test_execute_cannot_be_called_twice_after_completion(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            executor.execute(execution.id, confirm=True)

            with pytest.raises(ValueError, match="is not RUNNING"):
                executor.execute(execution.id, confirm=True)

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_refuses_to_run_on_execution_with_partial_audits(tmp_path) -> None:
    """Even if the execution is still RUNNING, the executor refuses to
    "resume" one that already has some recorded results - that path
    belongs to recover_stale_execution, never to a silent retry here."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root, dup_count=2)
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)
            plan_action = _plan_actions_ordered(db, plan.id)[0]

            # Simulate a prior partial attempt having recorded one result.
            execution_service.record_action_result(
                execution.id,
                plan_action.id,
                DedupExecutionActionResult.NOT_ATTEMPTED,
            )

            with pytest.raises(ValueError, match="already has"):
                executor.execute(execution.id, confirm=True)

            # Nothing was touched by this refused call.
            assert dup_files[0].exists()
            assert dup_files[1].exists()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_raises_for_nonexistent_execution(tmp_path) -> None:
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)

        with pytest.raises(ValueError, match="not found"):
            executor.execute(999999999, confirm=True)
