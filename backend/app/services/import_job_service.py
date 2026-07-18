from sqlalchemy import select
from sqlalchemy.orm import Session
from app.schemas.import_job import ImportJobCreate
from app.models.import_job import ImportJob


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

    def get_job(self, job_id: int) -> ImportJob | None:
        return self.db.get(ImportJob, job_id)

    def list_jobs(self) -> list[ImportJob]:
        statement = select(ImportJob).order_by(
            ImportJob.created_at.desc()
        )
        return list(self.db.scalars(statement))