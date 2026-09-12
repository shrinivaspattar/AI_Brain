from pydantic import BaseModel, Field

from app.schemas.document import DocumentResponse


class ExactDuplicateGroupResponse(BaseModel):
    content_hash: str
    documents: list[DocumentResponse]


class NearDuplicatePairResponse(BaseModel):
    document_a: DocumentResponse
    document_b: DocumentResponse
    similarity: float = Field(ge=0, le=1)
