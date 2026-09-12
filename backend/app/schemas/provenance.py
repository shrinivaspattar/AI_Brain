from datetime import datetime

from pydantic import BaseModel

from app.schemas.document import DocumentResponse
from app.schemas.import_job import ImportJobResponse


class CitingMessageResponse(BaseModel):
    message_id: int
    conversation_id: str
    created_at: datetime


class DocumentProvenanceResponse(BaseModel):
    document: DocumentResponse
    import_job: ImportJobResponse | None
    chunk_count: int
    cited_in: list[CitingMessageResponse]
