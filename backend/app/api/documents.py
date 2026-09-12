from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.provenance.service import ProvenanceService
from app.schemas.document import DocumentCreate, DocumentResponse
from app.schemas.provenance import DocumentProvenanceResponse
from app.services.document_service import DocumentService

router = APIRouter(
    prefix="/documents",
    tags=["Documents"],
)


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_document(
    document_data: DocumentCreate,
    db: Session = Depends(get_db),
) -> DocumentResponse:
    service = DocumentService(db)
    return service.create_document(document_data)


@router.get(
    "/{document_id}/provenance",
    response_model=DocumentProvenanceResponse,
    responses={404: {"description": "Document not found"}},
)
def get_document_provenance(
    document_id: str,
    db: Session = Depends(get_db),
) -> DocumentProvenanceResponse:
    service = ProvenanceService(db)

    try:
        return service.trace_document(document_id)

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
