from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException, status
from app.db.session import get_db
from app.schemas.import_job import ImportJobCreate, ImportJobResponse
from app.services.import_job_service import ImportJobService

router = APIRouter(
    prefix="/import-jobs",
    tags=["Import Jobs"],
)


@router.get(
    "/{job_id}",
    response_model=ImportJobResponse,
    responses={
        404: {
            "description": "Import job not found"
        }
    },
)
def get_import_job(
    job_id: int,
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    service = ImportJobService(db)

    job = service.get_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import job {job_id} not found",
        )

    return job