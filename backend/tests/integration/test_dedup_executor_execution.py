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

import errno
import hashlib
import inspect
import os
import socket
import threading
import time
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
from app.dedup.executor import DedupFilesystemExecutor, _device_of, _pin_and_hash
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
    assert ("vscode" + "/data") not in source


def test_executor_tests_never_reference_real_paths_or_discover_roots_dynamically() -> None:
    """Test isolation, proven the same way the production code's
    isolation is proven above: THIS test file's own source contains no
    reference to the real corpus, to the user's home directory as a
    path prefix, or to any of this project's own settings that name a
    real on-disk location - none of which should ever be needed to
    construct an executor test fixture. Every allowed_root/
    quarantine_root in this file is a `tmp_path` subdirectory, created
    fresh per test and torn down by pytest - never an existing, real,
    personal-data directory. (The forbidden strings themselves are
    deliberately not spelled out literally in this docstring or in the
    assertions below - doing so would make this test fail against its
    own source.)"""
    this_file = Path(__file__).read_text()

    # Built via concatenation, deliberately: a literal, contiguous
    # occurrence of these strings anywhere else in this file would be
    # a real violation, but this function's own assertions necessarily
    # have to name what they're checking for - splitting the literals
    # here keeps this test from flagging itself.
    forbidden_corpus_root = "/mnt/" + "t7ssd"
    forbidden_corpus_subdir = "vscode" + "/data"
    # The user's home directory as a literal path PREFIX - not merely
    # the substring "personal" on its own, which would false-positive
    # on pytest's own disposable tmp_path prefix (a system temp
    # directory, never a personal-data one).
    forbidden_home_prefix = "/home/" + "personal"
    forbidden_env_lookup = "os." + "environ"
    forbidden_base_dir = "BASE_" + "DIR"
    forbidden_ingestion_dir = "INGESTION_" + "DIR"

    assert forbidden_corpus_root not in this_file
    assert forbidden_corpus_subdir not in this_file
    assert forbidden_home_prefix not in this_file
    assert forbidden_env_lookup not in this_file
    assert forbidden_base_dir not in this_file
    assert forbidden_ingestion_dir not in this_file


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


# --- _pin_and_hash: the TOCTOU-closure primitive, tested directly ------


def test_pin_and_hash_matches_manual_sha256_and_real_stat(tmp_path) -> None:
    """The core correctness property of the pinning primitive itself,
    isolated from the rest of the executor pipeline: the hash it
    computes matches a plain, independent SHA-256 of the same bytes,
    and the identity it captures matches a plain, independent stat()
    of the same path."""
    path = tmp_path / "file.txt"
    content = b"pin and hash me"
    path.write_bytes(content)

    pinned = _pin_and_hash(path)

    assert pinned.content_hash == hashlib.sha256(content).hexdigest()
    assert pinned.file_size == len(content)
    real_stat = path.stat()
    assert pinned.device == real_stat.st_dev
    assert pinned.inode == real_stat.st_ino


def test_pin_and_hash_raises_for_missing_file(tmp_path) -> None:
    with pytest.raises(OSError):
        _pin_and_hash(tmp_path / "does-not-exist.txt")


def test_pin_and_hash_raises_for_directory(tmp_path) -> None:
    """A directory can be opened with O_RDONLY on POSIX - the rejection
    must come from fstat's own mode check, not from open() failing."""
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(OSError, match="not a regular file"):
        _pin_and_hash(directory)


def test_pin_and_hash_detects_different_inodes_for_identical_content(tmp_path) -> None:
    """The whole point of pinning: two files with byte-for-byte
    identical content are still distinguishable by identity, which a
    hash/size comparison alone could never provide."""
    content = b"identical bytes"
    first = tmp_path / "first.txt"
    first.write_bytes(content)
    second = tmp_path / "second.txt"
    second.write_bytes(content)

    pinned_first = _pin_and_hash(first)
    pinned_second = _pin_and_hash(second)

    assert pinned_first.content_hash == pinned_second.content_hash
    assert pinned_first.file_size == pinned_second.file_size
    assert pinned_first.inode != pinned_second.inode


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
            assert "destination hash/size does not match expected" in audits[0].error_message

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_destination_that_is_a_symlink_is_unknown(tmp_path) -> None:
    """Even if hash/size somehow matched, a destination that turns out
    to be a symlink rather than the real regular file the move was
    supposed to produce must never be reported SUCCESS - this is
    exactly the "destination is not a symlink" check the post-move
    verification is required to make explicit, not merely implied by
    a hash comparison."""
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

        real_is_symlink = Path.is_symlink

        def fake_is_symlink(self):
            if str(quarantine_root) in str(self):
                return True
            return real_is_symlink(self)

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with patch.object(Path, "is_symlink", fake_is_symlink):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.NEEDS_REVIEW
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.UNKNOWN
            assert audits[0].filesystem_mutation_occurred is None
            assert "destination is a symlink" in audits[0].error_message

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


