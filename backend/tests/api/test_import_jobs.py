from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models.import_job import ImportStatus


def test_execute_import_job() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            job = MagicMock()
            job.id = 42
            job.name = "Test Import"
            job.source_path = "/documents/source"
            job.source_type = "filesystem"
            job.status = ImportStatus.COMPLETED
            job.progress = 100
            job.files_discovered = 2
            job.files_processed = 2
            job.error_message = None
            job.started_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
            job.finished_at = datetime(2026, 8, 12, 10, 1, tzinfo=UTC)
            job.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
            job.updated_at = datetime(2026, 8, 12, 10, 1, tzinfo=UTC)

            service_class.return_value.execute_job.return_value = job

            client = TestClient(app)

            response = client.post("/import-jobs/42/execute")

            assert response.status_code == 200
            assert response.json() == {
                "id": 42,
                "name": "Test Import",
                "source_path": "/documents/source",
                "source_type": "filesystem",
                "status": "COMPLETED",
                "progress": 100,
                "files_discovered": 2,
                "files_processed": 2,
                "error_message": None,
                "started_at": "2026-08-12T10:00:00Z",
                "finished_at": "2026-08-12T10:01:00Z",
                "created_at": "2026-08-12T10:00:00Z",
                "updated_at": "2026-08-12T10:01:00Z",
            }

            service_class.return_value.execute_job.assert_called_once_with(42)

    finally:
        app.dependency_overrides.clear()


def test_execute_import_job_returns_conflict_for_invalid_state() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.execute_job.side_effect = ValueError(
                "Import job 42 cannot be executed"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/execute")

            assert response.status_code == 409
            assert response.json() == {"detail": "Import job 42 cannot be executed"}

            service_class.return_value.execute_job.assert_called_once_with(42)

    finally:
        app.dependency_overrides.clear()


def test_execute_import_job_returns_not_found() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.execute_job.side_effect = ValueError(
                "Import job 42 not found"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/execute")

            assert response.status_code == 404
            assert response.json() == {"detail": "Import job 42 not found"}

            service_class.return_value.execute_job.assert_called_once_with(42)

    finally:
        app.dependency_overrides.clear()


def test_start_import_job_returns_conflict_for_invalid_state() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.mark_running.side_effect = ValueError(
                "Import job 42 cannot transition to RUNNING"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/start")

            assert response.status_code == 409
            assert response.json() == {
                "detail": "Import job 42 cannot transition to RUNNING"
            }

    finally:
        app.dependency_overrides.clear()


def test_start_import_job_returns_not_found() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.mark_running.side_effect = ValueError(
                "Import job 42 not found"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/start")

            assert response.status_code == 404
            assert response.json() == {"detail": "Import job 42 not found"}

    finally:
        app.dependency_overrides.clear()


def test_complete_import_job_returns_conflict_for_invalid_state() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.mark_completed.side_effect = ValueError(
                "Import job 42 cannot transition to COMPLETED"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/complete")

            assert response.status_code == 409
            assert response.json() == {
                "detail": "Import job 42 cannot transition to COMPLETED"
            }

    finally:
        app.dependency_overrides.clear()


def test_complete_import_job_returns_not_found() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.mark_completed.side_effect = ValueError(
                "Import job 42 not found"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/complete")

            assert response.status_code == 404
            assert response.json() == {"detail": "Import job 42 not found"}

    finally:
        app.dependency_overrides.clear()
