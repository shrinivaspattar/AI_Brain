import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.dedup.execution_plan_service import DedupExecutionPlanService, _observe_file
from app.models.dedup_execution_plan import (
    DedupExecutionPlan,
    DedupExecutionPlanAction,
    DedupPlanActionType,
    DedupPlanStatus,
)
from app.models.dedup_review import (
    DuplicateMatchType,
    DuplicateReview,
    DuplicateReviewMember,
    DuplicateReviewMemberRole,
    DuplicateReviewStatus,
)
from app.models.document import Document


def _document(
    doc_id: str, title: str, source: str, source_type: str = "txt"
) -> Document:
    return Document(
        id=doc_id,
        title=title,
        source=source,
        source_type=source_type,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _member(
    review_id: int, document_id: str, role: DuplicateReviewMemberRole
) -> DuplicateReviewMember:
    return DuplicateReviewMember(
        review_id=review_id, document_id=document_id, role=role
    )


def _review(
    review_id: int,
    match_type: DuplicateMatchType,
    status: DuplicateReviewStatus,
    human_selected_canonical_document_id: str | None = None,
) -> DuplicateReview:
    return DuplicateReview(
        id=review_id,
        match_type=match_type,
        confidence=1.0,
        recommendation_reason="test",
        evidence={},
        status=status,
        human_selected_canonical_document_id=human_selected_canonical_document_id,
    )


def _service_for(db, review, pairs) -> DedupExecutionPlanService:
    service = DedupExecutionPlanService(db)
    service.review_service.get_review = MagicMock(return_value=review)
    service.review_service.get_review_members_with_documents = MagicMock(
        return_value=pairs
    )
    return service


def _plan(
    plan_id: int,
    canonical_document_id: str,
    canonical_source_path: str,
    canonical_observed_exists: bool,
    canonical_observed_content_hash: str | None,
    canonical_observed_file_size: int | None,
) -> DedupExecutionPlan:
    return DedupExecutionPlan(
        id=plan_id,
        review_id=1,
        canonical_document_id=canonical_document_id,
        canonical_source_path=canonical_source_path,
        canonical_observed_exists=canonical_observed_exists,
        canonical_observed_content_hash=canonical_observed_content_hash,
        canonical_observed_file_size=canonical_observed_file_size,
        status=DedupPlanStatus.GENERATED,
    )


def _action(
    action_id: int,
    plan_id: int,
    document_id: str,
    source_path: str,
    observed_exists: bool,
    observed_content_hash: str | None,
    observed_file_size: int | None,
) -> DedupExecutionPlanAction:
    return DedupExecutionPlanAction(
        id=action_id,
        plan_id=plan_id,
        document_id=document_id,
        action=DedupPlanActionType.DELETE,
        source_path=source_path,
        target_document_id="doc-canonical",
        target_path="/anywhere/canonical.txt",
        observed_exists=observed_exists,
        observed_content_hash=observed_content_hash,
        observed_file_size=observed_file_size,
        reason="test",
    )


# --- _observe_file ---------------------------------------------------------


def test_observe_file_returns_hash_and_size_for_existing_file(tmp_path) -> None:
    f = tmp_path / "x.txt"
    f.write_bytes(b"content")

    observation = _observe_file(str(f))

    assert observation.exists is True
    assert observation.content_hash == hashlib.sha256(b"content").hexdigest()
    assert observation.file_size == 7


def test_observe_file_reports_missing_file() -> None:
    observation = _observe_file("/definitely/does/not/exist-xyz.txt")

    assert observation.exists is False
    assert observation.content_hash is None
    assert observation.file_size is None


def test_observe_file_reports_directory_as_missing(tmp_path) -> None:
    observation = _observe_file(str(tmp_path))

    assert observation.exists is False


# --- generate_plan_for_review: invalid candidate / state guards -----------


def test_generate_plan_raises_for_missing_review() -> None:
    db = MagicMock()
    service = DedupExecutionPlanService(db)
    service.review_service.get_review = MagicMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        service.generate_plan_for_review(999)


def test_generate_plan_raises_for_pending_review() -> None:
    db = MagicMock()
    review = _review(1, DuplicateMatchType.EXACT, DuplicateReviewStatus.PENDING)
    service = _service_for(db, review, [])

    with pytest.raises(ValueError, match="is not approved"):
        service.generate_plan_for_review(1)


def test_generate_plan_raises_for_rejected_review() -> None:
    db = MagicMock()
    review = _review(1, DuplicateMatchType.EXACT, DuplicateReviewStatus.REJECTED)
    service = _service_for(db, review, [])

    with pytest.raises(ValueError, match="is not approved"):
        service.generate_plan_for_review(1)


def test_generate_plan_raises_for_approved_near_review_without_canonical() -> None:
    """The core near-duplicate safety rule: a review approved without an
    explicit canonical choice does not carry a decision sufficient to
    generate an execution plan - and this must never be inferred."""
    db = MagicMock()
    review = _review(
        1,
        DuplicateMatchType.NEAR,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id=None,
    )
    service = _service_for(db, review, [])

    with pytest.raises(ValueError, match="without an explicit human-selected canonical"):
        service.generate_plan_for_review(1)


def test_generate_plan_raises_for_no_members() -> None:
    db = MagicMock()
    review = _review(
        1,
        DuplicateMatchType.EXACT,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id="doc-1",
    )
    service = _service_for(db, review, [])

    with pytest.raises(ValueError, match="has no members"):
        service.generate_plan_for_review(1)


def test_generate_plan_raises_for_canonical_not_a_member() -> None:
    db = MagicMock()
    review = _review(
        1,
        DuplicateMatchType.EXACT,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id="doc-999",
    )
    doc_a = _document("doc-1", "a.txt", "/nonexistent/a.txt")
    doc_b = _document("doc-2", "b.txt", "/nonexistent/b.txt")
    pairs = [
        (_member(1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL), doc_a),
        (_member(1, "doc-2", DuplicateReviewMemberRole.DUPLICATE), doc_b),
    ]
    service = _service_for(db, review, pairs)

    with pytest.raises(ValueError, match="not one of its members"):
        service.generate_plan_for_review(1)


# --- generate_plan_for_review: exact approved review ----------------------


def test_generate_plan_for_exact_review_creates_plan_and_action(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    review = _review(
        1,
        DuplicateMatchType.EXACT,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id="doc-canonical",
    )
    doc_canonical = _document("doc-canonical", "canonical.txt", str(canonical_file))
    doc_dup = _document("doc-dup", "dup.txt", str(dup_file))
    pairs = [
        (
            _member(1, "doc-canonical", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
            doc_canonical,
        ),
        (_member(1, "doc-dup", DuplicateReviewMemberRole.DUPLICATE), doc_dup),
    ]

    db = MagicMock()
    service = _service_for(db, review, pairs)

    plan = service.generate_plan_for_review(1)

    expected_hash = hashlib.sha256(b"hello world").hexdigest()

    assert plan.review_id == 1
    assert plan.canonical_document_id == "doc-canonical"
    assert plan.canonical_source_path == str(canonical_file)
    assert plan.canonical_observed_exists is True
    assert plan.canonical_observed_content_hash == expected_hash
    assert plan.canonical_observed_file_size == 11
    assert plan.status == DedupPlanStatus.GENERATED

    added_actions = [
        call.args[0] for call in db.add.call_args_list if hasattr(call.args[0], "action")
    ]
    assert len(added_actions) == 1
    action = added_actions[0]
    assert action.document_id == "doc-dup"
    assert action.action == DedupPlanActionType.DELETE
    assert action.source_path == str(dup_file)
    assert action.target_document_id == "doc-canonical"
    assert action.target_path == str(canonical_file)
    assert action.observed_exists is True
    assert action.observed_content_hash == expected_hash
    assert action.observed_file_size == 11
    assert "review #1" in action.reason
    assert "canonical.txt" in action.reason


def test_generate_plan_handles_multiple_duplicate_members(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file_1 = tmp_path / "dup1.txt"
    dup_file_1.write_text("hello world")
    dup_file_2 = tmp_path / "dup2.txt"
    dup_file_2.write_text("hello world")

    review = _review(
        1,
        DuplicateMatchType.EXACT,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id="doc-canonical",
    )
    pairs = [
        (
            _member(1, "doc-canonical", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
            _document("doc-canonical", "canonical.txt", str(canonical_file)),
        ),
        (
            _member(1, "doc-dup1", DuplicateReviewMemberRole.DUPLICATE),
            _document("doc-dup1", "dup1.txt", str(dup_file_1)),
        ),
        (
            _member(1, "doc-dup2", DuplicateReviewMemberRole.DUPLICATE),
            _document("doc-dup2", "dup2.txt", str(dup_file_2)),
        ),
    ]

    db = MagicMock()
    service = _service_for(db, review, pairs)

    service.generate_plan_for_review(1)

    added_actions = [
        call.args[0] for call in db.add.call_args_list if hasattr(call.args[0], "action")
    ]
    assert {a.document_id for a in added_actions} == {"doc-dup1", "doc-dup2"}


def test_generate_plan_records_missing_source_honestly(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello")
    missing_path = str(tmp_path / "does-not-exist.txt")

    review = _review(
        1,
        DuplicateMatchType.EXACT,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id="doc-canonical",
    )
    pairs = [
        (
            _member(1, "doc-canonical", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
            _document("doc-canonical", "canonical.txt", str(canonical_file)),
        ),
        (
            _member(1, "doc-missing", DuplicateReviewMemberRole.DUPLICATE),
            _document("doc-missing", "missing.txt", missing_path),
        ),
    ]

    db = MagicMock()
    service = _service_for(db, review, pairs)

    service.generate_plan_for_review(1)

    added_actions = [
        call.args[0] for call in db.add.call_args_list if hasattr(call.args[0], "action")
    ]
    assert added_actions[0].observed_exists is False
    assert added_actions[0].observed_content_hash is None
    assert added_actions[0].observed_file_size is None


# --- generate_plan_for_review: near duplicate with explicit canonical -----


def test_generate_plan_for_near_review_with_explicit_human_canonical(tmp_path) -> None:
    """NEAR members are all role=DUPLICATE in the database - the plan
    must exclude the human-*chosen* canonical from actions based on
    human_selected_canonical_document_id, not on the (absent) role."""
    file_a = tmp_path / "a.txt"
    file_a.write_text("version A")
    file_b = tmp_path / "b.txt"
    file_b.write_text("version B, somewhat different")

    review = _review(
        2,
        DuplicateMatchType.NEAR,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id="doc-a",
    )
    pairs = [
        (_member(2, "doc-a", DuplicateReviewMemberRole.DUPLICATE), _document("doc-a", "a.txt", str(file_a))),
        (_member(2, "doc-b", DuplicateReviewMemberRole.DUPLICATE), _document("doc-b", "b.txt", str(file_b))),
    ]

    db = MagicMock()
    service = _service_for(db, review, pairs)

    plan = service.generate_plan_for_review(2)

    assert plan.canonical_document_id == "doc-a"
    added_actions = [
        call.args[0] for call in db.add.call_args_list if hasattr(call.args[0], "action")
    ]
    assert len(added_actions) == 1
    assert added_actions[0].document_id == "doc-b"


# --- deterministic / repeated plan generation ------------------------------


def test_repeated_plan_generation_is_deterministic(tmp_path) -> None:
    """Two consecutive plans generated for the same review against an
    unchanged filesystem must describe the exact same action content -
    same hashes, sizes, and paths - even though each call creates a
    genuinely new, independent row (see DedupExecutionPlan's docstring:
    a plan is an immutable audit record, never updated in place)."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")

    review = _review(
        1,
        DuplicateMatchType.EXACT,
        DuplicateReviewStatus.APPROVED,
        human_selected_canonical_document_id="doc-canonical",
    )
    pairs = [
        (
            _member(1, "doc-canonical", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
            _document("doc-canonical", "canonical.txt", str(canonical_file)),
        ),
        (
            _member(1, "doc-dup", DuplicateReviewMemberRole.DUPLICATE),
            _document("doc-dup", "dup.txt", str(dup_file)),
        ),
    ]

    db = MagicMock()
    service = _service_for(db, review, pairs)

    first_plan = service.generate_plan_for_review(1)
    first_actions = [
        call.args[0] for call in db.add.call_args_list if hasattr(call.args[0], "action")
    ]

    db.reset_mock()
    second_plan = service.generate_plan_for_review(1)
    second_actions = [
        call.args[0] for call in db.add.call_args_list if hasattr(call.args[0], "action")
    ]

    assert first_plan.canonical_observed_content_hash == second_plan.canonical_observed_content_hash
    assert first_plan.canonical_observed_file_size == second_plan.canonical_observed_file_size
    assert first_actions[0].observed_content_hash == second_actions[0].observed_content_hash
    assert first_actions[0].observed_file_size == second_actions[0].observed_file_size
    assert first_actions[0].source_path == second_actions[0].source_path


# --- check_plan_validity ---------------------------------------------------


def test_check_plan_validity_true_when_unchanged(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    content_hash = hashlib.sha256(b"hello world").hexdigest()

    plan = _plan(1, "doc-canonical", str(canonical_file), True, content_hash, 11)
    action = _action(1, 1, "doc-dup", str(dup_file), True, content_hash, 11)

    db = MagicMock()
    db.get.side_effect = lambda model, doc_id: _document(doc_id, doc_id, str(canonical_file) if doc_id == "doc-canonical" else str(dup_file))

    service = DedupExecutionPlanService(db)
    service.get_plan = MagicMock(return_value=plan)
    db.scalars.return_value = [action]

    validity = service.check_plan_validity(1)

    assert validity.canonical_valid is True
    assert validity.actions[0].is_valid is True
    assert validity.is_valid is True


def test_check_plan_validity_detects_changed_hash(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world - but now edited")
    content_hash = hashlib.sha256(b"hello world").hexdigest()

    plan = _plan(1, "doc-canonical", str(canonical_file), True, content_hash, 11)
    # observed_content_hash reflects what the file looked like when the
    # plan was generated - now stale relative to the edited file.
    action = _action(1, 1, "doc-dup", str(dup_file), True, content_hash, 11)

    db = MagicMock()
    db.get.side_effect = lambda model, doc_id: _document(doc_id, doc_id, str(canonical_file) if doc_id == "doc-canonical" else str(dup_file))
    db.scalars.return_value = [action]

    service = DedupExecutionPlanService(db)
    service.get_plan = MagicMock(return_value=plan)

    validity = service.check_plan_validity(1)

    assert validity.actions[0].hash_matches is False
    assert validity.actions[0].is_valid is False
    assert validity.is_valid is False


def test_check_plan_validity_detects_changed_size(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    content_hash = hashlib.sha256(b"hello world").hexdigest()

    plan = _plan(1, "doc-canonical", str(canonical_file), True, content_hash, 11)
    # Wrong recorded size relative to the real (unchanged) 11-byte file.
    action = _action(1, 1, "doc-dup", str(dup_file), True, content_hash, 999)

    db = MagicMock()
    db.get.side_effect = lambda model, doc_id: _document(doc_id, doc_id, str(canonical_file) if doc_id == "doc-canonical" else str(dup_file))
    db.scalars.return_value = [action]

    service = DedupExecutionPlanService(db)
    service.get_plan = MagicMock(return_value=plan)

    validity = service.check_plan_validity(1)

    assert validity.actions[0].size_matches is False
    assert validity.actions[0].is_valid is False
    assert validity.is_valid is False


def test_check_plan_validity_detects_missing_source(tmp_path) -> None:
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    content_hash = hashlib.sha256(b"hello world").hexdigest()
    missing_path = str(tmp_path / "gone.txt")

    plan = _plan(1, "doc-canonical", str(canonical_file), True, content_hash, 11)
    action = _action(1, 1, "doc-dup", missing_path, True, "some-old-hash", 5)

    db = MagicMock()
    db.get.side_effect = lambda model, doc_id: _document(doc_id, doc_id, str(canonical_file) if doc_id == "doc-canonical" else missing_path)
    db.scalars.return_value = [action]

    service = DedupExecutionPlanService(db)
    service.get_plan = MagicMock(return_value=plan)

    validity = service.check_plan_validity(1)

    assert validity.actions[0].exists_now is False
    assert validity.actions[0].is_valid is False
    assert validity.is_valid is False


def test_check_plan_validity_detects_changed_document_path(tmp_path) -> None:
    """The Document row's source may have been updated since the plan
    was generated (e.g. a re-ingest) - the plan only ever reads its own
    frozen path, so a changed live path must be flagged, not ignored."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    content_hash = hashlib.sha256(b"hello world").hexdigest()

    plan = _plan(1, "doc-canonical", str(canonical_file), True, content_hash, 11)
    action = _action(1, 1, "doc-dup", str(dup_file), True, content_hash, 11)

    # The live Document now points somewhere else entirely.
    moved_document = _document("doc-dup", "dup.txt", str(tmp_path / "new-location.txt"))

    db = MagicMock()
    db.get.side_effect = lambda model, doc_id: (
        _document("doc-canonical", "canonical.txt", str(canonical_file))
        if doc_id == "doc-canonical"
        else moved_document
    )
    db.scalars.return_value = [action]

    service = DedupExecutionPlanService(db)
    service.get_plan = MagicMock(return_value=plan)

    validity = service.check_plan_validity(1)

    assert validity.actions[0].path_changed is True
    assert validity.actions[0].is_valid is False
    assert validity.is_valid is False


def test_check_plan_validity_detects_deleted_document(tmp_path) -> None:
    """A Document row deleted entirely since plan generation must make
    the plan invalid - not be silently skipped from the report."""
    canonical_file = tmp_path / "canonical.txt"
    canonical_file.write_text("hello world")
    dup_file = tmp_path / "dup.txt"
    dup_file.write_text("hello world")
    content_hash = hashlib.sha256(b"hello world").hexdigest()

    plan = _plan(1, "doc-canonical", str(canonical_file), True, content_hash, 11)
    action = _action(1, 1, "doc-dup", str(dup_file), True, content_hash, 11)

    db = MagicMock()
    db.get.side_effect = lambda model, doc_id: (
        _document("doc-canonical", "canonical.txt", str(canonical_file))
        if doc_id == "doc-canonical"
        else None
    )
    db.scalars.return_value = [action]

    service = DedupExecutionPlanService(db)
    service.get_plan = MagicMock(return_value=plan)

    validity = service.check_plan_validity(1)

    assert validity.actions[0].document_exists is False
    assert validity.actions[0].is_valid is False
    assert validity.is_valid is False


def test_check_plan_validity_raises_for_nonexistent_plan() -> None:
    db = MagicMock()
    service = DedupExecutionPlanService(db)
    service.get_plan = MagicMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        service.check_plan_validity(999)


# --- listing ----------------------------------------------------------


def test_list_plans_filters_by_review_id() -> None:
    db = MagicMock()
    service = DedupExecutionPlanService(db)

    service.list_plans(review_id=1)

    statement = db.scalars.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "dedup_execution_plans.review_id" in compiled


def test_get_plan_actions_with_documents_returns_empty_for_no_actions() -> None:
    db = MagicMock()
    db.scalars.return_value = []
    service = DedupExecutionPlanService(db)

    assert service.get_plan_actions_with_documents(1) == []
