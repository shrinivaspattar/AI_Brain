from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.dedup.execution_plan_service import ActionValidity, PlanValidity
from app.models.dedup_execution_plan import (
    DedupExecutionPlan,
    DedupExecutionPlanAction,
    DedupPlanActionType,
    DedupPlanStatus,
)
from app.models.document import Document


def _document(doc_id: str, title: str, source: str) -> Document:
    return Document(
        id=doc_id,
        title=title,
        source=source,
        source_type="txt",
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def _plan() -> DedupExecutionPlan:
    return DedupExecutionPlan(
        id=1,
        review_id=1,
        canonical_document_id="doc-canonical",
        canonical_source_path="/documents/canonical.txt",
        canonical_observed_exists=True,
        canonical_observed_content_hash="hash-a",
        canonical_observed_file_size=11,
        status=DedupPlanStatus.GENERATED,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def _action() -> DedupExecutionPlanAction:
    return DedupExecutionPlanAction(
        id=1,
        plan_id=1,
        document_id="doc-dup",
        action=DedupPlanActionType.DELETE,
        source_path="/documents/dup.txt",
        target_document_id="doc-canonical",
        target_path="/documents/canonical.txt",
        observed_exists=True,
        observed_content_hash="hash-a",
        observed_file_size=11,
        reason="Proposed for removal as a duplicate of the canonical, per review #1.",
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


# --- generate ---------------------------------------------------------


def test_generate_dedup_execution_plan() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            plan = _plan()
            action = _action()
            doc_dup = _document("doc-dup", "dup.txt", "/documents/dup.txt")

            service_class.return_value.generate_plan_for_review.return_value = plan
            service_class.return_value.get_plan_actions_with_documents.return_value = [
                (action, doc_dup)
            ]

            client = TestClient(app)
            response = client.post("/dedup/reviews/1/plans")

            assert response.status_code == 201
            body = response.json()
            assert body["review_id"] == 1
            assert body["canonical_document_id"] == "doc-canonical"
            assert body["status"] == "generated"
            assert len(body["actions"]) == 1
            assert body["actions"][0]["document_id"] == "doc-dup"
            assert body["actions"][0]["action"] == "delete"

            service_class.return_value.generate_plan_for_review.assert_called_once_with(1)

    finally:
        app.dependency_overrides.clear()


def test_generate_dedup_execution_plan_returns_404_for_missing_review() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            service_class.return_value.generate_plan_for_review.side_effect = ValueError(
                "Duplicate review 999 not found"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/999/plans")

            assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


def test_generate_dedup_execution_plan_returns_409_for_unapproved_review() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            service_class.return_value.generate_plan_for_review.side_effect = ValueError(
                "Duplicate review 1 is not approved (status=pending) - a dry-run "
                "execution plan can only be generated for an approved review"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/1/plans")

            assert response.status_code == 409

    finally:
        app.dependency_overrides.clear()


def test_generate_dedup_execution_plan_returns_422_for_near_review_without_canonical() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            service_class.return_value.generate_plan_for_review.side_effect = ValueError(
                "Duplicate review 2 was approved without an explicit human-selected "
                "canonical document - it does not contain a decision sufficient to "
                "generate an execution plan"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/2/plans")

            assert response.status_code == 422

    finally:
        app.dependency_overrides.clear()


# --- list / get ---------------------------------------------------------


def test_list_dedup_execution_plans() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            plan = _plan()
            action = _action()
            doc_dup = _document("doc-dup", "dup.txt", "/documents/dup.txt")

            service_class.return_value.list_plans.return_value = [plan]
            service_class.return_value.get_plan_actions_with_documents.return_value = [
                (action, doc_dup)
            ]

            client = TestClient(app)
            response = client.get("/dedup/plans", params={"review_id": 1})

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["id"] == 1

            service_class.return_value.list_plans.assert_called_once_with(review_id=1)

    finally:
        app.dependency_overrides.clear()


def test_list_dedup_execution_plans_returns_empty_list() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            service_class.return_value.list_plans.return_value = []

            client = TestClient(app)
            response = client.get("/dedup/plans")

            assert response.status_code == 200
            assert response.json() == []

    finally:
        app.dependency_overrides.clear()


def test_get_dedup_execution_plan() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            plan = _plan()
            service_class.return_value.get_plan.return_value = plan
            service_class.return_value.get_plan_actions_with_documents.return_value = []

            client = TestClient(app)
            response = client.get("/dedup/plans/1")

            assert response.status_code == 200
            assert response.json()["id"] == 1

    finally:
        app.dependency_overrides.clear()


def test_get_dedup_execution_plan_returns_404_for_missing_plan() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            service_class.return_value.get_plan.return_value = None

            client = TestClient(app)
            response = client.get("/dedup/plans/999")

            assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


# --- validity -----------------------------------------------------------


def test_get_dedup_execution_plan_validity_valid() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            validity = PlanValidity(
                plan_id=1,
                canonical_document_exists=True,
                canonical_path_changed=False,
                canonical_exists_now=True,
                canonical_type_matches=True,
                canonical_hash_matches=True,
                canonical_size_matches=True,
                canonical_valid=True,
                actions=[
                    ActionValidity(
                        action_id=1,
                        document_id="doc-dup",
                        source_path="/documents/dup.txt",
                        document_exists=True,
                        path_changed=False,
                        exists_now=True,
                        type_matches=True,
                        hash_matches=True,
                        size_matches=True,
                        is_valid=True,
                    )
                ],
                is_valid=True,
            )
            service_class.return_value.check_plan_validity.return_value = validity

            client = TestClient(app)
            response = client.get("/dedup/plans/1/validity")

            assert response.status_code == 200
            body = response.json()
            assert body["is_valid"] is True
            assert body["actions"][0]["is_valid"] is True

    finally:
        app.dependency_overrides.clear()


def test_get_dedup_execution_plan_validity_stale() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            validity = PlanValidity(
                plan_id=1,
                canonical_document_exists=True,
                canonical_path_changed=False,
                canonical_exists_now=True,
                canonical_type_matches=True,
                canonical_hash_matches=True,
                canonical_size_matches=True,
                canonical_valid=True,
                actions=[
                    ActionValidity(
                        action_id=1,
                        document_id="doc-dup",
                        source_path="/documents/dup.txt",
                        document_exists=True,
                        path_changed=False,
                        exists_now=True,
                        type_matches=True,
                        hash_matches=False,
                        size_matches=True,
                        is_valid=False,
                    )
                ],
                is_valid=False,
            )
            service_class.return_value.check_plan_validity.return_value = validity

            client = TestClient(app)
            response = client.get("/dedup/plans/1/validity")

            assert response.status_code == 200
            body = response.json()
            assert body["is_valid"] is False
            assert body["actions"][0]["hash_matches"] is False

    finally:
        app.dependency_overrides.clear()


def test_get_dedup_execution_plan_validity_returns_404_for_missing_plan() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_execution_plans.DedupExecutionPlanService") as service_class:
            service_class.return_value.check_plan_validity.side_effect = ValueError(
                "Dedup execution plan 999 not found"
            )

            client = TestClient(app)
            response = client.get("/dedup/plans/999/validity")

            assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()
