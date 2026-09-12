from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models.import_job import ImportStatus


def _mock_job(
    job_id: int,
    name: str,
    status: ImportStatus,
    *,
    progress: float = 0,
    files_discovered: int = 0,
    files_processed: int = 0,
    error_message: str | None = None,
    source_path: str = "/documents/source",
    source_type: str = "filesystem",
) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.source_path = source_path
    job.source_type = source_type
    job.status = status
    job.progress = progress
    job.files_discovered = files_discovered
    job.files_processed = files_processed
    job.error_message = error_message
    job.started_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC) if status != ImportStatus.PENDING else None
    job.finished_at = (
        datetime(2026, 8, 12, 10, 5, tzinfo=UTC)
        if status in {ImportStatus.COMPLETED, ImportStatus.FAILED, ImportStatus.CANCELLED}
        else None
    )
    job.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    job.updated_at = datetime(2026, 8, 12, 10, 5, tzinfo=UTC)
    return job


def test_list_import_jobs_returns_empty_list() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.list_jobs.return_value = []

            client = TestClient(app)
            response = client.get("/import-jobs")

            assert response.status_code == 200
            assert response.json() == []

    finally:
        app.dependency_overrides.clear()


def test_list_import_jobs_returns_each_status() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            jobs = [
                _mock_job(1, "Pending job", ImportStatus.PENDING),
                _mock_job(2, "Running job", ImportStatus.RUNNING, progress=42.5),
                _mock_job(3, "Paused job", ImportStatus.PAUSED, progress=10),
                _mock_job(
                    4,
                    "Completed job",
                    ImportStatus.COMPLETED,
                    progress=100,
                    files_discovered=5,
                    files_processed=5,
                ),
                _mock_job(
                    5,
                    "Failed job",
                    ImportStatus.FAILED,
                    error_message="Source path does not exist",
                ),
                _mock_job(6, "Cancelled job", ImportStatus.CANCELLED),
            ]
            service_class.return_value.list_jobs.return_value = jobs

            client = TestClient(app)
            response = client.get("/import-jobs")

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 6
            assert [job["status"] for job in body] == [
                "PENDING",
                "RUNNING",
                "PAUSED",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            ]

    finally:
        app.dependency_overrides.clear()


def test_list_import_jobs_includes_error_message_for_failed_job() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.list_jobs.return_value = [
                _mock_job(
                    1,
                    "Failed job",
                    ImportStatus.FAILED,
                    error_message="[Errno 2] No such file or directory: '/nope'",
                )
            ]

            client = TestClient(app)
            response = client.get("/import-jobs")

            assert response.status_code == 200
            body = response.json()
            assert body[0]["status"] == "FAILED"
            assert body[0]["error_message"] == "[Errno 2] No such file or directory: '/nope'"

    finally:
        app.dependency_overrides.clear()


def test_list_import_jobs_handles_large_counter_values() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.list_jobs.return_value = [
                _mock_job(
                    1,
                    "Huge corpus import",
                    ImportStatus.RUNNING,
                    progress=37.2,
                    files_discovered=482_193,
                    files_processed=179_804,
                )
            ]

            client = TestClient(app)
            response = client.get("/import-jobs")

            assert response.status_code == 200
            body = response.json()
            assert body[0]["files_discovered"] == 482_193
            assert body[0]["files_processed"] == 179_804

    finally:
        app.dependency_overrides.clear()


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


def test_execute_import_job_returns_422_for_missing_source_path() -> None:
    """Regression test: a nonexistent source path is an expected import
    failure (already recorded as FAILED with error_message by the
    service by the time this exception is raised), not an unhandled
    server error - it must not become a bare 500."""
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.execute_job.side_effect = FileNotFoundError(
                "/documents/does-not-exist"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/execute")

            assert response.status_code == 422
            body = response.json()
            assert body["detail"] == "/documents/does-not-exist"
            assert "Traceback" not in body["detail"]

    finally:
        app.dependency_overrides.clear()


def test_execute_import_job_returns_422_for_source_not_a_directory() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.import_jobs.ImportJobService") as service_class:
            service_class.return_value.execute_job.side_effect = NotADirectoryError(
                "/documents/a-file.txt"
            )

            client = TestClient(app)

            response = client.post("/import-jobs/42/execute")

            assert response.status_code == 422
            assert response.json() == {"detail": "/documents/a-file.txt"}

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
