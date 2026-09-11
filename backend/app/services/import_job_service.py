import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.embeddings.client import EmbeddingClient
from app.ingestion.document_ingestor import DocumentIngestor
from app.models.document import Document
from app.models.import_job import ImportJob, ImportStatus
from app.schemas.import_job import ImportJobCreate
from app.services.document_service import DocumentService
from app.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)


class ImportJobService:
    def __init__(
        self,
        db: Session,
        ingestion_dir: Path | None = None,
        embedding_client: EmbeddingClient | None = None,
    ):
        self.db = db
        self.ingestion_dir = ingestion_dir or settings.INGESTION_DIR
        self.embedding_client = embedding_client

    def create_job(
        self,
        job_data: ImportJobCreate,
    ) -> ImportJob:
        job = ImportJob(
            name=job_data.name,
            source_path=job_data.source_path,
            source_type=job_data.source_type,
        )

        try:
            self.db.add(job)
            self.db.commit()
            self.db.refresh(job)
            return job

        except Exception:
            self.db.rollback()
            raise

    def get_job(
        self,
        job_id: int,
    ) -> ImportJob | None:
        return self.db.get(ImportJob, job_id)

    def list_jobs(
        self,
    ) -> list[ImportJob]:
        statement = select(ImportJob).order_by(ImportJob.created_at.desc())
        return list(self.db.scalars(statement))

    def _get_job_or_raise(
        self,
        job_id: int,
    ) -> ImportJob:
        job = self.db.get(ImportJob, job_id)

        if job is None:
            raise ValueError(f"Import job {job_id} not found")

        return job

    def mark_running(
        self,
        job_id: int,
    ) -> ImportJob:
        job = self._get_job_or_raise(job_id)

        if job.status not in {
            ImportStatus.PENDING,
            ImportStatus.FAILED,
        }:
            raise ValueError(f"Import job {job.id} cannot transition to RUNNING")

        try:
            job.status = ImportStatus.RUNNING
            job.started_at = datetime.now(UTC)

            self.db.commit()
            self.db.refresh(job)

            return job

        except Exception:
            self.db.rollback()
            raise

    def mark_completed(
        self,
        job_id: int,
    ) -> ImportJob:
        job = self._get_job_or_raise(job_id)

        if job.status != ImportStatus.RUNNING:
            raise ValueError(
                f"Import job {job.id} cannot transition to COMPLETED"
            )

        try:
            job.status = ImportStatus.COMPLETED
            job.progress = 100
            job.finished_at = datetime.now(UTC)

            self.db.commit()
            self.db.refresh(job)

            return job

        except Exception:
            self.db.rollback()
            raise

    def _embed_documents(
        self,
        documents: list[Document],
    ) -> None:
        """Embed each document's text content, synchronously, best-effort.

        A document that can't be read as text (binary, missing, wrong
        encoding) or that fails to embed (e.g. Ollama unreachable) is
        skipped rather than failing the whole import job.
        """
        embedding_service = EmbeddingService(
            self.db,
            embedding_client=self.embedding_client,
        )

        for document in documents:
            try:
                content = Path(document.source).read_text(encoding="utf-8")
                embedding_service.embed_document(document, content)
            except Exception:
                logger.warning(
                    "Skipping embedding for document %s",
                    document.id,
                    exc_info=True,
                )

    def execute_job(
        self,
        job_id: int,
    ) -> ImportJob:
        job = self._get_job_or_raise(job_id)

        if job.status in {
            ImportStatus.RUNNING,
            ImportStatus.PAUSED,
            ImportStatus.COMPLETED,
            ImportStatus.CANCELLED,
        }:
            raise ValueError(f"Import job {job.id} cannot be executed")

        self.mark_running(job_id)

        try:
            destination = self.ingestion_dir / str(job.id)

            document_service = DocumentService(self.db)
            ingestor = DocumentIngestor(document_service)

            documents = ingestor.ingest(
                Path(job.source_path),
                destination,
                import_job_id=job.id,
            )

            job.files_discovered = ingestor.last_discovered_count
            job.files_processed = ingestor.last_discovered_count

            self._embed_documents(documents)

            self.db.commit()
            self.db.refresh(job)

            return self.mark_completed(job_id)

        except Exception as exc:
            self.db.rollback()

            job.status = ImportStatus.FAILED
            job.error_message = str(exc)
            job.finished_at = datetime.now(UTC)

            self.db.commit()
            self.db.refresh(job)

            raise
