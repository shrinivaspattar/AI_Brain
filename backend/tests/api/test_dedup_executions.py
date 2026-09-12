from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models.dedup_execution import (
    DedupExecution,
    DedupExecutionActionAudit,
    DedupExecutionActionResult,
    DedupExecutionStatus,
)
from app.models.dedup_execution_plan import DedupPlanActionType


def _execution(
    execution_id: int = 1,
    authorization_id: int = 1,
    plan_id: int = 1,
    status: DedupExecutionStatus = DedupExecutionStatus.RUNNING,
    ended_at=None,
    failure_reason=None,
) -> DedupExecution:
    return DedupExecution(
        id=execution_id,
        authorization_id=authorization_id,
        plan_id=plan_id,
        status=status,
        executor_identity="test-executor",
        started_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
        ended_at=ended_at,
        failure_reason=failure_reason,
        updated_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
    )


def _audit(
    audit_id: int = 1,
    execution_id: int = 1,
    plan_action_id: int = 1,
    result: DedupExecutionActionResult = DedupExecutionActionResult.SUCCESS,
) -> DedupExecutionActionAudit:
    return DedupExecutionActionAudit(
        id=audit_id,
        execution_id=execution_id,
        plan_action_id=plan_action_id,
        document_id="doc-dup",
        planned_action=DedupPlanActionType.DELETE,
        source_path="/documents/dup.txt",
        target_path="/documents/canonical.txt",
        expected_content_hash="hash-a",
        expected_file_size=11,
        result=result,
        observed_content_hash="hash-a" if result == DedupExecutionActionResult.SUCCESS else None,
        observed_file_size=11 if result == DedupExecutionActionResult.SUCCESS else None,
        filesystem_mutation_occurred=(result == DedupExecutionActionResult.SUCCESS),
        created_at=datetime(2026, 9, 12, 10, 5, tzinfo=UTC),
    )


# --- start execution -----------------------------------------------------


def test_start_execution_succeeds() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            execution = _execution()
            service_class.return_value.start_execution.return_value = execution

            client = TestClient(app)
            response = client.post(
                "/dedup/authorizations/1/executions",
                json={"confirm": True, "executor_identity": "test-executor"},
            )

            assert response.status_code == 201
            body = response.json()
            assert body["authorization_id"] == 1
            assert body["status"] == "running"
            assert body["ended_at"] is None

            service_class.return_value.start_execution.assert_called_once_with(
                1, executor_identity="test-executor"
            )
    finally:
        app.dependency_overrides.clear()


def test_start_execution_requires_confirm_true() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            client = TestClient(app)
            response = client.post(
                "/dedup/authorizations/1/executions", json={"confirm": False}
            )

            assert response.status_code == 422
            service_class.return_value.start_execution.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_start_execution_returns_404_for_missing_authorization() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.start_execution.side_effect = ValueError(
                "Dedup plan authorization 999 not found"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/authorizations/999/executions", json={"confirm": True}
            )

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_start_execution_returns_409_for_inactive_authorization() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.start_execution.side_effect = ValueError(
                "Authorization 1 is not active (status=revoked)"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/authorizations/1/executions", json={"confirm": True}
            )

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_start_execution_returns_409_for_existing_execution() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.start_execution.side_effect = ValueError(
                "Authorization 1 already has an execution (execution 5, status=running)"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/authorizations/1/executions", json={"confirm": True}
            )

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_start_execution_returns_422_for_stale_plan() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.start_execution.side_effect = ValueError(
                "Dedup execution plan 1 is no longer valid"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/authorizations/1/executions", json={"confirm": True}
            )

            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


# --- list / get executions -----------------------------------------------


def test_list_executions() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.list_executions.return_value = [_execution()]

            client = TestClient(app)
            response = client.get("/dedup/executions", params={"plan_id": 1})

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["id"] == 1

            service_class.return_value.list_executions.assert_called_once_with(
                plan_id=1, authorization_id=None, status=None
            )
    finally:
        app.dependency_overrides.clear()


def test_get_execution() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.get_execution.return_value = _execution()

            client = TestClient(app)
            response = client.get("/dedup/executions/1")

            assert response.status_code == 200
            assert response.json()["id"] == 1
    finally:
        app.dependency_overrides.clear()


def test_get_execution_returns_404_for_missing() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.get_execution.return_value = None

            client = TestClient(app)
            response = client.get("/dedup/executions/999")

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- record action result -------------------------------------------------


