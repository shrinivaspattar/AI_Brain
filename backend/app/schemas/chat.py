from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class Citation(BaseModel):
    document_chunk_id: int
    document_id: str
    document_title: str
    document_source: str


class MessageResponse(BaseModel):
    id: int
    conversation_id: str
    role: str
    content: str
    citations: list[Citation] | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChatResponse(BaseModel):
    conversation_id: str
    message: MessageResponse
