from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models.import_job import ImportJob, ImportStatus
from app.services.import_job_service import ImportJobService


def test_execute_job_ingests_source_and_completes() -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=ImportStatus.PENDING,
    )

    db.get.return_value = job

    documents = [MagicMock(), MagicMock()]

    service = ImportJobService(db)

    with patch("app.services.import_job_service.DocumentIngestor") as ingestor_class:
        ingestor_class.return_value.ingest.return_value = documents
        ingestor_class.return_value.last_discovered_count = len(documents)

        result = service.execute_job(42)

    assert result is job
    assert result.status == ImportStatus.COMPLETED
    assert result.progress == 100
    assert result.files_discovered == 2
    assert result.files_processed == 2
    assert result.finished_at is not None

    ingestor_class.return_value.ingest.assert_called_once_with(
        Path("/documents/source"),
        service.ingestion_dir / "42",
    )


def test_execute_job_marks_failed_on_ingestion_error() -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=ImportStatus.PENDING,
    )

    db.get.return_value = job

    service = ImportJobService(db)

    with patch("app.services.import_job_service.DocumentIngestor") as ingestor_class:
        ingestor_class.return_value.ingest.side_effect = RuntimeError(
            "ingestion failed"
        )

        with pytest.raises(RuntimeError, match="ingestion failed"):
            service.execute_job(42)

    assert job.status == ImportStatus.FAILED
    assert job.error_message == "ingestion failed"
    assert job.finished_at is not None


def test_execute_job_rejects_missing_job() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = ImportJobService(db)

    with pytest.raises(ValueError, match="Import job 42 not found"):
        service.execute_job(42)



def test_execute_job_allows_failed_job_retry() -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=ImportStatus.FAILED,
    )

    db.get.return_value = job

    documents = [MagicMock()]

    service = ImportJobService(db)

    with patch("app.services.import_job_service.DocumentIngestor") as ingestor_class:
        ingestor_class.return_value.ingest.return_value = documents
        ingestor_class.return_value.last_discovered_count = len(documents)

        result = service.execute_job(42)

    assert result is job
    assert result.status == ImportStatus.COMPLETED
    assert result.files_discovered == 1
    assert result.files_processed == 1


def test_mark_completed_allows_running_job() -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=ImportStatus.RUNNING,
    )

    db.get.return_value = job

    service = ImportJobService(db)

    result = service.mark_completed(42)

    assert result is job
    assert result.status == ImportStatus.COMPLETED
    assert result.progress == 100
    assert result.finished_at is not None


def test_mark_running_allows_pending_job() -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=ImportStatus.PENDING,
    )

    db.get.return_value = job

    service = ImportJobService(db)

    result = service.mark_running(42)

    assert result is job
    assert result.status == ImportStatus.RUNNING
    assert result.started_at is not None


def test_mark_running_allows_failed_job_retry() -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=ImportStatus.FAILED,
    )

    db.get.return_value = job

    service = ImportJobService(db)

    result = service.mark_running(42)

    assert result is job
    assert result.status == ImportStatus.RUNNING
    assert result.started_at is not None


@pytest.mark.parametrize(
    "status",
    [
        ImportStatus.PENDING,
        ImportStatus.FAILED,
        ImportStatus.PAUSED,
        ImportStatus.COMPLETED,
        ImportStatus.CANCELLED,
    ],
)
def test_mark_completed_rejects_invalid_statuses(
    status: ImportStatus,
) -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=status,
    )

    db.get.return_value = job

    service = ImportJobService(db)

    with pytest.raises(
        ValueError,
        match="Import job 42 cannot transition to COMPLETED",
    ):
        service.mark_completed(42)

    assert job.status == status


def test_mark_completed_rejects_missing_job() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = ImportJobService(db)

    with pytest.raises(ValueError, match="Import job 42 not found"):
        service.mark_completed(42)
