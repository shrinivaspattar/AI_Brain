from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

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


@router.post(
    "",
    response_model=ImportJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_import_job(
    job_data: ImportJobCreate,
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    service = ImportJobService(db)
    return service.create_job(job_data)


@router.get(
    "",
    response_model=List[ImportJobResponse],
)
def list_import_jobs(
    db: Session = Depends(get_db),
) -> list[ImportJobResponse]:
    service = ImportJobService(db)
    return service.list_jobs()


@router.post(
    "/{job_id}/start",
    response_model=ImportJobResponse,
    responses={
        404: {
            "description": "Import job not found"
        }
    },
)
def start_import_job(
    job_id: int,
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    service = ImportJobService(db)

    try:
        return service.mark_running(job_id)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    
    
@router.post(
    "/{job_id}/complete",
    response_model=ImportJobResponse,
    responses={
        404: {
            "description": "Import job not found"
        }
    },
)
def complete_import_job(
    job_id: int,
    db: Session = Depends(get_db),
) -> ImportJobResponse:
    service = ImportJobService(db)

    try:
        return service.mark_completed(job_id)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )