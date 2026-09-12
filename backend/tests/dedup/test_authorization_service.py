from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.dedup.authorization_service import DedupPlanAuthorizationService
from app.dedup.execution_plan_service import ActionValidity, PlanValidity
from app.models.dedup_authorization import DedupPlanAuthorization, DedupPlanAuthorizationStatus
from app.models.dedup_execution_plan import DedupExecutionPlan, DedupPlanStatus
from app.models.dedup_review import DuplicateReview, DuplicateReviewStatus


def _plan(plan_id: int = 1, review_id: int = 1) -> DedupExecutionPlan:
    return DedupExecutionPlan(
        id=plan_id,
        review_id=review_id,
        canonical_document_id="doc-canonical",
        canonical_source_path="/documents/canonical.txt",
        canonical_observed_exists=True,
        canonical_observed_content_hash="hash-a",
        canonical_observed_file_size=11,
        status=DedupPlanStatus.GENERATED,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def _review(review_id: int = 1, status: DuplicateReviewStatus = DuplicateReviewStatus.APPROVED) -> DuplicateReview:
    return DuplicateReview(
        id=review_id,
        confidence=1.0,
        recommendation_reason="test",
        evidence={},
        status=status,
        human_selected_canonical_document_id="doc-canonical",
    )


def _validity(plan_id: int = 1, is_valid: bool = True) -> PlanValidity:
    return PlanValidity(
        plan_id=plan_id,
        canonical_document_exists=True,
        canonical_path_changed=False,
        canonical_exists_now=True,
        canonical_type_matches=True,
        canonical_hash_matches=is_valid,
        canonical_size_matches=True,
        canonical_valid=is_valid,
        actions=[
            ActionValidity(
                action_id=1,
                document_id="doc-dup",
                source_path="/documents/dup.txt",
                document_exists=True,
                path_changed=False,
                exists_now=True,
                type_matches=True,
                hash_matches=is_valid,
                size_matches=True,
                is_valid=is_valid,
            )
        ],
        is_valid=is_valid,
    )


def _service_for(
    db,
    plan=None,
    review=None,
    validity=None,
    existing_active=None,
) -> DedupPlanAuthorizationService:
    service = DedupPlanAuthorizationService(db)
    service.plan_service.get_plan = MagicMock(return_value=plan)
    service.plan_service.check_plan_validity = MagicMock(return_value=validity)
    service.review_service.get_review = MagicMock(return_value=review)
    service._get_active_authorization = MagicMock(return_value=existing_active)
    return service


# --- authorize_plan: happy path -------------------------------------------


def test_authorize_plan_succeeds_for_approved_review_and_valid_plan() -> None:
    db = MagicMock()
    plan = _plan()
    review = _review()
    validity = _validity(is_valid=True)
    service = _service_for(db, plan=plan, review=review, validity=validity)

    authorization = service.authorize_plan(1, authorized_by="tester")

    assert authorization.plan_id == 1
    assert authorization.status == DedupPlanAuthorizationStatus.AUTHORIZED
    assert authorization.authorized_by == "tester"
    assert authorization.validity_snapshot["is_valid"] is True
    db.add.assert_called_once()
    db.commit.assert_called_once()


# --- authorize_plan: pre-check failures ------------------------------------


def test_authorize_plan_raises_for_missing_plan() -> None:
    db = MagicMock()
    service = _service_for(db, plan=None)

    with pytest.raises(ValueError, match="not found"):
        service.authorize_plan(999)

    db.add.assert_not_called()


def test_authorize_plan_raises_for_missing_review() -> None:
    db = MagicMock()
    plan = _plan()
    service = _service_for(db, plan=plan, review=None)

    with pytest.raises(ValueError, match="not found"):
        service.authorize_plan(1)

    db.add.assert_not_called()


def test_authorize_plan_raises_for_pending_review() -> None:
    db = MagicMock()
    plan = _plan()
    review = _review(status=DuplicateReviewStatus.PENDING)
    service = _service_for(db, plan=plan, review=review)

    with pytest.raises(ValueError, match="is not approved"):
        service.authorize_plan(1)

    db.add.assert_not_called()


def test_authorize_plan_raises_for_rejected_review() -> None:
    db = MagicMock()
    plan = _plan()
    review = _review(status=DuplicateReviewStatus.REJECTED)
    service = _service_for(db, plan=plan, review=review)

    with pytest.raises(ValueError, match="is not approved"):
        service.authorize_plan(1)

    db.add.assert_not_called()


def test_authorize_plan_raises_for_stale_plan() -> None:
    db = MagicMock()
    plan = _plan()
    review = _review()
    validity = _validity(is_valid=False)
    service = _service_for(db, plan=plan, review=review, validity=validity)

    with pytest.raises(ValueError, match="no longer valid"):
        service.authorize_plan(1)

    db.add.assert_not_called()


def test_authorize_plan_raises_for_existing_active_authorization() -> None:
    db = MagicMock()
    plan = _plan()
    review = _review()
    validity = _validity(is_valid=True)
    existing = DedupPlanAuthorization(
        id=5,
        plan_id=1,
        status=DedupPlanAuthorizationStatus.AUTHORIZED,
        validity_snapshot={},
    )
    service = _service_for(
        db, plan=plan, review=review, validity=validity, existing_active=existing
    )

    with pytest.raises(ValueError, match="already has an active authorization"):
        service.authorize_plan(1)

    db.add.assert_not_called()


def test_authorize_plan_does_not_call_validity_check_before_confirming_review_approved() -> None:
    """Cheap, purely-DB pre-checks (review approval) must short-circuit
    before the more expensive filesystem-touching validity re-check."""
    db = MagicMock()
    plan = _plan()
    review = _review(status=DuplicateReviewStatus.PENDING)
    service = _service_for(db, plan=plan, review=review)

    with pytest.raises(ValueError, match="is not approved"):
        service.authorize_plan(1)

    service.plan_service.check_plan_validity.assert_not_called()


# --- get / list -------------------------------------------------------------


def test_list_authorizations_filters_by_plan_id_and_status() -> None:
    db = MagicMock()
    service = DedupPlanAuthorizationService(db)

    service.list_authorizations(plan_id=1, status=DedupPlanAuthorizationStatus.AUTHORIZED)

    statement = db.scalars.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "dedup_plan_authorizations.plan_id" in compiled
    assert "dedup_plan_authorizations.status" in compiled


def test_get_authorization_returns_none_for_missing() -> None:
    db = MagicMock()
    db.get.return_value = None
    service = DedupPlanAuthorizationService(db)

    assert service.get_authorization(999) is None


# --- revoke_authorization ----------------------------------------------------


def test_revoke_authorization_succeeds() -> None:
    db = MagicMock()
    authorization = DedupPlanAuthorization(
        id=1,
        plan_id=1,
        status=DedupPlanAuthorizationStatus.AUTHORIZED,
        validity_snapshot={},
    )
    service = DedupPlanAuthorizationService(db)
    service.get_authorization = MagicMock(return_value=authorization)

    revoked = service.revoke_authorization(1, reason="changed my mind")

    assert revoked.status == DedupPlanAuthorizationStatus.REVOKED
    assert revoked.revocation_reason == "changed my mind"
    assert revoked.revoked_at is not None
    db.commit.assert_called_once()


def test_revoke_authorization_raises_for_missing() -> None:
    db = MagicMock()
    service = DedupPlanAuthorizationService(db)
    service.get_authorization = MagicMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        service.revoke_authorization(999)


def test_revoke_authorization_raises_for_already_revoked() -> None:
    db = MagicMock()
    authorization = DedupPlanAuthorization(
        id=1,
        plan_id=1,
        status=DedupPlanAuthorizationStatus.REVOKED,
        validity_snapshot={},
    )
    service = DedupPlanAuthorizationService(db)
    service.get_authorization = MagicMock(return_value=authorization)

    with pytest.raises(ValueError, match="already been revoked"):
        service.revoke_authorization(1)

    db.commit.assert_not_called()


# --- check_currency (TOCTOU) -------------------------------------------------


def test_check_currency_actionable_when_authorized_and_still_valid() -> None:
    db = MagicMock()
    authorization = DedupPlanAuthorization(
        id=1,
        plan_id=1,
        status=DedupPlanAuthorizationStatus.AUTHORIZED,
        validity_snapshot={},
    )
    service = DedupPlanAuthorizationService(db)
    service.get_authorization = MagicMock(return_value=authorization)
    service.plan_service.check_plan_validity = MagicMock(return_value=_validity(is_valid=True))

    _, validity, is_still_actionable = service.check_currency(1)

    assert is_still_actionable is True
    assert validity.is_valid is True


def test_check_currency_not_actionable_when_plan_went_stale() -> None:
    """Simulates the TOCTOU case: authorization is still AUTHORIZED, but
    the filesystem changed since - `check_currency` must reflect a
    freshly-run check, not the authorization's frozen snapshot."""
    db = MagicMock()
    authorization = DedupPlanAuthorization(
        id=1,
        plan_id=1,
        status=DedupPlanAuthorizationStatus.AUTHORIZED,
        validity_snapshot={"is_valid": True},
    )
    service = DedupPlanAuthorizationService(db)
    service.get_authorization = MagicMock(return_value=authorization)
    service.plan_service.check_plan_validity = MagicMock(return_value=_validity(is_valid=False))

    _, validity, is_still_actionable = service.check_currency(1)

    assert is_still_actionable is False
    assert validity.is_valid is False


def test_check_currency_not_actionable_when_revoked_even_if_plan_valid() -> None:
    db = MagicMock()
    authorization = DedupPlanAuthorization(
        id=1,
        plan_id=1,
        status=DedupPlanAuthorizationStatus.REVOKED,
        validity_snapshot={},
    )
    service = DedupPlanAuthorizationService(db)
    service.get_authorization = MagicMock(return_value=authorization)
    service.plan_service.check_plan_validity = MagicMock(return_value=_validity(is_valid=True))

    _, _, is_still_actionable = service.check_currency(1)

    assert is_still_actionable is False


def test_check_currency_raises_for_missing_authorization() -> None:
    db = MagicMock()
    service = DedupPlanAuthorizationService(db)
    service.get_authorization = MagicMock(return_value=None)

    with pytest.raises(ValueError, match="not found"):
        service.check_currency(999)
