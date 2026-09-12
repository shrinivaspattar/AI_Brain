from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob
from app.models.message import Message


@dataclass(frozen=True)
class CitingMessage:
    message_id: int
    conversation_id: str
    created_at: datetime


@dataclass(frozen=True)
class DocumentProvenance:
    document: Document
    import_job: ImportJob | None
    chunk_count: int
    cited_in: list[CitingMessage]


class ProvenanceService:
    """Formalizes the provenance chain that already exists implicitly in
    the schema, as one explicit, queryable trace - it does not add any
    new source of truth, just walks the existing foreign keys:

        ImportJob --(Document.import_job_id)--> Document
        Document  --(DocumentChunk.document_id)--> DocumentChunk
        DocumentChunk --(used in RAG retrieval, denormalized into
            Message.citations)--> Message --(conversation_id)--> Conversation

    Read-only: this never mutates anything, it only reports what already
    links to what.
    """

    def __init__(self, db: Session):
        self.db = db

    def trace_document(self, document_id: str) -> DocumentProvenance:
        """Trace a document back to its import job and forward to every
        chat message that cited it.

        Raises ValueError if the document doesn't exist, matching this
        codebase's convention for "not found" in a service layer (the
        API layer maps it to a 404).
        """
        document = self.db.get(Document, document_id)
        if document is None:
            raise ValueError(f"Document {document_id} not found")

        import_job = (
            self.db.get(ImportJob, document.import_job_id)
            if document.import_job_id is not None
            else None
        )

        chunk_count = (
            self.db.scalar(
                select(func.count(DocumentChunk.id)).where(
                    DocumentChunk.document_id == document_id
                )
            )
            or 0
        )

        # Citations are a denormalized JSONB snapshot (see Message model),
        # not a foreign key, so finding citing messages means scanning
        # messages and checking each one in Python. Fine at personal-
        # corpus message volumes; revisit with a Postgres JSONB
        # containment query if that stops being true.
        #
        # The `.is_not(None)` filter is a best-effort pre-filter, not a
        # guarantee: a JSONB column storing Python None serializes to a
        # JSON `null` by default, which is NOT the same as a SQL NULL and
        # does not satisfy `IS NOT NULL` - so `message.citations` can
        # still come back as None here and must be checked again.
        cited_in = [
            CitingMessage(
                message_id=message.id,
                conversation_id=message.conversation_id,
                created_at=message.created_at,
            )
            for message in self.db.scalars(
                select(Message).where(Message.citations.is_not(None))
            )
            if message.citations
            and any(
                citation.get("document_id") == document_id
                for citation in message.citations
            )
        ]

        return DocumentProvenance(
            document=document,
            import_job=import_job,
            chunk_count=chunk_count,
            cited_in=cited_in,
        )
