from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemoryCreate(BaseModel):
    content: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    conversation_id: str | None = None
    message_id: int | None = None


class MemoryResponse(BaseModel):
    id: int
    content: str
    confidence: float | None
    status: str
    conversation_id: str | None
    message_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
