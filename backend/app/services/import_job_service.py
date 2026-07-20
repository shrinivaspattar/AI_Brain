from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.import_job import ImportJob, ImportStatus
from app.schemas.import_job import ImportJobCreate


class ImportJobService:
    def __init__(self, db: Session):
        self.db = db

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
        statement = select(ImportJob).order_by(
            ImportJob.created_at.desc()
        )
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