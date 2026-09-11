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
        import_job_id=42,
    )


def test_execute_job_completes_even_if_embedding_fails(tmp_path: Path) -> None:
    db = MagicMock()

    job = ImportJob(
        id=42,
        name="Test Import",
        source_path="/documents/source",
        source_type="filesystem",
        status=ImportStatus.PENDING,
    )

    db.get.return_value = job

    document = MagicMock()
    document.id = "doc-1"
    document.source = str(tmp_path / "missing.txt")

    service = ImportJobService(db)

    with patch("app.services.import_job_service.DocumentIngestor") as ingestor_class:
        ingestor_class.return_value.ingest.return_value = [document]
        ingestor_class.return_value.last_discovered_count = 1

        result = service.execute_job(42)

    assert result.status == ImportStatus.COMPLETED


def test_embed_documents_embeds_readable_text_documents(tmp_path: Path) -> None:
    db = MagicMock()
    service = ImportJobService(db)

    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello AI_Brain")

    document = MagicMock()
    document.id = "doc-1"
    document.source = str(file_path)

    with patch(
        "app.services.import_job_service.EmbeddingService"
    ) as embedding_service_class:
        service._embed_documents([document])

    embedding_service_class.assert_called_once_with(db, embedding_client=None)
    embedding_service_class.return_value.embed_document.assert_called_once_with(
        document, "hello AI_Brain"
    )


def test_embed_documents_skips_unreadable_document_without_raising() -> None:
    db = MagicMock()
    service = ImportJobService(db)

    document = MagicMock()
    document.id = "doc-1"
    document.source = "/does/not/exist.txt"

    with patch(
        "app.services.import_job_service.EmbeddingService"
    ) as embedding_service_class:
        service._embed_documents([document])

    embedding_service_class.return_value.embed_document.assert_not_called()


def test_embed_documents_skips_document_when_embedding_fails(tmp_path: Path) -> None:
    db = MagicMock()
    service = ImportJobService(db)

    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello AI_Brain")

    document = MagicMock()
    document.id = "doc-1"
    document.source = str(file_path)

    with patch(
        "app.services.import_job_service.EmbeddingService"
    ) as embedding_service_class:
        embedding_service_class.return_value.embed_document.side_effect = RuntimeError(
            "ollama unreachable"
        )

        service._embed_documents([document])


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