# --- root symlink rejection (Executor Hardening) --------------------------


def test_executor_rejects_allowed_root_as_symlink(tmp_path) -> None:
    real_dir = tmp_path / "real_corpus"
    real_dir.mkdir()
    symlinked_root = tmp_path / "corpus_link"
    symlinked_root.symlink_to(real_dir)
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()

    with pytest.raises(ValueError, match="must not be a symlink"):
        DedupFilesystemExecutor(db=None, allowed_root=symlinked_root, quarantine_root=quarantine)


def test_executor_rejects_quarantine_root_as_symlink(tmp_path) -> None:
    allowed = tmp_path / "corpus"
    allowed.mkdir()
    real_quarantine = tmp_path / "real_quarantine"
    real_quarantine.mkdir()
    symlinked_quarantine = tmp_path / "quarantine_link"
    symlinked_quarantine.symlink_to(real_quarantine)

    with pytest.raises(ValueError, match="must not be a symlink"):
        DedupFilesystemExecutor(db=None, allowed_root=allowed, quarantine_root=symlinked_quarantine)


# --- additional filesystem security tests (Executor Hardening) ------------


def test_execute_directory_source_is_excluded_at_plan_generation(tmp_path) -> None:
    """A Document.source that points at a directory can never even
    reach the executor: `_observe_file` (shared by plan generation,
    `check_plan_validity`, AND the executor's own explicit `is_file()`
    check) treats anything that isn't `Path.is_file()` as non-existent
    - so `generate_plan_for_review`'s existing exclusion logic (see
    "Execution Recovery & Partial-Replanning Design") drops a
    directory member before a plan is even created. This is the REAL,
    reachable rejection point for this input - not the executor's own
    `is_file()` check, which is real defense-in-depth but is
    unreachable through the normal pipeline today, since every earlier
    layer already keys off the identical `_observe_file` primitive.
    """
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")
        dup_dir = allowed_root / "dup_directory"
        dup_dir.mkdir()

        document_service = DocumentService(db)
        canonical_doc = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="exec-directory-hash",
            )
        )
        dup_doc = document_service.create_document(
            DocumentCreate(
                title="dup_directory",
                source=str(dup_dir),
                source_type="txt",
                content_hash="exec-directory-hash",
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="exec-directory-hash", documents=[dup_doc, canonical_doc]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=canonical_doc.id)

        plan_service = DedupExecutionPlanService(db)
        try:
            plan_service.generate_plan_for_review(review.id)
            raise AssertionError(
                "Expected no plannable work - the only duplicate is a "
                "directory, which _observe_file treats as non-existent"
            )
        except ValueError as exc:
            assert "no plannable work left" in str(exc)

        assert dup_dir.is_dir()
        _cleanup(db, review.id, [canonical_doc.id, dup_doc.id])


def test_execute_fifo_source_is_excluded_at_plan_generation(tmp_path) -> None:
    """Same reasoning as the directory case: a FIFO is not
    `Path.is_file()`, so it is excluded before a plan can even be
    generated."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")
        fifo_path = allowed_root / "dup_fifo"
        os.mkfifo(fifo_path)

        document_service = DocumentService(db)
        canonical_doc = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="exec-fifo-hash",
            )
        )
        dup_doc = document_service.create_document(
            DocumentCreate(
                title="dup_fifo",
                source=str(fifo_path),
                source_type="txt",
                content_hash="exec-fifo-hash",
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="exec-fifo-hash", documents=[dup_doc, canonical_doc]
        )
        review = review_service.create_review_from_exact_group(group)
        review_service.approve_review(review.id, canonical_document_id=canonical_doc.id)

        plan_service = DedupExecutionPlanService(db)
        try:
            plan_service.generate_plan_for_review(review.id)
            raise AssertionError(
                "Expected no plannable work - the only duplicate is a "
                "FIFO, which _observe_file treats as non-existent"
            )
        except ValueError as exc:
            assert "no plannable work left" in str(exc)

        assert fifo_path.exists()
        _cleanup(db, review.id, [canonical_doc.id, dup_doc.id])


def test_execute_socket_source_is_excluded_at_plan_generation(tmp_path) -> None:
    """Same reasoning again: a Unix domain socket is not
    `Path.is_file()`, so it is excluded before a plan can even be
    generated."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")
        socket_path = allowed_root / "dup.sock"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(socket_path))

            document_service = DocumentService(db)
            canonical_doc = document_service.create_document(
                DocumentCreate(
                    title="canonical.txt",
                    source=str(canonical_file),
                    source_type="txt",
                    content_hash="exec-socket-hash",
                )
            )
            dup_doc = document_service.create_document(
                DocumentCreate(
                    title="dup.sock",
                    source=str(socket_path),
                    source_type="txt",
                    content_hash="exec-socket-hash",
                )
            )
            dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
            db.commit()

            review_service = DedupReviewService(db)
            group = ExactDuplicateGroup(
                content_hash="exec-socket-hash", documents=[dup_doc, canonical_doc]
            )
            review = review_service.create_review_from_exact_group(group)
            review_service.approve_review(
                review.id, canonical_document_id=canonical_doc.id
            )

            plan_service = DedupExecutionPlanService(db)
            try:
                plan_service.generate_plan_for_review(review.id)
                raise AssertionError(
                    "Expected no plannable work - the only duplicate is a "
                    "socket, which _observe_file treats as non-existent"
                )
            except ValueError as exc:
                assert "no plannable work left" in str(exc)

            _cleanup(db, review.id, [canonical_doc.id, dup_doc.id])
        finally:
            sock.close()


