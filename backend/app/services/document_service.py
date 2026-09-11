from sqlalchemy.orm import Session

from app.models.document import Document
from app.schemas.document import DocumentCreate


class DocumentService:
    def __init__(self, db: Session):
        self.db = db

    def create_document(
        self,
        document_data: DocumentCreate,
    ) -> Document:
        document = Document(
            title=document_data.title,
            source=document_data.source,
            source_type=document_data.source_type,
            import_job_id=document_data.import_job_id,
        )

        try:
            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)
            return document

        except Exception:
            self.db.rollback()
            raise