def test_record_action_result_succeeds() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            audit = _audit()
            service_class.return_value.record_action_result.return_value = audit

            client = TestClient(app)
            response = client.post(
                "/dedup/executions/1/actions",
                json={
                    "plan_action_id": 1,
                    "result": "success",
                    "observed_content_hash": "hash-a",
                    "observed_file_size": 11,
                    "filesystem_mutation_occurred": True,
                },
            )

            assert response.status_code == 201
            body = response.json()
            assert body["result"] == "success"
            assert body["filesystem_mutation_occurred"] is True
            assert body["planned_action"] == "delete"

            service_class.return_value.record_action_result.assert_called_once()
            call_kwargs = service_class.return_value.record_action_result.call_args
            assert call_kwargs.args[0] == 1
            assert call_kwargs.args[1] == 1
            assert call_kwargs.args[2] == DedupExecutionActionResult.SUCCESS
    finally:
        app.dependency_overrides.clear()


def test_record_action_result_rejects_invalid_result_value() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            client = TestClient(app)
            response = client.post(
                "/dedup/executions/1/actions",
                json={"plan_action_id": 1, "result": "not-a-real-result"},
            )

            assert response.status_code == 422
            service_class.return_value.record_action_result.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_record_action_result_returns_409_for_non_running_execution() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.record_action_result.side_effect = ValueError(
                "Execution 1 is not RUNNING (status=completed)"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/executions/1/actions",
                json={"plan_action_id": 1, "result": "success"},
            )

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_record_action_result_returns_409_for_duplicate() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.record_action_result.side_effect = ValueError(
                "Execution 1 already has a recorded result for plan action 1 (audit 5)"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/executions/1/actions",
                json={"plan_action_id": 1, "result": "success"},
            )

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_record_action_result_returns_422_for_plan_action_mismatch() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.record_action_result.side_effect = ValueError(
                "Plan action 1 belongs to plan 2, not execution 1's plan 1"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/executions/1/actions",
                json={"plan_action_id": 1, "result": "success"},
            )

            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_record_action_result_returns_404_for_missing_plan_action() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.record_action_result.side_effect = ValueError(
                "Dedup execution plan action 999 not found"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/executions/1/actions",
                json={"plan_action_id": 999, "result": "success"},
            )

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- list action audits -----------------------------------------------


def test_list_execution_action_audits() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.get_execution.return_value = _execution()
            service_class.return_value.get_action_audits.return_value = [_audit()]

            client = TestClient(app)
            response = client.get("/dedup/executions/1/actions")

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["result"] == "success"
    finally:
        app.dependency_overrides.clear()


def test_list_execution_action_audits_returns_404_for_missing_execution() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.get_execution.return_value = None

            client = TestClient(app)
            response = client.get("/dedup/executions/999/actions")

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


# --- complete execution -----------------------------------------------


def test_complete_execution_succeeds() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            completed = _execution(
                status=DedupExecutionStatus.COMPLETED,
                ended_at=datetime(2026, 9, 12, 10, 10, tzinfo=UTC),
            )
            service_class.return_value.complete_execution.return_value = completed

            client = TestClient(app)
            response = client.post("/dedup/executions/1/complete")

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "completed"
            assert body["ended_at"] is not None
    finally:
        app.dependency_overrides.clear()


def test_complete_execution_reports_partial_completion() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            partial = _execution(
                status=DedupExecutionStatus.PARTIALLY_COMPLETED,
                ended_at=datetime(2026, 9, 12, 10, 10, tzinfo=UTC),
                failure_reason="Action for plan action 3 reported failed",
            )
            service_class.return_value.complete_execution.return_value = partial

            client = TestClient(app)
            response = client.post("/dedup/executions/1/complete")

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "partially_completed"
            assert "plan action 3" in body["failure_reason"]
    finally:
        app.dependency_overrides.clear()


def test_complete_execution_returns_422_for_incomplete_audit_trail() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.complete_execution.side_effect = ValueError(
                "Execution 1 cannot be finalized - 2 of 3 planned action(s) "
                "have no recorded outcome yet"
            )

            client = TestClient(app)
            response = client.post("/dedup/executions/1/complete")

            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_complete_execution_returns_409_for_already_finalized() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.complete_execution.side_effect = ValueError(
                "Execution 1 is not RUNNING (status=completed)"
            )

            client = TestClient(app)
            response = client.post("/dedup/executions/1/complete")

            assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_complete_execution_returns_404_for_missing() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_executions.DedupExecutionService") as service_class:
            service_class.return_value.complete_execution.side_effect = ValueError(
                "Dedup execution 999 not found"
            )

            client = TestClient(app)
            response = client.post("/dedup/executions/999/complete")

            assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