def test_execute_own_regular_file_check_independently_rejects_a_directory(
    tmp_path,
) -> None:
    """Defense-in-depth proof for the executor's OWN `is_file()` check
    (distinct from the plan-generation-level exclusion proven above):
    even if a plan action somehow existed against a non-regular-file
    target with `check_plan_validity` reporting it valid (never
    reachable through the real pipeline today, since that check uses
    the identical `_observe_file` primitive - simulated here by
    patching `check_currency` to bypass it), the executor's own
    explicit `source_path.is_file()` check still independently
    refuses. This proves the redundancy is real protection, not dead
    code that merely happens to agree with an earlier check."""
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

        import dataclasses

        from app.dedup.execution_plan_service import DedupExecutionPlanService as _PlanSvc

        real_check_plan_validity = _PlanSvc.check_plan_validity

        def bypassed_check_plan_validity(self, plan_id):
            # `_attempt_action` derives actionability directly from
            # `validity.canonical_valid`/`action_validity.is_valid`,
            # NOT from check_currency's own summary boolean - so the
            # bypass has to happen here, at the actual validity
            # dataclass, to reach the executor's own is_file() check.
            real_validity = real_check_plan_validity(self, plan_id)
            patched_actions = [
                dataclasses.replace(a, exists_now=True, is_valid=True)
                for a in real_validity.actions
            ]
            return dataclasses.replace(real_validity, actions=patched_actions)

        try:
            # start_execution runs its OWN, unpatched check_plan_validity
            # - it must happen while the file is still a normal, valid
            # regular file, or this whole setup would be refused before
            # ever reaching the executor.
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            # ONLY NOW replace the duplicate's file with a directory -
            # after authorization/start_execution's own (unpatched)
            # validity checks have already passed against a real file.
            dup_files[0].unlink()
            dup_files[0].mkdir()

            with patch.object(
                _PlanSvc, "check_plan_validity", bypassed_check_plan_validity
            ):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert "not a regular file" in audits[0].error_message
            assert dup_files[0].is_dir()

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_symlinked_intermediate_directory_escape_is_precondition_failed(
    tmp_path,
) -> None:
    """A source path that stays lexically "inside" allowed_root but
    traverses through a symlinked intermediate DIRECTORY pointing
    outside it must be refused - the containment check is required to
    run on the fully resolved path for exactly this reason."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")

        escape_target = outside_dir / "escape_target.txt"
        escape_target.write_bytes(b"hello world")
        escape_link = allowed_root / "escape_link"
        escape_link.symlink_to(outside_dir)
        # This path is lexically under allowed_root, but escape_link
        # is a symlink to a directory OUTSIDE it.
        source_via_symlinked_dir = escape_link / "escape_target.txt"

        document_service = DocumentService(db)
        canonical_doc = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="exec-escape-hash",
            )
        )
        dup_doc = document_service.create_document(
            DocumentCreate(
                title="escape_target.txt",
                source=str(source_via_symlinked_dir),
                source_type="txt",
                content_hash="exec-escape-hash",
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="exec-escape-hash", documents=[dup_doc, canonical_doc]
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
            assert escape_target.exists()
            assert escape_target.read_bytes() == b"hello world"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_dotdot_traversal_in_source_path_is_precondition_failed(
    tmp_path,
) -> None:
    """A raw `..`-containing path string, lexically starting under
    allowed_root but resolving to a location outside it, must be
    refused - proving `resolve()`'s normalization plus the containment
    check actually defeats literal `..` segments, not merely paths
    that are already-clean absolute strings pointing elsewhere."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    sub_dir = allowed_root / "sub"
    sub_dir.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()
    outside_dir = tmp_path / "outside_via_dotdot"
    outside_dir.mkdir()

    engine = _engine()

    with Session(engine) as db:
        canonical_file = allowed_root / "canonical.txt"
        canonical_file.write_bytes(b"hello world")

        real_target = outside_dir / "target.txt"
        real_target.write_bytes(b"hello world")
        # Literal ".." segments in the raw string - lexically "under"
        # allowed_root/sub, but resolves to outside_dir/target.txt.
        traversal_path = str(sub_dir / ".." / ".." / "outside_via_dotdot" / "target.txt")
        assert ".." in traversal_path

        document_service = DocumentService(db)
        canonical_doc = document_service.create_document(
            DocumentCreate(
                title="canonical.txt",
                source=str(canonical_file),
                source_type="txt",
                content_hash="exec-dotdot-hash",
            )
        )
        dup_doc = document_service.create_document(
            DocumentCreate(
                title="target.txt",
                source=traversal_path,
                source_type="txt",
                content_hash="exec-dotdot-hash",
            )
        )
        dup_doc.created_at = canonical_doc.created_at + timedelta(seconds=1)
        db.commit()

        review_service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="exec-dotdot-hash", documents=[dup_doc, canonical_doc]
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
            assert real_target.exists()
            assert real_target.read_bytes() == b"hello world"

        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, dup_doc.id],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_exdev_on_rename_is_failed(tmp_path) -> None:
    """EXDEV raised by the actual os.rename() mutation path (as
    opposed to the constructor's own device pre-check) must fall into
    the same FAILED/mutation=False bucket as any other rename failure
    - never a copy+delete fallback, never anything else."""
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

        def fake_rename(src, dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with patch("app.dedup.executor.os.rename", side_effect=fake_rename):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.FAILED
            assert audits[0].filesystem_mutation_occurred is False
            assert "Invalid cross-device link" in audits[0].error_message
            # No copy+delete fallback occurred - the source is exactly
            # where it started, and no file exists in quarantine.
            assert dup_files[0].exists()
            assert dup_files[0].read_bytes() == b"hello world"
            assert list(quarantine_root.rglob("*")) == [] or all(
                not p.is_file() for p in quarantine_root.rglob("*")
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


def test_execute_destination_parent_wrong_device_is_failed(tmp_path) -> None:
    """Defense in depth: even though the constructor already confirmed
    allowed_root and quarantine_root share a device, the destination
    PARENT's own device is independently re-verified immediately
    before the rename - simulated here via a patched device lookup
    distinguishing the destination path from the roots."""
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

        real_device = executor._quarantine_device

        def fake_device_of(path):
            if str(quarantine_root) in str(path) and str(path) != str(quarantine_root):
                return real_device + 1  # simulate a mismatched destination parent
            return real_device

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with patch("app.dedup.executor._device_of", side_effect=fake_device_of):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert audits[0].result == DedupExecutionActionResult.FAILED
            assert audits[0].filesystem_mutation_occurred is False
            assert "quarantine filesystem device" in audits[0].error_message
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


# --- TOCTOU closure via inode/device identity pinning ------------------


def test_execute_identity_substitution_with_identical_content_is_unknown(
    tmp_path,
) -> None:
    """The adversarial proof of the TOCTOU closure itself. Before
    inode-pinning, a source file substituted for a DIFFERENT file with
    byte-for-byte IDENTICAL content (same hash, same size) in the
    window between the executor's final re-observation and
    `os.rename()` was an accepted, unclosable residual - the moved
    bytes would be indistinguishable from the intended ones, and the
    action would be recorded SUCCESS. This test performs that exact
    substitution for real (not by mocking a comparison result) and
    proves it is now always caught: the destination's post-rename
    identity cannot match what was pinned and hashed from the
    ORIGINAL file, since the substitute is a genuinely different
    inode."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(db, allowed_root, content=b"hello world")
        )
        execution_service = DedupExecutionService(db)
        executor = DedupFilesystemExecutor(db, allowed_root, quarantine_root)
        execution_ids: list[int] = []

        real_rename = os.rename

        def substitute_then_rename(src, dst):
            # Simulate an external actor replacing the source file, in
            # the window between the executor pinning/hashing it (which
            # already happened, earlier in the pipeline, before this
            # patched rename is ever called) and the real rename that
            # follows - with a DIFFERENT file that happens to have
            # byte-for-byte identical content. os.replace is itself
            # atomic and swaps in a genuinely different inode at this
            # exact path.
            substitute = Path(str(src) + ".substitute")
            substitute.write_bytes(b"hello world")
            os.replace(substitute, src)
            real_rename(src, dst)

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with patch(
                "app.dedup.executor.os.rename", side_effect=substitute_then_rename
            ):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.NEEDS_REVIEW

            audits = execution_service.get_action_audits(execution.id)
            assert len(audits) == 1
            assert audits[0].result == DedupExecutionActionResult.UNKNOWN
            assert audits[0].filesystem_mutation_occurred is None
            assert "pinned and hashed" in audits[0].error_message

            # The substituted file WAS still moved (rename is content-
            # blind, and only ever inspects identity, not bytes, at the
            # OS level) - the whole point is that this is never
            # reported SUCCESS despite the content matching perfectly.
            assert not dup_files[0].exists()
            moved_files = [p for p in quarantine_root.rglob("*") if p.is_file()]
            assert len(moved_files) == 1
            assert moved_files[0].read_bytes() == b"hello world"
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_destination_identity_unavailable_after_rename_is_unknown(
    tmp_path,
) -> None:
    """If the destination becomes entirely unstat-able after a
    successful rename (simulated here as ENOENT on every stat of that
    one path - a permission or filesystem error would look the same)
    the result must still be `UNKNOWN`, with the identity check named
    among the reasons, never a guessed `SUCCESS`. Pathlib's own
    `is_symlink()`/`is_file()` share the identical underlying `os.stat`
    call this identity check uses, so a destination that is genuinely
    unstat-able fails EVERY post-move check simultaneously, not just
    this one - that is the realistic scenario this test represents,
    not an artificially isolated one. `errno.ENOENT` specifically is
    used so pathlib's own internal error handling degrades gracefully
    (returns False) rather than re-raising a bare, errno-less OSError,
    matching what a genuinely vanished destination would actually look
    like at the syscall level."""
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
            source_path = Path(dup_files[0]).resolve()
            destination = (
                quarantine_root / str(execution.id) / f"{plan_action.id}__{source_path.name}"
            )

            real_os_stat = os.stat

            def selective_stat_failure(path, *args, **kwargs):
                if Path(path) == destination:
                    raise FileNotFoundError(
                        errno.ENOENT, "stat unavailable for this specific path"
                    )
                return real_os_stat(path, *args, **kwargs)

            with patch(
                "app.dedup.executor.os.stat", side_effect=selective_stat_failure
            ):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.NEEDS_REVIEW
            audits = execution_service.get_action_audits(execution.id)
            assert len(audits) == 1
            assert audits[0].result == DedupExecutionActionResult.UNKNOWN
            assert audits[0].filesystem_mutation_occurred is None
            # Identity is named among the failed checks - it is never
            # silently dropped just because other destination-based
            # checks failed for the same underlying reason.
            assert "pinned and hashed" in audits[0].error_message

            # The file was genuinely moved - a destination stat
            # failure is a verification-layer problem, never a
            # rename-layer one, and never undoes the mutation.
            assert not dup_files[0].exists()
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


def test_execute_source_removed_immediately_before_pin_is_precondition_failed(
    tmp_path,
) -> None:
    """If the source disappears in the narrow window between the
    earlier path-based regular-file check and `_pin_and_hash`'s own
    `os.open()` call, the result is a clean `PRECONDITION_FAILED` -
    `_pin_and_hash` raises `OSError` before any mutation is attempted,
    and the executor's own try/except around it turns that into a
    named refusal rather than an uncaught exception."""
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

        def vanish_then_raise(path):
            dup_files[0].unlink()
            raise OSError("source vanished immediately before pin-and-hash")

        try:
            execution = execution_service.start_execution(authorization.id)
            execution_ids.append(execution.id)

            with patch(
                "app.dedup.executor._pin_and_hash", side_effect=vanish_then_raise
            ):
                finalized = executor.execute(execution.id, confirm=True)

            assert finalized.status == DedupExecutionStatus.FAILED
            audits = execution_service.get_action_audits(execution.id)
            assert len(audits) == 1
            assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
            assert audits[0].filesystem_mutation_occurred is False
            assert "could not open and hash source" in audits[0].error_message
            assert not dup_files[0].exists()
            assert list(quarantine_root.rglob("*")) == []
        finally:
            _cleanup(
                db,
                review.id,
                [canonical_doc.id, *[d.id for d in dup_docs]],
                plan_ids=[plan.id],
                authorization_ids=[authorization.id],
                execution_ids=execution_ids,
            )


# --- concurrency exclusivity (Executor Hardening) --------------------------


def test_concurrent_execute_calls_only_one_claims_and_mutates(tmp_path) -> None:
    """The central hardening property: two genuinely concurrent
    `execute()` calls on the SAME execution_id, from two SEPARATE
    database sessions/connections (simulating two separate executor
    processes), must never both enter the mutation loop. Exactly one
    must win the SELECT ... FOR UPDATE claim; the other must fail
    cleanly and deterministically, never with a raw IntegrityError and
    never by falling through to an uncaught exception from
    complete_execution."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    with Session(engine) as setup_db:
        review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
            _setup_authorized_plan(setup_db, allowed_root)
        )
        execution = DedupExecutionService(setup_db).start_execution(authorization.id)
        execution_id = execution.id
        # Captured as plain values now, before this `with` block closes
        # `setup_db` - accessing ORM attributes on a detached instance
        # afterward raises DetachedInstanceError.
        review_id = review.id
        canonical_doc_id = canonical_doc.id
        dup_doc_ids = [d.id for d in dup_docs]
        plan_id = plan.id
        authorization_id = authorization.id

    db_a = Session(engine)
    db_b = Session(engine)

    # Slow down thread A's commits so thread B has a real window to
    # contend for the SAME row lock, rather than finding it already
    # released by the time it tries. This does not change correctness
    # - it only makes genuine contention deterministic in a fast,
    # single-action synthetic test instead of leaving it to luck.
    real_commit_a = db_a.commit

    def slow_commit():
        time.sleep(0.3)
        real_commit_a()

    db_a.commit = slow_commit

    executor_a = DedupFilesystemExecutor(db_a, allowed_root, quarantine_root)
    executor_b = DedupFilesystemExecutor(db_b, allowed_root, quarantine_root)

    results = {}
    errors = {}
    barrier = threading.Barrier(2)

    def run(name, executor):
        barrier.wait()
        try:
            results[name] = executor.execute(execution_id, confirm=True)
        except Exception as exc:  # noqa: BLE001 - capturing for assertion below
            errors[name] = exc

    thread_a = threading.Thread(target=run, args=("A", executor_a))
    thread_b = threading.Thread(target=run, args=("B", executor_b))

    try:
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert not thread_a.is_alive() and not thread_b.is_alive(), (
            "a thread did not finish - possible deadlock"
        )

        # Exactly one caller succeeded, exactly one failed.
        assert len(results) == 1, f"expected exactly one success, got {results}"
        assert len(errors) == 1, f"expected exactly one failure, got {errors}"

        (loser_exc,) = errors.values()
        # A clean, well-typed refusal - never a raw IntegrityError, and
        # never an uncaught exception surfacing from complete_execution.
        assert isinstance(loser_exc, ValueError)
        assert "IntegrityError" not in type(loser_exc).__name__
        assert (
            "claimed" in str(loser_exc)
            or "not RUNNING" in str(loser_exc)
            or "already has" in str(loser_exc)
        )

        # The filesystem was mutated exactly once.
        with Session(engine) as verify_db:
            audits = DedupExecutionService(verify_db).get_action_audits(execution_id)
            success_audits = [
                a for a in audits if a.result == DedupExecutionActionResult.SUCCESS
            ]
            assert len(success_audits) == 1

        assert not dup_files[0].exists()
        moved_files = [p for p in quarantine_root.rglob("*") if p.is_file()]
        assert len(moved_files) == 1
        assert moved_files[0].read_bytes() == b"hello world"

    finally:
        db_a.close()
        db_b.close()
        with Session(engine) as cleanup_db:
            _cleanup(
                cleanup_db,
                review_id,
                [canonical_doc_id, *dup_doc_ids],
                plan_ids=[plan_id],
                authorization_ids=[authorization_id],
                execution_ids=[execution_id],
            )


# --- authorization freshness (Executor Hardening) --------------------------


def test_authorization_revoked_from_another_session_is_observed_even_with_expire_on_commit_disabled(
    tmp_path,
) -> None:
    """Regression test for the explicit freshness mechanism
    (`self.db.expire_all()` at the top of `_attempt_action`): proves
    the executor observes an authorization revoked by a SEPARATE
    session even when THIS test deliberately disables
    `expire_on_commit` on the executor's own session - i.e. even when
    the SQLAlchemy default this property used to silently depend on is
    turned off, freshness still holds, because the mechanism is now
    explicit rather than incidental."""
    allowed_root = tmp_path / "corpus"
    allowed_root.mkdir()
    quarantine_root = tmp_path / "quarantine"
    quarantine_root.mkdir()

    engine = _engine()

    # expire_on_commit=False deliberately: if freshness depended on
    # that default (as the security review flagged it might), this
    # session configuration would cause a stale read and this test
    # would fail. It does not, because expire_all() is now explicit.
    executor_db = Session(engine, expire_on_commit=False)

    review, canonical_doc, dup_docs, canonical_file, dup_files, plan, authorization = (
        _setup_authorized_plan(executor_db, allowed_root, dup_count=2)
    )
    execution = DedupExecutionService(executor_db).start_execution(authorization.id)
    execution_ids = [execution.id]

    # Captured as plain values now, before executor_db is closed in the
    # `finally` block below - accessing ORM attributes on a detached,
    # expired instance after close() raises DetachedInstanceError.
    review_id = review.id
    canonical_doc_id = canonical_doc.id
    dup_doc_ids = [d.id for d in dup_docs]
    plan_id = plan.id
    authorization_id = authorization.id

    executor = DedupFilesystemExecutor(executor_db, allowed_root, quarantine_root)

    try:
        # Revoke from a COMPLETELY SEPARATE session/connection, after
        # the executor's own session has already loaded (and, with
        # expire_on_commit=False, would otherwise keep caching) the
        # authorization object.
        with Session(engine) as other_db:
            DedupPlanAuthorizationService(other_db).revoke_authorization(
                authorization.id, reason="revoked from another session mid-run"
            )

        finalized = executor.execute(execution.id, confirm=True)

        assert finalized.status == DedupExecutionStatus.FAILED
        audits = DedupExecutionService(executor_db).get_action_audits(execution.id)
        assert audits[0].result == DedupExecutionActionResult.PRECONDITION_FAILED
        assert "AUTHORIZED" in audits[0].error_message
        # Nothing was mutated - the revocation was observed before any
        # action was attempted.
        for f in dup_files:
            assert f.exists()

    finally:
        executor_db.close()
        with Session(engine) as cleanup_db:
            _cleanup(
                cleanup_db,
                review_id,
                [canonical_doc_id, *dup_doc_ids],
                plan_ids=[plan_id],
                authorization_ids=[authorization_id],
                execution_ids=execution_ids,
            )
