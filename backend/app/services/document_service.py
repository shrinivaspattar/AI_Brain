from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.schemas.document import DocumentCreate

DEFAULT_LIST_LIMIT = 100


class DocumentService:
    def __init__(self, db: Session):
        self.db = db

    def list_documents(
        self,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[Document]:
        statement = (
            select(Document)
            .order_by(Document.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(statement))

    def create_document(
        self,
        document_data: DocumentCreate,
    ) -> Document:
        document = Document(
            title=document_data.title,
            source=document_data.source,
            source_type=document_data.source_type,
            content_hash=document_data.content_hash,
            import_job_id=document_data.import_job_id,
            content_identity_group_id=document_data.content_identity_group_id,
        )

        try:
            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)
            return document

        except Exception:
            self.db.rollback()
            raise
