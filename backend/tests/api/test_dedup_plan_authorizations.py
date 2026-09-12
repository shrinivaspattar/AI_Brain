from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.dedup.execution_plan_service import ActionValidity, PlanValidity
from app.models.dedup_authorization import DedupPlanAuthorization, DedupPlanAuthorizationStatus


def _authorization(
    authorization_id: int = 1,
    plan_id: int = 1,
    status: DedupPlanAuthorizationStatus = DedupPlanAuthorizationStatus.AUTHORIZED,
    revoked_at=None,
    revocation_reason=None,
) -> DedupPlanAuthorization:
    return DedupPlanAuthorization(
        id=authorization_id,
        plan_id=plan_id,
        status=status,
        validity_snapshot={"plan_id": plan_id, "is_valid": True},
        authorized_by="tester",
        authorized_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        revoked_at=revoked_at,
        revocation_reason=revocation_reason,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def _validity(is_valid: bool = True) -> PlanValidity:
    return PlanValidity(
        plan_id=1,
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


# --- authorize ---------------------------------------------------------


def test_authorize_plan_succeeds() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            authorization = _authorization()
            service_class.return_value.authorize_plan.return_value = authorization

            client = TestClient(app)
            response = client.post(
                "/dedup/plans/1/authorize",
                json={"confirm": True, "authorized_by": "tester"},
            )

            assert response.status_code == 201
            body = response.json()
            assert body["plan_id"] == 1
            assert body["status"] == "authorized"
            assert body["authorized_by"] == "tester"

            service_class.return_value.authorize_plan.assert_called_once_with(
                1, authorized_by="tester"
            )
    finally:
        app.dependency_overrides.clear()


def test_authorize_plan_requires_confirm_true() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            client = TestClient(app)
            response = client.post("/dedup/plans/1/authorize", json={"confirm": False})

            assert response.status_code == 422
            service_class.return_value.authorize_plan.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_authorize_plan_returns_404_for_missing_plan() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.authorize_plan.side_effect = ValueError(
                "Dedup execution plan 999 not found"
            )

            client = TestClient(app)
            response = client.post("/dedup/plans/999/authorize", json={"confirm": True})

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_authorize_plan_returns_409_for_unapproved_review() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.authorize_plan.side_effect = ValueError(
                "Duplicate review 1 backing plan 1 is not approved (status=pending)"
            )

            client = TestClient(app)
            response = client.post("/dedup/plans/1/authorize", json={"confirm": True})

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_authorize_plan_returns_409_for_existing_active_authorization() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.authorize_plan.side_effect = ValueError(
                "Dedup execution plan 1 already has an active authorization "
                "(authorization 5)"
            )

            client = TestClient(app)
            response = client.post("/dedup/plans/1/authorize", json={"confirm": True})

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_authorize_plan_returns_422_for_stale_plan() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.authorize_plan.side_effect = ValueError(
                "Dedup execution plan 1 is no longer valid"
            )

            client = TestClient(app)
            response = client.post("/dedup/plans/1/authorize", json={"confirm": True})

            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


# --- list / get ----------------------------------------------------------


def test_list_authorizations() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.list_authorizations.return_value = [_authorization()]

            client = TestClient(app)
            response = client.get("/dedup/authorizations", params={"plan_id": 1})

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["id"] == 1

            service_class.return_value.list_authorizations.assert_called_once_with(
                plan_id=1, status=None
            )
    finally:
        app.dependency_overrides.clear()


def test_get_authorization() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.get_authorization.return_value = _authorization()

            client = TestClient(app)
            response = client.get("/dedup/authorizations/1")

            assert response.status_code == 200
            assert response.json()["id"] == 1
    finally:
        app.dependency_overrides.clear()


def test_get_authorization_returns_404_for_missing() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.get_authorization.return_value = None

            client = TestClient(app)
            response = client.get("/dedup/authorizations/999")

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- currency (TOCTOU) -----------------------------------------------------


def test_get_authorization_currency_actionable() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            authorization = _authorization()
            validity = _validity(is_valid=True)
            service_class.return_value.check_currency.return_value = (
                authorization,
                validity,
                True,
            )

            client = TestClient(app)
            response = client.get("/dedup/authorizations/1/currency")

            assert response.status_code == 200
            body = response.json()
            assert body["is_still_actionable"] is True
            assert body["current_validity"]["is_valid"] is True
    finally:
        app.dependency_overrides.clear()


def test_get_authorization_currency_not_actionable_when_stale() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            authorization = _authorization()
            validity = _validity(is_valid=False)
            service_class.return_value.check_currency.return_value = (
                authorization,
                validity,
                False,
            )

            client = TestClient(app)
            response = client.get("/dedup/authorizations/1/currency")

            assert response.status_code == 200
            body = response.json()
            assert body["is_still_actionable"] is False
            assert body["current_validity"]["is_valid"] is False
    finally:
        app.dependency_overrides.clear()


def test_get_authorization_currency_returns_404_for_missing() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.check_currency.side_effect = ValueError(
                "Dedup plan authorization 999 not found"
            )

            client = TestClient(app)
            response = client.get("/dedup/authorizations/999/currency")

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- revoke ----------------------------------------------------------------


def test_revoke_authorization_succeeds() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            revoked = _authorization(
                status=DedupPlanAuthorizationStatus.REVOKED,
                revoked_at=datetime(2026, 8, 12, 11, 0, tzinfo=UTC),
                revocation_reason="changed my mind",
            )
            service_class.return_value.revoke_authorization.return_value = revoked

            client = TestClient(app)
            response = client.post(
                "/dedup/authorizations/1/revoke", json={"reason": "changed my mind"}
            )

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "revoked"
            assert body["revocation_reason"] == "changed my mind"
    finally:
        app.dependency_overrides.clear()


def test_revoke_authorization_returns_404_for_missing() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.revoke_authorization.side_effect = ValueError(
                "Dedup plan authorization 999 not found"
            )

            client = TestClient(app)
            response = client.post("/dedup/authorizations/999/revoke", json={})

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_revoke_authorization_returns_409_for_already_revoked() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch(
            "app.api.dedup_plan_authorizations.DedupPlanAuthorizationService"
        ) as service_class:
            service_class.return_value.revoke_authorization.side_effect = ValueError(
                "Authorization 1 has already been revoked"
            )

            client = TestClient(app)
            response = client.post("/dedup/authorizations/1/revoke", json={})

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()
